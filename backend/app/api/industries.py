# -*- coding: utf-8 -*-
"""行业聚合视角:证监会大类的横截面水位表(设计见 docs/设计-行业聚合视角.md)。

零 DDL、读时聚合——单日切片(全 A ≈5,500 行)拉进 Python 算中位数,不在 SQL 里玩
median;进程内缓存 60 秒(同日重复请求零成本)。

口径要点(前端 tooltip 与 meta 同步写死,防误读):
- PB/PE 中位只取**正值**(亏损股混入会把 PE 中位拉成负数);
- 买点密度分母 = 该派**有买点锚**的家数(buy_* 非空),不是行业总数——成长股多数派
  无资产/盈利锚,buy 为 NULL,拿 n 当分母会把密度系统性压低;
- PB 分位 join 的是 valuation_pctile 每股一行的最新观测(2026-10 起免费源,wind 时代
  港股与部分 A 股停更,行自陈旧),响应 meta 里带数据日期。
"""
import time
from datetime import date as date_type
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import QuoteDaily, ScoreDaily, Security, ValuationPctile

router = APIRouter(prefix="/api/industries", tags=["industries"])

SCHOOLS = ("graham_agg", "graham_def", "schloss", "buffett")
CACHE_TTL = 60.0
_cache: dict[tuple, tuple[float, dict]] = {}

ANCHOR_NOTE = (
    "PB/PE 中位仅计正值;买点密度分母=该派有买点参考价的家数(无锚者不计);"
    "PB 分位为每股一行最新观测,数据日期见 meta.pb_pctile_date。"
)


def _median(vals):
    vs = sorted(v for v in vals if v is not None)
    if not vs:
        return None
    n = len(vs)
    mid = n // 2
    return vs[mid] if n % 2 else (vs[mid - 1] + vs[mid]) / 2


def _r2(v):
    return round(v, 2) if v is not None else None


def _latest_date(db: Session) -> date_type | None:
    """两表都有行的最新交易日(score/quote 的 trade_date 会错一天——假日快照与行情
    落库时点不同步,单看 score 会取到无行情的孤立日,内连接直接清零)。"""
    return db.execute(
        select(ScoreDaily.trade_date)
        .join(QuoteDaily, (QuoteDaily.sid == ScoreDaily.sid)
              & (QuoteDaily.trade_date == ScoreDaily.trade_date))
        .order_by(ScoreDaily.trade_date.desc()).limit(1)
    ).scalar_one_or_none()


def _pb_pctile_date(db: Session) -> date_type | None:
    return db.execute(
        select(ValuationPctile.trade_date).order_by(ValuationPctile.trade_date.desc()).limit(1)
    ).scalar_one_or_none()


def _overview(db: Session, day: date_type) -> dict:
    rows = db.execute(
        select(Security.industry,
               QuoteDaily.price, QuoteDaily.pb, QuoteDaily.pe_ttm,
               ScoreDaily.buy_graham_agg, ScoreDaily.buy_graham_def,
               ScoreDaily.buy_schloss, ScoreDaily.buy_buffett,
               ScoreDaily.gate, ScoreDaily.st_risk,
               ScoreDaily.fraud, ScoreDaily.mgmt, ScoreDaily.cycle, ScoreDaily.cyclical,
               ScoreDaily.div_yield,
               ValuationPctile.pb_pctile)
        .join(ScoreDaily, ScoreDaily.sid == Security.sid)
        .join(QuoteDaily, (QuoteDaily.sid == Security.sid)
              & (QuoteDaily.trade_date == ScoreDaily.trade_date))
        .outerjoin(ValuationPctile, ValuationPctile.sid == Security.sid)
        .where(Security.market == "A", ScoreDaily.trade_date == day)
    ).all()

    by_ind: dict[str, list] = {}
    for r in rows:
        ind = r[0] or "未知"
        by_ind.setdefault(ind, []).append(r)

    out = []
    for ind, rs in by_ind.items():
        # 行按属性名取(索引取列在 16 列上错一格就是静默串列,此前的实现真踩过)
        buy = {}
        for sch in SCHOOLS:
            anchor = hit = 0
            for r in rs:
                b = getattr(r, f"buy_{sch}")
                if b is not None:
                    anchor += 1
                    px = r.price
                    if px is not None and float(px) <= b:
                        hit += 1
            buy[sch] = {"hit": hit, "n_anchor": anchor}
        gate_pass = sum(1 for r in rs if r.gate is True)
        st_high = sum(1 for r in rs if (r.st_risk or 0) > 0)
        cyc = [r.cycle for r in rs if r.cyclical and r.cycle is not None]
        out.append({
            "industry": ind, "n": len(rs),
            "med_pb": _r2(_median([r.pb for r in rs if r.pb is not None and r.pb > 0])),
            "med_pe": _r2(_median([r.pe_ttm for r in rs if r.pe_ttm is not None and r.pe_ttm > 0])),
            "med_pb_pct": _r2(_median([r.pb_pctile for r in rs if r.pb_pctile is not None])),
            "buy": buy, "gate_pass": gate_pass, "st_high": st_high,
            "med_fraud": _r2(_median([r.fraud for r in rs])),
            "med_mgmt": _r2(_median([r.mgmt for r in rs])),
            "med_cycle": _r2(_median(cyc)),
            "med_div": _r2(_median([r.div_yield for r in rs])),
        })
    out.sort(key=lambda x: -x["n"])
    return {"rows": out, "meta": {
        "date": day.isoformat(), "n_stocks": len(rows), "anchor_note": ANCHOR_NOTE}}


@router.get("/overview")
def overview(
    market: str = Query("A", pattern="^A$",
                        description="v1 仅 A 股(证监会大类口径);HK 行业来自另一套采集字典,口径未验"),
    date: date_type | None = Query(None, description="回看某交易日(默认最新)"),
    db: Session = Depends(get_session),
):
    day = date or _latest_date(db)
    if day is None:
        raise HTTPException(404, "score_daily 无数据")
    # 回看限近 90 天:更早的分位行早已被覆盖,横截面会静默失真
    if date and day < datetime.now().date() - timedelta(days=90):
        raise HTTPException(422, "仅支持回看近 90 天")
    key = ("A", day)
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        payload = hit[1]
    else:
        payload = _overview(db, day)
        payload["meta"]["pb_pctile_date"] = str(_pb_pctile_date(db) or "")
        _cache.clear()          # 单键缓存:只留最新一个日期,防回看刷缓存
        _cache[key] = (now, payload)
    return payload
