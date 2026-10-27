# -*- coding: utf-8 -*-
"""业绩预测模块 API(首批标的:紫金矿业/广信股份)。

数据分工:价格序列引用 cmd_*(commodity 各商品组+forecast 组)与 agro_*(农化全链);
标的专属数据(分部量价成本/指引/观察哨)在 fc_target.payload(targets.json 人工结构化,
每季随公告更新)。本接口只做读时合成,改假设不碰采集:

    - 分部行: 最新季量 + QTD/上季均价与环比 + 单位成本 + 价差(价-成本) + ±10% 敏感度
    - 指引完成度: Q1-3 量 / 年指引
    - 敏感度换算: Δ毛利(年化,亿) = Δ价 × 量 × 单位换算 / 1e8,权益默认 100%
      (卡莫阿权益法/巨龙并表等少数股东差异单列 equity_notes,不暗中折)
    - 广信现金池线: 类现金 × 收益率参照(10Y),不做绝对利润预测(thesis 声明)

免责定位: 研究工具非投资建议;量/成本为披露滞后数据,价格为库内最新——
两者时点差是本模块误差的主来源,页面对每个数字带 as-of。
"""
import datetime as dt
from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import AgroPrice, CmdValue, FcTarget

router = APIRouter(prefix="/api/forecast", tags=["forecast"])


def _quarter_of(d: dt.date) -> str:
    return "%dQ%d" % (d.year, (d.month - 1) // 3 + 1)


def _qtd_stats(points):
    """[(date_iso, val)] → {quarter, qtd, prev_q, qoq, latest, latest_date}。

    按自然季分桶:最新非空季均值=QTD,前一季=prev_q。
    """
    if not points:
        return {}
    buckets = defaultdict(list)
    for d, v in points:
        if v is not None:
            buckets[_quarter_of(dt.date.fromisoformat(d))].append(v)
    q_order = sorted(buckets)
    if not q_order:
        return {}
    last_q = q_order[-1]
    qtd = sum(buckets[last_q]) / len(buckets[last_q])
    prev_q = (sum(buckets[q_order[-2]]) / len(buckets[q_order[-2]])
              if len(q_order) >= 2 else None)
    return {
        "quarter": last_q, "qtd": round(qtd, 2),
        "prev_q": round(prev_q, 2) if prev_q is not None else None,
        "qoq": round((qtd / prev_q - 1) * 100, 2) if prev_q else None,
        "latest": points[-1][1], "latest_date": points[-1][0],
    }


def _load_cmd_series(db, codes):
    pts = defaultdict(list)
    if not codes:
        return pts
    rows = db.execute(
        select(CmdValue.cmd_code, CmdValue.data_date, CmdValue.val)
        .where(CmdValue.cmd_code.in_(codes)).order_by(CmdValue.data_date)
    ).all()
    for code, d, v in rows:
        pts[code].append([d.isoformat(), float(v) if v is not None else None])
    return pts


def _load_agro_series(db, product_ids):
    """agro_price 同日多条(多交易商)先按日中位数再返回序列。"""
    out = defaultdict(list)
    if not product_ids:
        return out
    by_day = defaultdict(lambda: defaultdict(list))
    rows = db.execute(
        select(AgroPrice.product_id, AgroPrice.price_date, AgroPrice.price)
        .where(AgroPrice.product_id.in_(product_ids)).order_by(AgroPrice.price_date)
    ).all()
    for pid, d, v in rows:
        by_day[pid][d.isoformat()].append(float(v))
    for pid, days in by_day.items():
        vals = sorted((sum(vs) / len(vs), d) for d, vs in days.items())
        out[pid] = [[d, round(v, 2)] for v, d in vals]
    return out


# 单位换算: 价×量 → 元(毛利变动),再 /1e8 → 亿
_UNIT_MULT = {
    ("元/克", "吨"): 1e6, ("元/吨", "万吨"): 1e4, ("元/吨", "吨"): 1.0,
    ("元/千克", "吨"): 1e3, ("元/吨", "万吨(当量)"): 1e4,
}


def _sensitivity(price_unit, volume_unit, annual_vol, price, pct=0.1):
    mult = _UNIT_MULT.get((price_unit, volume_unit))
    if mult is None or not annual_vol or not price:
        return None
    return round(price * pct * annual_vol * mult / 1e8, 2)


def _ann_vol(segments_volume, guidance_qty, latest_q_frac=0.75):
    """年化量估计:有指引用指引;否则最新季量年化(粗)。"""
    if guidance_qty:
        return guidance_qty
    if segments_volume:
        q = segments_volume[-1].get("qty")
        if q:
            return q / latest_q_frac
    return None


def _target_detail(db, t, cmd_cache, agro_cache):
    """单标的合成:分部行(量/价/成本/敏感度)+指引完成度+观察哨。"""
    segs_out = []
    for seg in t.get("segments", []):
        links = seg.get("price_links") or ([seg["price_link"]] if seg.get("price_link") else [])
        prices = []
        for lk in links:
            if lk.get("type") == "cmd" and lk.get("code"):
                pts = cmd_cache.get(lk["code"]) or _load_cmd_series(db, [lk["code"]]).get(lk["code"])
                if pts:
                    cmd_cache[lk["code"]] = pts
                    prices.append({"name": lk.get("name") or lk["code"], "code": lk["code"],
                                   "unit": lk.get("unit", ""), "stats": _qtd_stats(pts),
                                   "points": pts})
            elif lk.get("type") == "agro" and lk.get("id"):
                pts = agro_cache.get(lk["id"]) or _load_agro_series(db, [lk["id"]]).get(lk["id"])
                if pts:
                    agro_cache[lk["id"]] = pts
                    prices.append({"name": lk.get("name") or lk["id"], "code": lk["id"],
                                   "unit": "元/吨", "stats": _qtd_stats(pts), "points": pts})
        g = (t.get("guidance_2026") or {}).get(seg["product"]) or {}
        vol_rows = seg.get("volume") or []
        vol = vol_rows[-1] if vol_rows else {}
        unit_cost = (seg.get("unit_cost") or [{}])[-1]
        price_unit = next((p["unit"] for p in prices if p["unit"]), None)
        vol_unit = seg.get("volume_unit") or (vol or {}).get("unit")
        ann_vol = _ann_vol(vol_rows, g.get("qty"))
        main_price = prices[0]["stats"] if prices else {}
        segs_out.append({
            "product": seg["product"],
            "prices": prices,
            "volume": {"latest": vol, "all": vol_rows},
            "unit_cost": {"latest": unit_cost, "all": seg.get("unit_cost") or []},
            "cost_drivers": seg.get("cost_drivers") or [],
            "params": seg.get("params") or {},
            "guidance": g or None,
            "guidance_progress": (round(g["q1_3"] / g["qty"] * 100, 1)
                                  if g.get("q1_3") and g.get("qty") else None),
            "sensitivity_10pct": _sensitivity(price_unit, vol_unit,
                                              ann_vol, main_price.get("qtd")),
            "sensitivity_note": seg.get("sensitivity"),
            "spread_extra": seg.get("spread_extra") or [],
            "note": seg.get("note"),
        })
    return {
        "sid": t["sid"], "name": t["name"], "market": t.get("market"),
        "thesis": t.get("thesis"), "period_note": t.get("period_note"),
        "guidance_2026": t.get("guidance_2026"),
        "guidance_source": t.get("guidance_source"),
        "equity_notes": t.get("equity_notes"),
        "watchlist": t.get("watchlist") or [],
        "segments": segs_out,
    }


@router.get("")
def forecast_targets(db: Session = Depends(get_session)):
    """全部标的(含分部合成)。两三个标的体量,一次全返;价格 points 限近 400 点。"""
    targets = db.execute(select(FcTarget).order_by(FcTarget.sid)).scalars().all()
    cmd_cache, agro_cache = {}, {}
    out = []
    for t in targets:
        d = _target_detail(db, t.payload, cmd_cache, agro_cache)
        for seg in d["segments"]:
            for p in seg["prices"]:
                p["points"] = p["points"][-400:]
        out.append(d)
    return {"updated_at": dt.date.today().isoformat(), "targets": out}
