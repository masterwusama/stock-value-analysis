# -*- coding: utf-8 -*-
"""证券接口:分页列表 + 详情(响应结构与原 companies/*.json 对齐,降低前端移植成本)。"""
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, case, func, not_, or_, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.fin_columns import restore_row
from app.models import (
    Dividend,
    FinBalance,
    FinCashflow,
    FinIncome,
    FinIndicator,
    PeriodicReport,
    QuoteDaily,
    ScoreDaily,
    Security,
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
    # 价格参考列(原表头可排序)
    "price": QuoteDaily.price,
    "fair_liq": ScoreDaily.fair_liq,
    "net_cash_ratio": ScoreDaily.net_cash_ratio,
}
# 四派参考价三档(买入/保守卖/公允卖)：列头按买入性价比、格内保守/公允小字按各自卖价。
# 买价本身是"每家自己的"绝对值，跨标的比大小没有意义(5 元的票不比 50 元的便宜)，
# 故 buy_* 排的是折价率 1 - 现价/买价，降序 = 相对买入价打得最深的第一屏。
_SCHOOLS = ("graham_agg", "graham_def", "schloss", "buffett")
# 低于一分钱的买价不算参考价：评分公式相减会留下 1e-17 这种浮点零渣，
# 当分母能把折价率吹到 1e20 量级；而一分以下的价格没有任何标的真能买入。
MIN_BUY_REF = 0.01


def _buy_discount(buy_col):
    """买入性价比排序表达式：越大越便宜；算不出就不给值(NULL→沉底)

    买价低于 MIN_BUY_REF 时不给值而不是让它参与除法——负的买价会把比率翻成负数，
    把根本不该买入的标的顶到"最便宜"那一端，近零的买价则在另一端造出天文数字。
    现价缺失同理(无法定位折价深度)。
    """
    return case(
        (and_(QuoteDaily.price.is_not(None), buy_col.is_not(None), buy_col >= MIN_BUY_REF),
         1 - QuoteDaily.price / buy_col),
        else_=None,
    )


SORT_COLS.update({f"buy_{s}": _buy_discount(getattr(ScoreDaily, f"buy_{s}")) for s in _SCHOOLS})
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


def _flt_keys(raw: str | None, valid: dict, name: str) -> list[str]:
    """逗号分隔复选键解析 + 白名单校验(非法直接 400,不做静默丢弃)。"""
    if not raw:
        return []
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    bad = [k for k in keys if k not in valid]
    if bad:
        raise HTTPException(status_code=400, detail=f"invalid {name}: {','.join(bad)}")
    return keys


# 全市场 5500 只 × 每日快照：quote_daily/score_daily 一年即百万行，
# 裸写 GROUP BY sid + MAX(trade_date) 会走全索引扫(实测 type=index,代价随行数线性)。
# 行情/评分整批按交易日落盘，各证券最新一行必落在最近 RECENT_DATES 个交易日内
# （美股/港股收盘滞后数日、周末补跑同样覆盖），故限定窗口后再取 max，代价与表总量无关。
RECENT_DATES = 15


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
    keyword: str | None = Query(None, max_length=32, description="代码/名称模糊匹配"),
    industry: str | None = Query(None, max_length=64),
    fraud_max: float | None = Query(None, ge=0, le=100, description="造假风险≤(wind=1 时按增强分)"),
    mgmt_min: float | None = Query(None, ge=0, le=100, description="管理能力≥(wind=1 时按增强分)"),
    cap_min: float | None = Query(None, ge=0, description="总市值≥(本币元,与响应 market_cap 同单位;港股/美股是 HKD/USD)"),
    cap_max: float | None = Query(None, ge=0, description="总市值≤(本币元,与响应 market_cap 同单位;港股/美股是 HKD/USD)"),
    wind: bool = Query(False, description="事件增强分档：造假/管理两列的筛选与排序改用基础分+Wind 事件增量（响应里两列仍为基础分，显示值由前端叠 wind_* 字段换算）"),
    buys: str | None = Query(None, max_length=64, description="买点复选(逗号分隔,同时满足)"),
    sells: str | None = Query(None, max_length=64, description="卖点复选(现价≥公允卖价即命中,公允恒高于保守)"),
    discount: float | None = Query(None, gt=0, le=500, description="买点折扣%,仅与 buys 配合"),
    sort: str = Query("code", description=f"排序字段: {'/'.join(SORT_COLS)}（buy_* 是折价率 1-现价/买价，降序=相对买入价折得最深）"),
    order: Literal["asc", "desc"] = Query("asc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_session),
):
    """证券分页列表:join 各自最新一行行情与评分。

    逐证券 max(trade_date)(对齐原 index.json"各市场各取最近收盘"语义:
    美股在北京时间白天落后一天时仍显示昨收,不被全局快照日剔除);
    筛选语义对齐原前端 passFlt:依赖的价格/参考价缺失(SQL NULL)自动排除。
    """
    latest_quote = _latest_sub(db, QuoteDaily, "qdate")
    latest_score = _latest_sub(db, ScoreDaily, "sdate")
    # 造假/管理两列的口径跟着 wind 档切（旧内嵌页的“排序/筛选跟随”）：无事件数据公司在
    # Wind 档下表达式出 NULL，既排到末尾也被 fraud_max/mgmt_min 自动排除，与列页显示“-”一致
    fraud_col = _wind_score(ScoreDaily.fraud, ScoreDaily.wind_fraud_delta) if wind else ScoreDaily.fraud
    mgmt_col = _wind_score(ScoreDaily.mgmt, ScoreDaily.wind_mgmt_delta) if wind else ScoreDaily.mgmt

    q = (
        select(Security, QuoteDaily, ScoreDaily)
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
    )

    conds = []
    if market:
        conds.append(Security.market == market)
    if board:
        prefixes = BOARDS[board]
        conds.append(Security.market == "A")
        conds.append(or_(*[Security.code.startswith(p) for p in prefixes]))
    if st is not None:
        is_st = func.upper(Security.name).like("%ST%")
        conds.append(is_st if st else not_(is_st))
    if industry:
        conds.append(Security.industry == industry)
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
            wind_fraud_delta=_f(score.wind_fraud_delta) if score else None,
            wind_mgmt_delta=_f(score.wind_mgmt_delta) if score else None,
            wind_flags=score.wind_flags if score else None,
            wind_hit=bool(score is not None and score.wind_overlay is not None),
            fair_liq=_f(score.fair_liq) if score else None,
            net_cash_ratio=_f(score.net_cash_ratio) if score else None,
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
        for sec, quote, score in rows
    ]
    return SecurityListOut(total=total, page=page, page_size=page_size,
                           # 展示用全局最新评分日(行内数据已逐证券取各自最新)
                           trade_date=db.execute(
                               select(func.max(ScoreDaily.trade_date))).scalar_one(),
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
        "scores": _load_scores(db, sec.sid),
        "events": _load_events(db, sec.sid, sec.name),
    }
