# -*- coding: utf-8 -*-
"""证券接口:分页列表 + 详情(响应结构与原 companies/*.json 对齐,降低前端移植成本)。"""
from datetime import date, datetime, timedelta
from math import isfinite
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AfterValidator, BaseModel
from sqlalchemy import and_, case, func, not_, or_, select, text
from sqlalchemy.orm import Session

from app.db import get_session
from app.fin_columns import restore_row
from app.models import (
    Dividend,
    FinBalance,
    FinCashflow,
    FinIncome,
    FinIndicator,
    FinNote,
    PeriodicReport,
    QuoteDaily,
    ScoreDaily,
    Security,
    ValuationPctile,
    WindEvent,
    WindHolder,
)

router = APIRouter(prefix="/api/securities", tags=["securities"])

# wind_event.etype(中文) → 原 events JSON 分组键
EVENT_GROUPS = {
    "增减持": "increase_hold", "并购": "ma", "违规": "penalty",
    "诉讼": "lawsuit", "ST": "st_change",
}
# wind_holder.holder_type → 原 holders JSON 分组键
HOLDER_GROUPS = {"top10": "top10", "top10_float": "top10_float",
                 "institution": "institutions", "controller": "actual_controller",
                 "unlock": "unlock"}
# score_daily 列前缀 → 原 index.json scores 流派键
SCHOOL_KEYS = {"graham_agg": "grahamAgg", "graham_def": "grahamDef", "schloss": "schloss", "buffett": "buffett"}


def _f(v):
    return float(v) if v is not None else None


def _age_months(from_d: date, to_d: date) -> int:
    """整月报告龄：同年按月份差、跨年折成 12*n+月份差，未足月（终止日的日早于起始日的日）退一格。

    拿天数除 30 会把「差两天满 13 月」算成 12 月，财报期次是按日历走的，这里就得按日历算。
    """
    m = (to_d.year - from_d.year) * 12 + (to_d.month - from_d.month)
    return m - 1 if to_d.day < from_d.day else m


def _d(v):
    return v.isoformat() if v is not None else None


def _dt(v):
    return v.isoformat(timespec="seconds") if v is not None else None


# ---------- 列表 ----------

class SecurityItem(BaseModel):
    """列表行:主数据 + 最新快照日行情/评分(与原 index.json 字段对齐)。"""

    sid: int
    code: str
    market: str
    name: str
    industry: str | None = None
    currency: str
    price: float | None = None
    change_pct: float | None = None
    pe_ttm: float | None = None
    pb: float | None = None
    market_cap: float | None = None
    score_graham_agg: float | None = None
    score_graham_def: float | None = None
    score_schloss: float | None = None
    score_buffett: float | None = None
    fraud: float | None = None
    mgmt: float | None = None
    cycle: float | None = None
    # 硬门槛（1=触发，0=可判且未触发，null=一个信号都判不了）与命中项，口径见 import_legacy.GATE_FLAGS
    gate: bool | None = None
    gate_flags: list | None = None
    # 评分基准报告期与报告龄（月）：四派分永远建在最新年报上，那一期距今越久分越旧。
    # score_date 是这一行评分快照自己的交易日：逐证券各取最近收盘，美股会比顶部那个
    # 全局快照日旧一天，而 report_age_months 就是相对它算的——不给出它，这个月数无法自证。
    report_date: date | None = None
    report_age_months: int | None = None
    score_date: date | None = None
    # Wind 事件档的溯源三元组（前端算显示值 + 悬停提示用）：fraud/mgmt 本身恒为
    # 财报基础分，不随 wind 参数变化
    # wind_hit 单独给一个硬布尔：事件条目存在但 delta 全空时，前端不能拿“delta 为空”
    # 误判成“无事件数据”，也不能与“基础分本身缺失”的 NULL 混为一谈
    wind_hit: bool = False
    wind_fraud_delta: float | None = None
    wind_mgmt_delta: float | None = None
    wind_flags: list | None = None
    # 价格参考(原列表"买/保/公"四流派合并列 + 清算/净现金)
    fair_liq: float | None = None
    net_cash_ratio: float | None = None
    # PB 近十年历史分位（Wind 口径，百分比数值 0~100）。列表页只放这一列：PE 分位对亏损
    # 标的恒置空会留大片 "-"，PS 分位没有并列的可读宽度。days 是外源实际用的交易日数。
    pb_pctile: float | None = None
    pb_days: int | None = None
    buy_graham_agg: float | None = None
    sell_cons_graham_agg: float | None = None
    sell_fair_graham_agg: float | None = None
    buy_graham_def: float | None = None
    sell_cons_graham_def: float | None = None
    sell_fair_graham_def: float | None = None
    buy_schloss: float | None = None
    sell_cons_schloss: float | None = None
    sell_fair_schloss: float | None = None
    buy_buffett: float | None = None
    sell_cons_buffett: float | None = None
    sell_fair_buffett: float | None = None


class SecurityListOut(BaseModel):
    total: int
    page: int
    page_size: int
    trade_date: date | None = None
    # 估值分位列的截止日期：那一列是外源(Wind)按批滚动铺的，采集链路一旦停摆(客户端掉登录、
    # skill 改路由名、积分耗尽)整列会静默停在旧日期上，故单独给一个日期让页面标出来
    valuation_date: date | None = None
    items: list[SecurityItem]


# 列表排序白名单(防注入)
SORT_COLS = {
    "code": Security.code,
    "market_cap": QuoteDaily.market_cap,
    "pe_ttm": QuoteDaily.pe_ttm,
    "pb": QuoteDaily.pb,
    "score_graham_agg": ScoreDaily.score_graham_agg,
    "score_graham_def": ScoreDaily.score_graham_def,
    "score_schloss": ScoreDaily.score_schloss,
    "score_buffett": ScoreDaily.score_buffett,
    "fraud": ScoreDaily.fraud,
    "mgmt": ScoreDaily.mgmt,
    "cycle": ScoreDaily.cycle,
    # 现价、净现金/市值(后者本身已是比率，跨标的可比，直接按值排)
    "price": QuoteDaily.price,
    "net_cash_ratio": ScoreDaily.net_cash_ratio,
    # PB 十年分位：升序 = 处在自身十年最低那一头（分位本身已是跨市场可比的 0~100）
    "pb_pctile": ValuationPctile.pb_pctile,
}
# 价格参考列的排序口径：买价与每股清算价值都是"每家自己的"绝对值，跨标的比大小没有意义
# (5 元的票不比 50 元的便宜；每股清算 860 的 NVR 现价 6327，按绝对值反倒占了榜首)，
# 故 buy_* 与 fair_liq 排的是折价率 1 - 现价/参考值，降序 = 相对参考值打得最深的第一屏。
# 响应字段与格内数字仍是绝对值，只有排序键换成了比率。
_SCHOOLS = ("graham_agg", "graham_def", "schloss", "buffett")
# 低于一分钱的参考值不算参考值：评分公式相减会留下 1e-17 这种浮点零渣，
# 当分母能把折价率吹到 1e20 量级；而一分以下的价格没有任何标的真能买入。
MIN_PRICE_REF = 0.01


def _discount(ref_col):
    """性价比排序表达式：现价相对参考值折得越深值越大；算不出就不给值(NULL→沉底)

    参考值低于 MIN_PRICE_REF 时不给值而不是让它参与除法——负的参考值会把比率翻成负数，
    把根本不该买入的标的顶到"最便宜"那一端，近零的参考值则在另一端造出天文数字。
    现价缺失同理(无法定位折价深度)。
    """
    return case(
        (and_(QuoteDaily.price.is_not(None), ref_col.is_not(None), ref_col >= MIN_PRICE_REF),
         1 - QuoteDaily.price / ref_col),
        else_=None,
    )


SORT_COLS.update({f"buy_{s}": _discount(getattr(ScoreDaily, f"buy_{s}")) for s in _SCHOOLS})
SORT_COLS["fair_liq"] = _discount(ScoreDaily.fair_liq)
SORT_COLS.update({
    col: getattr(ScoreDaily, col)
    for s in _SCHOOLS
    for col in (f"sell_cons_{s}", f"sell_fair_{s}")
})


# 列表筛选:买点/卖点复选键(与原页 data-flt-buy 一致) → score_daily 价格参考列
FLT_BUY_COLS = {
    "grahamAgg": ScoreDaily.buy_graham_agg,
    "grahamDef": ScoreDaily.buy_graham_def,
    "schloss": ScoreDaily.buy_schloss,
    "buffett": ScoreDaily.buy_buffett,
}
FLT_SELL_COLS = {
    "grahamAgg": (ScoreDaily.sell_cons_graham_agg, ScoreDaily.sell_fair_graham_agg),
    "grahamDef": (ScoreDaily.sell_cons_graham_def, ScoreDaily.sell_fair_graham_def),
    "schloss": (ScoreDaily.sell_cons_schloss, ScoreDaily.sell_fair_schloss),
    "buffett": (ScoreDaily.sell_cons_buffett, ScoreDaily.sell_fair_buffett),
}

# 市场板块(全市场 5500 只规模下的基本维度):按代码前缀判定,无需额外字段
BOARDS = {
    "shMain": ("60",),                  # 沪市主板(含 900 B 股)
    "szMain": ("00",),                  # 深市主板
    "gem": ("30",),                     # 创业板
    "star": ("68",),                    # 科创板
    "bj": ("92", "83", "87", "43"),  # 北交所
}

# ---------- Wind 事件增强分（列表“事件增强分”切换档）----------
# 语义 1:1 对齐旧内嵌页 stockLegacy.js 的 dispFraudCode/dispMgmtCode：
#   有事件条目 → 基础分 + delta 钉到 0~100；无条目 → 不给分(NULL，前端显示“-”)；
#   基础分本身缺失 → 无基可加，同样 NULL（排序/筛选随之排除）。
# 命中判据用 wind_overlay 非空：import_legacy 只在 events/index.json byCode 有条目时
# 写这三个 wind_* 列，故它等价于旧前端判的 eventOverlay[code] 存在（目前仅 A 股 21 家）。
WIND_HIT = ScoreDaily.wind_overlay.is_not(None)


def _wind_score(base, delta):
    """基础分列 + 事件 delta 列 → Wind 档的 SQL 表达式

    只用在筛选与排序上；响应里的 fraud/mgmt 恒为基础分（Wind 档下前端拿 wind_*
    三字段自己算显示值，同一 clip 规则），详情页等消费方不会因传了 wind=1 而拿到
    两套口径的 fraud。旧内嵌页本就是全量前端算（dispFraudCode + sortVal/passFlt），
    搬到后端后“算法在 SQL、显示在 JS”两处各存一份，改坏一边就会排序与展示错位，
    故两个口径的钉边界行为都在此注明：无条目/无基→NULL，有则夹 0~100。
    """
    return case(
        (not_(WIND_HIT), None),
        (base.is_(None), None),
        else_=func.least(func.greatest(base + func.coalesce(delta, 0.0), 0.0), 100.0),
    )


def _finite(v: float) -> float:
    """挡 NaN/Infinity：float("NaN") 不抛，pydantic 也认，一路走到 MySQL 驱动才炸成 500。
    带 ge/le 的参数顺带被拦（NaN 与任何数比较都不为真），但净现金/市值刻意不结界，
    所以这道判断挂在参数类型上，而不是靠每个参数各自补一个假的界。
    带界的几个也一并换用：浮点筛选参数只留一条规则，将来谁放宽了界也不会重新开出口子。"""
    if not isfinite(v):
        raise ValueError("必须是有限数值")
    return v


FiniteF = Annotated[float, AfterValidator(_finite)]


def _flt_keys(raw: str | None, valid: dict | set, name: str) -> list[str]:
    """逗号分隔复选键解析 + 白名单校验(非法直接 400,不做静默丢弃)。"""
    if not raw:
        return []
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    bad = [k for k in keys if k not in valid]
    if bad:
        raise HTTPException(status_code=400, detail=f"invalid {name}: {','.join(bad)}")
    return keys


def _industry_set(db: Session) -> set[str]:
    """库里现有行业名全集（排除筛选的白名单）。行业不是写死的枚举，随采集源的字典变（当前 123 个），
    只能现取；空名与 NULL 同义，都不参与排除，故一并挡在门外。"""
    rows = db.execute(select(Security.industry)
                      .where(Security.industry.isnot(None), Security.industry != "")
                      .distinct()).all()
    return {r[0] for r in rows}


# 全市场 5500 只 × 每日快照：quote_daily/score_daily 一年即百万行，
# 裸写 GROUP BY sid + MAX(trade_date) 会走全索引扫(实测 type=index,代价随行数线性)。
# 行情/评分整批按交易日落盘，各证券最新一行必落在最近 RECENT_DATES 个交易日内
# （美股/港股收盘滞后数日、周末补跑同样覆盖），故限定窗口后再取 max，代价与表总量无关。
RECENT_DATES = 15
# 估值分位只留"最近一次观测"，但一轮跨天铺完约 1.8 天，各标的的观测日本身就错开着；
# 45 天容得下机器关机/长假的正常滞后，再旧就是 Wind 链路停摆，那一列宁可显示 "-"。
VAL_STALE_DAYS = 45


def _latest_sub(db: Session, model, label):
    """(sid, 最近交易日) 子查询：仅在最近 RECENT_DATES 个交易日内取每证券最大。"""
    dates = db.execute(
        select(model.trade_date).distinct().order_by(model.trade_date.desc()).limit(RECENT_DATES)
    ).scalars().all()
    q = select(model.sid, func.max(model.trade_date).label(label))
    if dates:
        q = q.where(model.trade_date >= dates[-1])
    return q.group_by(model.sid).subquery()


@router.get("", response_model=SecurityListOut)
def list_securities(
    market: Literal["A", "HK", "US"] | None = Query(None),
    board: Literal["shMain", "szMain", "gem", "star", "bj"] | None = Query(
        None, description="A 股板块(代码前缀):沪主/深主/创业/科创/北交"),
    st: bool | None = Query(None, description="True 仅 ST/*ST,False 排除"),
    gate: bool | None = Query(
        None,
        description="True 仅触发硬门槛的标的,False 排除它们（审计非标/风险警示/立案处罚/负权益；"
                    "gate 为 NULL 即一个信号都判不了的标的两种筛选都不进）"),
    report_age_max: int | None = Query(
        None, ge=0, le=60, description="评分基准报告期距今 ≤ 多少月（只拦财报陈旧的公司，不改分数）"),
    keyword: str | None = Query(None, max_length=32, description="代码/名称模糊匹配"),
    industry: str | None = Query(None, max_length=64),
    ex_industry: str | None = Query(
        None, max_length=1024,
        description="排除行业复选(逗号分隔,同时排除;无行业标注的标的不受排除影响)"),
    fraud_max: FiniteF | None = Query(None, ge=0, le=100, description="造假风险≤(wind=1 时按增强分)"),
    mgmt_min: FiniteF | None = Query(None, ge=0, le=100, description="管理能力≥(wind=1 时按增强分)"),
    cap_min: FiniteF | None = Query(None, ge=0, description="总市值≥(本币元,与响应 market_cap 同单位;港股/美股是 HKD/USD)"),
    cap_max: FiniteF | None = Query(None, ge=0, description="总市值≤(本币元,与响应 market_cap 同单位;港股/美股是 HKD/USD)"),
    # 净现金/市值门槛：与响应 net_cash_ratio 同为小数比率(0.35=35%)，只有列面按百分比显示。
    # 刻意不设 ge/le：造假与管理有 0~100 的定义域、市值恒正，这列三者都不是——实测全市场
    # −87.7~1.73（两个极值都是港美股，所以分子改读附注的定期存款/受限货币资金后区间端点不动；
    # 深负仍是地产/建筑/AMC，A 股最深 −61.4），A 股上限 0.84，负数是“净负债”的真实值不是缺失，
    # 编一个界只会把合法的深负区间挡在门外。填错单位(把 50% 手填成 50)表现为结果偏少，
    # 看得见，不靠 422 兜；非有限值(NaN/inf)是另一回事，会一路炸到 SQL 变 500，故由 FiniteF 拦下。
    ncr_min: FiniteF | None = Query(None, description="净现金/市值≥(小数比率,0.35=35%;负数=净负债;含边界)"),
    ncr_max: FiniteF | None = Query(None, description="净现金/市值≤(小数比率,0.35=35%;用于专门捞净负债标的;含边界)"),
    # PB 十年分位：与响应 pb_pctile 同单位，是 0~100 的百分比数值（30 = 处于自身十年 30% 位），
    # 不要按 net_cash_ratio 的小数习惯填 0.35。这一列定义域就是 0~100，故给 ge/le。
    pbp_min: FiniteF | None = Query(None, ge=0, le=100, description="PB 十年分位≥(0~100,含边界)"),
    pbp_max: FiniteF | None = Query(None, ge=0, le=100, description="PB 十年分位≤(0~100,含边界;5=十年最便宜的那一档)"),
    wind: bool = Query(False, description="事件增强分档：造假/管理两列的筛选与排序改用基础分+Wind 事件增量（响应里两列仍为基础分，显示值由前端叠 wind_* 字段换算）"),
    buys: str | None = Query(None, max_length=64, description="买点复选(逗号分隔,同时满足)"),
    sells: str | None = Query(None, max_length=64, description="卖点复选(现价≥公允卖价即命中,公允恒高于保守)"),
    discount: float | None = Query(None, gt=0, le=500, description="买点折扣%,仅与 buys 配合"),
    sort: str = Query("code", description=f"排序字段: {'/'.join(SORT_COLS)}（buy_* 与 fair_liq 是折价率 1-现价/参考值，降序=相对参考值折得最深；响应里仍是绝对值）"),
    order: Literal["asc", "desc"] = Query("asc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_session),
):
    """证券分页列表:join 各自最新一行行情与评分。

    逐证券 max(trade_date)(对齐原 index.json"各市场各取最近收盘"语义:
    美股在北京时间白天落后一天时仍显示昨收,不被全局快照日剔除);
    筛选语义对齐原前端 passFlt:依赖的价格/参考价缺失(SQL NULL)自动排除。
    ex_industry 是排除语义:命中名单的行业整体去掉,没有行业标注的标的保留(NULL 不属于任何
    被排除的行业);与 industry 同时给出时按 AND 处理,自相矛盾的组合结果为空集。
    """
    latest_quote = _latest_sub(db, QuoteDaily, "qdate")
    latest_score = _latest_sub(db, ScoreDaily, "sdate")
    # 估值分位每标的只存一行最新观测（滚动一轮要跨天铺完，故不用 _latest_sub 找截面），
    # 但同样要防"整表停在旧日期"：Wind 采集停摆时客户端掉登录/改路由名都会让分位留在
    # 几个月前，那种值参与排序比显示 "-" 更坏，故只接受距全表最新观测日 VAL_STALE_DAYS 内的行。
    val_date = db.execute(select(func.max(ValuationPctile.trade_date))).scalar()
    val_floor = (val_date - timedelta(days=VAL_STALE_DAYS)) if val_date else date.max
    # 造假/管理两列的口径跟着 wind 档切（旧内嵌页的“排序/筛选跟随”）：无事件数据公司在
    # Wind 档下表达式出 NULL，既排到末尾也被 fraud_max/mgmt_min 自动排除，与列页显示“-”一致
    fraud_col = _wind_score(ScoreDaily.fraud, ScoreDaily.wind_fraud_delta) if wind else ScoreDaily.fraud
    mgmt_col = _wind_score(ScoreDaily.mgmt, ScoreDaily.wind_mgmt_delta) if wind else ScoreDaily.mgmt
    # 排除行业的名校验走白名单：排除类筛选失败是"看不见的失败"，半截名或改过名的旧名若被
    # 静默忽略，用户看着排除生效了、实际一片都没少。白名单只在参数非空时取，别为普通请求多付一次 distinct。
    ex_names = _flt_keys(ex_industry, _industry_set(db), "ex_industry") if ex_industry else []

    q = (
        select(Security, QuoteDaily, ScoreDaily, ValuationPctile)
        .join(
            latest_quote, latest_quote.c.sid == Security.sid, isouter=True,
        )
        .join(
            QuoteDaily,
            and_(QuoteDaily.sid == Security.sid, QuoteDaily.trade_date == latest_quote.c.qdate),
            isouter=True,
        )
        .join(
            latest_score, latest_score.c.sid == Security.sid, isouter=True,
        )
        .join(
            ScoreDaily,
            and_(ScoreDaily.sid == Security.sid, ScoreDaily.trade_date == latest_score.c.sdate),
            isouter=True,
        )
        .join(
            ValuationPctile,
            and_(ValuationPctile.sid == Security.sid, ValuationPctile.trade_date >= val_floor),
            isouter=True,
        )
    )

    conds = []
    if market:
        conds.append(Security.market == market)
    if board:
        prefixes = BOARDS[board]
        conds.append(Security.market == "A")
        conds.append(or_(*[Security.code.startswith(p) for p in prefixes]))
    if st is not None:
        # 风险警示是 A 股专有标记：港股、美股没有 ST 制度,而美股简称里带 ST 的（STAR、Stifel…）
        # 实测误命中 88 家,所以判定不能只拼名称子串
        is_st = and_(Security.market == "A", func.upper(Security.name).like("%ST%"))
        conds.append(is_st if st else not_(is_st))
    if gate is not None:
        # 只比 True/False：三值逻辑下 NULL（判不了）既不满足 =1 也不满足 =0→两类筛选都自动排除，
        # 与市值/净现金那几列「算不出就不进区间」同一语义
        conds.append(ScoreDaily.gate.is_(True) if gate else ScoreDaily.gate.is_(False))
    if report_age_max is not None:
        # 按行自身的 trade_date 算,不能用全局最新日：逐证券各取最新一行时三者日期本就错开着
        conds.append(func.timestampdiff(text("MONTH"), ScoreDaily.report_date,
                                       ScoreDaily.trade_date) <= report_age_max)
    if industry:
        conds.append(Security.industry == industry)
    if ex_names:
        # 必须补 IS NULL 那半边：NOT IN 遇 NULL 出 NULL，会把 39 家没有行业标注的标的(38 A + 1 美)
        # 一起静默丢掉，而它们不属于任何被排除的行业。空串在库里不存在(实测 0 家)，不必第三支。
        conds.append(or_(Security.industry.is_(None), Security.industry.notin_(ex_names)))
    if keyword:
        kw = f"%{keyword}%"
        conds.append(or_(Security.code.like(kw), Security.name.like(kw)))
    if fraud_max is not None:
        conds.append(fraud_col <= fraud_max)
    if mgmt_min is not None:
        conds.append(mgmt_col >= mgmt_min)
    # 市值门槛走 quote_daily.market_cap（本币元）：无行情行的公司在 outer join 下是 NULL,
    # SQL 比较不为真→自动排除,与 buys/sells 那类价格门槛同一语义,不用额外兼容。
    # 也不做“折成人民币再比”：汇率源未落地,拿估算汇率折算会污染与 index.json 对答案的基线;
    # 跨市场比体量请分市场 tab 各自筛（前端已注明单位是本币亿）。
    if cap_min is not None:
        conds.append(QuoteDaily.market_cap >= cap_min)
    if cap_max is not None:
        conds.append(QuoteDaily.market_cap <= cap_max)
    # 净现金/市值：与上面市值两条共用三值逻辑——没有评分行、或财报科目不足以算出这列的公司
    # (实测 126 家：A 58 / 港股 43 / 美股 25)是 NULL，比较不为真→自动排除，不额外兼容。
    # 这里不补 IS NULL 那半边(与 ex_industry 相反)：被去掉的是“算不出来的人”而不是“没被点到的人”。
    # 于是 ≤ 那侧的语义是“算得出来且净负债”，不是“所有不净现金的公司”——这差别写进筛选栏提示。
    if ncr_min is not None:
        conds.append(ScoreDaily.net_cash_ratio >= ncr_min)
    if ncr_max is not None:
        conds.append(ScoreDaily.net_cash_ratio <= ncr_max)
    # PB 十年分位：没铺到的标的、以及被采集侧判不可信而置空的（亏损股的 PE 分位、序列停在
    # 过去的日期）都是 NULL，与上面几列同一套三值逻辑→自动排除，不补 IS NULL 那半边。
    if pbp_min is not None:
        conds.append(ValuationPctile.pb_pctile >= pbp_min)
    if pbp_max is not None:
        conds.append(ValuationPctile.pb_pctile <= pbp_max)
    factor = (discount if discount is not None else 100.0) / 100.0
    for k in _flt_keys(buys, FLT_BUY_COLS, "buys"):
        conds.append(QuoteDaily.price <= FLT_BUY_COLS[k] * factor)
    for k in _flt_keys(sells, FLT_SELL_COLS, "sells"):
        cons, fair = FLT_SELL_COLS[k]
        # 两条都写是为了照字面语义（同时越过保守与公允）。实测 6939 行里四派公允恒为
        # 保守的 1.3~1.5 倍且两者同生同灭（sellFair<sellCons 0 次、只有一个为空 0 次），
        # 所以这等价于 price >= fair；保留 cons 那条只为将来某派公允被改到低于保守时不失守。
        conds.append(QuoteDaily.price >= cons)
        conds.append(QuoteDaily.price >= fair)
    if conds:
        q = q.where(*conds)

    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()

    col = SORT_COLS.get(sort)
    if col is None:
        raise HTTPException(status_code=400, detail=f"invalid sort: {sort}")
    if wind and sort in ("fraud", "mgmt"):
        col = fraud_col if sort == "fraud" else mgmt_col
    prim = col.desc() if order == "desc" else col.asc()
    # 参考价/评分列允许 NULL(未抓财务、科目缺失、无评分行)：MySQL 升序把 NULL 排最前,
    # 按买入价升序会先看一屏"-",故升序补一个 NULL 沉底键;降序本就把 NULL 放最后,
    # 不加表达式以保留 idx_list_* 的有序扫描(避免 filesort)。
    q = q.order_by(*([col.is_(None)] if order == "asc" else []), prim, Security.code.asc())

    rows = db.execute(q.limit(page_size).offset((page - 1) * page_size)).all()

    items = [
        SecurityItem(
            sid=sec.sid, code=sec.code, market=sec.market, name=sec.name,
            industry=sec.industry, currency=sec.currency,
            # 行情/评分均 left join:当日抓取缺失的证券返回 NULL 字段而非 500
            price=_f(quote.price) if quote else None,
            change_pct=_f(quote.change_pct) if quote else None,
            pe_ttm=_f(quote.pe_ttm) if quote else None,
            pb=_f(quote.pb) if quote else None,
            market_cap=_f(quote.market_cap) if quote else None,
            score_graham_agg=score.score_graham_agg if score else None,
            score_graham_def=score.score_graham_def if score else None,
            score_schloss=score.score_schloss if score else None,
            score_buffett=score.score_buffett if score else None,
            fraud=score.fraud if score else None,
            mgmt=score.mgmt if score else None,
            cycle=score.cycle if score else None,
            gate=score.gate if score else None,
            gate_flags=score.gate_flags if score else None,
            report_date=score.report_date if score else None,
            report_age_months=(_age_months(score.report_date, score.trade_date)
                               if (score and score.report_date and score.trade_date) else None),
            score_date=score.trade_date if score else None,
            wind_fraud_delta=_f(score.wind_fraud_delta) if score else None,
            wind_mgmt_delta=_f(score.wind_mgmt_delta) if score else None,
            wind_flags=score.wind_flags if score else None,
            wind_hit=bool(score is not None and score.wind_overlay is not None),
            fair_liq=_f(score.fair_liq) if score else None,
            net_cash_ratio=_f(score.net_cash_ratio) if score else None,
            pb_pctile=_f(val.pb_pctile) if val else None,
            pb_days=val.pb_days if val else None,
            buy_graham_agg=_f(score.buy_graham_agg) if score else None,
            sell_cons_graham_agg=_f(score.sell_cons_graham_agg) if score else None,
            sell_fair_graham_agg=_f(score.sell_fair_graham_agg) if score else None,
            buy_graham_def=_f(score.buy_graham_def) if score else None,
            sell_cons_graham_def=_f(score.sell_cons_graham_def) if score else None,
            sell_fair_graham_def=_f(score.sell_fair_graham_def) if score else None,
            buy_schloss=_f(score.buy_schloss) if score else None,
            sell_cons_schloss=_f(score.sell_cons_schloss) if score else None,
            sell_fair_schloss=_f(score.sell_fair_schloss) if score else None,
            buy_buffett=_f(score.buy_buffett) if score else None,
            sell_cons_buffett=_f(score.sell_cons_buffett) if score else None,
            sell_fair_buffett=_f(score.sell_fair_buffett) if score else None,
        )
        for sec, quote, score, val in rows
    ]
    return SecurityListOut(total=total, page=page, page_size=page_size,
                           # 展示用全局最新评分日(行内数据已逐证券取各自最新)
                           trade_date=db.execute(
                               select(func.max(ScoreDaily.trade_date))).scalar_one(),
                           valuation_date=val_date,
                           items=items)


@router.get("/industries")
def list_industries(
    market: Literal["A", "HK", "US"] | None = Query(None),
    db: Session = Depends(get_session),
):
    """行业下拉选项(含只数):全市场 5500 只下行业不可枚举自当页数据。

    走 idx_market_industry (market, industry) 前缀,仅 distinct 分组几百个行业。
    注：本路由必须在 `/{code}` 之前声明,否则会被详情路由吃掉。
    """
    q = (select(Security.industry, func.count().label("cnt"))
         .where(Security.industry.isnot(None), Security.industry != ""))
    if market:
        q = q.where(Security.market == market)
    q = q.group_by(Security.industry).order_by(func.count().desc(), Security.industry)
    return [{"industry": r.industry, "count": r.cnt} for r in db.execute(q)]


# ---------- 详情 ----------

def _load_scores(db: Session, sid: int) -> dict | None:
    """score_daily 最新快照 → 原 index.json scores 结构(含价格参考/Wind 覆盖)。"""
    s = db.execute(
        select(ScoreDaily).where(ScoreDaily.sid == sid).order_by(ScoreDaily.trade_date.desc()).limit(1)
    ).scalars().first()
    if not s:
        return None
    refs = {"fairLiq": s.fair_liq, "netCashRatio": s.net_cash_ratio}
    if s.net_cash_calc:
        refs["netCashCalc"] = s.net_cash_calc
    for col_prefix, json_key in SCHOOL_KEYS.items():
        refs[json_key] = {
            "buy": getattr(s, f"buy_{col_prefix}"),
            "sellCons": getattr(s, f"sell_cons_{col_prefix}"),
            "sellFair": getattr(s, f"sell_fair_{col_prefix}"),
        }
    out = {
        "tradeDate": s.trade_date.isoformat(),
        "reportDate": s.report_date.isoformat(),
        "grahamAgg": s.score_graham_agg,
        "grahamDef": s.score_graham_def,
        "schloss": s.score_schloss,
        "buffett": s.score_buffett,
        "fraud": s.fraud,
        "mgmt": s.mgmt,
        "cycle": s.cycle,
        "cyclical": s.cyclical,
        "cycleTrend": s.cycle_trend,
        "priceRefs": refs,
    }
    if s.wind_fraud_delta is not None or s.wind_mgmt_delta is not None or s.wind_flags:
        # 优先透传原始覆盖层条目(含 st/penaltyCount/defendantLawsuit/instHold 等⑨总览字段)
        if s.wind_overlay:
            out["wind"] = s.wind_overlay
        else:
            out["wind"] = {
                "fraudDelta": s.wind_fraud_delta,
                "mgmtDelta": s.wind_mgmt_delta,
                "flags": s.wind_flags,
            }
    return out


def _load_events(db: Session, sid: int, sec_name: str) -> dict | None:
    """wind_event / wind_holder → 原 events/{code}.json 分组结构(detail 列即原始行)。"""
    events: dict[str, list] = {}
    fetched: datetime | None = None
    for etype, rows, fa in db.execute(
        select(WindEvent.etype, WindEvent.detail, WindEvent.fetched_at)
        .where(WindEvent.sid == sid).order_by(WindEvent.event_date)
    ):
        events.setdefault(EVENT_GROUPS.get(etype, etype), []).append(rows)
        if fa and (fetched is None or fa > fetched):
            fetched = fa
    holders: dict[str, list] = {}
    for htype, detail in db.execute(
        select(WindHolder.holder_type, WindHolder.detail).where(WindHolder.sid == sid).order_by(WindHolder.id)
    ):
        holders.setdefault(HOLDER_GROUPS.get(htype, htype), []).append(detail)
    if not events and not holders:
        return None
    # name 供 legacy 诉讼被告主体 substring 判断;fetched_at 供⑨"抓取于"脚注
    return {"name": sec_name,
            "fetched_at": fetched.isoformat(timespec="seconds") if fetched else None,
            "events": events, "holders": holders}


@router.get("/{code}")
def get_security_detail(code: str, db: Session = Depends(get_session)):
    """单证券详情:结构与原 companies/{code}.json 对齐 + scores/events 扩展。

    财务行由 核心列 + extras JSON 还原为中文键原样行(见 app/fin_columns.py)。
    """
    sec = db.execute(
        select(Security).where(Security.code == code).order_by(Security.sid)
    ).scalars().first()
    if not sec:
        raise HTTPException(status_code=404, detail=f"security {code} not found")

    quote = db.execute(
        select(QuoteDaily).where(QuoteDaily.sid == sec.sid).order_by(QuoteDaily.trade_date.desc()).limit(1)
    ).scalars().first()
    snapshot = None
    if quote:
        snapshot = {
            "name": sec.name,
            "price": _f(quote.price), "change_pct": _f(quote.change_pct),
            "pe_ttm": _f(quote.pe_ttm), "pb": _f(quote.pb),
            "market_cap": _f(quote.market_cap), "float_market_cap": _f(quote.float_market_cap),
            "turnover_rate": _f(quote.turnover_rate),
            "time": _dt(quote.fetched_at),
        }

    def fin_rows(model):
        rows = db.execute(
            select(model).where(model.sid == sec.sid).order_by(model.report_date.desc())
        ).scalars().all()
        return [restore_row(r) for r in rows]

    dividends = [
        {
            "year": r.div_year, "type": r.div_type, "description": r.description,
            "bonus_per_10": _f(r.bonus_per_10), "transfer_per_10": _f(r.transfer_per_10),
            "announce_date": _d(r.announce_date), "record_date": _d(r.record_date),
            "ex_date": _d(r.ex_date), "pay_date": _d(r.pay_date),
        }
        for r in db.execute(
            select(Dividend).where(Dividend.sid == sec.sid).order_by(Dividend.ex_date.desc())
        ).scalars()
    ]
    reports = [
        {
            "title": r.title, "category": r.category, "date": _d(r.report_date),
            "pdf_url": r.pdf_url, "detail_url": r.detail_url,
            "audit_firm": r.audit_firm, "audit_opinion": r.audit_opinion,
        }
        for r in db.execute(
            select(PeriodicReport).where(PeriodicReport.sid == sec.sid).order_by(PeriodicReport.report_date.desc())
        ).scalars()
    ]

    # 现金类构成附注（定期报告 PDF 解析）：键是报告期，形状与 companies/{code}.json 的
    # notes 一致，前端 priceReferences 直接读它算净现金/市值
    notes = {
        _d(r.report_date): {
            "termDeposit": _f(r.term_deposit),
            "restrictedCash": _f(r.restricted_cash),
            "source": r.source,
        }
        for r in db.execute(
            select(FinNote).where(FinNote.sid == sec.sid).order_by(FinNote.report_date)
        ).scalars()
        if r.report_date
    }

    # 估值分位（Wind 口径的近十年分位，每标的一行最新观测）：只作展示，不参与四派评分与
    # 参考价。单项可能为 NULL——亏损股的 PE 分位、外源序列停更都在采集侧置了空。列表页对
    # 陈旧行做了钳位（排序不能被半年前的分位污染），详情页不钳：这里把观测日一起给出去，
    # 新旧由看的人判断，而不是把值抹成 "-" 之后什么都不知道。
    vrow = db.execute(
        select(ValuationPctile).where(ValuationPctile.sid == sec.sid)
    ).scalar_one_or_none()
    valuation = None
    if vrow is not None:
        valuation = {
            "date": _d(vrow.trade_date), "source": "Wind",
            "window": "10y",
            "pe": {"pct": _f(vrow.pe_pctile), "days": vrow.pe_days},
            "pb": {"pct": _f(vrow.pb_pctile), "days": vrow.pb_days},
            "ps": {"pct": _f(vrow.ps_pctile), "days": vrow.ps_days},
        }

    return {
        "code": sec.code, "name": sec.name, "market": sec.market, "currency": sec.currency,
        "updated_at": _dt(sec.updated_at),
        "info": {"行业": sec.industry, "股票简称": sec.name, "上市日期": _d(sec.list_date)},
        "snapshot": snapshot,
        "indicators": fin_rows(FinIndicator),
        "income": fin_rows(FinIncome),
        "balance": fin_rows(FinBalance),
        "cashflow": fin_rows(FinCashflow),
        "dividends": dividends,
        "reports": reports,
        "notes": notes,
        "valuationPctile": valuation,
        "scores": _load_scores(db, sec.sid),
        "events": _load_events(db, sec.sid, sec.name),
    }
