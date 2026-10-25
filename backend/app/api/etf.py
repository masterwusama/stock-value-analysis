# -*- coding: utf-8 -*-
"""ETF 综合评估接口:读时合成、不落新表(与 pick 同一套取舍)。

数据全部来自 etf_* 六表(fetch_etf.py 采集 → import_legacy.import_etf 回灌):
    etf_info / etf_spot / etf_holding / etf_index_val / etf_index_close / etf_ann

综合评分 etf_score(0~100,可解释无黑箱):
    成份分 50 —— 持仓按权重加权推荐分 R(score_daily 最新交易日),覆盖不足按
                  覆盖率折算并披露;覆盖权重<80% 开始打折,一半封底。
    估值分 30 —— 跟踪指数 PE/PB 历史分位(自算序列,PE 0.6 / PB 0.4),越低越高;
                  样本<30 日不给分(权重重新归一并注明);季报前十口径打 8 折
                  (国证官方月度与招商申赎清单 PCF 均为全量,不打折)。
    股息分 20 —— 指数综合股息率阶梯(≥6% 满 20)。
    修正项 —— 溢价 >1% 每 pp 扣 3(封顶 6);折价 >1% 加 2(封顶 4);规模<2 亿扣 5。
分层阈值与掘金台一致:≥78 strong / ≥65 buy / ≥45 watch。
"""
import time
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import (
    EtfHolding,
    EtfIndexClose,
    EtfIndexVal,
    EtfInfo,
    EtfSpot,
    ScoreDaily,
    Security,
)

router = APIRouter(prefix="/api/etf", tags=["etf"])

STRONG_MIN, BUY_MIN, WATCH_MIN = 78.0, 65.0, 45.0
PCTILE_MIN_DAYS = 30          # 分位最少样本,不足则估值分缺省
COV_FULL = 0.8                # 成份覆盖率≥80% 不打折
DV_LADDER = ((6.0, 20.0), (5.0, 16.0), (4.0, 12.0), (3.0, 8.0), (2.0, 4.0))
W_CONST, W_VAL, W_DV = 50.0, 30.0, 20.0
TOP10_VAL_FACTOR = 0.8        # 前十口径的估值置信折扣

GATE_LABELS = {"audit_qualify": "审计非标", "risk_warning": "ST/风险警示",
               "case_filed": "证监会立案", "neg_equity": "资不抵债"}


# ---------------------------------------------------------------------------
# 装载(进程内缓存,键=各表最新日期)
# ---------------------------------------------------------------------------

_cache: dict = {"key": None, "at": 0.0, "payload": None}
_CACHE_TTL = 300.0


def _pct(vals: list[float], v: float | None):
    """与 fetch_valuation_em.percentile 同口径:正值样本,≤v 占比。"""
    if v is None or v <= 0:
        return None
    win = [x for x in vals if x is not None and x > 0]
    if not win:
        return None
    return min(100.0, max(0.0, round(sum(1 for x in win if x <= v) / len(win) * 100, 1))), len(win)


def _load(db: Session, fresh: bool = False):
    key = (
        db.execute(select(func.max(EtfSpot.trade_date))).scalar_one(),
        db.execute(select(func.max(EtfIndexVal.trade_date))).scalar_one(),
        db.execute(select(func.max(ScoreDaily.trade_date))).scalar_one(),
    )
    if (not fresh and _cache["payload"] is not None and _cache["key"] == key
            and time.time() - _cache["at"] < _CACHE_TTL):
        return _cache["payload"]

    infos = db.execute(select(EtfInfo).order_by(EtfInfo.code)).scalars().all()
    spots = {s.code: s for s in db.execute(select(EtfSpot)).scalars()}
    val_rows = db.execute(select(EtfIndexVal).order_by(EtfIndexVal.trade_date)).scalars().all()
    close_rows = db.execute(
        select(EtfIndexClose).order_by(EtfIndexClose.trade_date)).scalars().all()
    holds = db.execute(
        select(EtfHolding).order_by(EtfHolding.report_period.desc())).scalars().all()
    by_idx_val: dict[str, list] = {}
    for r in val_rows:
        by_idx_val.setdefault(r.index_key, []).append(r)
    by_idx_close: dict[str, list] = {}
    for r in close_rows:
        by_idx_close.setdefault(r.index_key, []).append(r)
    holds_by_etf: dict[str, list] = {}
    for h in holds:
        holds_by_etf.setdefault(h.code, []).append(h)

    # 成份股评分 join:score_daily 最新交易日 × security(code, market)
    td = key[2]
    score_rows = db.execute(
        select(Security.code, Security.market, Security.industry, ScoreDaily)
        .join(ScoreDaily, ScoreDaily.sid == Security.sid)
        .where(ScoreDaily.trade_date == td)
    ).all()
    smap = {(c, m): (ind, s) for c, m, ind, s in score_rows}

    out = {}
    for info in infos:
        out[info.code] = _synth_one(info, spots.get(info.code),
                                    by_idx_val.get(info.index_key) or [],
                                    by_idx_close.get(info.index_key) or [],
                                    holds_by_etf.get(info.code) or [], smap, td, key[0])
    payload = {"etfs": out, "score_date": td.isoformat() if td else None,
               "spot_date": key[0].isoformat() if key[0] else None}
    _cache.update(key=key, at=time.time(), payload=payload)
    return payload


def _tier_of_stock(s) -> str:
    """单只成份的分层:硬门槛与 ST 高危 → avoid;其余按 R 阈值。"""
    if s is None:
        return "na"
    if s.gate is True or s.st_risk == 2:
        return "avoid"
    if s.recommend is None:
        return "na"
    if s.recommend >= STRONG_MIN and (s.trap or 0) <= 20:
        return "strong"
    return "buy" if s.recommend >= BUY_MIN else (
        "watch" if s.recommend >= WATCH_MIN else "na")


def _synth_one(info, spot, val_series, close_series, holds, smap, score_td, spot_td):
    # ---- 持仓:official(国证官方月度) > pcf(申赎清单全量月度) > f10(季报前十) ----
    periods = sorted({h.report_period for h in holds}, reverse=True)
    official_periods = sorted({h.report_period for h in holds if h.source == "official"},
                              reverse=True)
    pcf_periods = sorted({h.report_period for h in holds if h.source == "pcf"},
                         reverse=True)
    if official_periods:
        basis_source, periods_use = "official", official_periods
    elif pcf_periods:
        basis_source, periods_use = "pcf", pcf_periods
    else:
        basis_source = "f10"
        periods_use = sorted({h.report_period for h in holds if h.source == "f10"},
                             reverse=True)
    latest = periods_use[0] if periods_use else None

    rows = [h for h in holds if h.report_period == latest] if latest else []
    rows.sort(key=lambda h: -(h.weight or 0))

    holdings_out, w_scored, w_r, w_all = [], 0.0, 0.0, 0.0
    tier_w = {"strong": 0.0, "buy": 0.0, "watch": 0.0, "na": 0.0, "avoid": 0.0}
    avoid_names, trap_names = [], []
    for h in rows:
        got = smap.get((h.stock_code, h.stock_market))
        ind, s = got if got else (None, None)
        tier = _tier_of_stock(s)
        w = h.weight or 0
        w_all += w
        tier_w[tier] = tier_w.get(tier, 0) + w
        rec = s.recommend if s else None
        if rec is not None:
            w_scored += w
            w_r += w * rec
        if tier == "avoid":
            avoid_names.append(h.stock_name or h.stock_code)
        if s is not None and (s.trap or 0) > 20:
            trap_names.append(h.stock_name or h.stock_code)
        holdings_out.append({
            "code": h.stock_code, "name": h.stock_name, "market": h.stock_market,
            "weight": h.weight, "industry": h.industry or ind,
            "recommend": rec, "value": s.value if s else None,
            "growth": s.growth if s else None,
            "fraud": s.fraud if s else None, "trap": s.trap if s else None,
            "div_yield": s.div_yield if s else None,
            "st_risk": s.st_risk if s else None,
            "gate_flags": [GATE_LABELS.get(f, f) for f in (s.gate_flags or [])] if s else [],
            "tier": tier,
        })
    cov = round(w_scored / w_all, 4) if w_all else 0.0
    r_avg = round(w_r / w_scored, 1) if w_scored else None

    # ---- 调仓:官方/PCF 月度相邻期 diff(不足两期回退 f10 季度 diff) ----
    changes = None
    ch_source = basis_source if basis_source in ("official", "pcf") else None
    ch_periods = periods_use if ch_source else sorted(
        {h.report_period for h in holds if h.source == "f10"}, reverse=True)
    if len(ch_periods) >= 2:
        a, b = ch_periods[0], ch_periods[1]
        cur = {h.stock_code: h for h in holds if h.report_period == a}
        prev = {h.stock_code: h for h in holds if h.report_period == b}
        changes = {
            "kind": {"official": "month", "pcf": "pcf"}.get(ch_source, "quarter"),
            "from": b, "to": a,
            "ins": [{"code": c, "name": cur[c].stock_name, "weight": cur[c].weight}
                    for c in cur if c not in prev],
            "outs": [{"code": c, "name": prev[c].stock_name, "weight": prev[c].weight}
                     for c in prev if c not in cur],
            "wchg": [{"code": c, "name": cur[c].stock_name, "from": prev[c].weight,
                      "to": cur[c].weight} for c in cur if c in prev
                     and (cur[c].weight or 0) != (prev[c].weight or 0)],
        }

    # ---- 估值序列与分位 ----
    val_out = {"basis": basis_source, "sample_days": len(val_series), "pe": None,
               "pb": None, "dv": None, "pctile": {}, "dates": [], "pe_series": [],
               "pb_series": [], "dv_series": [], "close_series": []}
    if val_series:
        pes = [r.pe for r in val_series]
        pbs = [r.pb for r in val_series]
        dvs = [r.dv for r in val_series]
        last = val_series[-1]
        val_out.update({
            "pe": last.pe, "pb": last.pb, "dv": last.dv,
            "dates": [r.trade_date.isoformat() for r in val_series],
            "pe_series": pes, "pb_series": pbs, "dv_series": dvs,
        })
        if len(val_series) >= PCTILE_MIN_DAYS:
            for name, arr, v in (("pe", pes, last.pe), ("pb", pbs, last.pb),
                                 ("dv", dvs, last.dv)):
                got = _pct(arr, v)
                if got:
                    val_out["pctile"][name] = {"pct": got[0], "days": got[1]}
    val_out["close_series"] = [[r.trade_date.isoformat(), r.close] for r in close_series]

    # ---- 综合评分 ----
    reasons, risks, parts = [], [], {}
    w_eff = W_CONST + W_VAL + W_DV
    if r_avg is not None:
        cov_factor = 0.5 + 0.5 * min(cov / COV_FULL, 1.0)
        parts["constituent"] = round(W_CONST * (r_avg / 100.0) * cov_factor, 1)
        reasons.append(f"成份分:加权推荐分 {r_avg}(覆盖率 {cov:.0%}"
                       f"{',覆盖不足已折算' if cov < COV_FULL else ''})")
    else:
        w_eff -= W_CONST
        risks.append("成份股无评分覆盖(评分库缺该市场标的)")
    if val_out["pctile"].get("pe") or val_out["pctile"].get("pb"):
        p_pe = val_out["pctile"].get("pe", {}).get("pct")
        p_pb = val_out["pctile"].get("pb", {}).get("pct")
        raw = 0.0
        if p_pe is not None:
            raw += 0.6 * (100 - p_pe)
        if p_pb is not None:
            raw += 0.4 * (100 - p_pb)
        factor = TOP10_VAL_FACTOR if basis_source == "f10" else 1.0
        parts["valuation"] = round(W_VAL * raw / 100.0 * factor, 1)
        reasons.append(f"估值分:PE 分位 {p_pe}/PB 分位 {p_pb}"
                       + ("(前十口径已打 8 折)" if factor < 1 else ""))
    elif val_series:
        w_eff -= W_VAL
        risks.append(f"估值分位样本不足({len(val_series)} 日<{PCTILE_MIN_DAYS}),"
                     "该项权重暂缺,总分按剩余权重归一")
    else:
        w_eff -= W_VAL
        risks.append("跟踪指数估值序列缺失")
    dv = val_out.get("dv")
    if dv is not None:
        dv_pts = next((p for th, p in DV_LADDER if dv >= th), round(max(0.0, dv * 2), 1))
        parts["dividend"] = round(dv_pts * W_DV / 20.0, 1)
        reasons.append(f"股息分:指数综合股息率 {dv:.2f}%")
    else:
        w_eff -= W_DV
        risks.append("综合股息率缺失")

    total = round(sum(parts.values()) / w_eff * 100.0, 1) if w_eff > 0 and parts else None
    adj = 0.0
    if spot and spot.premium_pct is not None:
        if spot.premium_pct > 1:
            adj -= min(6.0, (spot.premium_pct - 1) * 3)
            risks.append(f"场内溢价 {spot.premium_pct:.2f}%")
        elif spot.premium_pct < -1:
            adj += min(4.0, (-spot.premium_pct - 1))
            reasons.append(f"场内折价 {abs(spot.premium_pct):.2f}%")
    if spot and spot.scale_yi is not None and spot.scale_yi < 2:
        adj -= 5.0
        risks.append(f"规模偏小({spot.scale_yi:.2f} 亿)")
    if avoid_names:
        risks.append("回避档成份:" + "、".join(avoid_names[:4])
                     + ("…" if len(avoid_names) > 4 else ""))
    if trap_names:
        risks.append("陷阱标注成份:" + "、".join(trap_names[:4])
                     + ("…" if len(trap_names) > 4 else ""))
    if total is not None:
        total = round(max(0.0, min(100.0, total + adj)), 1)
    tier = ("strong" if total is not None and total >= STRONG_MIN else
            "buy" if total is not None and total >= BUY_MIN else
            "watch" if total is not None and total >= WATCH_MIN else "na")

    return {
        "code": info.code, "name": info.name, "full_name": info.full_name,
        "market": info.market, "index_key": info.index_key, "index_name": info.index_name,
        "index_family": info.index_family,
        "fees": {"mgmt": info.mgmt_fee, "custody": info.custody_fee},
        "inception": info.inception.isoformat() if info.inception else None,
        "list_date": info.list_date.isoformat() if info.list_date else None,
        "updated_at": info.updated_at.isoformat() if info.updated_at else None,
        "spot": {
            "date": spot_td.isoformat() if spot_td else None,
            "price": float(spot.price) if spot and spot.price else None,
            "change_pct": spot.change_pct if spot else None,
            "scale_yi": float(spot.scale_yi) if spot and spot.scale_yi else None,
            "nav": float(spot.nav) if spot and spot.nav else None,
            "nav_date": spot.nav_date.isoformat() if spot and spot.nav_date else None,
            "premium_pct": spot.premium_pct if spot else None,
            "week52_high": float(spot.week52_high) if spot and spot.week52_high else None,
            "week52_low": float(spot.week52_low) if spot and spot.week52_low else None,
        },
        "score": {"total": total, "tier": tier, "parts": parts,
                  "adjust": round(adj, 1) if adj else 0.0,
                  "reasons": reasons, "risks": risks},
        "holdings": {
            "source": basis_source, "period": latest, "count": len(rows),
            "weight_covered": round(w_all, 4), "score_coverage": cov,
            "r_weighted": r_avg, "tier_weight": {k: round(v, 4) for k, v in tier_w.items()},
            "rows": holdings_out,
        },
        "changes": changes,
        "valuation": val_out,
    }


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------

class EtfSummary(BaseModel):
    code: str
    name: str
    market: str | None = None
    index_name: str | None = None
    price: float | None = None
    change_pct: float | None = None
    scale_yi: float | None = None
    premium_pct: float | None = None
    dv: float | None = None
    pe: float | None = None
    pe_pctile: float | None = None
    pb_pctile: float | None = None
    sample_days: int | None = None
    score: float | None = None
    tier: str = "na"


@router.get("/list")
def etf_list(db: Session = Depends(get_session)):
    data = _load(db)
    out = []
    for code, d in data["etfs"].items():
        v = d["valuation"]
        out.append(EtfSummary(
            code=code, name=d["name"], market=d["market"], index_name=d["index_name"],
            price=d["spot"]["price"], change_pct=d["spot"]["change_pct"],
            scale_yi=d["spot"]["scale_yi"], premium_pct=d["spot"]["premium_pct"],
            dv=v.get("dv"), pe=v.get("pe"),
            pe_pctile=(v.get("pctile") or {}).get("pe", {}).get("pct"),
            pb_pctile=(v.get("pctile") or {}).get("pb", {}).get("pct"),
            sample_days=v.get("sample_days"),
            score=d["score"]["total"], tier=d["score"]["tier"],
        ))
    return {"score_date": data["score_date"], "items": out}


@router.get("/detail")
def etf_detail(code: str, db: Session = Depends(get_session)):
    data = _load(db)
    if code not in data["etfs"]:
        raise HTTPException(404, f"ETF {code} 不在采集名单")
    return {"score_date": data["score_date"], "spot_date": data["spot_date"],
            **data["etfs"][code]}
