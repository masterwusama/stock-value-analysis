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


def _load_series_for_link(db, lk, cmd_cache, agro_cache):
    """单条 price/cost link → [(date, val)] 升序;带缓存。"""
    if lk.get("type") == "cmd" and lk.get("code"):
        if lk["code"] not in cmd_cache:
            cmd_cache.update(_load_cmd_series(db, [lk["code"]]))
        return cmd_cache.get(lk["code"]) or []
    if lk.get("type") == "agro" and lk.get("id"):
        if lk["id"] not in agro_cache:
            agro_cache.update(_load_agro_series(db, [lk["id"]]))
        return agro_cache.get(lk["id"]) or []
    return []


def _spread_series(a_pts, b_pts, b_mult=1.0):
    """两序列按日对齐内连接做差(a-b×b_mult);b_mult 支持单耗系数(如铝耗氧化铝1.93)。"""
    mb = dict(b_pts)
    return [[d, round(v - mb[d] * b_mult, 2)] for d, v in a_pts if d in mb]


def _bucket_avg(points, year, quarter):
    vals = [v for d, v in points
            if v is not None and d.startswith(str(year))
            and (int(d[5:7]) - 1) // 3 + 1 == quarter]
    return sum(vals) / len(vals) if vals else None


def _dyn_price(stats, days_gone, days_total, scenario_pct=0.0):
    """动态加权预估价:QTD已实现均值×已过天数权重 + 最新价×(1+情景)×剩余权重。"""
    if not stats or stats.get("qtd") is None:
        return None
    w = min(1.0, max(0.0, days_gone / days_total))
    return stats["qtd"] * w + stats["latest"] * (1 + scenario_pct) * (1 - w)


def _q4_window(today=None):
    """Q4 已过/总天数(自然日,10-01~12-31)。"""
    today = today or dt.date.today()
    q_start, q_end = dt.date(today.year, 10, 1), dt.date(today.year, 12, 31)
    if today < q_start:
        return 0, (q_end - q_start).days + 1
    return (today - q_start).days + 1, (q_end - q_start).days + 1


SCN = ("low", "base", "high")


def _model_projection(t, cmd_cache, agro_cache=None):
    """增量桥模型:Q4E 与 FY2027E 三情景。结构化假设在 targets.json model 块,
    价格侧动态(库内序列),输出 营收/毛利率/归母/扣非 区间。"""
    m = t.get("model")
    if not m:
        return None
    conduit = m.get("conduit", 0.7)
    days_gone, days_total = _q4_window()
    out = {"method": m.get("method"), "conduit": conduit,
           "conduit_note": m.get("conduit_note"),
           "period_label": m.get("period_label"),
           "baseline_label": None,
           "q4_window": {"days_gone": days_gone, "days_total": days_total},
           "q4": None, "fy26": None, "fy27": None, "assumptions": []}
    q4m = m.get("q4_2026") or {}
    base = q4m.get("baseline") or {}
    out["baseline_label"] = base.get("label")
    price_scn = q4m.get("price_scenario_pct") or [-0.05, 0.0, 0.03]

    # —— 紫金式:增量桥(分部 Δ毛利 = (Δ价−Δ成本)×情景量) ——
    if any(s.get("price_code") for s in q4m.get("segments", [])):
        semi = m.get("period_mode") == "h2"  # 港股半年报:基准=H1实际,预估=H2
        rows, deltas = [], [0.0, 0.0, 0.0]
        year = dt.date.today().year
        for s in q4m.get("segments", []):
            if not s.get("price_code"):
                static = s.get("static_margin_change_yi") or [0, 0, 0]
                deltas = [d + x for d, x in zip(deltas, static)]
                continue
            pts = cmd_cache.get(s["price_code"]) or []
            stats = _qtd_stats(pts) if pts else {}
            if s.get("p_prev_override") is not None:
                p3 = s["p_prev_override"]
                p4_pair = None
            elif semi:
                # prev=H1均价(Q1+Q2桶),预估=Q3桶与Q4动态各半
                q1, q2 = _bucket_avg(pts, year, 1), _bucket_avg(pts, year, 2)
                q3 = _bucket_avg(pts, year, 3)
                p3 = (q1 + q2) / 2 if q1 is not None and q2 is not None else None
                p4_pair = (q3, None)
            else:
                p3 = stats.get("prev_q")
                p4_pair = None
            vols = s.get("vol_scenarios") or {}
            c3, cq = s.get("q3_cost"), 1 + (s.get("cost_qoq") or 0)
            cost_links = s.get("cost_links") or []
            mult = s.get("mult", 1)
            row = {"product": s["product"], "p_prev": p3, "p_dyn": None,
                   "vols": vols, "vol_note": s.get("vol_note")}
            seg_d = [0.0, 0.0, 0.0]
            for i, scn in enumerate(SCN):
                v = vols.get("base")
                if vols.get("guide") and scn == "high":
                    v = vols["guide"]
                elif scn == "low" and vols.get("low"):
                    v = vols["low"]
                elif scn == "high" and vols.get("high"):
                    v = vols["high"]
                if semi:
                    p4 = None
                    if p4_pair[0] is not None:
                        d4 = _dyn_price(stats, days_gone, days_total, price_scn[i])
                        p4 = (p4_pair[0] + d4) / 2 if d4 is not None else None
                else:
                    p4 = _dyn_price(stats, days_gone, days_total, price_scn[i])
                if p3 is None or p4 is None or v is None:
                    continue
                # 成本变动:cost_links(价格联动的单耗成本)优先,否则披露成本×环比
                cost_delta = 0.0
                for cl in cost_links:
                    if cl.get("type") == "agro":
                        if cl["id"] not in (agro_cache or {}):
                            (agro_cache or {}).update(_load_agro_series(db, [cl["id"]]))
                        cpts = (agro_cache or {}).get(cl["id"]) or []
                    else:
                        if cl["code"] not in cmd_cache:
                            cmd_cache.update(_load_cmd_series(db, [cl["code"]]))
                        cpts = cmd_cache.get(cl["code"]) or []
                    cm = cl.get("mult", 1.0)
                    if not cpts:
                        continue
                    if semi:
                        cq1, cq2 = _bucket_avg(cpts, year, 1), _bucket_avg(cpts, year, 2)
                        cq3, cdyn = _bucket_avg(cpts, year, 3), None
                        st4 = _qtd_stats(cpts)
                        if st4.get("qtd") is not None:
                            cdyn = _dyn_price(st4, days_gone, days_total, 0.0)
                        if None not in (cq1, cq2, cq3, cdyn):
                            cost_delta += (((cq3 + cdyn) / 2) - (cq1 + cq2) / 2) * cm
                    else:
                        cst = _qtd_stats(cpts)
                        # prev 可显式 override(新接入序列无上季历史,如纯苯2026-10起)
                        prev = cl["prev"] if cl.get("prev") is not None else cst.get("prev_q")
                        if cst.get("qtd") is not None and prev is not None:
                            cost_delta += (_dyn_price(cst, days_gone, days_total, 0.0)
                                           - prev) * cm
                if not cost_links and c3 is not None:
                    cost_delta = c3 * (cq - 1)
                d = (p4 - p3 - cost_delta) * v * mult * conduit / 1e8
                seg_d[i] += d
                if scn == "base":
                    row["p_dyn"] = round(p4, 1)
                    row["cost_delta"] = round(cost_delta, 1)
            deltas = [d + x for d, x in zip(deltas, seg_d)]
            row["delta_yi"] = [round(x, 1) for x in seg_d]
            rows.append(row)
        imp = (q4m.get("other_lines") or {}).get("impairment_yi") or [0, 0, 0]
        nonrec_q = ((base.get("attributable_yi") or 0) - base["non_gaap_yi"]
                    if base.get("non_gaap_yi") is not None else None)
        proj = []
        for i, scn in enumerate(SCN):
            att = base["attributable_yi"] + deltas[i] + imp[i]
            proj.append({"scenario": scn,
                         "attributable_yi": round(att, 1),
                         "non_gaap_yi": round(att - nonrec_q, 1) if nonrec_q is not None else None})
        out["q4"] = {"mode": "增量桥", "segments": rows,
                     "impairment_yi": imp,
                     "note": (q4m.get("other_lines") or {}).get("impairment_note"),
                     "proj": proj}
        if base.get("ytd_attributable_yi"):
            out["fy26"] = {"scenario": SCN,
                           "attributable_yi": [round(base["ytd_attributable_yi"] + p["attributable_yi"], 0) for p in proj],
                           "non_gaap_yi": [round(base.get("ytd_non_gaap_yi", 0) + p["non_gaap_yi"], 0)
                                           if p["non_gaap_yi"] is not None and base.get("ytd_non_gaap_yi") is not None
                                           else None for p in proj]}
        # FY27: 量增×当前单位毛利×传导 ± 价格情景
        f27 = m.get("fy_2027") or {}
        segs27, growth = f27.get("segments", []), [0.0, 0.0, 0.0]
        for s in segs27:
            v26 = s.get("vol_2026e")
            v27 = s.get("vol_2027") or [v26, v26, v26]
            pc = next((x for x in q4m.get("segments", []) if x.get("product") == s["product"]), None)
            if pc and v26:
                pts = cmd_cache.get(pc["price_code"]) or []
                stats = _qtd_stats(pts) if pts else {}
                p = stats.get("latest")
                c27 = pc.get("q3_cost", 0) * (1 + (f27.get("cost_escalation") or 0))
                mult = pc.get("mult", 1)
                for i in range(3):
                    growth[i] += ((v27[i] - v26) * (p - c27) * mult * conduit / 1e8
                                  + v26 * (p * (f27.get("price_scenario_pct") or [0, 0, 0])[i]) * mult * conduit / 1e8)
            elif s.get("delta_margin_yi"):
                growth = [g + x * conduit for g, x in zip(growth, s["delta_margin_yi"])]
        fy26_mid = out["fy26"]["attributable_yi"][1] if out.get("fy26") else None
        if fy26_mid:
            out["fy27"] = {"attributable_yi": [round(fy26_mid + g, 0) for g in growth],
                           "growth_yi": [round(g, 0) for g in growth],
                           "note": f27.get("note")}
        return out

    # —— 广信式:分部收入×毛利率直接重构(Q3公告给了分部量价) ——
    seg_rows, gross_all, rev_all = [], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
    for s in q4m.get("segments", []):
        gm_s = s.get("q4_gm_scenarios_pct") or [s.get("q3_gm_pct")] * 3
        rev_s = s.get("q4_rev_scenarios_yi") or [s.get("q3_rev_yi")] * 3
        for i in range(3):
            gross_all[i] += rev_s[i] * gm_s[i] / 100
            rev_all[i] += rev_s[i]
        seg_rows.append({"product": s["product"], "q3_rev_yi": s.get("q3_rev_yi"),
                         "q3_gm_pct": s.get("q3_gm_pct"),
                         "q4_rev_yi": rev_s, "q4_gm_pct": gm_s, "note": s.get("note")})
    opex = q4m.get("opex_q4_yi") or [-1.4, -1.4, -1.4]
    nop = q4m.get("non_op") or {}
    wealth = nop.get("wealth_yi") or [1.4, 1.4, 1.4]
    comp = nop.get("compensation_yi") or [0, 0, 0]
    subs = nop.get("subsidy_yi") or [0.1, 0.1, 0.1]
    proj = []
    for i, scn in enumerate(SCN):
        op = gross_all[i] + opex[i]
        non_gaap = op * 0.85  # 税15%
        att = non_gaap + wealth[i] + comp[i] + subs[i]
        proj.append({"scenario": scn,
                     "revenue_yi": round(rev_all[i], 2),
                     "gross_margin_pct": round(gross_all[i] / rev_all[i] * 100, 1) if rev_all[i] else None,
                     "non_gaap_yi": round(non_gaap, 2),
                     "attributable_yi": round(att, 2)})
    out["q4"] = {"mode": "分部重构", "segments": seg_rows,
                 "opex_yi": opex, "opex_note": q4m.get("opex_note"),
                 "non_op": nop, "proj": proj}
    if base.get("ytd_attributable_yi") is not None:
        out["fy26"] = {
            "attributable_yi": [round(base["ytd_attributable_yi"] + p["attributable_yi"], 2) for p in proj],
            "non_gaap_yi": [round(base.get("ytd_non_gaap_yi", 0) + p["non_gaap_yi"], 2) for p in proj],
            "revenue_yi": [round(base.get("ytd_revenue_yi", 0) + p["revenue_yi"], 1) for p in proj]}
    f27 = m.get("fy_2027") or {}
    if f27.get("items"):
        adds = [0.0, 0.0, 0.0]
        for it in f27["items"]:
            c = it.get("contrib_yi") or [0, 0, 0]
            adds = [a + x for a, x in zip(adds, c)]
            out["assumptions"].append({"name": it["name"], "contrib_yi": c, "driver": it.get("driver")})
        fy26_mid = out["fy26"]["non_gaap_yi"][1] if out.get("fy26") else 0
        out["fy27"] = {"non_gaap_yi": [round(fy26_mid + a, 2) for a in adds],
                       "crosscheck": f27.get("crosscheck")}
    return out


def _target_detail(db, t, cmd_cache, agro_cache):
    """单标的合成:分部行(量/价/成本/价差/敏感度)+指引完成度+观察哨。"""
    segs_out = []
    for seg in t.get("segments", []):
        links = seg.get("price_links") or ([seg["price_link"]] if seg.get("price_link") else [])
        prices, costs = [], []
        for lk in links:
            pts = _load_series_for_link(db, lk, cmd_cache, agro_cache)
            if not pts:
                continue
            unit = lk.get("unit") or ("元/吨" if lk.get("type") == "agro" else "")
            prices.append(
                {"name": lk.get("name") or lk.get("code") or lk["id"],
                 "code": lk.get("code") or lk["id"], "unit": unit,
                 "stats": _qtd_stats(pts), "points": pts})
        for lk in seg.get("cost_links") or []:
            pts = _load_series_for_link(db, lk, cmd_cache, agro_cache)
            if not pts:
                continue
            costs.append({
                "name": lk.get("name") or lk.get("code") or lk["id"],
                "code": lk.get("code") or lk["id"],
                "unit": lk.get("unit") or ("元/吨" if lk.get("type") == "agro" else ""),
                "stats": _qtd_stats(pts), "points": pts})
        spreads = []
        for sp in seg.get("spreads") or []:
            a = _load_series_for_link(db, sp["a"], cmd_cache, agro_cache)
            b = _load_series_for_link(db, sp["b"], cmd_cache, agro_cache)
            if not a or not b:
                continue
            spts = _spread_series(a, b, sp.get("b_mult", 1.0))
            if spts:
                spreads.append({"name": sp["name"], "code": "SPREAD:%s:%s" % (
                    sp["a"].get("code") or sp["a"]["id"], sp["b"].get("code") or sp["b"]["id"]),
                    "unit": "元/吨", "stats": _qtd_stats(spts), "points": spts})
        g = (t.get("guidance_2026") or {}).get(seg["product"]) or {}
        vol_rows = seg.get("volume") or []
        vol = vol_rows[-1] if vol_rows else {}
        unit_cost = (seg.get("unit_cost") or [{}])[-1]
        price_unit = next((p["unit"] for p in prices if p["unit"]), None)
        vol_unit = seg.get("volume_unit") or (vol or {}).get("unit")
        ann_vol = _ann_vol(vol_rows, g.get("qty"))
        main_price = prices[0]["stats"] if prices else {}
        # 单位毛利空间(紫金式:QTD价 − 最新披露成本;单位一致才可减)
        unit_margin = None
        if (main_price.get("qtd") is not None and unit_cost.get("cost") is not None
                and unit_cost.get("unit") and price_unit == unit_cost["unit"]):
            unit_margin = round(main_price["qtd"] - unit_cost["cost"], 2)
        segs_out.append({
            "product": seg["product"],
            "prices": prices,
            "costs": costs,
            "spreads": spreads,
            "volume": {"latest": vol, "all": vol_rows},
            "unit_cost": {"latest": unit_cost, "all": seg.get("unit_cost") or []},
            "unit_margin": unit_margin,
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
        "model": _model_projection(t, cmd_cache, agro_cache),
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
            for key in ("prices", "costs", "spreads"):
                for p in seg.get(key) or []:
                    p["points"] = p["points"][-400:]
        out.append(d)
    return {"updated_at": dt.date.today().isoformat(), "targets": out}
