# -*- coding: utf-8 -*-
"""社保及外资持仓接口(2026-11-18 P0,设计:docs/设计-社保及外资持仓模块.md)。

数据: inst_holding 一表(十大流通股东口径;采集 fetch_inst.py 当期+期后临时披露,
backfill_inst.py 冻结回填近 12 期),读时 join quote_daily/score_daily/security
取现价/推荐分/行业。缓存同 forecast:指纹(updated_at+行数+当期)一变即重建,
600s TTL,缓存序列化 bytes 而非 Response 对象(Response 不能跨请求复用)。

口径标注(页面横幅职责,本接口输出标注字段):
  - 前十大口径=进不了前十的持仓系统性缺席,不补全;
  - 季报口径滞后最多 3 个月,每个数字带 end_date/report_name as-of;
  - strategic 标签为近似口径:占流通比≥5% 且近 4 个标准期连续「不变」
    (占总股本比与完整持仓连续史表内没有,不硬算),前端可一键过滤。
"""
import json
import threading
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import and_, func, select, tuple_
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import InstHolding, QuoteDaily, ScoreDaily, Security

router = APIRouter(prefix="/api/inst", tags=["inst"])

_CACHE = {"key": None, "at": 0.0, "payload": None}
_LOCK = threading.Lock()
_TTL = 600.0

TYPES = ("社保", "QFII", "保险", "养老")

# 变动归一:源侧「新进/减持/不变」+ 增减数(采集端已拆 change_type 增持/减持)
_CHANGE_FILTERS = {"new": {"新进"}, "add": {"增持", "新进"},
                   "cut": {"减持"}, "flat": {"不变"}}

_SORT_COLS = {
    "mktcap": InstHolding.hold_mktcap, "holdnum": InstHolding.hold_num,
    "free_ratio": InstHolding.free_ratio, "qoq": InstHolding.free_ratio_qoq,
    "code": InstHolding.secu_code, "rank": InstHolding.holder_rank,
}


def _fingerprint(db: Session) -> tuple:
    row = db.execute(
        select(func.max(InstHolding.updated_at), func.count())
    ).one()
    period = db.execute(
        select(func.max(InstHolding.end_date)).where(InstHolding.is_std_period)
    ).scalar()
    return (str(row[0]), int(row[1] or 0), str(period))


def _std_periods(db: Session) -> list[str]:
    """全部标准报告期,降序。"""
    return [r[0] for r in db.execute(
        select(InstHolding.end_date).where(InstHolding.is_std_period)
        .group_by(InstHolding.end_date).order_by(InstHolding.end_date.desc())
    ).all()]


def _market_context(db: Session):
    """行情/评分最新交易日 join 映射。

    qmap[code]=(price,change_pct,market_cap);smap[code]=(recommend,gate);
    cmap[code]=(证券名,行业)——证券主表名优先于采集行 secu_name(评级口径名)。
    """
    td = db.execute(select(func.max(QuoteDaily.trade_date))).scalar_one()
    sd = db.execute(select(func.max(ScoreDaily.trade_date))).scalar_one()
    qmap, smap, cmap = {}, {}, {}
    if td is not None:
        for code, price, chg, mcap in db.execute(
            select(Security.code, QuoteDaily.price, QuoteDaily.change_pct,
                   QuoteDaily.market_cap)
            .join(QuoteDaily, and_(QuoteDaily.sid == Security.sid,
                                   QuoteDaily.trade_date == td))
            .where(Security.market == "A")
        ).all():
            qmap[code] = (float(price) if price is not None else None,
                          float(chg) if chg is not None else None,
                          float(mcap) if mcap is not None else None)
    if sd is not None:
        for code, rec, gate, name, ind in db.execute(
            select(Security.code, ScoreDaily.recommend, ScoreDaily.recommend_gate,
                   Security.name, Security.industry)
            .join(ScoreDaily, and_(ScoreDaily.sid == Security.sid,
                                   ScoreDaily.trade_date == sd))
            .where(Security.market == "A")
        ).all():
            smap[code] = (float(rec) if rec is not None else None, gate)
            cmap[code] = (name, ind)
    return td, sd, qmap, smap, cmap


_STRAT_CACHE: dict = {"key": None, "set": set(), "periods": []}


def _strategic_set(db: Session, periods: list[str], fp: tuple) -> set:
    """战略持股近似集合:近 4 个标准期全在列、全部「不变」且占流通比均值 ≥5%。

    占总股本比表内没有(只有占流通比),近似口径,tooltip 说明——宁可多标不漏标,
    过滤是给「想看交易性持仓」的人用的。按指纹缓存(数据不变不重算)。
    """
    if _STRAT_CACHE["key"] == fp and _STRAT_CACHE["periods"] == periods:
        return _STRAT_CACHE["set"]
    out = set()
    if len(periods) >= 4:
        win = periods[:4]
        rows = db.execute(
            select(InstHolding.holder_code, InstHolding.secu_code,
                   InstHolding.end_date, InstHolding.change_type,
                   InstHolding.free_ratio)
            .where(InstHolding.is_std_period, InstHolding.end_date.in_(win))
        ).all()
        acc = defaultdict(lambda: {"n": 0, "flat": 0, "fr": []})
        for hc, sc, d, ct, fr in rows:
            a = acc[(hc, sc)]
            a["n"] += 1
            if ct == "不变":
                a["flat"] += 1
            if fr is not None:
                a["fr"].append(float(fr))
        for key, a in acc.items():
            if a["n"] >= 4 and a["flat"] == a["n"] and a["fr"] and \
                    sum(a["fr"]) / len(a["fr"]) >= 5.0:
                out.add(key)
    _STRAT_CACHE.update(key=fp, set=out, periods=periods)
    return out


def _build(db: Session) -> dict:
    fp = _fingerprint(db)
    periods = _std_periods(db)
    period = periods[0] if periods else None
    td, sd, qmap, smap, cmap = _market_context(db)
    strategic = _strategic_set(db, periods, fp)

    report_name = None
    updated_at = None
    if period:
        row = db.execute(
            select(InstHolding.report_name, func.max(InstHolding.updated_at))
            .where(InstHolding.end_date == period, InstHolding.is_std_period)
            .group_by(InstHolding.report_name)
            .order_by(func.count().desc()).limit(1)
        ).first()
        report_name = row[0] if row else None
    updated_at = db.execute(select(func.max(InstHolding.updated_at))).scalar()

    # 当期各类型汇总
    types_out = {}
    adhoc_count = 0
    agg = defaultdict(lambda: {"rows": 0, "stocks": set(), "holders": set(),
                               "mktcap": 0.0, "new": 0, "add": 0, "cut": 0,
                               "flat": 0, "adhoc": 0})
    cur = []
    if period:
        cur = db.execute(
            select(InstHolding.holder_type, InstHolding.is_std_period,
                   InstHolding.change_type, InstHolding.hold_mktcap,
                   InstHolding.secu_code, InstHolding.holder_code)
            .where(InstHolding.end_date >= period)
        ).all()
    for ht, is_std, ct, mcap, sc, hc in cur:
        a = agg[ht]
        if not is_std:
            a["adhoc"] += 1
            adhoc_count += 1
            continue
        a["rows"] += 1
        a["stocks"].add(sc)
        a["holders"].add(hc)
        a["mktcap"] += float(mcap or 0)
        if ct == "新进":
            a["new"] += 1
        elif ct == "增持":
            a["add"] += 1
        elif ct == "减持":
            a["cut"] += 1
        elif ct == "不变":
            a["flat"] += 1
    for t in TYPES:
        a = agg.get(t)
        types_out[t] = {
            "rows": a["rows"], "stocks": len(a["stocks"]),
            "holders": len(a["holders"]),
            "mktcap_yi": round(a["mktcap"] / 1e8, 1),
            "new": a["new"], "add": a["add"], "cut": a["cut"], "flat": a["flat"],
            "adhoc": a["adhoc"],
        } if a else {"rows": 0, "stocks": 0, "holders": 0, "mktcap_yi": 0.0,
                     "new": 0, "add": 0, "cut": 0, "flat": 0, "adhoc": 0}

    # 抱团榜:当期 ≥3 个社保组合同持一只,按组合数/合计市值排
    ssf = defaultdict(list)
    if period:
        for hc, sc, hn, mcap in db.execute(
            select(InstHolding.holder_code, InstHolding.secu_code,
                   InstHolding.holder_name, InstHolding.hold_mktcap)
            .where(InstHolding.end_date == period, InstHolding.is_std_period,
                   InstHolding.holder_type == "社保")
        ).all():
            ssf[sc].append((hc, hn, float(mcap or 0)))
    cluster = []
    for sc, combos in ssf.items():
        if len(combos) < 3:
            continue
        cluster.append({
            "secu_code": sc, "name": (cmap.get(sc) or (None,))[0],
            "combos": [{"code": c[0], "name": c[1],
                        "mktcap_yi": round(c[2] / 1e8, 2)} for c in combos],
            "combo_count": len(combos),
            "total_yi": round(sum(c[2] for c in combos) / 1e8, 1),
            "recommend": (smap.get(sc) or (None, None))[0],
        })
    cluster.sort(key=lambda x: (-x["combo_count"], -x["total_yi"]))

    return {
        "period": period, "report_name": report_name,
        "updated_at": str(updated_at) if updated_at else None,
        "quote_date": td.isoformat() if td else None,
        "score_date": sd.isoformat() if sd else None,
        "types": types_out, "adhoc_count": adhoc_count,
        "cluster": cluster[:10],
        "periods": [{"period": p, "rows": n} for p, n in db.execute(
            select(InstHolding.end_date, func.count())
            .where(InstHolding.is_std_period)
            .group_by(InstHolding.end_date)
            .order_by(InstHolding.end_date.desc())).all()],
    }


@router.get("")
def inst_overview(db: Session = Depends(get_session)):
    """总览:最新标准期各类型汇总+抱团榜+期列表。"""
    fp = _fingerprint(db)
    with _LOCK:
        if (_CACHE["payload"] is not None and _CACHE["key"] == fp
                and time.time() - _CACHE["at"] < _TTL):
            return Response(content=_CACHE["payload"], media_type="application/json")
    body = json.dumps(_build(db), ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    with _LOCK:
        _CACHE.update(key=fp, at=time.time(), payload=body)
    return Response(content=body, media_type="application/json")


@router.get("/holding")
def inst_holding(
    type: str = Query("社保", alias="type"),
    period: str | None = None,
    change: str = Query("all", pattern="^(all|new|add|cut|flat)$"),
    q: str | None = None,
    exclude_strategic: bool = False,
    sort: str = Query("mktcap", pattern="^(mktcap|holdnum|free_ratio|qoq|code|rank)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=10, le=200),
    db: Session = Depends(get_session),
):
    """当期明细表:类型/变动过滤+检索+排序+分页,join 现价/推荐分/战略持股标签。"""
    periods = _std_periods(db)
    period = period if period in periods else (periods[0] if periods else None)
    if period is None:
        return {"period": None, "total": 0, "page": page, "size": size, "rows": []}
    if type not in TYPES:
        raise HTTPException(400, f"type 须为 {'/'.join(TYPES)}")

    td, sd, qmap, smap, cmap = _market_context(db)
    strategic = _strategic_set(db, periods, _fingerprint(db))

    conds = [InstHolding.end_date == period, InstHolding.holder_type == type]
    # 明细表默认只出标准期行;临时披露走 /stock 单股视图(设计:总览与趋势只统计标准期)
    conds.append(InstHolding.is_std_period)
    allowed = _CHANGE_FILTERS.get(change)
    if allowed:
        conds.append(InstHolding.change_type.in_(allowed))
    if q:
        qlike = f"%{q.strip()}%"
        conds.append((InstHolding.secu_code.like(qlike)
                      | InstHolding.secu_name.like(qlike)
                      | InstHolding.holder_name.like(qlike)))
    fp = _fingerprint(db)
    if exclude_strategic:
        strat = _strategic_set(db, periods, fp)
        if strat:
            # 行构造器 NOT IN 进 SQL:total 与分页同口径,不会出现滤后短页
            conds.append(tuple_(InstHolding.holder_code, InstHolding.secu_code)
                         .not_in(strat))
    base = select(InstHolding).where(and_(*conds))
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    col = _SORT_COLS[sort]
    direction = col.desc() if order == "desc" else col.asc()
    rows = db.execute(
        base.order_by(direction, InstHolding.holder_code)
        .offset((page - 1) * size).limit(size)
    ).scalars().all()

    out = []
    for r in rows:
        price, chg, mcap = qmap.get(r.secu_code, (None, None, None))
        rec, gate = smap.get(r.secu_code, (None, None))
        out.append({
            "holder_code": r.holder_code, "holder_name": r.holder_name,
            "holder_type": r.holder_type, "holder_type_sub": r.holder_type_sub,
            "secu_code": r.secu_code,
            "name": (cmap.get(r.secu_code) or (None, None))[0] or r.secu_name,
            "end_date": r.end_date, "report_name": r.report_name,
            "hold_num": r.hold_num,
            "free_ratio": r.free_ratio, "free_ratio_qoq": r.free_ratio_qoq,
            "change_type": r.change_type, "change_num": r.change_num,
            "hold_mktcap_yi": round(r.hold_mktcap / 1e8, 2) if r.hold_mktcap else None,
            "holder_rank": r.holder_rank,
            "price": price, "change_pct": chg,
            "market_cap_yi": round(mcap / 1e8, 1) if mcap else None,
            "recommend": rec, "recommend_gate": gate,
            "industry": (cmap.get(r.secu_code) or (None, None))[1],
            "strategic": (r.holder_code, r.secu_code) in strategic,
        })
    return {"period": period, "total": total, "page": page, "size": size,
            "rows": out, "quote_date": td.isoformat() if td else None,
            "score_date": sd.isoformat() if sd else None}


@router.get("/stock/{code}")
def inst_stock(code: str, db: Session = Depends(get_session)):
    """单股视图:该股全部类型×全部期的机构持仓史(含临时披露),附现价与推荐分。"""
    sec = db.execute(
        select(Security).where(Security.code == code, Security.market == "A")
    ).scalar_one_or_none()
    if sec is None and not code.isdigit():
        raise HTTPException(404, f"未知代码 {code}")
    td, sd, qmap, smap, cmap = _market_context(db)
    rows = db.execute(
        select(InstHolding).where(InstHolding.secu_code == code)
        .order_by(InstHolding.end_date.desc(), InstHolding.holder_type,
                  InstHolding.hold_mktcap.desc())
    ).scalars().all()
    price, chg, mcap = qmap.get(code, (None, None, None))
    rec, gate = smap.get(code, (None, None))
    # 每期各类型合计(前端画堆叠/趋势)
    by_period = defaultdict(lambda: defaultdict(lambda: [0.0, 0]))
    for r in rows:
        if r.is_std_period and r.hold_mktcap:
            cell = by_period[r.end_date][r.holder_type]
            cell[0] += float(r.hold_mktcap)
            cell[1] += 1
    periods_out = [
        {"period": p, "types": {t: {"mktcap_yi": round(v[0] / 1e8, 2),
                                    "holders": v[1]}
                                for t, v in tp.items()}}
        for p, tp in sorted(by_period.items(), reverse=True)
    ]
    return {
        "secu_code": code,
        "name": (cmap.get(code) or (None, None))[0] or (sec.name if sec else None),
        "industry": (cmap.get(code) or (None, None))[1] or (sec.industry if sec else None),
        "price": price, "change_pct": chg,
        "market_cap_yi": round(mcap / 1e8, 1) if mcap else None,
        "recommend": rec, "recommend_gate": gate,
        "rows": [{
            "end_date": r.end_date, "report_name": r.report_name,
            "is_std_period": r.is_std_period,
            "holder_type": r.holder_type, "holder_type_sub": r.holder_type_sub,
            "holder_code": r.holder_code, "holder_name": r.holder_name,
            "hold_num": r.hold_num, "free_ratio": r.free_ratio,
            "free_ratio_qoq": r.free_ratio_qoq,
            "change_type": r.change_type, "change_num": r.change_num,
            "hold_mktcap_yi": round(r.hold_mktcap / 1e8, 2) if r.hold_mktcap else None,
            "holder_rank": r.holder_rank,
        } for r in rows],
        "by_period": periods_out,
        "quote_date": td.isoformat() if td else None,
        "score_date": sd.isoformat() if sd else None,
    }


@router.get("/org/{holder_code}")
def inst_org(holder_code: str, db: Session = Depends(get_session)):
    """组合视图:单一社保组合/QFII 的当期全持仓+逐期合计(跟随调仓)。"""
    name_row = db.execute(
        select(InstHolding.holder_name, InstHolding.holder_type)
        .where(InstHolding.holder_code == holder_code)
        .limit(1)
    ).first()
    if name_row is None:
        raise HTTPException(404, f"未知组合 {holder_code}")
    periods = _std_periods(db)
    period = periods[0] if periods else None
    td, sd, qmap, smap, cmap = _market_context(db)
    cur = []
    if period:
        for r in db.execute(
            select(InstHolding).where(
                InstHolding.holder_code == holder_code,
                InstHolding.end_date == period, InstHolding.is_std_period)
            .order_by(InstHolding.hold_mktcap.desc())
        ).scalars().all():
            price, chg, mcap = qmap.get(r.secu_code, (None, None, None))
            rec, gate = smap.get(r.secu_code, (None, None))
            cur.append({
                "secu_code": r.secu_code,
                "name": (cmap.get(r.secu_code) or (None, None))[0] or r.secu_name,
                "industry": (cmap.get(r.secu_code) or (None, None))[1],
                "hold_num": r.hold_num, "free_ratio": r.free_ratio,
                "free_ratio_qoq": r.free_ratio_qoq,
                "change_type": r.change_type, "change_num": r.change_num,
                "hold_mktcap_yi": round(r.hold_mktcap / 1e8, 2) if r.hold_mktcap else None,
                "price": price, "change_pct": chg,
                "market_cap_yi": round(mcap / 1e8, 1) if mcap else None,
                "recommend": rec, "recommend_gate": gate,
            })
    hist = [{"period": p, "mktcap_yi": round((mv or 0) / 1e8, 1), "stocks": n}
            for p, mv, n in db.execute(
                select(InstHolding.end_date, func.sum(InstHolding.hold_mktcap),
                       func.count(func.distinct(InstHolding.secu_code)))
                .where(InstHolding.holder_code == holder_code,
                       InstHolding.is_std_period)
                .group_by(InstHolding.end_date)
                .order_by(InstHolding.end_date.desc())).all()]
    return {"holder_code": holder_code, "holder_name": name_row[0],
            "holder_type": name_row[1], "period": period,
            "quote_date": td.isoformat() if td else None,
            "score_date": sd.isoformat() if sd else None,
            "rows": cur, "history": hist}
