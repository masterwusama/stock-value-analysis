# -*- coding: utf-8 -*-
"""情绪温度 T / 极端度 E / 连续天数 streak —— 采集端与 API 共用的纯函数模块。

设计文档: docs/设计-情绪温度与极端度.md。要点:
- T 是 sentiment_daily 六个现有列(r5/red_rate/zt_count/dt_count/yz_dt_count/zha_rate)
  的纯函数,读时计算不落库,公式演进对全部历史一致生效(防口径漂移);
- 输入先过锚点分位表再加权合成(涨停/跌停重尾,原始值刻度会被极端日拉爆);
- 锚点三级出处:【校准】=校准常数(如 zt_p90=100 即涨停家数 p90)、【软锚】=由
  状态机阈值反推的近似分位、【插值】=形态假设点。阶段二(history 满 60 交易日)
  换滚动 250 日 as-of 分位,锚点表退役为兜底;
- ranks 统一下发**原始分位**(第几分位就是几分位,方向只体现在权重里),
  极端日的主导触发指标由调用方按 rank≥0.8(正向)/≤0.2(反向)自行点名。

无第三方依赖(纯 stdlib),采集脚本直跑与 uvicorn 两种上下文均可 import。
"""
import math

ANCHOR_VERSION = "static-v1"

# 值升序锚点表;超出首尾即钳制到端点分位(重尾饱和)。分位上限 0.999 → 满分按构造不可达。
ANCHORS = {
    # R5:【软锚】r5_weak=-5%/r5_strong=+4% 为状态机边界,按 OR 触发占比与国证2000
    # 5 日波动(σ≈3.4%)反推 ≈ p8/p92;尾部为【插值】
    "r5": [(-0.15, 0.005), (-0.10, 0.02), (-0.05, 0.08), (-0.02, 0.25),
           (0.00, 0.40), (0.02, 0.60), (0.04, 0.92), (0.07, 0.98),
           (0.10, 0.995), (0.15, 0.999)],
    # 红盘率:【软锚】red_weak=0.25≈p8、red_strong=0.60≈p78(极强 OR 条件之一,占比
    # 宽于 R5 路径);中位 0.45 为经验值【插值】
    "red": [(0.15, 0.02), (0.25, 0.08), (0.35, 0.25), (0.45, 0.45),
            (0.55, 0.68), (0.60, 0.78), (0.70, 0.93), (0.80, 0.985), (0.90, 0.999)],
    # 涨停家数:【校准】(100, 0.90) 即 zt_p90;其余为【插值】
    "zt": [(20, 0.10), (35, 0.25), (50, 0.50), (70, 0.70),
           (100, 0.90), (150, 0.97), (250, 0.995), (400, 0.999)],
    # 空头强度 s=跌停+2×一字:【软锚】dt_panic=30≈p95(恐慌共同条件);其余【插值】
    "short": [(0, 0.10), (3, 0.30), (8, 0.55), (15, 0.80),
              (30, 0.95), (60, 0.985), (120, 0.998)],
    # 炸板率:【插值】(现校准未单列其分位,2026-09 七日样本形态佐证)
    "zha": [(0.10, 0.15), (0.18, 0.40), (0.25, 0.65), (0.35, 0.85),
            (0.50, 0.97), (0.70, 0.998)],
}
# 方向:r5/red/zt 越大越热;short/zha 越大越冷。R5 是本体系定义的温度计,权重最高。
WEIGHTS = {"r5": 0.35, "red": 0.20, "zt": 0.20, "short": 0.15, "zha": 0.10}
POS_KEYS = ("r5", "red", "zt")          # 正向:rank 高 = 火
NEG_KEYS = ("short", "zha")             # 反向:rank 低 = 火
RANK_LABELS = {"r5": "R5", "red": "红盘", "zt": "涨停", "short": "空头", "zha": "炸板"}


def rank_of(key, x):
    """锚点表分位插值:x 超出首尾钳制到端点分位,否则值空间线性插值。"""
    a = ANCHORS[key]
    if x <= a[0][0]:
        return a[0][1]
    if x >= a[-1][0]:
        return a[-1][1]
    for (v0, p0), (v1, p1) in zip(a, a[1:]):
        if v0 <= x <= v1:
            return p0 + (x - v0) / (v1 - v0) * (p1 - p0)
    return a[-1][1]


def _first(ind, *keys):
    for k in keys:
        if ind.get(k) is not None:
            return ind[k]
    return None


def _short_strength(ind):
    dt = _first(ind, "dt_count", "dt")
    yz = _first(ind, "yz_dt_count", "yz_dt")
    if dt is None:
        return None
    return dt + 2 * (yz or 0)


def temperature(ind):
    """六列 → {t, ranks}。任一输入缺失返回 None(历史脏数据兜底,前端显示 '-')。

    键名兼容两种方言:DB/采集 indicators 的 zt_count/dt_count/yz_dt_count 与
    history.json 的短名 zt/dt/yz_dt。
    t 取 [1,99]:端点钳制后极值 99.9,四舍五入会到 100——满分即宣称不可能,不给。
    """
    raw = {"r5": ind.get("r5"), "red": _first(ind, "red_rate", "red"),
           "zt": _first(ind, "zt_count", "zt"), "short": _short_strength(ind),
           "zha": _first(ind, "zha_rate", "zha")}
    if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in raw.values()):
        return None
    ranks = {k: rank_of(k, float(v)) for k, v in raw.items()}
    acc = sum(WEIGHTS[k] * (ranks[k] if k in POS_KEYS else 1 - ranks[k])
              for k in WEIGHTS)
    t = min(99, max(1, round(100 * acc)))
    return {"t": t, "ranks": {k: round(v, 3) for k, v in ranks.items()}}


def extreme_of(state, t):
    """极端度 E:极强=T、极弱=100−T(统一语义「离中性多远」);普通/回暖无 E。"""
    if state == "极强":
        return t
    if state == "极弱":
        return 100 - t
    return None


def band_of(extreme):
    """档内三带:极端≥90 / 标准 80–89 / 踩线<80(单指标驱动的边界触发)。"""
    if extreme is None:
        return None
    if extreme >= 90:
        return "极端"
    if extreme >= 80:
        return "标准"
    return "踩线"


def divergence_of(state, t):
    """普通/回暖日综合温度已达极端带而状态机未触发:high=极强带 / low=极弱带。"""
    if state in ("极强", "极弱"):
        return None
    if t >= 80:
        return "high"
    if t <= 20:
        return "low"
    return None


def score_card(ind, state, streak):
    """温度全家桶:temperature + 极端度/带/背离,采集端 daily.json 与 API 共用形状。"""
    base = temperature(ind)
    if base is None:
        return None
    t = base["t"]
    e = extreme_of(state, t)
    return {"t": t, "extreme": e, "band": band_of(e),
            "divergence": divergence_of(state, t),
            "streak": streak, "ranks": base["ranks"], "anchor": ANCHOR_VERSION}


def dominant_of(state, ranks):
    """极端日的主导触发指标(踩线说明点名用)。

    极强:正向 rank≥0.8 或反向 rank≤0.2;极弱:正向 rank≤0.2 或反向 rank≥0.8。
    返回 ["R5 94%"] 之类的标签串列表;顺带把同向反常项(结构背离信号)排在最后。
    """
    if state not in ("极强", "极弱"):
        return []
    out = []
    for k in POS_KEYS + NEG_KEYS:
        r = ranks.get(k)
        if r is None:
            continue
        hot = r >= 0.8 if ((k in POS_KEYS) == (state == "极强")) else r <= 0.2
        if hot:
            out.append(f"{RANK_LABELS[k]} {round(r * 100)}%")
    return out


def streak_of(states_asc):
    """升序状态序列 → 逐日连续同档天数(含当日,换档重置为 1)。"""
    out, run = [], 0
    for i, s in enumerate(states_asc):
        run = run + 1 if i and s == states_asc[i - 1] else 1
        out.append(run)
    return out
