# -*- coding: utf-8 -*-
"""掘金台:全库 A股/港股的「值得买」合成与分层推荐。

数据全部来自既有物化列(score_daily 最新交易日 + quote_daily 最新行情 +
valuation_pctile 十年分位 + 投研库覆盖 + 大盘情绪),本模块不做采集、
不落新表:入场分 pick 与分层评级 tier 是**读时合成**,口径演进对全市场
即时生效——与情绪温度同一套「读时计算防双源漂移」的理由。

pick 与 recommend(R)的分工:R = 0.6×价值 + 0.4×成长,回答「这家公司
好不好、贵不贵」,它的资格线(fraud>40 / trap>20 / 中报恶化≤−0.70)已把
不合格者拦在门外;pick 在 R 之上叠加「当下值不值得下手」——价格相对两派
买点的安全边际、PB 十年分位的位置刻度、股息与净现金的垫子,再对灰色地带
(造假/陷阱未到拦截线、ST 关注旗、年报基准滞后)逐项扣分。两把尺子都不
改采集侧任何分数,只做决策合成。

分层(一票回避优先):
- avoid  硬门槛命中(gate)/ ST 高危 / 造假分>40 —— 不该进买入区;
- strong 强推:pick≥78 且安全边际/PB分位/股息至少一项过硬,且无关注旗、
   无中报恶化、年报不滞后、市值≥30 亿人民币当量(格防规模门槛的最低档);
- buy    值得买:pick≥65;
- watch  观察:pick≥45 或施洛斯烟蒂通道入围但未达标;
- na     不构成买入信号(无分或分低)。
无 R 的标的不直接出局:施洛斯分≥70 且现价较买点折 ≥10% 走烟蒂通道,
分数封顶 65(烟蒂按定义到不了「强推」)。
"""
import time
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.api.research import coverage as _research_cov
from app.api.securities import RECENT_DATES, _age_months, _latest_sub
from app.models import QuoteDaily, ScoreDaily, Security, SentimentDaily, ValuationPctile
from collector.scripts import sentiment_score

router = APIRouter(prefix="/api/pick", tags=["pick"])

# 市场范围:本模块只服务 A股/港股(美股不在本次推荐范围)
MARKETS = ("A", "HK")

# HKD→CNY 当量系数,与 scoring.FX_TO_CNY 的 HK 档一致,只用于市值门槛
HK_TO_CNY = 0.92

# ---- pick 加减分常量(全部集中于此,改口径只动这一处) ----
DISC_BONUS_PER = 0.40     # 现价每低于最优买点 1% 加 0.4 分
DISC_BONUS_CAP = 10.0     # 折价 25% 封顶
PCTILE_BONUS = ((10.0, 5.0), (25.0, 3.0), (50.0, 1.0))   # PB 十年分位越低越加
PCTILE_PENALTY = 4.0      # ≥80 分位(十年贵区)扣
DIV_BONUS = ((0.05, 4.0), (0.03, 2.0))                   # 股息率垫
NCR_BONUS = ((0.50, 5.0), (0.30, 3.0))                   # 净现金/市值垫
FRAUD_GRAY_FROM = 25.0    # 造假灰带起点(R 拦截线 40 之前开始扣)
FRAUD_GRAY_K = 0.80       # 40 分时 −12
TRAP_GRAY_FROM = 10.0     # 陷阱灰带起点(R 拦截线 20 之前开始扣)
TRAP_GRAY_K = 1.00        # 20 分时 −10
ST_WATCH_PENALTY = 6.0    # ST 关注旗(st_risk=1)
DIP_PENALTY_MAX = 8.0     # 中报恶化 −0.30 起扣,按深度线性到拦截线 −0.70 扣满
DIP_FROM = 0.30
DIP_TO = 0.70
AGE_LATE_SOFT = 3         # 年报基准落后「应有年报期」3 个月内不扣(披露季节奏)
AGE_LATE_PENALTY = 8.0    # 落后 >3 月:缺最新年报
AGE_LATE_HARD = 15        # 落后 >15 月:连前年年报都缺
AGE_LATE_HARD_PENALTY = 18.0

STRONG_MIN = 78.0
BUY_MIN = 65.0
WATCH_MIN = 45.0
STUB_MIN = 70.0           # 烟蒂通道的施洛斯分门槛
STUB_DISCOUNT = 0.10      # 烟蒂通道要求现价较最优买点折 ≥10%
STUB_CAP = 65.0           # 烟蒂封顶:到不了强推
STUB_BUY_MIN = 55.0       # 烟蒂通道的 buy 线
STRONG_CAP_FLOOR = 30e8   # 强推的市值下限(人民币当量;格防规模门槛最低档)
STRONG_STALE = 3          # 强推不允许年报基准落后

# gate 旗 → 中文(与 import_legacy.GATE_FLAGS 的取值一一对应)
GATE_LABELS = {
    "audit_qualify": "审计非标",
    "risk_warning": "ST/风险警示",
    "case_filed": "证监会立案",
    "neg_equity": "资不抵债",
}
TIER_KEYS = ("strong", "buy", "watch", "na", "avoid")

SORT_KEYS = {
    "pick", "recommend", "value", "growth", "fraud", "trap", "mgmt",
    "schloss", "graham_def", "discount", "pb_pctile", "div_yield",
    "net_cash_ratio", "market_cap", "st_risk", "report_late",
}


class PickItem(BaseModel):
    """掘金台一行:主数据 + 既有评分列 + 决策合成列 + 可解释理由。"""

    sid: int
    code: str
    name: str
    market: str
    currency: str
    industry: str | None = None
    price: float | None = None
    change_pct: float | None = None
    market_cap: float | None = None
    # 既有评分列(score_daily,与列表页/详情页同源同义)
    recommend: float | None = None
    recommend_gate: str | None = None
    value: float | None = None
    growth: float | None = None
    fraud: float | None = None
    trap: float | None = None
    mgmt: float | None = None
    schloss: float | None = None
    graham_def: float | None = None
    div_yield: float | None = None
    net_cash_ratio: float | None = None
    interim_dip: float | None = None
    cyclical: bool | None = None
    cycle_trend: str | None = None
    st_risk: int | None = None
    st_flags: str | None = None
    gate: bool | None = None
    gate_flags: list | None = None
    # 价格参考与折价(折价为负数=低于买点,展示层取绝对值说「低 x%」)
    buy_schloss: float | None = None
    buy_graham_def: float | None = None
    discount_schloss: float | None = None
    discount_graham: float | None = None
    discount: float | None = None          # 两锚中最深折价(最负者)
    discount_anchor: str | None = None     # 最深折价来自哪个锚: schloss / graham_def
    # 十年分位(陈旧判定见 _fresh_pctile;陈旧时为 None)
    pb_pctile: float | None = None
    pb_days: int | None = None
    # 报告期滞后(相对应有年报期,月;0=不滞后)
    report_late: int = 0
    # 决策合成
    pick: float | None = None
    tier: str = "na"
    channel: str | None = None             # R=综合推荐分通道 stub=施洛斯烟蒂通道
    reasons: list[str] = []
    risks: list[str] = []
    research: dict | None = None


class PickBoardOut(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[PickItem]


class PickStarsOut(BaseModel):
    meta: dict
    stats: dict
    stars: list[PickItem]


class PickCloudPoint(BaseModel):
    """散点云一粒:只带图上要用的字段,评级≥观察的全体(≈1300 粒)也要控制在百 KB 级。"""

    code: str
    name: str
    market: str
    tier: str
    pick: float | None = None
    recommend: float | None = None
    discount: float | None = None
    market_cap: float | None = None


# ---------------------------------------------------------------------------
# 合成逻辑
# ---------------------------------------------------------------------------

def _fresh_pctile(val_row, cutoff: date):
    """分位陈旧判定:观测日距 cutoff ≤45 天才可用(与列表页 VAL_STALE_DAYS 同口径)。"""
    if val_row is None or val_row.trade_date is None:
        return None, None
    if cutoff and (cutoff - val_row.trade_date).days > 45:
        return None, None
    return val_row.pb_pctile, val_row.pb_days


def _expected_annual(today: date) -> date:
    """当前应已披露的最新年报期:5 月起应有上一年年报(A股 4/30、港股 3/31 截止后留缓冲),
    1~4 月里上一年报还未到法定披露截止,应有的是前年年报。"""
    y = today.year - 1 if today.month >= 5 else today.year - 2
    return date(y, 12, 31)


def _report_late(report_date, today: date) -> int:
    """年报基准滞后月数:report_date 早于应有年报期几整月;不滞后为 0。"""
    if report_date is None:
        return 0
    exp = _expected_annual(today)
    if report_date >= exp:
        return 0
    return max(0, _age_months(report_date, exp))


def _discounts(price, buy_s, buy_g):
    """两派买点折价率(price/buy−1,负=折价)。买价非正(理论上不出现)视同缺失。"""
    ds = price / buy_s - 1 if price and buy_s and buy_s > 0 else None
    dg = price / buy_g - 1 if price and buy_g and buy_g > 0 else None
    if ds is None and dg is None:
        return ds, dg, None, None
    if dg is None or (ds is not None and ds <= dg):
        return ds, dg, ds, "schloss"
    return ds, dg, dg, "graham_def"


def _synth(s, price, mcap_cny, disc, disc_anchor, pb_pctile, report_late) -> dict:
    """pick/tier/理由合成。入参都是纯量,验证脚本可直接离线复用。"""
    reasons: list[str] = []
    risks: list[str] = []
    fraud = s.fraud
    trap = s.trap
    st = s.st_risk
    dip = s.interim_dip

    # 一票回避:硬门槛 / ST 高危 / 造假过线。回避者不参与打分——
    # 「回避却有 57 分」只会制造「能不能抄底」的错觉,分数留白,风险串说明原因。
    avoid = bool(s.gate) or st == 2 or (fraud is not None and fraud > 40)

    if s.recommend is not None:
        channel = "R"
        pick = float(s.recommend)
        if s.value is not None and s.growth is not None:
            reasons.append(f"综合推荐分 {s.recommend:.0f}(价值 {s.value:.0f} · 成长 {s.growth:.0f})")
        else:
            reasons.append(f"综合推荐分 {s.recommend:.0f}")
    elif (s.score_schloss is not None and s.score_schloss >= STUB_MIN
          and disc is not None and disc <= -STUB_DISCOUNT):
        channel = "stub"
        pick = min(STUB_CAP, s.score_schloss * 0.5 + min(DISC_BONUS_CAP, -disc * 100 * DISC_BONUS_PER))
    else:
        channel = None
        pick = None

    if pick is not None and not avoid:
        if disc is not None and disc < -0.01:
            pick += min(DISC_BONUS_CAP, -disc * 100 * DISC_BONUS_PER)
            anchor = "施洛斯买点" if disc_anchor == "schloss" else "格雷厄姆买点"
            reasons.append(f"现价较{anchor}低 {-disc * 100:.0f}%")
        if pb_pctile is not None:
            for hi, b in PCTILE_BONUS:
                if pb_pctile <= hi:
                    pick += b
                    reasons.append(f"PB 处十年 {pb_pctile:.0f}% 分位")
                    break
            else:
                if pb_pctile >= 80:
                    pick -= PCTILE_PENALTY
                    risks.append(f"PB 处十年 {pb_pctile:.0f}% 高分位")
        if s.div_yield is not None:
            for lo, b in DIV_BONUS:
                if s.div_yield >= lo:
                    pick += b
                    reasons.append(f"股息率 {s.div_yield * 100:.1f}%")
                    break
        if s.net_cash_ratio is not None:
            for lo, b in NCR_BONUS:
                if s.net_cash_ratio >= lo:
                    pick += b
                    reasons.append(f"净现金占市值 {s.net_cash_ratio * 100:.0f}%")
                    break
        if fraud is not None and fraud > FRAUD_GRAY_FROM:
            pick -= (fraud - FRAUD_GRAY_FROM) * FRAUD_GRAY_K
            risks.append(f"造假分 {fraud:.0f} 偏高")
        if trap is not None and trap > TRAP_GRAY_FROM:
            pick -= (trap - TRAP_GRAY_FROM) * TRAP_GRAY_K
            risks.append(f"价值陷阱特征 {trap:.0f}")
        if st == 1:
            pick -= ST_WATCH_PENALTY
            risks.append("ST 关注旗" + (f"({s.st_flags})" if s.st_flags else ""))
        if dip is not None and dip <= -DIP_FROM:
            depth = min(DIP_TO, -dip)
            pick -= depth / DIP_TO * DIP_PENALTY_MAX
            risks.append(f"最新中报净利同比 {dip * 100:.0f}%")
        if report_late > AGE_LATE_HARD:
            pick -= AGE_LATE_HARD_PENALTY
            risks.append(f"年报基准已滞后 {report_late} 个月")
        elif report_late > AGE_LATE_SOFT:
            pick -= AGE_LATE_PENALTY
            risks.append("尚未披露最新年报")

        pick = round(max(0.0, min(100.0, pick)), 1)

    # 强推加门:安全边际/分位/股息至少一项过硬,且无关注旗、无中报恶化、年报不滞后、有规模
    strong_ok = (
        not avoid and channel == "R"
        and ((disc is not None and disc <= -0.10)
             or (pb_pctile is not None and pb_pctile <= 25)
             or (s.div_yield is not None and s.div_yield >= 0.04))
        and st != 1 and (dip is None or dip > -DIP_FROM)
        and report_late <= STRONG_STALE
        and mcap_cny is not None and mcap_cny >= STRONG_CAP_FLOOR
    )

    if avoid:
        tier = "avoid"
        pick = None
        channel = None
        reasons = []
    elif pick is None:
        tier = "na"
    elif channel == "stub":
        tier = "buy" if pick >= STUB_BUY_MIN else "watch"
    elif pick >= STRONG_MIN and strong_ok:
        tier = "strong"
    elif pick >= BUY_MIN and report_late <= AGE_LATE_HARD:
        tier = "buy"
    elif pick >= WATCH_MIN:
        tier = "watch"
    else:
        tier = "na"

    # 回避者把「为什么不买」放最前
    if s.gate and s.gate_flags:
        risks.insert(0, "硬门槛:" + "、".join(GATE_LABELS.get(f, f) for f in s.gate_flags))
    if st == 2:
        risks.insert(0, "ST/退市风险高危")
    if fraud is not None and fraud > 40:
        risks.insert(0, f"造假分 {fraud:.0f} 过线")

    # 展示性备注:烟蒂分/商业模式(不动分)
    if channel != "stub" and s.score_schloss is not None and s.score_schloss >= 80 and not avoid:
        reasons.append(f"施洛斯烟蒂分 {s.score_schloss:.0f}")
    if not avoid and pick is not None:
        if s.bm_pricing:
            reasons.append("具备定价权")
        elif s.bm_light:
            reasons.append("轻资产")
    if s.cyclical:
        risks.append("强周期" + (f"(位置 {s.cycle_trend})" if s.cycle_trend else ""))

    return {"pick": pick, "tier": tier, "channel": channel,
            "reasons": reasons[:5], "risks": risks[:4]}


def _to_item(s, sec, q, val_row, cov, today) -> PickItem:
    price = float(q.price) if q.price is not None else None
    mcap_cny = float(q.market_cap) * (HK_TO_CNY if sec.market == "HK" else 1.0) if q.market_cap else None
    ds, dg, disc, anchor = _discounts(price, s.buy_schloss, s.buy_graham_def)
    pb, pb_days = _fresh_pctile(val_row, s.trade_date)
    late = _report_late(s.report_date, today)
    core = _synth(s, price, mcap_cny, disc, anchor, pb, late)
    return PickItem(
        sid=sec.sid, code=sec.code, name=sec.name, market=sec.market,
        currency=sec.currency, industry=sec.industry,
        price=price,
        change_pct=float(q.change_pct) if q.change_pct is not None else None,
        market_cap=float(q.market_cap) if q.market_cap is not None else None,
        recommend=s.recommend, recommend_gate=s.recommend_gate,
        value=s.value, growth=s.growth, fraud=s.fraud, trap=s.trap, mgmt=s.mgmt,
        schloss=s.score_schloss, graham_def=s.score_graham_def,
        div_yield=s.div_yield, net_cash_ratio=s.net_cash_ratio, interim_dip=s.interim_dip,
        cyclical=s.cyclical, cycle_trend=s.cycle_trend,
        st_risk=s.st_risk, st_flags=s.st_flags,
        gate=bool(s.gate) if s.gate is not None else None, gate_flags=s.gate_flags,
        buy_schloss=s.buy_schloss, buy_graham_def=s.buy_graham_def,
        discount_schloss=ds, discount_graham=dg, discount=disc, discount_anchor=anchor,
        pb_pctile=pb, pb_days=pb_days, report_late=late,
        pick=core["pick"], tier=core["tier"], channel=core["channel"],
        reasons=core["reasons"], risks=core["risks"],
        research=cov.get(sec.code),
    )


# ---------------------------------------------------------------------------
# 基础数据装载(进程内缓存:键=评分日+最新行情日,TTL 5 分钟)
# ---------------------------------------------------------------------------

_cache: dict = {"key": None, "at": 0.0, "items": None, "meta": None}
_CACHE_TTL = 300.0


def _sentiment_snapshot(db: Session) -> dict | None:
    row = db.execute(
        select(SentimentDaily).order_by(SentimentDaily.trade_date.desc()).limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    states = db.execute(
        select(SentimentDaily.state).where(SentimentDaily.trade_date <= row.trade_date)
        .order_by(SentimentDaily.trade_date.desc()).limit(500)
    ).scalars().all()
    streak = sentiment_score.streak_of(list(reversed(states)))[-1] if states else 0
    sc = sentiment_score.score_card(
        {"red_rate": row.red_rate, "zt_count": row.zt_count, "zha_rate": row.zha_rate,
         "dt_count": row.dt_count, "yz_dt_count": row.yz_dt_count, "r5": row.r5},
        row.state, streak)
    return {"date": row.trade_date.isoformat(), "state": row.state,
            "t": sc["t"] if sc else None, "red_rate": row.red_rate}


def _load_base(db: Session, fresh: bool = False):
    """全量合成行 + meta。缓存键 = (评分日, 最新行情日),同键 TTL 内直接复用。"""
    td = db.execute(select(func.max(ScoreDaily.trade_date))).scalar_one()
    qdates = db.execute(
        select(QuoteDaily.trade_date).distinct()
        .order_by(QuoteDaily.trade_date.desc()).limit(RECENT_DATES)
    ).scalars().all()
    key = (td.isoformat() if td else None,
           qdates[0].isoformat() if qdates else None)
    if (not fresh and _cache["items"] is not None and _cache["key"] == key
            and time.time() - _cache["at"] < _CACHE_TTL):
        return _cache["items"], _cache["meta"]

    today = date.today()
    # 行情取「最近 RECENT_DATES 个交易日内每家最新一行」(停牌/滞后收盘由窗口兜住),
    # 与列表页同一套子查询
    qd = _latest_sub(db, QuoteDaily, "qd")
    rows = db.execute(
        select(ScoreDaily, Security, QuoteDaily)
        .join(Security, Security.sid == ScoreDaily.sid)
        .join(qd, qd.c.sid == ScoreDaily.sid)
        .join(QuoteDaily, (QuoteDaily.sid == ScoreDaily.sid)
              & (QuoteDaily.trade_date == qd.c.qd))
        .where(ScoreDaily.trade_date == td, Security.market.in_(MARKETS))
    ).all()
    vals = {v.sid: v for v in db.execute(select(ValuationPctile)).scalars()}
    cov = _research_cov()

    items = [_to_item(s, sec, q, vals.get(sec.sid), cov, today) for s, sec, q in rows]
    meta = {
        "trade_date": key[0],
        "quote_date": key[1],
        "sentiment": _sentiment_snapshot(db),
        "count": len(items),
    }
    _cache.update(key=key, at=time.time(), items=items, meta=meta)
    return items, meta


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------

def _sort_key_of(sort: str):
    """返回 (排序键函数, 缺失值处理):null 一律垫底,与方向无关(desc 时倒序只作用于有值组)。"""
    if sort == "report_late":
        def key(it):
            return it.report_late
        missing = lambda it: False
        return key, missing
    attr = sort if sort in SORT_KEYS else "pick"

    def key(it):
        v = getattr(it, attr)
        return (v is None, v or 0.0)

    def missing(it):
        return getattr(it, attr) is None

    return key, missing


@router.get("/board", response_model=PickBoardOut)
def pick_board(
    market: str | None = Query(None, pattern="^(A|HK)$", description="缺省=A+港股全量"),
    tier: str | None = Query(None, description="评级复选(逗号分隔): strong/buy/watch/na/avoid"),
    industry: str | None = Query(None, max_length=64),
    keyword: str | None = Query(None, max_length=32, description="代码/名称模糊匹配"),
    sort: str = Query("pick", description=f"排序键: {'/'.join(sorted(SORT_KEYS))}"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    fresh: bool = Query(False, description="跳过进程内缓存重算"),
    db: Session = Depends(get_session),
):
    items, _ = _load_base(db, fresh=fresh)
    if market:
        items = [it for it in items if it.market == market]
    if tier:
        wants = {t.strip() for t in tier.split(",") if t.strip()}
        bad = wants - set(TIER_KEYS)
        if bad:
            raise HTTPException(status_code=400, detail=f"invalid tier: {','.join(sorted(bad))}")
        items = [it for it in items if it.tier in wants]
    if industry:
        items = [it for it in items if it.industry == industry]
    if keyword:
        kw = keyword.upper()
        items = [it for it in items if kw in it.code or kw in it.name.upper()]
    key, is_missing = _sort_key_of(sort)
    present = [it for it in items if not is_missing(it)]
    absent = [it for it in items if is_missing(it)]
    present.sort(key=key, reverse=(order == "desc"))
    items = present + absent
    total = len(items)
    start = (page - 1) * page_size
    return PickBoardOut(total=total, page=page, page_size=page_size,
                        items=items[start:start + page_size])


@router.get("/stars", response_model=PickStarsOut)
def pick_stars(
    limit: int = Query(12, ge=1, le=50),
    market: str | None = Query(None, pattern="^(A|HK)$",
                               description="只返回该市场的强推名单;stats 恒为两市场全量(前端按需取)"),
    fresh: bool = Query(False),
    db: Session = Depends(get_session),
):
    """顶部强推名单 + 全市场分层统计 + 数据日期/情绪 meta。"""
    items, meta = _load_base(db, fresh=fresh)
    pool = (it for it in items if it.tier == "strong")
    if market:
        pool = (it for it in pool if it.market == market)
    stars = sorted(pool, key=lambda it: -(it.pick or 0))[:limit]
    stats: dict = {}
    for it in items:
        m = stats.setdefault(it.market, {t: 0 for t in TIER_KEYS})
        m[it.tier] += 1
    return PickStarsOut(meta=meta, stats=stats, stars=stars)


@router.get("/cloud", response_model=list[PickCloudPoint])
def pick_cloud(
    market: str | None = Query(None, pattern="^(A|HK)$"),
    fresh: bool = Query(False),
    db: Session = Depends(get_session),
):
    """散点云:评级≥观察、可打分且有买点锚(折价可算)的全体。
    无锚者不进图——x 坐标只能取哨兵值,堆在左缘成柱反而误导;清单里照样可见。"""
    items, _ = _load_base(db, fresh=fresh)
    out = []
    for it in items:
        if it.tier not in ("strong", "buy", "watch") or it.pick is None:
            continue
        if it.discount is None:
            continue
        if market and it.market != market:
            continue
        out.append(PickCloudPoint(
            code=it.code, name=it.name, market=it.market, tier=it.tier,
            pick=it.pick, recommend=it.recommend, discount=it.discount,
            market_cap=it.market_cap))
    return out
