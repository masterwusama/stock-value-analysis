# -*- coding: utf-8 -*-
"""小微盘投机池:微盘/小票两档股票池 × 投机热度 × 排雷记分卡。

定位是**投机选股工具**,排雷是风控亮灯层——亮灯不拦截(trap 标注制同一哲学):
季报雷暴出清后小微盘是 A 股季节性投机主战场,而小市值恰是 ST/退市雷的重灾区,
热钱爱炒的与容易暴雷的经常是同一批票,两个维度必须同屏。

数据全部读时合成,与掘金台同一套「不做采集、不落新表」纪律:
    - 池子:quote_daily 最新日总市值切档(微盘<25 亿/小票 25~60 亿),
      排北交所(30%涨跌幅/流动性结构另类)、剔上市不满 1 年次新;
    - 排雷:2024 退市新规原文要件逐条独立亮灯,营收分板线复用采集侧
      scoring._st_board_line(主板 3 亿/双创 1 亿,同一函数不另起口径);
    - 热度:涨停生态复用 sentiment job 的东财榜单池(连板数/封板资金/炸板,
      全市场 15 交易日窗),资金流与概念题材来自 heat job 的 JSON 工件
      (data/heat/,个股主力净流入今日+5日、概念板块排行+热门概念成分映射);
    - 雷暴日历:fin_indicator 披露家数/全 A 分母——出清窗口=小微盘超额的
      历史时段,这是模块的选时锚。

heat 工件缺失时资金/概念两维度整体降级为「—」(显性标注,不冒充 0 分),
涨停生态+动量+换手的底座永远在——纯行情形态下模块依然完整可用。

排雷分级(逐条独立、取最高级,规则取新规原文不是拟合):
    3 确定  简称已 ST/*ST;最新年报利润/扣非孰低<0 且营收低于分板线(披露后
            必然戴帽,未戴帽者=即将戴帽);最新年报净资产为负(gate 同源)
    2 高危  审计非标;证监会立案;亏损+营收不足线 2 倍(明年触线主候选);
            20 日均价<1.5 元或现价<1.2 元(面值退市提前量,它不走 ST 程序);
            总市值逼近市值退市线(主板 5 亿/双创 3 亿+20% 提前量);
            仅最新披露期净资产转负(年报仍正的盲区——不进确定级,年报级
            定性事实才判死,与掘金台资不抵债双道同语法)
    1 关注  资产负债率>90%(非金融);年报亏损但营收在线上;连续两年扣非
            亏(第二年营收再崩即触线);最新披露期转亏(业绩变脸)
    0 安全  无旗

双创上市不满 4 个完整会计年度的未盈利豁免窗内触线,降级为关注(旗名带
_ex 后缀),与 scoring._st_risk 同一豁免口径。

热度分 heat_score(0~100)只是排序器,每个因子独立成列——透明加权,缺数
因子按可用权重归一(总分权重<30 不给分),绝不缺列冒 0:
    主力净流入强度(今日/流通市值)0~30 | 5日累计 0~15 | 换手自身分位 0~15 |
    连板数×8 封顶 20 | 5日/20日动量各 0~10 | 所属热门概念 top15 +10
"""
import csv
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.db import get_session
from app.models import (FinBalance, FinIncome, FinIndicator, QuoteDaily,
                        ScoreDaily, Security, SentimentDaily)

# scoring.py 是 collector 侧平铺 import,API 进程先挂 sys.path(pick.py 同款);
# 营收分板线取同一函数,排雷口径与采集侧 ST 前验旗永不漂移
_CSCRIPTS = str(Path(__file__).resolve().parents[2] / "collector" / "scripts")
if _CSCRIPTS not in sys.path:
    sys.path.insert(0, _CSCRIPTS)
from scoring import _st_board_line  # noqa: E402

from app.api.pick import _discounts  # 两派买点折价,与掘金台同一把尺

router = APIRouter(prefix="/api/microcap", tags=["microcap"])

BACKEND = Path(__file__).resolve().parents[2]
HEAT_DIR = BACKEND / "collector" / "data" / "heat"
SENT_DIR = BACKEND / "collector" / "data" / "sentiment"

# ---- 池子口径(改口径只动这一处) ----
MICRO_CAP_MAX = 25e8      # 微盘上界:总市值 <25 亿
SMALL_CAP_MAX = 60e8      # 小票上界:25 亿 ≤ 总市值 <60 亿
LIST_AGE_MIN_YEARS = 1    # 次新剔除:上市不满 1 年(无完整年报、波动结构另类)
BJ_PREFIXES = ("43", "82", "83", "87", "88", "92")   # 北交所代码前缀,整段排除

# ---- 排雷线(与 scoring/_st_risk 同族,这里把面值/市值两族补齐) ----
FACE_AVG20 = 1.5          # 20 日均价预警线(1 元面值退市的提前量)
FACE_PRICE = 1.2          # 现价预警线(=scoring.price_low 同线)
MCAP_THIN_MAIN = 6e8      # 主板市值退市线 5 亿 +20% 提前量
MCAP_THIN_CY = 4e8        # 双创市值退市线 3 亿 +20% 提前量
ST_FIN_KW = ("金融", "银行", "保险", "证券", "货币金融", "资本市场")  # 同 scoring

# ---- 热度分权重(总分只是排序器,因子列各自独立展示) ----
W_INFLOW_T = 30.0         # 今日主力净流入/流通市值,3% 封顶
INFLOW_T_FULL = 0.03
W_INFLOW_D5 = 15.0        # 5 日主力净流入/流通市值,10% 封顶
INFLOW_D5_FULL = 0.10
W_TURN = 15.0             # 换手自身分位(近 min(120,可用) 交易日)
W_STREAK = 20.0           # 连板数 ×8 封顶
STREAK_PER = 8.0
W_R5 = 10.0               # 5 日涨幅 15% 封顶
R5_FULL = 0.15
W_R20 = 10.0              # 20 日涨幅 30% 封顶
R20_FULL = 0.30
W_CONCEPT = 10.0          # 所属热门概念(top15 板)
MIN_WEIGHT = 30.0         # 可用权重低于此不给分(判不动 ≠ 0 分)
TURN_WIN_MAX = 120        # 换手分位窗上限(表滚动窗现约 43 日,加深自动受益)
TURN_MIN_ROWS = 20        # 分位窗最少行数
HEAT_BANDS = ((70.0, "hot"), (50.0, "warm"))   # 火爆/温热/冷 三档下界

# ---- 风险旗 → 中文 ----
RISK_LABELS = {
    "st_named": "已ST/风险警示",
    "loss_rev_hit": "触线:亏损且营收低于退市线",
    "loss_rev_ex": "触线(双创豁免窗)",
    "neg_equity": "年报资不抵债",
    "negeq_soft": "最新期净资产转负",
    "audit_qualify": "审计非标",
    "case_filed": "证监会立案",
    "loss_rev_near": "亏损且营收不足线2倍",
    "face_value": "面值危险(20日均价<1.5元)",
    "mcap_thin": "市值逼近退市线",
    "debt90": "资产负债率>90%",
    "year_loss": "年报亏损(营收在线上)",
    "loss2y": "连续两年扣非亏",
    "q_turn_loss": "最新期转亏",
}

# 情绪状态 → 次日竞价溢价校准(与 fetch_sentiment.PREMIUM 同源快照,那边改了须同步这里;
# 不直接 import 采集脚本——其模块级会清代理环境变量,不值得为 4 行字典引入副作用)
_PREMIUM_LOCAL = {"极强": "+3.6%(胜率73%)", "普通": "+2.5%(67%)",
                  "回暖": "+2.0%(60%)", "极弱": "+2.2%(63%)"}

# ---- 年历层(出处:桃子课 117-121「A股一年三阶段行情规律」,2018-2024 年复一复现; ----
#      用户 _quant/val_b_monthly 以国证2000 2019-2026 独立复核)。12 个月的大环境=情绪
#      =氛围各不相同:「2 5 11 月玩大票(容量),其余玩小票;做反=极强全错过、长征全赶上」。
#      small_ok: True=小票进攻月(本模块的主场) / False=防守月(降仓/看戏) / None=只做主线核心
MONTH_REGIME = {
    1:  ("极弱", "1月底易血崩(7次崩6次)·极弱激活5-10倍弹性·防守月,雷暴中", False),
    2:  ("极强", "业绩真空期=全年最宽松·7次反弹7次(毛+37.5%)·小票主升", True),
    3:  ("大震", "继承2月弹性+政治事件多·先暴跌后暴涨·月后10天神奇回正·波动大勿追", None),
    4:  ("极弱", "4/20 后年报雷(6年崩6次)·与雷暴日历互证·防守月", False),
    5:  ("极强", "仅次于2月·题材猛·雷暴出清后的主升", True),
    6:  ("长征初", "指数还有活力·大概率收小红·只主线核心", None),
    7:  ("长征", "要史诗级题材才救红(如20年7月券商)·只主线", False),
    8:  ("长征", "资金疲劳·基本跌·看成交额水量(≈6000亿=不够)", False),
    9:  ("长征末", "全年最差·资金疲劳顶点·低吸铁死月", False),
    10: ("极弱", "资金疲劳+三季报雷·防守月·长征终点前最黑", False),
    11: ("极强", "长征后的极强·出清窗口=小票主升·本模块主场", True),
    12: ("博弈", "6次崩5次·低位大阴线次日恐慌点低吸·只能博弈的思路", None),
}

# ---- 红盘率分布校准(出处:桃子课 111-120 s19「中证2000涨跌幅→全A红盘率」正态分布表; ----
#      小盘剔除银行等高股息~10%常年红的干扰后更极端:-2% 时小盘赚钱率实际≈0)。用池内中位
#      涨幅作「平均股价」代理(等权口径),对照当日实际红盘率→偏离即分布异常日
_RED_RATE_CURVE = ((-3.0, 0.03), (-2.0, 0.10), (-1.0, 0.20), (-0.25, 0.40),
                   (0.25, 0.60), (1.0, 0.80), (2.0, 0.90), (3.0, 0.95))


def _red_rate_expect(median_chg_pct: float | None) -> float | None:
    """池内中位涨幅(%) → 分布预期红盘率(0~1),课程校准表线性插值。"""
    if median_chg_pct is None:
        return None
    pts = _RED_RATE_CURVE
    if median_chg_pct <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if median_chg_pct <= x1:
            return y0 + (y1 - y0) * (median_chg_pct - x0) / (x1 - x0)
    return pts[-1][1]


# ---------------------------------------------------------------------------
# 纯函数:排雷分级与热度打分(离线可测,verify_microcap.py 的不变量落在这里)
# ---------------------------------------------------------------------------

def _loss_min(np, ded):
    """利润总额侧的「孰低」:净利润/扣非净利润里可判者取最小;全缺返回 None。"""
    vals = [x for x in (np, ded) if x is not None]
    return min(vals) if vals else None


def _is_biotech_exempt(code: str, annual_year: int, list_date) -> bool:
    """双创未盈利豁免窗:上市不满 4 个完整会计年度,财务类退市指标不适用。"""
    if code[:3] in ("688", "689") or code[:2] == "30":
        if list_date:
            try:
                return annual_year - list_date.year < 4
            except (TypeError, ValueError):
                return False
    return False


def _classify(rec: dict) -> tuple[int, list[str]]:
    """排雷记分卡:入参纯量 dict → (level 0~3, flags)。优先级取最高级,旗全量保留。

    rec 必备键:name/code/annual_np/annual_ded/annual_rev/annual_debt/list_date/
    prev_min_np(上一年报孰低)/latest_np(最新披露期累计净利)/latest_is_annual/
    latest_eq(最新披露期归母权益)/gate_flags(list)/mcap/avg20/price
    """
    flags: list[str] = []
    code = rec["code"]
    is_st = "ST" in str(rec["name"]).upper()
    gate = set(rec.get("gate_flags") or [])

    # ---- 确定级素材 ----
    if is_st:
        flags.append("st_named")
    annual_min = _loss_min(rec["annual_np"], rec["annual_ded"])
    rev = rec["annual_rev"]
    hit = False
    if annual_min is not None and annual_min < 0 and rev is not None:
        line = _st_board_line(code)
        if rev < line:
            if _is_biotech_exempt(code, rec.get("annual_year") or 0, rec.get("list_date")):
                flags.append("loss_rev_ex")
            else:
                flags.append("loss_rev_hit")
                hit = True
        elif rev < 2 * line:
            flags.append("loss_rev_near")
    if annual_min is not None and annual_min < 0 and "loss_rev_hit" not in flags \
            and "loss_rev_ex" not in flags and "loss_rev_near" not in flags:
        flags.append("year_loss")
    if "neg_equity" in gate:
        flags.append("neg_equity")

    # ---- 高危级素材 ----
    if "audit_qualify" in gate:
        flags.append("audit_qualify")
    if "case_filed" in gate:
        flags.append("case_filed")
    eq = rec.get("latest_eq")
    if eq is not None and eq < 0 and "neg_equity" not in gate:
        flags.append("negeq_soft")
    avg20, price = rec.get("avg20"), rec.get("price")
    if (avg20 is not None and avg20 < FACE_AVG20) or (price is not None and 0 < price <= FACE_PRICE):
        flags.append("face_value")
    mcap = rec.get("mcap")
    if mcap:
        thin = MCAP_THIN_CY if (code[:3] in ("688", "689") or code[:2] == "30") else MCAP_THIN_MAIN
        if mcap < thin:
            flags.append("mcap_thin")

    # ---- 关注级素材 ----
    dr = rec.get("annual_debt")
    if dr is not None and dr > 0.9 and not any(k in str(rec.get("industry") or "") for k in ST_FIN_KW):
        flags.append("debt90")
    prev_min = rec.get("prev_min_np")
    if annual_min is not None and annual_min < 0 and prev_min is not None and prev_min < 0:
        flags.append("loss2y")
    lnp = rec.get("latest_np")
    if not rec.get("latest_is_annual") and lnp is not None and lnp < 0 \
            and annual_min is not None and annual_min > 0:
        flags.append("q_turn_loss")

    # ---- 定级:命中即最高档 ----
    L3 = {"st_named", "loss_rev_hit", "neg_equity"}
    L2 = {"negeq_soft", "audit_qualify", "case_filed", "loss_rev_near", "face_value", "mcap_thin"}
    if any(f in L3 for f in flags):
        return 3, flags
    if any(f in L2 for f in flags):
        return 2, flags
    # 双创豁免窗触线降为关注(旗仍在,前端区分文案)
    if flags:
        return 1, flags
    return 0, flags


def _clamp01(x):
    return max(0.0, min(1.0, x))


def heat_score(f: dict) -> float | None:
    """热度分 0~100。缺数因子按可用权重归一;可用权重 <MIN_WEIGHT 判不动返回 None。

    f: inflow_t/float_mcap, inflow_d5/float_mcap(强度比率), turn_pctile(0~1),
       streak(今日连板数), r5/r20(区间收益), hot_concept(bool)
    """
    pts, ws = 0.0, 0.0
    its, dfs = f.get("inflow_strength"), f.get("inflow_d5_strength")
    if its is not None:
        pts += _clamp01(its / INFLOW_T_FULL) * W_INFLOW_T
        ws += W_INFLOW_T
    if dfs is not None:
        pts += _clamp01(dfs / INFLOW_D5_FULL) * W_INFLOW_D5
        ws += W_INFLOW_D5
    tp = f.get("turn_pctile")
    if tp is not None:
        pts += _clamp01(tp) * W_TURN
        ws += W_TURN
    st = f.get("streak")
    if st is not None:
        pts += min(st * STREAK_PER, W_STREAK)
        ws += W_STREAK
    r5, r20 = f.get("r5"), f.get("r20")
    if r5 is not None:
        pts += _clamp01(r5 / R5_FULL) * W_R5
        ws += W_R5
    if r20 is not None:
        pts += _clamp01(r20 / R20_FULL) * W_R20
        ws += W_R20
    if f.get("hot_concept"):
        pts += W_CONCEPT
        ws += W_CONCEPT
    if ws < MIN_WEIGHT:
        return None
    return round(pts / ws * 100.0, 1)


# ---- 危险度(≈随机买入后短期大幅回撤的概率),2026-11-11 ----
# 每个信号独立加权、命中即累加封顶 95——权重全部有库内校准/课程出处,不是拍脑袋:
# 炸板次日 -1.6%/胜率20%(sentiment 回测)、跌停日次日 -2.3~-4.7%(用户 val_c2)、
# 断板次日低开~8%(94课)、高位连板断板=核按钮(80课)、追高=均值回归、
# 确定雷=退市通道(排雷记分卡)、微盘闪崩带(流通<10亿)、拥挤/换手极端=退潮放大器
DANGER_W = {
    "dt_now": 20,        # 今日跌停
    "zha_y": 15,         # 昨日炸板/炸板黑名单
    "broke": 10,         # 断板(昨涨停今未再板)
    "space_high": 25,    # 高位连板 ≥5 板(断板=核按钮)
    "high_streak": 15,   # 连板 3~4 板
    "chase50": 18,       # 20日涨幅 ≥50%
    "chase30": 10,       # 20日涨幅 ≥30%
    "chase5": 6,         # 5日涨幅 ≥20%
    "mine3": 30,         # 排雷确定级
    "mine2": 15,         # 排雷高危级
    "mine1": 6,          # 排雷关注级
    "tiny10": 10,        # 流通市值 <10 亿(微盘闪崩带)
    "tiny15": 5,         # 流通市值 <15 亿
    "turn98": 6,         # 换手自身分位 ≥98%(筹码极端)
    "crowded": 5,        # 热度 ≥90(拥挤交易)
}
DANGER_CAP = 95.0

DANGER_LABELS = {
    "dt_now": "今日跌停",
    "zha_y": "昨日炸板(次日-1.6%校准)",
    "broke": "断板(次日多低开~8%)",
    "space_high": "高位连板(断板=核按钮)",
    "high_streak": "连板3~4板(高度断板风险)",
    "chase50": "追高:20日+50%",
    "chase30": "追高:20日+30%",
    "chase5": "追高:5日+20%",
    "mine3": "排雷确定级",
    "mine2": "排雷高危级",
    "mine1": "排雷关注级",
    "tiny10": "流通<10亿闪崩带",
    "tiny15": "流通<15亿",
    "turn98": "换手分位≥98%",
    "crowded": "热度拥挤≥90",
}


def danger_score(f: dict) -> tuple[int, list[str]]:
    """危险度 0~95 与旗列表。入参纯量 dict,离线可测。

    f: risk_level(0~3), dt_today, zha_flag, zt_yesterday, streak,
       r5/r20, float_mcap, turn_pctile(0~1), heat
    """
    pts = 0
    flags: list[str] = []
    rl = f.get("risk_level") or 0
    if rl >= 3:
        pts += DANGER_W["mine3"]; flags.append("mine3")
    elif rl == 2:
        pts += DANGER_W["mine2"]; flags.append("mine2")
    elif rl == 1:
        pts += DANGER_W["mine1"]; flags.append("mine1")
    if f.get("dt_today"):
        pts += DANGER_W["dt_now"]; flags.append("dt_now")
    if f.get("zha_flag"):
        pts += DANGER_W["zha_y"]; flags.append("zha_y")
    if f.get("zt_yesterday") and not f.get("streak"):
        pts += DANGER_W["broke"]; flags.append("broke")
    st = f.get("streak")
    if st is not None:
        if st >= 5:
            pts += DANGER_W["space_high"]; flags.append("space_high")
        elif st >= 3:
            pts += DANGER_W["high_streak"]; flags.append("high_streak")
    r20, r5 = f.get("r20"), f.get("r5")
    if r20 is not None and r20 >= 0.5:
        pts += DANGER_W["chase50"]; flags.append("chase50")
    elif r20 is not None and r20 >= 0.3:
        pts += DANGER_W["chase30"]; flags.append("chase30")
    if r5 is not None and r5 >= 0.2:
        pts += DANGER_W["chase5"]; flags.append("chase5")
    fm = f.get("float_mcap")
    if fm is not None:
        if fm < 10e8:
            pts += DANGER_W["tiny10"]; flags.append("tiny10")
        elif fm < 15e8:
            pts += DANGER_W["tiny15"]; flags.append("tiny15")
    tp = f.get("turn_pctile")
    if tp is not None and tp >= 0.98:
        pts += DANGER_W["turn98"]; flags.append("turn98")
    heat = f.get("heat")
    if heat is not None and heat >= 90:
        pts += DANGER_W["crowded"]; flags.append("crowded")
    return min(pts, int(DANGER_CAP)), flags


def spec_score(heat: float | None, danger: int) -> float | None:
    """投机推荐分 = 热度 ×(100−危险度)/100。

    乘法语义:热度是进攻(想吃到的肉),危险度是折损(吃肉路上被埋的概率)——
    「又冷又安全」不得高分(死票),「又热又危险」被重折(雷上加热)。
    热度判不动(None)则投机分同为 None,绝不冒 0。
    """
    if heat is None:
        return None
    return round(heat * (100 - danger) / 100.0, 1)


def spec_band(score: float | None) -> str | None:
    """≥65 可上手 / ≥45 观察 / 其余看戏。"""
    if score is None:
        return None
    if score >= 65:
        return "trade"
    if score >= 45:
        return "watch"
    return "rest"


def heat_band(score: float | None) -> str | None:
    if score is None:
        return None
    for lo, band in HEAT_BANDS:
        if score >= lo:
            return band
    return "cool"


# ---------------------------------------------------------------------------
# 文件工件装载(heat job / sentiment 榜单池;读不到按降级纪律返回空)
# ---------------------------------------------------------------------------

def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _heat_files() -> dict:
    """heat 工件:{latest, flow, boards, members}。全部可缺——调用侧按缺降级。"""
    latest = _read_json(HEAT_DIR / "latest.json", None)
    flow, boards, members = {}, [], {}
    if latest:
        d = (latest.get("date") or "").replace("-", "")
        flow = _read_json(HEAT_DIR / f"flow_{d}.json", {})
        boards = _read_json(HEAT_DIR / f"boards_{d}.json", [])
        members = _read_json(HEAT_DIR / "members.json", {})
    return {"latest": latest, "flow": flow, "boards": boards, "members": members}


def _pool_csv_map(kind: str, need: int) -> list[tuple[str, dict]]:
    """sentiment 榜单池 CSV,新→旧至多 need 份:[(yyyymmdd, {code: 行dict})]。"""
    out = []
    for p in sorted(SENT_DIR.glob(f"pool_{kind}_*.csv"), reverse=True):
        d = p.stem.split("_")[-1]
        try:
            with open(p, encoding="utf-8-sig", newline="") as fh:
                rows = {str(r.get("代码") or "").zfill(6): r for r in csv.DictReader(fh)}
        except OSError:
            continue
        out.append((d, rows))
        if len(out) >= need:
            break
    return out


def _read_daily_json() -> dict:
    return _read_json(SENT_DIR / "daily.json", {}) or {}


# ---------------------------------------------------------------------------
# 雷暴日历
# ---------------------------------------------------------------------------

def _calendar(today: date, progress: float | None, last_progress: float | None) -> dict:
    """当前/上一/下一个披露窗。窗内且进度<95%=雷暴进行中,其余=出清窗口
    (出清态带上一个刚收窗的报告期与终值进度——雷暴统计读的就是它)。"""
    y = today.year
    windows = [
        (date(y, 1, 1), date(y, 4, 30), date(y - 1, 12, 31), f"{y - 1}年报+{y}一季报"),
        (date(y, 7, 1), date(y, 8, 31), date(y, 6, 30), f"{y}中报"),
        (date(y, 10, 1), date(y, 10, 31), date(y, 9, 30), f"{y}三季报"),
        (date(y + 1, 1, 1), date(y + 1, 4, 30), date(y, 12, 31), f"{y}年报+{y + 1}一季报"),
    ]
    cur = next((w for w in windows if w[0] <= today <= w[1]), None)
    if cur:
        ws, we, period, label = cur
        revealing = progress is not None and progress < 0.95
        return {"period": period.isoformat(), "label": label,
                "window": [ws.isoformat(), we.isoformat()],
                "state": "revealing" if revealing else "cleared",
                "progress": progress, "days_to_next": 0,
                "next_label": None, "last_period": None, "last_label": None}
    nxt = next((w for w in windows if w[0] > today), None)
    last = next((w for w in reversed(windows) if w[1] < today), None)
    return {"period": None, "label": None, "window": None,
            "state": "cleared", "progress": None,
            "days_to_next": (nxt[0] - today).days if nxt else None,
            "next_label": nxt[3] if nxt else None,
            "last_period": last[2].isoformat() if last else None,
            "last_label": last[3] if last else None,
            "last_progress": last_progress}


# ---------------------------------------------------------------------------
# 行装载(进程缓存:键=评分日/行情日+当日行数+heat戳+榜单池戳,与 pick 同语法)
# ---------------------------------------------------------------------------

_cache: dict = {"key": None, "at": 0.0, "items": None, "meta": None}
_CACHE_TTL = 3600.0


def _heat_stamp() -> tuple:
    latest = _read_json(HEAT_DIR / "latest.json", None)
    if not latest:
        return ("-", 0)
    mem = HEAT_DIR / "members.json"
    return (latest.get("date"), int(mem.stat().st_mtime) if mem.exists() else 0)


def _csv_stamp() -> str:
    snaps = sorted(SENT_DIR.glob("pool_zt_*.csv"))
    return snaps[-1].stem if snaps else "-"


class MicrocapItem(BaseModel):
    """池内一行:行情+财务+排雷+热度+库内评分,前端表格列的超集。"""
    sid: int
    code: str
    name: str
    industry: str | None = None
    price: float | None = None
    chg_pct: float | None = None
    market_cap: float | None = None
    float_mcap: float | None = None
    pool: str                      # micro / small
    list_date: str | None = None
    list_date_missing: bool = False
    # 排雷
    risk_level: int = 0
    risk_flags: list[str] = []
    risk_labels: list[str] = []
    # 财务快照(最新年报 + 最新披露期)
    annual_date: str | None = None
    annual_np: float | None = None
    annual_ded: float | None = None
    annual_rev: float | None = None
    rev_line: float | None = None          # 分板营收线(元)
    rev_line_ratio: float | None = None    # 营收/线,>1 在线外
    debt_ratio: float | None = None
    latest_period: str | None = None
    latest_np: float | None = None
    avg20: float | None = None
    # 热度
    heat: float | None = None
    heat_band: str | None = None
    # 危险度与投机推荐分(2026-11-11)
    danger: int = 0
    danger_flags: list[str] = []
    danger_labels: list[str] = []
    spec: float | None = None
    spec_band: str | None = None
    inflow_t: float | None = None
    inflow_strength: float | None = None   # 今日净流入/流通市值
    inflow_d5: float | None = None
    turn_rate: float | None = None
    turn_pctile: float | None = None
    streak: int | None = None              # 今日连板数(非涨停日为 None)
    seal_amt: float | None = None          # 封板资金(元)
    seal_strength: float | None = None     # 封板强度=封单/流通市值(答疑口径:封单大≠强,看盘子)
    zt_5d: int = 0                         # 近 5 个榜单日涨停次数
    zt_yesterday: bool = False             # 上一榜单日在涨停池(今日晋级/断板的判定底)
    zha_flag: bool = False                 # 昨日炸板/黑名单
    dt_today: bool = False
    turn_profit: bool = False              # 扭亏/摘帽候:年报亏损(或已ST)且最新披露期转正
    concept_lead: list[str] = []           # 此股是其龙头(身位最高)的热门概念名
    r5: float | None = None
    r20: float | None = None
    hot_concepts: list[str] = []
    # 库内评分与买点(照掘金台,帮助挑「干净的超跌小票」)
    recommend: float | None = None
    value: float | None = None
    growth: float | None = None
    div_yield: float | None = None
    discount: float | None = None
    discount_anchor: str | None = None


class OverviewOut(BaseModel):
    trade_date: str | None
    quote_date: str | None
    pools: dict
    risk_dist: dict
    env: dict
    calendar: dict
    battle: dict
    spec: dict
    heat: dict
    mine_stats: dict


class BoardOut(BaseModel):
    total: int
    page: int
    page_size: int
    meta: dict
    items: list[MicrocapItem]


def _load_base(db: Session, fresh: bool = False):
    """全量池内行 + meta。缓存键含 heat/latest 戳与榜单池最新文件名:
    heat job 落盘即失效,不等 TTL。"""
    td = db.execute(select(func.max(ScoreDaily.trade_date))).scalar_one()
    qd_max = db.execute(select(func.max(QuoteDaily.trade_date))).scalar_one()
    key = (
        td.isoformat() if td else None,
        db.execute(select(func.count()).select_from(ScoreDaily)
                   .where(ScoreDaily.trade_date == td)).scalar_one() if td else None,
        qd_max.isoformat() if qd_max else None,
        db.execute(select(func.count()).select_from(QuoteDaily)
                   .where(QuoteDaily.trade_date == qd_max)).scalar_one() if qd_max else None,
        _heat_stamp(), _csv_stamp(),
    )
    if (not fresh and _cache["items"] is not None and _cache["key"] == key
            and time.time() - _cache["at"] < _CACHE_TTL):
        return _cache["items"], _cache["meta"]

    today = date.today()
    list_cutoff = date(today.year - LIST_AGE_MIN_YEARS, today.month, today.day)

    # ---- 主行:A股 × 最新评分 × 最新行情(相关子查询取每家最新一行,pick 同款) ----
    _Q2 = aliased(QuoteDaily)
    rows = db.execute(
        select(ScoreDaily, Security, QuoteDaily)
        .join(Security, Security.sid == ScoreDaily.sid)
        .join(QuoteDaily, and_(
            QuoteDaily.sid == ScoreDaily.sid,
            QuoteDaily.trade_date == select(func.max(_Q2.trade_date))
            .where(_Q2.sid == ScoreDaily.sid).scalar_subquery()))
        .where(ScoreDaily.trade_date == td, Security.market == "A",
               or_(Security.list_date.is_(None), Security.list_date < list_cutoff))
    ).all()

    recs: dict[int, dict] = {}
    keep_sids: list[int] = []
    for s, sec, q in rows:
        code = sec.code
        if code[:2] in BJ_PREFIXES:
            continue
        mcap = float(q.market_cap) if q.market_cap else None
        if not mcap or mcap >= SMALL_CAP_MAX:
            continue
        recs[sec.sid] = {
            "sid": sec.sid, "code": code, "name": sec.name,
            "industry": sec.industry, "list_date": sec.list_date,
            "price": float(q.price) if q.price is not None else None,
            "chg_pct": float(q.change_pct) if q.change_pct is not None else None,
            "mcap": mcap,
            "float_mcap": float(q.float_market_cap) if q.float_market_cap else None,
            "turn_rate": float(q.turnover_rate) if q.turnover_rate is not None else None,
            "gate_flags": s.gate_flags or [],
            "recommend": s.recommend, "value": s.value, "growth": s.growth,
            "div_yield": s.div_yield,
            "buy_schloss": s.buy_schloss, "buy_graham_def": s.buy_graham_def,
            # 财务槽位待填
            "annual_year": None, "annual_np": None, "annual_ded": None,
            "annual_rev": None, "annual_debt": None, "annual_date": None,
            "prev_min_np": None, "latest_np": None, "latest_period": None,
            "latest_is_annual": False, "latest_eq": None,
        }
        keep_sids.append(sec.sid)

    # ---- 财务:fin_income 最近两个年报(扣非/营收/净利) ----
    ann = (
        select(FinIncome.sid, FinIncome.report_date,
               func.row_number().over(
                   partition_by=FinIncome.sid,
                   order_by=FinIncome.report_date.desc()).label("rn"))
        .where(FinIncome.sid.in_(keep_sids),
               func.month(FinIncome.report_date) == 12,
               FinIncome.report_date >= date(today.year - 4, 1, 1))
        .subquery()
    )
    for fi in db.execute(
        select(FinIncome).join(ann, and_(ann.c.sid == FinIncome.sid,
                                         ann.c.report_date == FinIncome.report_date,
                                         ann.c.rn <= 2))
    ).scalars():
        rec = recs.get(fi.sid)
        if rec is None:
            continue
        m = _loss_min(fi.net_profit, fi.deducted_net_profit)
        if rec["annual_date"] is None or fi.report_date > rec["annual_date"]:
            rec.update(annual_date=fi.report_date, annual_year=fi.report_date.year,
                       annual_np=float(fi.net_profit) if fi.net_profit is not None else None,
                       annual_ded=float(fi.deducted_net_profit) if fi.deducted_net_profit is not None else None,
                       annual_rev=float(fi.revenue_total) if fi.revenue_total is not None else None)
        elif m is not None:
            rec["prev_min_np"] = m

    # ---- fin_indicator:最新年报(资产负债率兜底营收/净利) + 最新披露期(转亏判定) ----
    ind_ann = (
        select(FinIndicator.sid, FinIndicator.report_date,
               func.row_number().over(
                   partition_by=FinIndicator.sid,
                   order_by=FinIndicator.report_date.desc()).label("rn"))
        .where(FinIndicator.sid.in_(keep_sids),
               func.month(FinIndicator.report_date) == 12,
               FinIndicator.report_date >= date(today.year - 4, 1, 1))
        .subquery()
    )
    for fi in db.execute(
        select(FinIndicator).join(ind_ann, and_(ind_ann.c.sid == FinIndicator.sid,
                                                ind_ann.c.report_date == FinIndicator.report_date,
                                                ind_ann.c.rn <= 1))
    ).scalars():
        rec = recs.get(fi.sid)
        if rec is None:
            continue
        if fi.debt_ratio is not None:
            rec["annual_debt"] = float(fi.debt_ratio)
        if rec["annual_np"] is None and fi.net_profit is not None:
            rec["annual_np"] = float(fi.net_profit)
        if rec["annual_rev"] is None and fi.revenue is not None:
            rec["annual_rev"] = float(fi.revenue)
        if rec["annual_year"] is None:
            rec["annual_year"] = fi.report_date.year

    ind_last = (
        select(FinIndicator.sid, func.max(FinIndicator.report_date).label("md"))
        .where(FinIndicator.sid.in_(keep_sids))
        .group_by(FinIndicator.sid).subquery()
    )
    for fi in db.execute(
        select(FinIndicator).join(ind_last, and_(ind_last.c.sid == FinIndicator.sid,
                                                 ind_last.c.md == FinIndicator.report_date))
    ).scalars():
        rec = recs.get(fi.sid)
        if rec is None:
            continue
        rec["latest_period"] = fi.report_date
        rec["latest_is_annual"] = fi.report_date.month == 12
        rec["latest_np"] = float(fi.net_profit) if fi.net_profit is not None else None

    # ---- 最新披露期归母权益(资不抵债软盲区;pick._neg_equity_latest_sids 同式) ----
    bal_last = (
        select(FinBalance.sid, func.max(FinBalance.report_date).label("md"))
        .where(FinBalance.sid.in_(keep_sids))
        .group_by(FinBalance.sid).subquery()
    )
    for fb in db.execute(
        select(FinBalance).join(bal_last, and_(bal_last.c.sid == FinBalance.sid,
                                               bal_last.c.md == FinBalance.report_date))
    ).scalars():
        rec = recs.get(fb.sid)
        if rec is None:
            continue
        eq = fb.equity_parent if fb.equity_parent is not None else fb.equity_total
        rec["latest_eq"] = float(eq) if eq is not None else None

    # ---- 行情历史:近 ~75 自然日(现窗约 43 行)→20日均价/动量/换手自身分位 ----
    hist: dict[int, list] = defaultdict(list)
    if qd_max and keep_sids:
        for sid, tdate, price, turn in db.execute(
            select(QuoteDaily.sid, QuoteDaily.trade_date, QuoteDaily.price,
                   QuoteDaily.turnover_rate)
            .where(QuoteDaily.sid.in_(keep_sids),
                   QuoteDaily.trade_date >= qd_max - timedelta(days=75))
            .order_by(QuoteDaily.sid, QuoteDaily.trade_date)
        ):
            hist[sid].append((tdate, float(price) if price is not None else None,
                              float(turn) if turn is not None else None))

    # ---- 文件工件:heat + 榜单池 ----
    heat = _heat_files()
    flow: dict = heat["flow"] if heat["latest"] and heat["latest"].get("flow") else {}
    boards: list = heat["boards"] if heat["latest"] and heat["latest"].get("boards") else []
    members: dict = heat["members"] or {}
    member_sets = {b: set(m.get("codes") or []) for b, m in members.items()}
    zt_snaps = _pool_csv_map("zt", 16)   # 近16榜:5日频次窗 + 高度轨迹(榜单池只留15日)
    zha_snaps = _pool_csv_map("zha", 2)
    dt_snaps = _pool_csv_map("dt", 2)
    qd_compact = qd_max.strftime("%Y%m%d") if qd_max else ""
    zt_today = dict(zt_snaps[0][1]) if zt_snaps and zt_snaps[0][0] == qd_compact else {}
    # 「上一榜单日」涨停池:今日快照在列时取它的前一份,否则(周末/榜单未出)最新那份即上一交易日
    if zt_snaps and zt_snaps[0][0] == qd_compact:
        zt_prev_map = dict(zt_snaps[1][1]) if len(zt_snaps) > 1 else {}
    else:
        zt_prev_map = dict(zt_snaps[0][1]) if zt_snaps else {}
    zt_hist = Counter()
    for _d, m in zt_snaps[1:6]:
        zt_hist.update(m.keys())
    zha_yesterday = set(zha_snaps[1][1].keys()) if len(zha_snaps) > 1 else (
        set(zha_snaps[0][1].keys()) if zha_snaps else set())
    daily_json = _read_daily_json()
    blacklist = {str(b.get("code") or "").zfill(6)
                 for b in daily_json.get("blacklist") or []}
    dt_today = set(dt_snaps[0][1].keys()) if dt_snaps and dt_snaps[0][0] == qd_compact else set()

    # ---- 逐行合成 ----
    items: list[MicrocapItem] = []
    for sid, rec in recs.items():
        series = hist.get(sid) or []
        prices = [p for _d, p, _t in series if p is not None][-(TURN_WIN_MAX + 1):]
        turns = [t for _d, _p, t in series if t is not None][-TURN_WIN_MAX:]
        avg20 = (sum(prices[-20:]) / len(prices[-20:])) if len(prices) >= 5 else None
        r5 = r20 = None
        if len(prices) > 5 and prices[-6] > 0:
            r5 = prices[-1] / prices[-6] - 1
        if len(prices) > 20 and prices[-21] > 0:
            r20 = prices[-1] / prices[-21] - 1
        turn_pctile = None
        if rec["turn_rate"] is not None and len(turns) >= TURN_MIN_ROWS:
            below = sum(1 for t in turns if t <= rec["turn_rate"])
            turn_pctile = below / len(turns)
        # 涨停生态(榜单池口径:今日行 + 近 5 榜单日频次 + 上一榜单日 + 昨日炸板)
        zt = zt_today.get(rec["code"])
        streak = int(zt["连板数"]) if zt and zt.get("连板数") else None
        seal = float(zt["封板资金"]) if zt and zt.get("封板资金") else None
        zt_prev = rec["code"] in zt_prev_map
        # ---- 扭亏/摘帽候:年报亏损(或已ST)且最新披露期转正——雷区里的投机正主题 ----
        annual_min = _loss_min(rec["annual_np"], rec["annual_ded"])
        turn_profit = ((annual_min is not None and annual_min < 0) or "ST" in str(rec["name"]).upper()) \
            and rec["latest_np"] is not None and rec["latest_np"] > 0 \
            and rec["annual_date"] is not None and rec["latest_period"] is not None \
            and rec["latest_period"] > rec["annual_date"]
        # ---- 排雷分级 ----
        level, flags = _classify(rec)
        # ---- 热度 ----
        fw = flow.get(rec["code"]) or {}
        inflow_t = fw.get("inflow_t")
        fm = rec["float_mcap"]
        inflow_strength = (inflow_t / fm) if (inflow_t is not None and fm) else None
        d5 = fw.get("inflow_d5")
        d5_strength = (d5 / fm) if (d5 is not None and fm) else None
        concepts = [b["name"] for b in boards
                    if rec["code"] in member_sets.get(b["name"], ())][:3]
        hscore = heat_score({
            "inflow_strength": inflow_strength,
            "inflow_d5_strength": d5_strength,
            "turn_pctile": turn_pctile,
            "streak": streak,
            "r5": r5, "r20": r20,
            "hot_concept": bool(concepts),
        })
        # 危险度与投机推荐分
        dflags_pre = {"risk_level": level, "dt_today": rec["code"] in dt_today,
                      "zha_flag": rec["code"] in zha_yesterday or rec["code"] in blacklist,
                      "zt_yesterday": zt_prev, "streak": streak,
                      "r5": r5, "r20": r20, "float_mcap": fm,
                      "turn_pctile": turn_pctile, "heat": hscore}
        dscore, dflags = danger_score(dflags_pre)
        sscore = spec_score(hscore, dscore)
        seal_strength = (seal / fm) if (seal is not None and fm) else None
        price, bs, bg = rec["price"], rec["buy_schloss"], rec["buy_graham_def"]
        _ds, _dg, disc, anchor = _discounts(price, bs, bg)
        pool = "micro" if (rec["mcap"] or 0) < MICRO_CAP_MAX else "small"
        items.append(MicrocapItem(
            sid=rec["sid"], code=rec["code"], name=rec["name"], industry=rec["industry"],
            price=price, chg_pct=rec["chg_pct"], market_cap=rec["mcap"],
            float_mcap=fm, pool=pool,
            list_date=rec["list_date"].isoformat() if rec["list_date"] else None,
            list_date_missing=rec["list_date"] is None,
            risk_level=level,
            risk_flags=flags,
            risk_labels=[RISK_LABELS[f] for f in flags if f in RISK_LABELS],
            annual_date=rec["annual_date"].isoformat() if rec["annual_date"] else None,
            annual_np=rec["annual_np"], annual_ded=rec["annual_ded"],
            annual_rev=rec["annual_rev"],
            rev_line=float(_st_board_line(rec["code"])),
            rev_line_ratio=(rec["annual_rev"] / _st_board_line(rec["code"]))
            if rec["annual_rev"] is not None else None,
            debt_ratio=rec["annual_debt"],
            latest_period=rec["latest_period"].isoformat() if rec["latest_period"] else None,
            latest_np=rec["latest_np"],
            avg20=round(avg20, 3) if avg20 else None,
            heat=hscore, heat_band=heat_band(hscore),
            danger=dscore, danger_flags=dflags,
            danger_labels=[DANGER_LABELS[f] for f in dflags if f in DANGER_LABELS],
            spec=sscore, spec_band=spec_band(sscore),
            inflow_t=inflow_t, inflow_strength=inflow_strength,
            inflow_d5=d5,
            turn_rate=rec["turn_rate"], turn_pctile=round(turn_pctile, 3) if turn_pctile is not None else None,
            streak=streak, seal_amt=seal,
            seal_strength=round(seal_strength, 4) if seal_strength is not None else None,
            zt_5d=zt_hist.get(rec["code"], 0),
            zt_yesterday=zt_prev,
            zha_flag=rec["code"] in zha_yesterday or rec["code"] in blacklist,
            dt_today=rec["code"] in dt_today,
            turn_profit=bool(turn_profit),
            r5=round(r5, 4) if r5 is not None else None,
            r20=round(r20, 4) if r20 is not None else None,
            hot_concepts=concepts,
            recommend=rec["recommend"], value=rec["value"], growth=rec["growth"],
            div_yield=rec["div_yield"], discount=disc, discount_anchor=anchor,
        ))

    # ---- 投机作战素材:概念龙头(身位) + 昨日涨停今日表现(池内) + 全市场连板梯队 ----
    lead_by_code: dict[str, list[str]] = {}
    for b in boards:
        name = b["name"]
        if not member_sets.get(name):
            continue
        cands = [it for it in items if name in it.hot_concepts]
        if not cands:
            continue
        # 身位=今日连板,次序=热度(无板时热度高者也算「资金身位」)
        top = max(cands, key=lambda it: (it.streak or 0, it.heat or 0))
        lead_by_code.setdefault(top.code, []).append(name)
    for it in items:
        it.concept_lead = lead_by_code.get(it.code, [])

    yd = [it for it in items if it.zt_yesterday]
    yd_chgs = [it.chg_pct for it in yd if it.chg_pct is not None]
    zt_yd_perf = {
        "n": len(yd),
        "avg_chg": round(sum(yd_chgs) / len(yd_chgs), 2) if yd_chgs else None,
        "red_rate": (sum(1 for c in yd_chgs if c > 0) / len(yd_chgs)) if yd_chgs else None,
        "promo": sum(1 for it in yd if it.streak),   # 今日再板=晋级
    }
    ladder_dist = Counter()
    ladder_max = 0
    for row in zt_today.values():
        try:
            lv = int(row.get("连板数") or 1)
        except (TypeError, ValueError):
            lv = 1
        ladder_dist[lv] += 1
        ladder_max = max(ladder_max, lv)
    ladder = {"max": ladder_max, "total": sum(ladder_dist.values()),
              "dist": {str(k): ladder_dist[k] for k in sorted(ladder_dist, reverse=True)}}
    # 跌停侧(大A大B 负螺旋监控,出处:桃子课 39「业绩雷引发大跌」+59「股市黑洞」+答疑:
    # 一字跌停梯队带全体补跌=负螺旋;≥3 家=预警(10A 开团量级),≥5 家=极弱判定
    # (答疑文档口径:「极弱周期就是由是否出现早盘 5 个以上开盘一字跌停来判断」);
    # 一字梯队打开=正螺旋反弹起点)
    dt_rows = list(dt_snaps[0][1].values()) if dt_snaps and dt_snaps[0][0] == qd_compact else []
    dt_n = len(dt_rows)
    dt_oneword = 0
    for r in dt_rows:
        try:
            if abs(float(r.get("涨跌幅") or 0)) >= 9.8:
                dt_oneword += 1
        except (TypeError, ValueError):
            pass
    spiro_level = 2 if dt_oneword >= 5 else (1 if dt_oneword >= 3 else 0)

    # 高度轨迹(80-90课空间龙图:周期龙目标=超越前周期高度;「二板一定冰点」=高度压到
    # 2 板即情绪冰点反转观察信号)。从近 16 个榜单日各取当日最高连板数,旧→新
    def _snap_max(rows: dict) -> int:
        mx = 0
        for r in rows.values():
            try:
                mx = max(mx, int(r.get("连板数") or 0))
            except (TypeError, ValueError):
                pass
        return mx

    height_trail = [_snap_max(m) for _d, m in zt_snaps]
    height_trail.reverse()   # 旧→新,末位=今日
    prev_cycle_max = None
    cycle_note = None
    if len(height_trail) >= 4 and height_trail[-1] > 0:
        cur = height_trail[-1]
        trough_i = height_trail.index(min(height_trail[:-1])) if len(height_trail) > 2 else 0
        before = height_trail[:trough_i + 1]
        prev_cycle_max = max(before) if before else None
        if prev_cycle_max and cur > prev_cycle_max:
            cycle_note = f"刷新周期新高(前周期 {prev_cycle_max} 板)——情绪周期开启新空间"
        elif cur <= 2:
            cycle_note = "高度压到 2 板=二板冰点——情绪冰点反转观察信号,首板晋级率是关键"
        elif prev_cycle_max:
            cycle_note = f"前周期高度 {prev_cycle_max} 板,周期龙目标=超越它"

    # ---- meta:环境/日历/分布/热榜 ----
    sent = db.execute(
        select(SentimentDaily).order_by(SentimentDaily.trade_date.desc()).limit(1)
    ).scalar_one_or_none()
    env = {}
    if sent:
        env = {"date": sent.trade_date.isoformat(), "state": sent.state,
               "red_rate": sent.red_rate, "zt_count": sent.zt_count,
               "zha_rate": sent.zha_rate, "r5": sent.r5,
               "t": _sentiment_t(db, sent)}
    # 披露进度:当前窗(雷暴中)/上一窗(出清态)的报告期已披露家数 / 全 A
    cal = _calendar(today, None, None)
    total_a = db.execute(
        select(func.count()).select_from(Security)
        .where(Security.market == "A")).scalar_one()

    def _progress(period_iso: str | None):
        if not period_iso or not total_a:
            return None
        disclosed = db.execute(
            select(func.count(func.distinct(FinIndicator.sid)))
            .where(FinIndicator.report_date == date.fromisoformat(period_iso))).scalar_one()
        return disclosed / total_a

    if cal["period"]:
        cal = _calendar(today, _progress(cal["period"]), None)
    else:
        cal = _calendar(today, None, _progress(cal.get("last_period")))
    risk_dist = Counter(it.risk_level for it in items)
    pool_cnt = Counter(it.pool for it in items)
    pool_zt = sum(1 for it in items if it.streak)
    # 打法提示:当日 daily.json 新鲜则用采集侧落盘的 premium_hint/trap,
    # 否则退 _PREMIUM_LOCAL 按最新状态查表(字典与 fetch_sentiment 同源)
    chgs = [it.chg_pct for it in items if it.chg_pct is not None]
    daily_fresh = daily_json.get("date") == (qd_max.isoformat() if qd_max else "")
    premium_hint = (daily_json.get("premium_hint") if daily_fresh else None) \
        or _PREMIUM_LOCAL.get(env.get("state") or "")
    # 分布校准:池内中位涨幅=「平均股价」代理 → 课程红盘率分布预期 vs 实际红盘率
    median_chg = round(sorted(chgs)[len(chgs) // 2], 2) if chgs else None
    regime_label, regime_advice, small_ok = MONTH_REGIME[today.month]
    battle = {
        "premium_hint": premium_hint,
        "trap": bool(daily_json.get("trap")) if daily_fresh else False,
        "zt_yd": zt_yd_perf,
        "ladder": {**ladder, "trail": height_trail[-15:], "cycle_note": cycle_note,
                   "prev_cycle_max": prev_cycle_max},
        "regime": {"month": today.month, "label": regime_label,
                   "advice": regime_advice, "small_ok": small_ok},
        "calibration": {"median_chg": median_chg,
                        "expect_red": _red_rate_expect(median_chg),
                        "actual_red": (sum(1 for c in chgs if c > 0) / len(chgs)) if chgs else None},
        "dt_side": {"n": dt_n, "oneword": dt_oneword, "negative_spiro": dt_oneword >= 3,
                    "level": spiro_level},
    }
    # 投机推荐分分布与池内危险度中位
    specs = [it.spec for it in items if it.spec is not None]
    dangers = sorted(it.danger for it in items)
    spec_stat = {
        "trade": sum(1 for s in specs if s >= 65),
        "watch": sum(1 for s in specs if 45 <= s < 65),
        "rest": sum(1 for s in specs if s < 45),
        "danger_median": dangers[len(dangers) // 2] if dangers else None,
    }
    top_boards = []
    for b in boards:
        cnt = sum(1 for it in items if it.code in member_sets.get(b["name"], ()))
        top_boards.append({**b, "pool_count": cnt, "members_known": b["name"] in member_sets})
    meta = {
        "trade_date": key[0], "quote_date": key[2],
        "env": env, "calendar": cal, "battle": battle, "spec": spec_stat,
        "pools": {"micro": pool_cnt.get("micro", 0), "small": pool_cnt.get("small", 0)},
        "risk_dist": {str(k): risk_dist.get(k, 0) for k in (3, 2, 1, 0)},
        "pool_effect": {
            "avg_chg": round(sum(chgs) / len(chgs), 2) if chgs else None,
            "median_chg": round(sorted(chgs)[len(chgs) // 2], 2) if chgs else None,
            "red_rate": (sum(1 for c in chgs if c > 0) / len(chgs)) if chgs else None,
            "zt_count": pool_zt,
        },
        "heat": {
            "date": (heat["latest"] or {}).get("date"),
            "flow_ok": bool(flow),
            "missing_members": (heat["latest"] or {}).get("missing_members") or [],
            "updated_at": (heat["latest"] or {}).get("updated_at"),
            "boards": top_boards,
        },
    }
    _cache.update(key=key, at=time.time(), items=items, meta=meta)
    return items, meta


def _sentiment_t(db: Session, row) -> float | None:
    """情绪温度 T:pick._sentiment_snapshot 同式(streak 从近 500 日状态推)。"""
    from collector.scripts import sentiment_score  # noqa: PLC0415  API 进程延迟导入
    states = db.execute(
        select(SentimentDaily.state).where(SentimentDaily.trade_date <= row.trade_date)
        .order_by(SentimentDaily.trade_date.desc()).limit(500)
    ).scalars().all()
    streak = sentiment_score.streak_of(list(reversed(states)))[-1] if states else 0
    sc = sentiment_score.score_card(
        {"red_rate": row.red_rate, "zt_count": row.zt_count, "zha_rate": row.zha_rate,
         "dt_count": row.dt_count, "yz_dt_count": row.yz_dt_count, "r5": row.r5},
        row.state, streak)
    return sc["t"] if sc else None


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------

SORT_KEYS = {
    "heat", "inflow_t", "inflow_strength", "turn_rate", "r5", "r20", "streak",
    "market_cap", "chg_pct", "discount", "recommend", "div_yield", "risk_level",
    "rev_line_ratio", "value", "seal_strength", "spec", "danger",
}


def _sort_items(items: list[MicrocapItem], sort: str, order: str):
    attr = sort if sort in SORT_KEYS else "heat"

    def key(it):
        v = getattr(it, attr)
        return (v is None, v or 0.0)   # 缺失垫底,与方向无关

    return sorted(items, key=key, reverse=(order != "asc"))


@router.get("/overview", response_model=OverviewOut)
def microcap_overview(db: Session = Depends(get_session)):
    items, meta = _load_base(db)
    env = {**meta["env"], "pool_effect": meta["pool_effect"]}
    mine = {
        "st_named": sum(1 for it in items if "st_named" in it.risk_flags),
        "loss_rev_hit": sum(1 for it in items if "loss_rev_hit" in it.risk_flags),
        "loss_rev_near": sum(1 for it in items if "loss_rev_near" in it.risk_flags),
        "face_value": sum(1 for it in items if "face_value" in it.risk_flags),
        "case_filed": sum(1 for it in items if "case_filed" in it.risk_flags),
        "year_loss": sum(1 for it in items if "year_loss" in it.risk_flags),
    }
    return OverviewOut(
        trade_date=meta["trade_date"], quote_date=meta["quote_date"],
        pools=meta["pools"], risk_dist=meta["risk_dist"],
        env=env, calendar=meta["calendar"], battle=meta["battle"],
        spec=meta["spec"], heat=meta["heat"], mine_stats=mine,
    )


@router.get("/board", response_model=BoardOut)
def microcap_board(
    pool: str | None = Query(None, pattern="^(micro|small|all)$", description="缺省=两池全量"),
    risk: str | None = Query(None, description="风险等级复选(逗号分隔): 3/2/1/0"),
    band: str | None = Query(None, pattern="^(hot|warm|cool)$", description="热度档"),
    concept: str | None = Query(None, description="热门概念名,精确匹配"),
    industry: str | None = Query(None),
    keyword: str | None = Query(None, description="代码/名称包含"),
    safe_only: bool = Query(False, description="只看安全区(risk_level=0)"),
    zt: bool = Query(False, description="预设:只看今日涨停"),
    broke: bool = Query(False, description="预设:上一榜单日涨停、今日断板"),
    turn: bool = Query(False, description="预设:扭亏/摘帽候选(年报亏损或已ST,最新披露期转正)"),
    tradeable: bool = Query(False, description="预设:投机分可上手档(spec≥65)"),
    spec_band: str | None = Query(None, pattern="^(trade|watch|rest)$",
                                  description="投机分档: trade≥65 / watch 45~65 / rest<45"),
    sort: str = "spec",
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    fresh: bool = Query(False, description="绕过进程缓存重算"),
    db: Session = Depends(get_session),
):
    items, meta = _load_base(db, fresh=fresh)
    if pool and pool != "all":
        items = [it for it in items if it.pool == pool]
    if safe_only:
        items = [it for it in items if it.risk_level == 0]
    elif risk:
        wants = {int(x) for x in risk.split(",") if x.strip().isdigit()}
        if wants:
            items = [it for it in items if it.risk_level in wants]
    if zt:
        items = [it for it in items if it.streak is not None]
    if broke:
        items = [it for it in items if it.zt_yesterday and it.streak is None]
    if turn:
        items = [it for it in items if it.turn_profit]
    if tradeable:
        items = [it for it in items if it.spec is not None and it.spec >= 65]
    if spec_band:
        items = [it for it in items if it.spec_band == spec_band]
    if band:
        items = [it for it in items if it.heat_band == band]
    if concept:
        items = [it for it in items if concept in it.hot_concepts]
    if industry:
        items = [it for it in items if it.industry == industry]
    if keyword:
        kw = keyword.strip().lower()
        items = [it for it in items
                 if kw in it.code.lower() or kw in it.name.lower()]
    items = _sort_items(items, sort, order)
    total = len(items)
    start = (page - 1) * page_size
    return BoardOut(
        total=total, page=page, page_size=page_size,
        meta={"trade_date": meta["trade_date"], "quote_date": meta["quote_date"],
              "heat_date": meta["heat"]["date"], "flow_ok": meta["heat"]["flow_ok"]},
        items=items[start:start + page_size],
    )
