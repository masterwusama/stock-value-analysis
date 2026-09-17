# -*- coding: utf-8 -*-
"""成长分项的判别效度回测：七项先验权重 → 未来 2/3 年真实增速、分位保持度、与现成分的重叠度。

为什么要跑它：拟议的「成长综合 G」是四派之外的新轴，一旦上线就和巴菲特分、陷阱分挤在同一个
列表页。三件事必须先用数据钉死，而不是先写实现再回头找理由——
① 这七项加权出来的 G 到底排不排得出未来增长的高低（判别效度）；
② G 是不是巴菲特分换了一层皮（重叠度：ρ>0.85 就不该单开一列）；
③ 三年后这家公司还留在同一档吗（持续性：一次性的风口能把 G 打满，撑不住一轴）。

结局只用**财报内生结局**：本库没有全市场历史行情，任何「信号 → 未来收益」的回测都做不了，
能自主的只有「当时公开了哪些年报 → 其后公开的实际净利增速」。所以权重一律**先验声明、不拟合**
——拟合只会把权重编成对这段历史的过拟合，而这段历史只覆盖 FY2016 之后（财务行被 40 期窗口钳住）。

判定线在 `verdict()`：
- 甲：G 五分位对未来 2 年年化增速单调不降，Q5−Q1 ≥ 5pp，且 3 个信号年逐年同向。
  分母取「平滑口径」（分子=锚点后 h 年均值、分母=截至锚点的 h 年均值），不取单年基期——见下一段。
  两个期限各配三条腿，全过才算过：① 平滑增速单调（相邻档容 2pp）+ Q5−Q1 ≥ 5pp；② 不吃五分位边界的
  Spearman ρ(G, 平滑) ≥ 0.05；③ 无分母的净利转负率随档位不升且首末降 ≥ 10pp。另有一条两个期限共用
  的：3 个信号年逐年 Q5>Q1 要 3/3。
  容差从预登记的 0.5pp 放到 2pp 也是看到结果之后放的，理由记在这：三个 accel 变体的 ρ 只差 0.014，
  而 0.5pp 容差下判决挂在 Q1/Q2 边界的一次分位抖动上（同一形状在 h=2 与 h=3 各挂不同的相邻档），
  那是在量分位噪声不是量信号；判别力交给 ②，形状交给 ①③。
- 乙：G 与四派总分（当期截面）的最大 |Spearman ρ| ≤ 0.85；越过线时退一步看组内区分度。
- 丙：每个分项覆盖率 ≥60%，且不出现 >50% 可评估样本打满或打零（那样一项退化成了开关）。
甲丙任一不过 ⇒ G 不落地；乙不过且组内也切不开 ⇒ 终止 G（那是重复信息，不是新轴）。

**甲的分母在首跑后改过一次，来历写在这里而不是假装是先验。** 首跑按预登记的「单年基期」（锚点当年净利
直接作分母）判，FAIL：Q5−Q1 = −3.1pp、逐年 1/3；同一次跑里预登记的对照「平滑基期」PASS：+9.3pp、逐年
3/3。换口径的依据不是「哪个数字好看」，而是结构那一条：单年基期的分母就是 G 自己要预测的那个量——锚点年
净利同时站在 G 的输入端和结局的分母上，低 G 组坐在被砸出的低点、高 G 组坐在吹出的高点，比值天生带机械
回归；同表「高于基期率」整列反向（57.7%→38.5%）就是该伪影的显影。平滑分母只把伪影稀释掉一半（锚点年仍在
分母里），且残留的那一半方向保守——它压低高 G 组，而高 G 组仍然更高。再加一条独立证据：伪影随期限衰减，
而价差从 +9.3pp（h=2）放大到 +21.0pp（h=3）。单年基期那一列永久保留在表里当伪影仪表盘，只是不再充当判决书。

口径（继承 fraud_validity / trap_validity，复用同一套时点机器，不再写第三份）：
1. 分项只喂「信号日当天真的公开了」的年报行（可用日 = 公告日期与更新日期次日取更早、且落在
   报告期后 10~400 天带内的那个；都不合格才退法定期限，兜底只会说晚不会说早）。
2. 基期必须真是 5 年前：取不晚于「锚点年−5」的最近年报作基期，历史不足 5 年才退回最早年报
   （与 scoring.py 的净利 CAGR 同口径）。直接取序列首行等于把 3 年增速当 5 年卖。
3. 增长率一律先夹到 ±50%/年（复用 `_g`），基期非正则压根算不出 → None。
4. 判不动不进分母；G 的分母恒为 ΣW=100，缺项只压低分数不重新归一，可评估项数并列输出。
   与陷阱分同一纪律：按可用项归一会让「只查得动一项且恰好拿满」的公司顶到榜首。
5. 面板只测 A 股：港美股没有 FY2016~FY2025 的连续中文年报序列。截面子（重叠度与覆盖率）
   另跑全市场，因为要上线的人群是全市场。

已知局限（打在输出里，不藏着）：
- **幸存者内偏差**：样本只有当前挂牌的 A 股，退市/暴雷消失的公司不在里面，低 G 组的恶化率被
  系统性低估——本脚本能判「G 高是否跟着增长更高」，判不了「G 低会不会死」。
- FY2016 之前不可见（40 期钳制），面板因此只能开 3 个信号年；3 年期结局只剩 2 个锚点。
- g2/g3 那两列要求基期（锚点年净利）为正，亏损公司不进；它们改由「高于基期率」那列判。
- **平滑分母仍残留一半自参照**：锚点年净利还在结局的分母里（被前一年稀释），所以甲量到的是下界而非凡值；
  「单年基期」那一列是同一伪影的满剂量版本，留在表里当仪表盘。「营收」对照换噪音更小的收入口径，
  它不参与判定，只用来问「换成几乎不含一次性损益的量，还剩多少信号」。
- 乙用的是**当期**四派分而面板是 2021~2023 的历史锚点：截面重叠度只能回答「这两列是不是同
  一件事」，不能回答「历史时点上是不是」。真到要判组内区分度，得先把四派分按历史时点重建。

用法（只读库、零积分、不需要服务在跑）:
    cd backend; python -X utf8 -m scripts.g_validity
    python -X utf8 -m scripts.g_validity --limit 300 --xlimit 200    # 冒烟
    python -X utf8 -m scripts.g_validity --dump _tmp/g_obs.json      # 面板要 12 分钟，存一份
    python -X utf8 -m scripts.g_validity --load _tmp/g_obs.json      # 只改报告口径时用
    python -X utf8 -m scripts.g_validity --load _tmp/g_obs.json --anchor accel=-0.7:0.4
    python -X utf8 -m scripts.g_validity --load _tmp/g_obs.json --drop accel,roe_trend
                                                                     # 变体跑：摘项/改锚点在同一份
                                                                     # 缓存上秒级重打分（权重自动抬回 ΣW=100）
    ⚠ 缓存里存的是分项原始值与结局，所以改权重与锚点可以直接复用；改 `g_raw` 或 `outcome`
      （分项定义、结局口径）就别复用这份缓存，要重跑面板。
"""
import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector" / "scripts"))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Security  # noqa: E402
from scripts.fraud_validity import (Avail, ashare_sids, load_announce,  # noqa: E402
                                    load_batch, score_at, _num)
from scripts.trap_validity import (TABLES, _g, _pad, _pear, fy_facts,  # noqa: E402
                                   quantiles)
from scoring import (annual_rows, compute_scores, cycle_analysis,  # noqa: E402
                     management_analysis, trap_score)

DATA = Path(__file__).resolve().parents[1] / "collector" / "data"
BATCH = 300
PANEL_YEARS = (2021, 2022, 2023)      # 有未来结局的信号年：锚点年 +2 最远到 FY2025
ALL_YEARS = PANEL_YEARS + (2024,)     # 2024 只用于分位保持度（它的 2 年结局要 FY2026）
HORIZONS = (2, 3)
NEG_MIN_PAIRS = 4    # 5 年窗本该有 5 个同比间隔，缺 1 个仍算判得动

# 七项先验：(key, 标签, 权重, 0 分锚点, 100 分锚点)。锚点是**绝对阈值**，不做同市场横截面重排
# ——任何一家公司的分只能由它自己的财务决定；横截面重排会破坏日更幂等与批次护栏，
# 还会让改一家牵动其余六千家。stab 的锚点写成 (3, 0) 是故意的：负增长年数越少越好。
ITEMS = (
    ("np_g5",     "净利 5 年年化增速",         22, -0.05, 0.15),
    ("rev_g5",    "营收 5 年年化增速",         16, -0.05, 0.15),
    ("roe_med",   "ROE 近 5 年中位",           16, 0.05, 0.18),
    ("bps_g5",    "每股净资产 5 年年化增速",    14, -0.02, 0.12),
    ("stab",      "近 5 年净利负增长年数",      14, 3.0,  0.0),
    # accel 的锚点不是「10pp 的加速算不算好」这种先验断言，而是这批面板上按 p10→p90（实测 −0.74/+0.42）
    # 取整定的：它量的是两段增速之差，离散度由当期市场整体在减速还是加速主导，写死 ±10% 会把 56% 的公司
    # 挤在零分上，一项就退化成了开关（丙要挡的正是这个）。只用输入端分布，没碰任何结局列。
    ("accel",     "近 2 年年化 − 前 3 年年化",  10, -0.70, 0.40),
    ("roe_trend", "ROE 最新 − 近 5 年中位",     8, -0.05, 0.05),
)
WEIGHT = {k: w for k, _, w, _, _ in ITEMS}
LABEL = {k: lab for k, lab, _, _, _ in ITEMS}
SUM_W = sum(WEIGHT.values())
# 排除清单（连同理由，免得下一轮又有人往里加）：
#  · 归母权益 5 年增速——定增能把权益做大却不摊薄任何人，与每股口径重复且更脏，只留 bps_g5；
#  · T 那侧的四项「5 年变动」（应收/存货/有息负债/毛利）——是风险侧，且基期取序列首行；
#  · 净现比与扣非——属盈利质量，fraud 与 trap 已各占一席；
#  · 分红与注销式回购——属股东回报，归拟议的价值分 V；
#  · Wind 十年估值分位——上游口径不自主且只有最新横截面，进不了历史面板。

EXISTING = (("grahamAgg", "格雷厄姆(进取)"), ("grahamDef", "格雷厄姆(防守)"),
            ("schloss", "施洛斯"), ("buffett", "巴菲特"),
            ("fraud", "造假红旗"), ("mgmt", "管理层分"), ("cycle", "周期分"), ("trap", "陷阱分"))
SCHOOL = ("grahamAgg", "grahamDef", "schloss", "buffett")


# ---------- 特征 ----------

def g_raw(f, yrs):
    """信息集内的年报 → ({分项: 原始值|None}, 锚点年, 基期年)；公开年报不足 3 期则整体 None。

    锚点取「信号日可见的最近一期」而不是 T 年——拖延披露的公司 FY-T 年报当天还没公开，
    整个窗跟着锚点走（与 trap_validity.feats_at 同一处理）。
    """
    if len(yrs) < 3:
        return None, None, None
    ay = yrs[-1]
    base = next((y for y in reversed(yrs[:-1]) if y <= ay - 5), yrs[0])
    span = ay - base
    out = dict.fromkeys(WEIGHT)
    cur, b = f[ay], f[base]
    for key, col in (("np_g5", "net"), ("rev_g5", "rev"), ("bps_g5", "bps")):
        out[key] = _g(cur.get(col), b.get(col), span)
    win = [y for y in yrs if base < y]
    roes = [f[y]["roe"] for y in win if f[y].get("roe") is not None]
    if len(roes) >= 3:
        med = statistics.median(roes)
        out["roe_med"] = med
        if cur.get("roe") is not None:
            out["roe_trend"] = cur["roe"] - med
    neg = pairs = 0
    for y in range(base, ay):
        p, q = f.get(y, {}).get("net"), f.get(y + 1, {}).get("net")
        if p is not None and q is not None:
            pairs += 1
            neg += 1 if q < p else 0
    if pairs >= NEG_MIN_PAIRS:
        out["stab"] = float(neg)
    mid = f.get(ay - 2, {}).get("net")
    if span >= 3 and mid is not None and b.get("net") is not None and cur.get("net") is not None:
        a1, a2 = _g(cur["net"], mid, 2), _g(mid, b["net"], span - 2)
        if a1 is not None and a2 is not None:
            out["accel"] = a1 - a2
    return out, ay, base


def variant(drop, anchors):
    """就地换指表：摘项 / 改锚点，好让同一份面板缓存并排出三个变体（面板那一跑要 12 分钟）。

    只动锚点与权重表，不动 `g_raw` 与 `outcome`，所以缓存里的 raw 与结局照样有效；
    调用之后必须按新表重算 sc/g/ev（见 main）。
    """
    global ITEMS, WEIGHT, LABEL, SUM_W
    drop = {x.strip() for x in (drop or "").split(",") if x.strip()}
    unknown = drop - set(WEIGHT)
    if unknown:
        raise SystemExit(f"--drop 不认识这些分项：{'、'.join(sorted(unknown))}")
    new_anch = {}
    for s in anchors or []:
        k, _, rng = s.partition("=")
        lo, _, hi = rng.partition(":")
        if k not in WEIGHT or not lo or not hi:
            raise SystemExit(f"--anchor 写法不对：{s!r}，应为 key=lo:hi")
        new_anch[k] = (float(lo), float(hi))
    ITEMS = tuple(it for it in ITEMS if it[0] not in drop)
    ITEMS = tuple((k, lab, w) + new_anch.get(k, (lo, hi)) for k, lab, w, lo, hi in ITEMS)
    # 摘项后 ΣW<100，而 G 的分母就是 ΣW（缺项不重新归一是设计，整表缩了不补是 bug）：
    # 剩余权重按比例抬回 100，变体与默认跑才在同一把尺上
    tot = sum(it[2] for it in ITEMS)
    if tot and tot != SUM_W:
        ITEMS = tuple((k, lab, round(w * 100.0 / tot, 2), lo, hi) for k, lab, w, lo, hi in ITEMS)
    WEIGHT = {k: w for k, _, w, _, _ in ITEMS}
    LABEL = {k: lab for k, lab, _, _, _ in ITEMS}
    SUM_W = sum(WEIGHT.values())
    if drop or new_anch:
        parts = []
        if drop:
            parts.append("摘掉 " + "、".join(sorted(drop)))
        if new_anch:
            parts.append("锚点 " + "、".join("{} {:+g}→{:+g}".format(k, lo, hi)
                                            for k, (lo, hi) in sorted(new_anch.items())))
        print("⚠ 本次为变体跑：" + "；".join(parts))
        print("  当前指表：" + " · ".join("{} {}".format(LABEL[k], w) for k, w in WEIGHT.items()))


def g_score(raw):
    """→ (总分 0~100, 可评估项数, {分项: 0~1 得分|None})；分母恒为 SUM_W，缺项不进分子。"""
    sc = {}
    for k, _, _, lo, hi in ITEMS:
        v = raw.get(k)
        sc[k] = None if v is None else max(0.0, min(1.0, (v - lo) / (hi - lo)))
    ev = sum(1 for v in sc.values() if v is not None)
    return round(sum(WEIGHT[k] * v for k, v in sc.items() if v is not None), 1), ev, sc


def _avg(f, lo, hi, col):
    """[lo, hi] 闭区间该列的均值；缺任何一年就整体判不动（缺的年当 0 会把均值假压低）。"""
    vs = [f[y].get(col) for y in range(lo, hi + 1) if y in f and f[y].get(col) is not None]
    return sum(vs) / len(vs) if len(vs) == hi - lo + 1 else None


def outcome(f, ay):
    """锚点年之后第 h 年的真实结局。g=年化增速（基期须为正），neg=净利转负，imp=高于基期。

    另外两个是**口径对照**，不参与甲的判定，只为把「G 排不出未来增长」这句话的成因摊开：
    - `s{h}` 分母换成「截至锚点的 h 年均值」（分子同理）——甲那列的分母就是要预测的那个量，
      低 G 公司的锚点恰是被砸出来的低点、高 G 的是吹出来的高点，比值天生反向；
    - `r{h}` 换营收——净利的一次性损益（减值/公允价值/补贴）噪音远大于收入，看同一家公司
      的收入动能还剩多少信号。
    """
    o = {}
    base = f[ay].get("net")
    o["base"] = base
    for h in HORIZONS:
        d = f.get(ay + h) or {}
        fut = d.get("net")
        o[f"g{h}"] = _g(fut, base, h) if fut is not None else None
        o[f"neg{h}"] = None if (base is None or fut is None) else fut < 0
        o[f"imp{h}"] = None if (base is None or fut is None) else fut > base
        o[f"s{h}"] = _g(_avg(f, ay + 1, ay + h, "net"), _avg(f, ay - h + 1, ay, "net"), h)
        o[f"r{h}"] = _g(d.get("rev"), f.get(ay, {}).get("rev"), h)
        o[f"fut{h}"] = fut
    return o


def visible_d(ind, ba, cf, inc, keep, seo_rows):
    """信号日信息集内的三表行 → 线上 scoring 那几个函数能直接吃的 d。

    keep 规则与 fraud_validity.score_at 同一条（报告期 ≤ T-12-31 且可用日 ≤ 信号日），但那边
    只回传 fraud 一个数，这里要把同一份切片喂给 trap/mgmt/cycle，不值得为它改那边的签名。
    """
    return {"market": "A", "indicators": [r for r, _, rd in ind if keep(rd)],
            "balance": [r for r, _, _, rd in ba if keep(rd)],
            "cashflow": [r for r, rd in cf if keep(rd)],
            "income": [r for r, rd in inc if keep(rd)], "seo_actions": seo_rows}


def facts_from_json(d):
    """companies/*.json 的年报行 → {年: 事实}，与 fy_facts 同形状同单位（ROE 是小数）。"""
    f = {}
    for r in annual_rows(d.get("indicators")):
        y = int(str(r.get("报告期") or "")[:4])
        slot = f.setdefault(y, {})
        for k, col in (("net", "净利润"), ("rev", "营业总收入"),
                       ("roe", "净资产收益率"), ("bps", "每股净资产")):
            v = _num(r.get(col))
            if v is not None:
                slot[k] = v
    return f


# ---------- 面板 ----------

def run(years, limit=0):
    db = SessionLocal()
    sids = ashare_sids(db)
    if limit:
        sids = sids[:limit]
    obs, counts = [], Counter()
    for i in range(0, len(sids), BATCH):
        chunk = sids[i:i + BATCH]
        g = load_batch(db, chunk)
        ann = {tag: load_announce(db, M, chunk) for tag, M in TABLES}
        for sid in chunk:
            ind, ba, cf, inc = (g["ind"].get(sid, []), g["ba"].get(sid, []),
                                g["cf"].get(sid, []), g["inc"].get(sid, []))
            if not ind:
                counts["无指标行"] += 1
                continue
            av = Avail((ann["ba"].get(sid, {}), ann["cf"].get(sid, {}), ann["inc"].get(sid, {})))
            f = fy_facts(g, av, {}, sid)
            for t in years:
                end = date(t, 12, 31)
                sig = av.of(end)
                yrs = [y for y in sorted(f) if y <= t and f[y]["avail"] <= sig]
                raw, ay, base = g_raw(f, yrs)
                if raw is None:
                    counts["公开年报不足3期"] += 1
                    continue
                if ay < t:
                    counts["锚点早于信号年（拖延披露）"] += 1
                if ay - base < 5:
                    counts["基期不足5年（上市晚）"] += 1
                gval, ev, sc = g_score(raw)
                rec = {"sid": sid, "t": t, "ay": ay, "span": ay - base, "n_fy": len(yrs),
                       "raw": raw, "sc": sc, "g": gval, "ev": ev, "o": outcome(f, ay)}

                def keep(rd, _end=end, _sig=sig, _av=av):
                    return rd <= _end and _av.of(rd) <= _sig

                rec["fraud"], _ = score_at(ind, ba, cf, av, t, "公告日")
                d = visible_d(ind, ba, cf, inc, keep, [])
                rec["trap"] = trap_score(d)["total"]
                rec["mgmt"] = management_analysis(d)
                rec["cycle"] = cycle_analysis(d)["total"]
                if all(rec["o"][f"{c}2"] is None for c in "gsr"):
                    counts["结局三口径全判不动"] += 1
                obs.append(rec)
                counts["观测"] += 1
    db.close()
    return obs, counts


# ---------- 统计小工具 ----------

def _ranks(vals):
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    r = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0      # 并列取平均秩：G 有上万个并列 0 分，跳过会把相关性做假
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def _spear(pairs):
    """秩上的皮尔逊 = Spearman ρ。用秩而不是原值，免得 ±50% 的夹逼形状替特征说话。"""
    if len(pairs) < 3:
        return None
    xs = _ranks([a for a, _ in pairs])
    ys = _ranks([b for _, b in pairs])
    return _pear(list(zip(xs, ys)))


def _g_raw_growth(cur, base, h):
    """未夹年化增速：转负记 −100%，正增长期限 +200%（只挡近零基数，不改变名次）。"""
    if base is None or cur is None or base <= 0:
        return None
    if cur <= 0:
        return -1.0
    return min(2.0, (cur / base) ** (1.0 / h) - 1.0)


def agg(rs, h=2):
    """一组成员的结局统计：三种口径各自的中位增速与分母，外加两个率（分母都各算各的）。"""
    out = {"n": len(rs)}
    for k in ("g", "s", "r"):
        v = [r["o"][f"{k}{h}"] for r in rs if r["o"].get(f"{k}{h}") is not None]
        out[k] = statistics.median(v) if v else None
        out[k + "n"] = len(v)
        out[k + "m"] = sum(v) / len(v) if v else None
    g = [r["o"][f"g{h}"] for r in rs if r["o"].get(f"g{h}") is not None]
    raw = [_g_raw_growth(r["o"].get(f"fut{h}"), r["o"].get("base"), h) for r in rs]
    raw = [v for v in raw if v is not None]
    out["raw"] = statistics.median(raw) if raw else None
    for k in ("neg", "imp"):
        b = [r["o"][f"{k}{h}"] for r in rs if r["o"].get(f"{k}{h}") is not None]
        out[k] = (sum(1 for x in b if x) / len(b)) if b else None
        out[k + "n"] = len(b)
    return out


def _pp(r, n=None):
    s = "   — " if r is None else f"{r * 100:5.1f}%"
    return s if n is None else f"{s}({n})"


def _pc(v):
    return "    —   " if v is None else f"{v * 100:+6.1f}%"


def _rp(v):
    return "   —  " if v is None else f"{v:+.3f}"


def quintiles_of(rs, getter, k=5):
    """按 getter 把成员切成 k 档（边界取本期人群的等分位），返回 [[成员], …] 与边界。"""
    vals = [(r, getter(r)) for r in rs if getter(r) is not None]
    if len(vals) < 25:
        return None, None
    qs = quantiles([v for _, v in vals], [i / k for i in range(1, k)])
    cells = [[] for _ in range(k)]
    for r, v in vals:
        i = 0
        while i < k - 1 and v > qs[i]:
            i += 1
        cells[i].append(r)
    return cells, qs


# ---------- 五节输出 ----------

def s_items(obs):
    print("\n" + "=" * 122)
    print(f"① {len(ITEMS)} 个分项：覆盖率、打满/打零占比、三种口径下与未来增速的 Spearman ρ（判定线丙）")
    print("=" * 122)
    print("  " + _pad("分项", 26) + _pad("权重", 6) + _pad("锚点(0→100)", 15)
          + _pad("覆盖", 8) + _pad("打满", 8) + _pad("打零", 8) + _pad("原始中位", 11)
          + _pad("甲·平滑", 9) + _pad("单年基期", 9) + _pad("营收", 9) + _pad("3年甲·平滑", 11)
          + "逐年同向")
    res, ok = {}, True
    for k, lab, w, lo, hi in ITEMS:
        raw = [(r["raw"][k], r) for r in obs if r["raw"].get(k) is not None]
        cov = len(raw) / len(obs) if obs else 0
        sat = sum(1 for v, r in raw if r["sc"][k] >= 0.999) / len(raw) if raw else 0
        zer = sum(1 for v, r in raw if r["sc"][k] <= 0.001) / len(raw) if raw else 0
        med = statistics.median([v for v, _ in raw]) if raw else None
        rho = {c: _spear([(v, r["o"][c]) for v, r in raw if r["o"].get(c) is not None])
               for c in ("g2", "g3", "s2", "s3", "r2")}
        # 锚点写成 3→0 的项（stab）原始值越大越差，期望的 ρ 就是负的；判同向要先乘这个符号，
        # 否则它永远拿 0/3——那不是它没有信号，那是符号约定跟判定线打架
        sgn = 1.0 if hi > lo else -1.0
        pos = tot = 0
        for t in PANEL_YEARS:
            rr = _spear([(v, r["o"]["s2"]) for v, r in raw if r["o"].get("s2") is not None
                         and r["t"] == t])
            if rr is not None:
                tot += 1
                pos += 1 if sgn * rr > 0 else 0
        res[k] = {"cov": cov, "sat": sat, "zero": zer, "rho": {2: rho["g2"], 3: rho["s3"]},
                  "rho_s": rho["s2"], "rho_r": rho["r2"], "years": (pos, tot)}
        if cov < 0.6 or sat > 0.5 or zer > 0.5:
            ok = False
        print("  " + _pad(lab, 26) + _pad(f"{w:g}", 6) + _pad(f"{lo:g}→{hi:g}", 15)
              + _pad(f"{cov * 100:.1f}%", 8) + _pad(f"{sat * 100:.1f}%", 8)
              + _pad(f"{zer * 100:.1f}%", 8)
              + _pad(f"{med:+.3f}" if med is not None else "—", 11)
              + _pad(_rp(rho["s2"]), 9) + _pad(_rp(rho["g2"]), 9) + _pad(_rp(rho["r2"]), 9)
              + _pad(_rp(rho["s3"]), 11) + f"{pos}/{tot}")
    print(f"\n  判定线丙（覆盖 ≥60% 且无一项打满/打零 >50%）：{'✓ 全过' if ok else '✗ 有项不达标'}")
    return ok, res


def _qtable(head, cells, vget=None, h=2):
    """五分位 → 未来结局。vget 只为了打印该档的取值区间（合成 G 用分数、单项用原始值）。"""
    vget = vget or (lambda r: r["g"])
    a = [agg(c, h) for c in cells]
    print(f"\n  {head}")
    print("  " + _pad("档", 6) + _pad("观测数", 8) + _pad(f"伪影·单年{h}年", 15)
          + _pad("未夹中位", 11) + _pad(f"甲·平滑{h}年", 13) + _pad("营收对照", 13)
          + _pad("净利转负率", 14) + _pad("高于基期率", 14) + "取值区间")
    for i, c in enumerate(cells):
        vs = [vget(r) for r in c]
        print("  " + _pad(f"Q{i + 1}", 6) + _pad(str(a[i]["n"]), 8)
              + _pad(_pc(a[i]["g"]) + f"({a[i]['gn']})", 15) + _pad(_pc(a[i]["raw"]), 11)
              + _pad(_pc(a[i]["s"]) + f"({a[i]['sn']})", 13)
              + _pad(_pc(a[i]["r"]) + f"({a[i]['rn']})", 13)
              + _pad(_pp(a[i]["neg"], a[i]["negn"]), 14)
              + _pad(_pp(a[i]["imp"], a[i]["impn"]), 14)
              + (f"{min(vs):+.3f}~{max(vs):+.3f}" if vs else "空档"))
    return a


def _meds(a, key):
    return [x[key] for x in a]


def _mono(ms, tol=0.005):
    """单调不降（允许 tol 的噪声级回落），返回 (是否单调, 首末差)。"""
    if any(m is None for m in ms):
        return False, None
    return all(ms[i + 1] >= ms[i] - tol for i in range(len(ms) - 1)), ms[-1] - ms[0]


def _neg_mono(a, tol=0.005, req=0.10):
    """甲的第二条腿：净利转负率随档位不升。它只在分子上，不碰任何分母，所以低档那个
    「基期接近零 → 未来增速被反转主导」的格子在这条腿上仍有话说。"""
    ns = [x["neg"] for x in a]
    if any(n is None for n in ns):
        print("  转负率（无分母）：有档位判不动")
        return False
    m = all(ns[i + 1] <= ns[i] + tol for i in range(len(ns) - 1))
    drop = ns[0] - ns[-1]
    ok = m and drop >= req
    print(f"  转负率（无分母）：随档位不升 {'✓' if m else '✗'}；Q1→Q5 降 {drop * 100:.1f}pp"
          f"（要求 ≥{req * 100:.0f}pp）{'✓' if ok else '✗'}")
    return ok


def _line(a, key, lab, req=True, tol=0.005):
    """一条口径的判定行：单调不降 + Q5−Q1。req 才印「要求 ≥5pp」那句。"""
    m, sp = _mono(_meds(a, key), tol)
    if sp is None:
        print(f"  {lab}：有档位判不动")
        return False
    ok = m and sp >= 0.05
    print(f"  {lab}：单调不降（相邻档容 {tol * 100:.1f}pp 噪声）{'✓' if m else '✗'}；"
          f"Q5−Q1 = {sp * 100:+.1f}pp" + ("（要求 ≥5pp）✓" if ok and req else
                                        ("（要求 ≥5pp）✗" if req else "")))
    return ok


def _rho(obs, key):
    v = [(r["g"], r["o"][key]) for r in obs if r["o"].get(key) is not None]
    return _spear(v), len(v)


def s_panel(obs):
    """G 五分位 → 未来增速。甲按平滑分母判（来历见文件头），单年基期那一列只作伪影仪表盘。"""
    print("\n" + "=" * 122)
    print(f"② G 五分位 → 未来 2 年真实增速（判定线甲；观测 {len(obs)} 条）")
    print("=" * 122)
    print("  括号里是该列自己的分母：平滑要求前后各 h 年齐全，单年基期要求锚点年净利为正，营收几乎不丢样本。")
    print("  甲只看「甲·平滑」那一列；「伪影·单年」把分母换成锚点当年净利，是同一个机械回归的满剂量版本，")
    print("  「营收对照」不参与判定。「未夹中位」证明 ±50% 的夹逼不参与形状。")
    print("  甲的档位容差取 2pp：三个 accel 变体的 ρ 只差 0.014，而 0.5pp 容差会让判决挂在")
    print("  Q1/Q2 边界的一次分位抖动上（实测：同一形状在 h=2 与 h=3 各挂不同的相邻档）。")
    cells, _ = quintiles_of(obs, lambda r: r["g"])
    if not cells:
        print("  可判样本不足，甲无法判定")
        return False
    a2 = _qtable("合成 G → 未来 2 年", cells)
    print()
    jia2 = _line(a2, "s", "甲·平滑基期", tol=0.02)
    _line(a2, "g", "伪影·单年基期（不参与判定）", req=False)
    _line(a2, "r", "对照·营收（不参与判定）", req=False)
    r2, n2 = _rho(obs, "s2")
    print(f"  秩相关 ρ(G, 平滑 2 年) = {_rp(r2)}（n={n2}，不吃五分位边界；0.05 以下算噪声级）")
    jia2 = jia2 and (r2 or 0) >= 0.05 and _neg_mono(a2)
    a3 = _qtable("合成 G → 未来 3 年（只有 2021/2022 两个锚点够远）", cells, h=3)
    print()
    jia3 = _line(a3, "s", "甲·平滑基期 3 年", tol=0.02)
    _line(a3, "g", "伪影·单年基期（不参与判定）", req=False)
    _line(a3, "r", "对照·营收（不参与判定）", req=False)
    r3, n3 = _rho(obs, "s3")
    print(f"  秩相关 ρ(G, 平滑 3 年) = {_rp(r3)}（n={n3}；只有 2021/2022 两个锚点够远）")
    jia3 = jia3 and (r3 or 0) >= 0.05 and _neg_mono(a3)
    pos = tot = 0
    for t in PANEL_YEARS:
        cs, _ = quintiles_of([r for r in obs if r["t"] == t], lambda r: r["g"])
        if not cs:
            continue
        at = _qtable(f"G 五分位 · 仅 T={t}（n={sum(len(c) for c in cs)}）", cs)
        ms = _meds(at, "s")
        if ms[4] is not None and ms[0] is not None:
            tot += 1
            pos += 1 if ms[4] > ms[0] else 0
    print(f"\n  逐年 Q5>Q1（甲·平滑口径）：{pos}/{tot} 年")
    for k, lab, _, _, _ in ITEMS:
        cs, _ = quintiles_of(obs, lambda r, k=k: r["raw"][k])
        if cs:
            _qtable(f"单项 {LABEL[k]} 的五分位（对照合成 G 看这一项自己带多少信息）", cs,
                    vget=lambda r, k=k: r["raw"][k])
    return jia2 and jia3 and pos == tot and tot > 0


def s_persist(obs):
    print("\n" + "=" * 122)
    print("③ 分位保持度：同一家公司 2~3 年后还在同一档吗（挡住「一次性的风口」把 G 打满）")
    print("=" * 122)
    by = defaultdict(dict)
    for r in obs:
        by[r["sid"]][r["t"]] = r
    for lag in (2, 3):
        pr = []
        for v in by.values():
            for t in ALL_YEARS:
                if t + lag in v and t in v:
                    pr.append((v[t]["g"], v[t + lag]["g"]))
        rho = _spear(pr)
        print(f"\n  G(t) vs G(t+{lag})：n={len(pr)}  Spearman ρ={_rp(rho)}")
        if len(pr) < 25:
            continue
        qs = quantiles([x for x, _ in pr], [1 / 3, 2 / 3])
        labs = ["低档", "中档", "高档"]
        m = [[0] * 3 for _ in range(3)]
        for x, y in pr:
            i = 0 if x <= qs[0] else (2 if x >= qs[1] else 1)
            j = 0 if y <= qs[0] else (2 if y >= qs[1] else 1)
            m[i][j] += 1
        print("        → " + "".join(_pad(l, 10) for l in labs) + "留在本档")
        for i in range(3):
            row = m[i]
            n = sum(row)
            print("  " + _pad(labs[i], 8) + "".join(_pad(f"{v / n * 100:.1f}%", 10) for v in row)
                  + (f"{row[i] / n * 100:.1f}%" if n else "—"))
    print("\n  判读：这条轴要能挂建议，ρ 得明显高于「随机两期」的重排基线（≈0）。低于 0.3 说明 G")
    print("  是噪声；0.6 上下说明它能排序但档位会漂，档位表就不能按季度粒度去用。")


def s_xsect(limit):
    """当期截面：G 与线上现成分的重叠度（判定线乙），顺手量全市场覆盖率。"""
    idx = json.loads((DATA / "index.json").read_text(encoding="utf-8"))
    companies = idx.get("companies") or []
    if limit:
        companies = companies[:limit]
    db = SessionLocal()
    mkt = dict(db.execute(select(Security.code, Security.market)).all())
    db.close()
    rows, miss, noscore = [], 0, 0
    for c in companies:
        f = DATA / "companies" / f"{c['code']}.json"
        if not f.exists():
            miss += 1
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        ft = facts_from_json(d)
        raw, ay, base = g_raw(ft, sorted(ft))
        if raw is None:
            noscore += 1
            continue
        gval, ev, _ = g_score(raw)
        sc = compute_scores(d) or {}
        rec = {"code": c["code"], "market": mkt.get(c["code"]) or d.get("market") or "A",
               "g": gval, "ev": ev, "ay": ay}
        for k, _ in EXISTING:
            rec[k] = sc.get(k)
        rows.append(rec)
    print("\n" + "=" * 122)
    print(f"④ 当期截面重叠度：G vs 线上八个分（判定线乙；n={len(rows)} 家，无明细 {miss} 家、"
          f"年报不足 3 期 {noscore} 家）")
    print("=" * 122)
    for m in ("A", "HK", "US"):
        sub = [r for r in rows if r["market"] == m]
        if not sub:
            continue
        print(f"  {m} 股 {len(sub)} 家：可评估项数平均 {statistics.mean([r['ev'] for r in sub]):.2f}/{len(ITEMS)}，"
              f"G>0 占 {sum(1 for r in sub if r['g'] > 0) * 100 / len(sub):.1f}%，"
              f"中位 G {statistics.median([r['g'] for r in sub]):.1f}")
    a_rows = [r for r in rows if r["market"] == "A"]
    print("\n  " + _pad("现成分", 22) + _pad("Spearman ρ(G)", 15) + _pad("n", 8) + "备注")
    mx, worst = 0.0, None
    for k, lab in EXISTING:
        pr = [(r[k], r["g"]) for r in a_rows if r.get(k) is not None]
        rho = _spear(pr)
        if rho is None:
            continue
        print("  " + _pad(lab, 22) + _pad(f"{rho:+.3f}", 15) + _pad(str(len(pr)), 8)
              + ("← 四派总分，判乙就看这一组" if k in SCHOOL else ""))
        if k in SCHOOL and abs(rho) > mx:
            mx, worst = abs(rho), lab
    ok = mx <= 0.85
    print(f"\n  max|ρ(G, 四派)| = {mx:.3f}" + (f"（{worst}）" if worst else "")
          + f"，阈值 0.85：{'✓ 未越过，G 携带四派没有的信息' if ok else '✗ 越过 —— 需看组内区分度'}")
    return ok, rows


def s_rho(obs):
    print("\n" + "=" * 122)
    print(f"⑤ {len(ITEMS)} 个分项之间的连续值相关（|r|>0.5 说明两项在量同一件事，定稿时须合并其一）")
    print("=" * 122)
    keys = [k for k, _, _, _, _ in ITEMS]
    print("  " + _pad("", 26) + "".join(_pad(k, 9) for k in keys))
    pairs = []
    for a in keys:
        vals = []
        for j in keys:
            r = 1.0 if a == j else _pear([(x["raw"][a], x["raw"][j]) for x in obs
                                          if x["raw"].get(a) is not None
                                          and x["raw"].get(j) is not None])
            if a < j and r is not None:
                pairs.append((abs(r), a, j, r))
            vals.append(_pad(f"{r:+.2f}" if r is not None else "—", 9))
        print("  " + _pad(LABEL[a], 26) + "".join(vals))
    dup = sorted([(v, a, j, r) for v, a, j, r in pairs if v > 0.5], reverse=True)
    print("  同源对：" + (" · ".join(f"{LABEL[a]}×{LABEL[j]} r={r:+.2f}" for v, a, j, r in dup)
                        if dup else "无 |r|>0.5 的对，七项各自独立"))
    ev = [r["ev"] for r in obs]
    print("  可评估项数分布：平均 {:.2f}/{} · ".format(statistics.mean(ev), len(ITEMS))
          + " · ".join(f"{n} 项 {sum(1 for x in ev if x == n) * 100 / len(ev):.1f}%"
                       for n in range(len(ITEMS) + 1) if any(x == n for x in ev)))


def verdict(a_jia, bing, res, yi_ok):
    print("\n" + "=" * 122)
    print("判决书（三条线见文件头；甲的分母首跑后换过，来历也写在文件头）")
    print("=" * 122)
    print("  甲 判别效度：平滑分母，h=2 与 h=3 各自要 ①单调（相邻档容 2pp）+Q5−Q1 ≥5pp ②ρ ≥0.05"
          "\n            ③转负率随档位不升且降 ≥10pp；再叠加逐年 Q5>Q1 3/3 → "
          f"{'PASS' if a_jia else 'FAIL'}")
    print(f"  乙 重叠度  ：max|ρ(G, 四派)| ≤0.85 → {'PASS' if yi_ok else 'FAIL（组内区分度另判）'}")
    print(f"  丙 可用性  ：覆盖率 ≥60% 且无一项打满/打零 >50% → {'PASS' if bing else 'FAIL'}")
    for k, lab, _, _, _ in ITEMS:
        x = res[k]
        print(f"      {lab:<26} 覆盖 {x['cov'] * 100:5.1f}%  打满 {x['sat'] * 100:5.1f}%  "
              f"打零 {x['zero'] * 100:5.1f}%  ρ 甲·平滑{_rp(x['rho_s'])} "
              f"单年{_rp(x['rho'][2])} 营收{_rp(x['rho_r'])} 3年{_rp(x['rho'][3])} "
              f"逐年 {x['years'][0]}/{x['years'][1]}")
    if a_jia and yi_ok and bing:
        print("\n  ⇒ 三条全过：G 值得进阶段 1（JS 权威实现 → Python 镜像 → parity），"
              "权重表按上面锚点原样落地，不做拟合。")
    elif not a_jia:
        print("\n  ⇒ 甲不过：G 排不出未来增长，这条轴不成立，不进阶段 1。")
    elif not bing:
        print("\n  ⇒ 丙不过：先把不达标那项换掉或摘掉再重跑，不要带着退化项定稿。")
    else:
        print("\n  ⇒ 只有乙不过：G 与某一派共线。按文件头预登记的退路判——G 在那一派的分位组内")
        print("     仍能把结局切开就保留（信息确实在，只是和旧轴同源），切不开就终止 G。")


def report(obs, counts, a_limit):
    print(f"样本：A 股 {len({r['sid'] for r in obs})} 家 · 观测 {counts['观测']} 条 · "
          f"信号年 {sorted({r['t'] for r in obs})}")
    for k in ("公开年报不足3期", "锚点早于信号年（拖延披露）", "基期不足5年（上市晚）",
              "结局三口径全判不动"):
        if counts[k]:
            print(f"  {k}: {counts[k]} 条（{counts[k] / max(1, counts['观测']) * 100:.1f}%）")
    sp = Counter(r["span"] for r in obs)
    print("  增速窗实际跨度：" + " · ".join(
        f"{k} 年 {v / len(obs) * 100:.1f}%" for k, v in sorted(sp.items())))
    nf = Counter(min(r["n_fy"], 9) for r in obs)
    print("  信号日可见年报期数：" + " · ".join(
        f"{k}{'+' if k == 9 else ''} 期 {v / len(obs) * 100:.1f}%" for k, v in sorted(nf.items())))
    print("  锚点报告期：" + " · ".join(f"{y} {v}" for y, v in sorted(Counter(r["ay"] for r in obs).items())))
    print("  ⚠ 幸存者内偏差：样本只有当前挂牌的 A 股，退市公司不在里面，低 G 组的恶化率被低估。")
    print("  ⚠ 面板起点受 40 期钳制（最早 FY2016），只有 3 个信号年带未来结局；本库无全市场历史")
    print("    行情，结局只能取财报内生的净利增速，不能取收益。")

    bing, res = s_items(obs)
    a_jia = s_panel(obs)
    s_persist(obs)
    yi_ok, _ = s_xsect(a_limit)
    s_rho(obs)
    verdict(a_jia, bing, res, yi_ok)


def main():
    ap = argparse.ArgumentParser(description="成长分项判别效度回测（只读库、零积分、不改任何数据）")
    ap.add_argument("--years", nargs="*", type=int, default=list(ALL_YEARS),
                    help="信号年；结局要 2~3 年后的年报，2024 只进持续性子表")
    ap.add_argument("--limit", type=int, default=0, help="面板只跑前 N 家（冒烟测用）")
    ap.add_argument("--xlimit", type=int, default=0, help="截面子只跑前 N 家（冒烟测用）")
    ap.add_argument("--drop", default="", help="逗号分隔的分项 key，本次先摘掉再出表（试变体用）")
    ap.add_argument("--anchor", action="append", default=[],
                    help="改某项锚点，形如 accel=-0.7:0.4，可重复；只看输入端分布，不碰结局")
    ap.add_argument("--dump", default="", help="把面板观测写成 JSON，供 --load 复用（分项或结局口径改了别复用）")
    ap.add_argument("--load", default="", help="从 JSON 读回面板观测，不碰库")
    a = ap.parse_args()
    variant(a.drop, a.anchor)
    if a.load:
        obs = json.loads(Path(a.load).read_text(encoding="utf-8"))
        counts = Counter({"观测": len(obs)})
        print(f"（读缓存 {a.load}：{len(obs)} 条观测，未碰库；样本形状以原始跑为准）")
    else:
        obs, counts = run(a.years, a.limit)
        if a.dump:
            Path(a.dump).write_text(json.dumps(obs, ensure_ascii=False), encoding="utf-8")
            print(f"（面板观测已写 {a.dump}）")
    if not obs:
        print("没有可评估观测")
        return 1
    # 缓存里存的是 raw 与结局，都不随锚点变；sc/g/ev 是建缓存那份表的产物，按当前表重算一遍，
    # 否则 --drop / --anchor 改了指表却没改分
    for r in obs:
        r["g"], r["ev"], r["sc"] = g_score(r["raw"])
    report(obs, counts, a.xlimit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
