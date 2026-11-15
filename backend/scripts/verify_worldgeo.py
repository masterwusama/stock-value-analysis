# -*- coding: utf-8 -*-
"""世界局势推演模块·验证脚本(backend 目录跑: python -m scripts.verify_worldgeo)。

检查六层:①人工档 4 个 JSON 的 schema 与交叉一致性;②派生序列产物抽查
(抽 3 个日期点手工重算 XAU÷CL 与 GEO_GOLD_OIL 对比);③cmd_* 两表 worldgeo
行与点数;④温度计 10 个 code 在 cmd_value 的覆盖与新鲜度;⑤应验度代理映射
的 code 全部在库;⑥API 端点 smoke(服务在跑时)。

零写操作(只读库只读文件),可随时跑;红=数据/档位问题,不影响线上。
"""
import datetime as dt
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
GEO_DIR = BACKEND / "collector" / "commodity-worldgeo" / "data"
sys.path.insert(0, str(BACKEND))

fails, notes = [], []


def check(name, ok, detail=""):
    (notes if ok else fails).append("%s%s" % (name, (" — " + detail) if detail else ""))


def main():
    # ---- ① 人工档 schema 与交叉一致性 ----
    events = json.loads((GEO_DIR / "events.json").read_text(encoding="utf-8"))
    matrix = json.loads((GEO_DIR / "geo_matrix.json").read_text(encoding="utf-8"))
    scenarios = json.loads((GEO_DIR / "scenarios.json").read_text(encoding="utf-8"))
    playbooks = json.loads((GEO_DIR / "playbooks.json").read_text(encoding="utf-8"))

    cats = matrix.get("categories", [])
    check("矩阵类别数=10", len(cats) == 10, "实际 %d" % len(cats))
    refs_ok = {"gold", "silver", "copper", "aluminum", "lithium", "crudeoil",
               "ferrous", "agri", "fx_cny", "shipping", "chemicals", "aviation", "semis"}
    row_refs = set()
    for r in matrix.get("rows", []):
        row_refs.add(r["ref"])
        cells = r.get("cells", [])
        if len(cells) != len(cats):
            fails.append("矩阵行 %s 格子数 %d ≠ 类别数 %d" % (r["ref"], len(cells), len(cats)))
        for i, c in enumerate(cells):
            if c.get("d") not in (1, 0, -1) or not (0 <= c.get("s", -1) <= 3):
                fails.append("矩阵行 %s 列 %d 方向/强度非法: %s" % (r["ref"], i, c))
    check("矩阵行覆盖 13 资产", row_refs == refs_ok,
          "缺 %s 多 %s" % (refs_ok - row_refs, row_refs - refs_ok) if row_refs != refs_ok else "")

    ids = {e["id"] for e in events.get("events", [])}
    pb_ids = {p["id"] for p in playbooks.get("playbooks", [])}
    for e in events.get("events", []):
        for k in ("date", "title", "category", "status", "assets"):
            if k not in e:
                fails.append("事件 %s 缺字段 %s" % (e.get("id"), k))
        if e.get("status") not in ("active", "resolved"):
            fails.append("事件 %s status 非法: %s" % (e.get("id"), e.get("status")))
        try:
            dt.date.fromisoformat(e.get("date") or "")
        except ValueError:
            fails.append("事件 %s 日期非法: %s" % (e.get("id"), e.get("date")))
        for a in e.get("assets", []):
            if a.get("type") not in ("commodity", "fx", "sector", "stock"):
                fails.append("事件 %s 资产 type 非法: %s" % (e.get("id"), a.get("type")))
            if a.get("direction") not in ("bullish", "bearish", "neutral"):
                fails.append("事件 %s 资产 direction 非法: %s" % (e.get("id"), a.get("direction")))
            if not (0 <= a.get("strength", -1) <= 3):
                fails.append("事件 %s 资产 strength 非法: %s" % (e.get("id"), a.get("strength")))
            if a.get("type") in ("commodity", "fx") and a.get("ref") not in {
                    "gold", "silver", "copper", "aluminum", "lithium", "crudeoil",
                    "ferrous", "agri", "fx_cny", "natgas", "jetfuel", "ironore"}:
                fails.append("事件 %s 商品面 ref 无代理映射: %s" % (e.get("id"), a.get("ref")))
        pb = e.get("playbook_ref")
        if pb and pb not in pb_ids:
            fails.append("事件 %s playbook_ref 不存在: %s" % (e.get("id"), pb))
    check("事件档校验", True, "%d 个事件" % len(events.get("events", [])))

    for s in scenarios.get("sets", []):
        p_sum = sum(x.get("probability", 0) for x in s.get("scenarios", []))
        if abs(p_sum - 1.0) > 0.01:
            fails.append("情景集 %s 概率和=%.2f ≠ 1" % (s.get("set_id"), p_sum))
        lk = s.get("linked_event")
        if lk and lk not in ids:
            fails.append("情景集 %s linked_event 不存在: %s" % (s.get("set_id"), lk))
    check("情景集校验", True, "%d 套" % len(scenarios.get("sets", [])))
    check("剧本档校验", True, "%d 个剧本" % len(playbooks.get("playbooks", [])))

    # ---- ② 派生序列产物抽查(手工重算对比) ----
    gold = json.loads((BACKEND / "collector" / "commodity-gold" / "data" / "gold.json")
                      .read_text(encoding="utf-8"))
    crude = json.loads((BACKEND / "collector" / "commodity-crudeoil" / "data" / "crudeoil.json")
                       .read_text(encoding="utf-8"))
    copper = json.loads((BACKEND / "collector" / "commodity-copper" / "data" / "copper.json")
                        .read_text(encoding="utf-8"))
    wgeo = json.loads((GEO_DIR / "worldgeo.json").read_text(encoding="utf-8"))
    check("worldgeo.json commodity=worldgeo", wgeo.get("commodity") == "worldgeo")

    def pts_map(doc, code):
        for s in doc.get("series", []):
            if s["code"] == code:
                return {p[0]: float(p[1]) for p in s.get("points", [])}
        return {}

    xau, cl = pts_map(gold, "XAU_USD"), pts_map(crude, "CL_MAIN")
    cad = pts_map(copper, "CAD_MAIN")
    go = {p[0]: float(p[1]) for p in next(
        (s["points"] for s in wgeo["series"] if s["code"] == "GEO_GOLD_OIL"), [])}
    oc = {p[0]: float(p[1]) for p in next(
        (s["points"] for s in wgeo["series"] if s["code"] == "GEO_OIL_CU"), [])}
    check("GEO_GOLD_OIL 非空", bool(go), "%d 点" % len(go))
    check("GEO_OIL_CU 非空", bool(oc), "%d 点" % len(oc))
    bad = 0
    for d, v in list(go.items())[-3:]:  # 抽最近 3 点手工重算
        if d in xau and d in cl and abs(v - xau[d] / cl[d]) > 0.011:
            bad += 1
            fails.append("GEO_GOLD_OIL %s 重算不符: %s vs %s/%s" % (d, v, xau[d], cl[d]))
    for d, v in list(oc.items())[-3:]:
        if d in cl and d in cad and abs(v - cl[d] / cad[d] * 1000) > 0.011:
            bad += 1
            fails.append("GEO_OIL_CU %s 重算不符" % d)
    check("派生值抽查(最近3点重算)", bad == 0)
    dates = list(go.keys())
    check("GEO_GOLD_OIL 日期升序", dates == sorted(dates))
    check("GEO_GOLD_OIL 全为正值", all(v > 0 for v in go.values()))

    # ---- ③④⑤ 数据库层 ----
    from app.db import SessionLocal
    from app.models import CmdIndicator, CmdValue
    from sqlalchemy import func, select

    db = SessionLocal()
    try:
        inds = db.execute(select(CmdIndicator).where(
            CmdIndicator.commodity == "worldgeo")).scalars().all()
        check("cmd_indicator worldgeo 行数=2", len(inds) == 2,
              "实际 %d: %s" % (len(inds), [i.cmd_code for i in inds]))
        # 温度计 10 code 覆盖与新鲜度
        thermo_codes = ["GPR", "VIXCLS", "GEO_GOLD_OIL", "GOLD_SILVER", "CU_GOLD",
                        "GEO_OIL_CU", "BRENT_WTI", "DTWEXBGS", "DFII10", "USDCNY_FIX"]
        for code in thermo_codes:
            mx = db.execute(select(func.max(CmdValue.data_date))
                            .where(CmdValue.cmd_code == code)).scalar()
            n = db.execute(select(func.count()).select_from(CmdValue)
                           .where(CmdValue.cmd_code == code)).scalar()
            age = (dt.date.today() - mx).days if mx else None
            lim = 70 if code == "GPR" else 14
            check("温度计 %s 在库(%d 点)" % (code, n), mx is not None)
            check("温度计 %s 新鲜(≤%d 天)" % (code, lim), age is not None and age <= lim,
                  "最新 %s(陈旧 %s 天)" % (mx, age))
        # 应验度代理映射全部在库
        for ref, code in {"crudeoil": "CL_MAIN", "gold": "XAU_USD", "silver": "XAG_USD",
                          "copper": "CAD_MAIN", "aluminum": "AHD_MAIN",
                          "lithium": "LC0_CLOSE", "ferrous": "RB0_CLOSE",
                          "agri": "LH0_CLOSE", "fx_cny": "USDCNY_FIX",
                          "natgas": "NG_MAIN", "jetfuel": "AV_JETFUEL"}.items():
            mx = db.execute(select(func.max(CmdValue.data_date))
                            .where(CmdValue.cmd_code == code)).scalar()
            check("代理映射 %s→%s 在库" % (ref, code), mx is not None)
        # 互灌防线:GEO_ 前缀序列的 commodity 必须是 worldgeo
        for i in inds:
            if not i.cmd_code.startswith("GEO_"):
                fails.append("worldgeo 行 %s 不带 GEO_ 前缀(互灌风险)" % i.cmd_code)
            if i.commodity != "worldgeo":
                fails.append("cmd_indicator %s commodity=%s(应 worldgeo)" % (i.cmd_code, i.commodity))
    finally:
        db.close()

    # ---- ⑥ API smoke(服务在跑时) ----
    import urllib.request
    for ep in ("overview", "events", "scenarios", "matrix"):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/api/world-geo/" + ep, timeout=5) as r:
                body = json.loads(r.read().decode("utf-8"))
                check("API /%s 200" % ep, r.status == 200 and bool(body))
        except Exception as e:  # noqa: BLE001 服务未起时提示而非判死
            notes.append("API /%s 不可达(服务未跑?): %s" % (ep, type(e).__name__))

    print("== verify_worldgeo ==")
    for n in notes:
        print("  ✓", n)
    for f in fails:
        print("  ✗", f)
    print("== %d 通过 / %d 失败 ==" % (len(notes), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
