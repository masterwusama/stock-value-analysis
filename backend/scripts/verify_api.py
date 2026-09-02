# -*- coding: utf-8 -*-
"""P2 详情接口验证:GET /api/securities/{code} 与源 companies/events JSON 对比。

前提:uvicorn 已在 BASE 运行(默认 127.0.0.1:8000)。
用法(在 backend 目录下):
    python -X utf8 -m scripts.verify_api
"""
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import LEGACY_DATA_DIR  # 对比基线 = JSON 工作目录(采集后为 collector/)

BASE = "http://127.0.0.1:8000"
DATA = LEGACY_DATA_DIR / "data"
PF = LEGACY_DATA_DIR / "portfolio" / "data"
AGRO = LEGACY_DATA_DIR / "agro-price" / "data"

EVENT_KEYS = ("increase_hold", "ma", "penalty", "lawsuit", "st_change")
HOLDER_KEYS = ("top10", "top10_float", "institutions", "actual_controller", "unlock")


def get(url):
    with urllib.request.urlopen(BASE + url, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def load(p):
    return json.loads(p.read_text(encoding="utf-8"))


def same_val(a, b):
    """宽松对比:数字精度安全，兼容 '--' 等脏值。"""
    if a == b:
        return True
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def check_company(code):
    src = load(DATA / "companies" / f"{code}.json")
    api = get(f"/api/securities/{code}")
    print(f"-- {code} {src['name']}")

    # 行情快照
    assert api["snapshot"]["price"] == src["snapshot"]["price"], "price mismatch"
    assert api["snapshot"]["pe_ttm"] == src["snapshot"]["pe_ttm"], "pe mismatch"
    assert float(api["snapshot"]["market_cap"]) == float(src["snapshot"]["market_cap"]), "mcap mismatch"

    # 财务四表:行数一致
    for key in ("indicators", "income", "balance", "cashflow"):
        assert len(api[key]) == len(src[key]), f"{key} rows {len(api[key])} != {len(src[key])}"

    # 整行键集合/值对比(抽样 3 期;核心科目还原可能用首选键,允许键名替换但值必须在)
    diffs = 0
    for key, date_k in (("indicators", "报告期"), ("income", "报告日"), ("balance", "报告日")):
        for i in (0, len(src[key]) // 2, len(src[key]) - 1):
            sa, ss = api[key][i], src[key][i]
            for k, v in ss.items():
                if k == date_k:
                    continue
                if k not in sa:
                    diffs += 1
                    print(f"   [key-missing] {key}[{i}] 缺 {k}={v}")
                elif not same_val(sa[k], v):
                    diffs += 1
                    print(f"   [value] {key}[{i}] {k}: {sa[k]!r} != {v!r}")
    assert diffs == 0, f"{diffs} 处还原差异"

    # 分红:type 非空的 (year,type) 唯一键去重后行数一致
    seen, uniq = set(), 0
    for r in src["dividends"]:
        k = (r.get("year"), r.get("type"))
        if r.get("type") is not None:
            if k in seen:
                continue
            seen.add(k)
        uniq += 1
    assert len(api["dividends"]) == uniq, f"dividends {len(api['dividends'])} != {uniq}"

    # 定期报告:导入跳过无 date 行
    expect = sum(1 for r in src["reports"] if r.get("date"))
    assert len(api["reports"]) == expect, f"reports {len(api['reports'])} != {expect}"

    # 评分快照 vs index.json
    idx = load(DATA / "index.json")
    sc_src = next(c["scores"] for c in idx["companies"] if c["code"] == code)
    sc = api["scores"]
    for k in ("grahamAgg", "grahamDef", "schloss", "buffett", "fraud", "mgmt", "cycle"):
        assert sc[k] == sc_src[k], f"score {k}: {sc[k]} != {sc_src[k]}"
    for school in ("grahamAgg", "grahamDef", "schloss", "buffett"):
        for kk in ("buy", "sellCons", "sellFair"):
            a, b = sc["priceRefs"][school][kk], sc_src["priceRefs"][school][kk]
            assert a == b, f"priceRefs {school}.{kk}: {a} != {b}"
    assert sc["priceRefs"]["fairLiq"] == sc_src["priceRefs"]["fairLiq"]

    # Wind 事件/股东分组条数(有事件文件的公司才检)
    ev_path = DATA / "events" / f"{code}.json"
    if ev_path.exists():
        ev = load(ev_path)
        groups = api["events"]
        for g in EVENT_KEYS:
            want = len((ev.get("events") or {}).get(g) or [])
            got = len((groups or {}).get("events", {}).get(g, []))
            assert got == want, f"event {g}: {got} != {want}"
        for g in HOLDER_KEYS:
            want = len((ev.get("holders") or {}).get(g) or [])
            got = len((groups or {}).get("holders", {}).get(g, []))
            assert got == want, f"holder {g}: {got} != {want}"
        print(f"   events 分组核对 OK")
    print("   OK")


def check_portfolio():
    src = load(PF / "portfolio.json")
    api = get("/api/portfolio")
    assert set(api["strategies"]) == set(src["strategies"]), "strategy keys"
    assert api["as_of"] == src["as_of"], "as_of"
    for k, s in src["strategies"].items():
        a = api["strategies"][k]
        for f in ("label", "init_cap", "cash", "nav", "prev_nav",
                  "day_pnl", "day_pnl_pct", "total_pnl", "total_pnl_pct", "position_pct", "as_of"):
            assert same_val(a[f], s[f]), f"{k}.{f}: {a.get(f)!r} != {s.get(f)!r}"
        sp = {p["code"]: p for p in s["positions"]}
        ap = {p["code"]: p for p in a["positions"]}
        assert set(sp) == set(ap), f"{k} position codes"
        for code, p in sp.items():
            q = ap[code]
            for f in ("shares", "cost", "bought_at", "tranches", "days"):
                assert same_val(q[f], p[f]), f"{k}/{code}.{f}: {q.get(f)!r} != {p.get(f)!r}"
            for f in ("price", "value", "pnl", "pnl_pct"):
                assert abs(q[f] - p[f]) < 0.01, f"{k}/{code}.{f}: {q[f]} != {p[f]}"
    print("-- portfolio 三策略 + 持仓实时计算 OK")


def check_trades():
    src = load(PF / "trades.json")
    api = get("/api/portfolio/trades")
    for k, rows in src.items():
        arows = api.get(k, [])
        assert len(arows) == len(rows), f"{k}: {len(arows)} != {len(rows)}"
        for s, a in zip(rows, arows):
            for f in ("date", "code", "side"):
                assert a[f] == s[f], f"{k}.{f}: {a.get(f)} != {s.get(f)}"
            for f in ("price", "shares", "amount"):
                assert same_val(a[f], s.get(f)), f"{k}/{s['date']}.{f}: {a[f]!r} != {s.get(f)!r}"
    print(f"-- trades 全策略 {sum(len(v) for v in src.values())} 笔逐行 OK")


def check_products():
    src = load(AGRO / "products.json")
    api = get("/api/agro/products")
    assert {p["id"] for p in api["products"]} == {p["id"] for p in src["products"]}, "product ids"
    for sp in src["products"]:
        ap = next(p for p in api["products"] if p["id"] == sp["id"])
        for f in ("name", "category", "spec", "unit"):
            assert ap[f] == sp.get(f), f"{sp['id']}.{f}"
        smap = {(r["date"], r.get("source", "")): r["price"] for r in sp["prices"]}
        amap = {(r["date"], r["source"]): r["price"] for r in ap["prices"]}
        assert smap == amap, f"{sp['id']} prices 不一致"
    print("-- agro products 全量价格序列 OK")


def check_edb():
    src = load(AGRO / "edb.json")
    api = get("/api/agro/edb")
    assert [c["id"] for c in api["categories"]] == [c["id"] for c in src["categories"]], "cat 顺序"
    for sc, ac in zip(src["categories"], api["categories"]):
        assert ac["name"] == sc["name"], f"cat name {sc['id']}"
        assert [i["code"] for i in ac["indicators"]] == [i["code"] for i in sc["indicators"]], \
            f"cat {sc['id']} 指标顺序"
        for si, ai in zip(sc["indicators"], ac["indicators"]):
            for f in ("name", "label", "unit", "freq", "source", "group"):
                assert ai[f] == si.get(f), f"{si['code']}.{f}: {ai[f]!r} != {si.get(f)!r}"
            assert len(ai["points"]) == len(si["points"]), \
                f"{si['code']} points {len(ai['points'])} != {len(si['points'])}"
            for pa, ps in zip(ai["points"], si["points"]):
                assert pa[0] == ps[0] and same_val(pa[1], ps[1]), f"{si['code']} @ {ps[0]}"
    print("-- edb 全部分类/指标/数据点逐点 OK")


print("== 详情接口 vs 源 JSON ==")
for code in sys.argv[1:] or ["600309", "002027", "600809", "01378", "NVDA"]:
    check_company(code)

print("== 404 行为 ==")
try:
    get("/api/securities/999999")
    raise AssertionError("should 404")
except urllib.error.HTTPError as e:
    assert e.code == 404, e.code
print("   404 OK")

print("== P3 组合/农价/EDB 接口 vs 源 JSON ==")
check_portfolio()
check_trades()
check_products()
check_edb()

print("API ALL CHECKS PASSED")
