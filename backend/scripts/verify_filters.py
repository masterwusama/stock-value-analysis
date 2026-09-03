# -*- coding: utf-8 -*-
"""P4.5 回归:列表筛选参数 vs 原 index.json 本地语义对答案 + 详情透传字段。

用法(需 API 服务在 :8000 运行): python -X utf8 scripts/verify_filters.py
"""
import io
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import LEGACY_DATA_DIR  # 对答案基线 = JSON 工作目录(采集后为 collector/)

BASE = "http://127.0.0.1:8000/api"
IDX = json.load(io.open(LEGACY_DATA_DIR / "data" / "index.json", encoding="utf-8"))
COLS = {"grahamAgg": "grahamAgg", "grahamDef": "grahamDef", "schloss": "schloss", "buffett": "buffett"}
# 板块前缀(与 app/api/securities.py BOARDS 一致)
BOARDS = {"shMain": ("60",), "szMain": ("00",), "gem": ("30",),
          "star": ("68",), "bj": ("92", "83", "87", "43")}


def api(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.load(r)


def local_flt(fraud_max=None, mgmt_min=None, buys=None, sells=None, discount=None,
              market=None, board=None, st=None):
    """复刻原 stock.js passFlt(base 分口径,windMode 关)。"""
    out = []
    for c in IDX["companies"]:
        if market and c.get("market", "A") != market:
            continue
        if board:
            # 板块仅限 A 股:港股代码 00700 也以 "00" 开头,不能混入深主
            if c.get("market", "A") != "A" or not str(c["code"]).startswith(BOARDS[board]):
                continue
        if st is not None:
            if ("ST" in str(c.get("name") or "").upper()) != st:
                continue
        sc = c.get("scores") or {}
        refs = sc.get("priceRefs") or {}
        if fraud_max is not None:
            f = sc.get("fraud")
            if f is None or f > fraud_max:
                continue
        if mgmt_min is not None:
            m = sc.get("mgmt")
            if m is None or m < mgmt_min:
                continue
        cur = c.get("price")
        disc = (discount / 100) if (discount is not None and buys) else 1
        ok = True
        for k in buys or []:
            r = refs.get(k) or {}
            if r.get("buy") is None or cur is None or cur > r["buy"] * disc:
                ok = False
                break
        if ok:
            for k in sells or []:
                r = refs.get(k) or {}
                if (r.get("sellCons") is None or r.get("sellFair") is None
                        or cur is None or cur < r["sellCons"] or cur < r["sellFair"]):
                    ok = False
                    break
        if ok:
            out.append(c["code"])
    return set(out)


def _api_pages(cases):
    """逐页拉全(全市场 5500 只下单页 200 行装不下,不能只取首页对答案)。"""
    q = {k: v for k, v in cases.items() if v is not None}
    if q.get("buys"):
        q["buys"] = ",".join(q["buys"])
    if q.get("sells"):
        q["sells"] = ",".join(q["sells"])
    q["page_size"] = 200
    page, items, total = 1, [], None
    while True:
        d = api("/securities?" + urllib.parse.urlencode(dict(q, page=page)))
        items.extend(d["items"])
        total = d["total"] if total is None else total
        if d["total"] != total:
            raise AssertionError(f"翻页中 total 变了: {total} -> {d['total']}")
        if not d["items"] or len(items) >= total or page > 60:
            break
        page += 1
    return items, total


def api_flt(cases):
    items, total = _api_pages(cases)
    codes = {i["code"] for i in items}
    assert total == len(codes), f"total={total} != codes={len(codes)}(代码重复?)"
    return codes


def api_flt_items(cases):
    items, _ = _api_pages(cases)
    return {i["code"]: i for i in items}


fails = 0


def check(label, want, got):
    global fails
    ok = want == got
    if not ok:
        fails += 1
    print(("OK  " if ok else "FAIL") + f" {label}: local={len(want)} api={len(got)}"
          + ("" if ok else f" diff={sorted(want ^ got)}"))


cases_list = [
    {"fraud_max": 20}, {"fraud_max": 50}, {"mgmt_min": 70}, {"mgmt_min": 60, "market": "A"},
    {"buys": ["grahamAgg"]}, {"buys": ["grahamAgg"], "discount": 120},
    {"buys": ["schloss", "buffett"]}, {"buys": ["schloss", "buffett"], "discount": 90},
    {"sells": ["buffett"]}, {"sells": ["grahamDef", "schloss"]},
    {"fraud_max": 40, "mgmt_min": 50, "buys": ["grahamDef"], "discount": 130},
    # 全市场规模维度：板块 / ST
    {"board": "bj"}, {"board": "gem"}, {"board": "star"}, {"board": "shMain"},
    {"st": True}, {"st": False}, {"board": "szMain", "st": False},
    {"board": "bj", "fraud_max": 40, "mgmt_min": 60},
]
for cs in cases_list:
    label = "&".join(f"{k}={v}" for k, v in cs.items())
    check(label, local_flt(**cs), api_flt(cs))

# ---------- Wind 事件增强分档（wind=true）----------
# 同一口径有三处独立实现：后端 _wind_score()(SQL) / 列表页 dispScore()(JS) / 本地基线(Python)，
# 三边必须一致，否则“按增强分排序 + 按门槛筛选”与列面显示会互相打脸。基线读 events/index.json，
# 不抄后端的 SQL，否则这道防线就成了自证。
def _wind_disp(base, delta):
    if base is None:
        return None
    return max(0.0, min(100.0, base + (delta or 0)))


OV_PATH = Path(LEGACY_DATA_DIR) / "data" / "events" / "index.json"
if OV_PATH.exists():
    OVERLAY = json.load(io.open(OV_PATH, encoding="utf-8")).get("byCode", {})

    def local_wind(fraud_max=None, mgmt_min=None):
        out = set()
        for c in IDX["companies"]:
            e = OVERLAY.get(str(c["code"]))
            if not e:
                continue  # 无事件条目 → Wind 档不给分，有门槛时必被排除
            sc = c.get("scores") or {}
            f = _wind_disp(sc.get("fraud"), e.get("fraudDelta"))
            m = _wind_disp(sc.get("mgmt"), e.get("mgmtDelta"))
            if fraud_max is not None and (f is None or f > fraud_max):
                continue
            if mgmt_min is not None and (m is None or m < mgmt_min):
                continue
            out.add(str(c["code"]))
        return out

    for cs in ({"fraud_max": 40}, {"mgmt_min": 60}, {"fraud_max": 40, "mgmt_min": 50}):
        label = "wind&" + "&".join(f"{k}={v}" for k, v in cs.items())
        check(label, local_wind(**cs), api_flt(dict(cs, wind="true")))
    # 溯源字段必须能复原前端显示值，且 fraud 仍是基础分（防“wind 默认开”这种静默口径切换）
    _sample = api_flt_items({"wind": "true", "fraud_max": 60})
    _hit = next((v for v in _sample.values() if v.get("wind_hit") and v.get("fraud") is not None), None)
    if _hit is None:
        print("FAIL wind 样本为空（一条有事件数据且带基础分的公司都没拉到）"); fails += 1
    else:
        _e = OVERLAY.get(str(_hit["code"])) or {}
        _want = _wind_disp(_hit["fraud"], _e.get("fraudDelta"))
        _api_disp = max(0.0, min(100.0, _hit["fraud"] + (_hit["wind_fraud_delta"] or 0)))
        # delta 用容差比而非 ==：0.0 与 None 在两侧语义相同（无增量），不能因类型差异报红
        ok = (abs((_hit["wind_fraud_delta"] or 0) - (_e.get("fraudDelta") or 0)) < 1e-6
              and abs(_api_disp - _want) < 1e-6)
        print(("OK  " if ok else "FAIL") + " wind 档 fraud 仍为基础分且溯源字段可复原显示值"
              + f"({_hit['code']}: {_hit['fraud']} +{_hit['wind_fraud_delta']} → {_want})")
        if not ok:
            fails += 1
else:
    print("SKIP events/index.json 不在，未校验 wind 档")

# 非法键 400
try:
    api_flt({"buys": ["badKey"]})
    print("FAIL badkey no 400"); fails += 1
except urllib.error.HTTPError as e:
    print(("OK  " if e.code == 400 else "FAIL") + f" bad buys key -> {e.code}")
    if e.code != 400:
        fails += 1

# 列表价格参考字段 vs index.json priceRefs(买/保/公 ×4流派 + 清算/净现金)
SCHOOL_COLS = {"grahamAgg": "graham_agg", "grahamDef": "graham_def",
               "schloss": "schloss", "buffett": "buffett"}
ref_bad, ref_n = [], 0
all_items = api_flt_items({})
for c in IDX["companies"]:
    it = all_items.get(c["code"])
    if not it:
        continue
    refs = (c.get("scores") or {}).get("priceRefs") or {}
    pairs = [(refs.get("fairLiq"), it.get("fair_liq")),
             (refs.get("netCashRatio"), it.get("net_cash_ratio"))]
    for k, col in SCHOOL_COLS.items():
        r = refs.get(k) or {}
        for skey, dk in (("buy", "buy_%s"), ("sellCons", "sell_cons_%s"), ("sellFair", "sell_fair_%s")):
            pairs.append((r.get(skey), it.get(dk % col)))
    for a, b in pairs:
        ref_n += 1
        if (a is None) != (b is None) or (a is not None and b is not None and abs(a - b) > 1e-6):
            ref_bad.append((c["code"], a, b))
check(f"价格参考字段一致({ref_n}项)", set(), set(f"{x}" for x in ref_bad) if ref_bad else set())

# 表头排序：前端可发的每个 sort 键都要 200 + 单调有序 + NULL 不占首页。
# 曾经的 bug：列表 COLS 拿流派驼峰键（grahamAgg）当排序键，而后端白名单只有列名，
# 点四派参考价列头直接 400、整表变“加载失败”；前后端键名漂移无人拦截。
sort_keys = (["code", "price", "pe_ttm", "pb", "market_cap", "fair_liq", "net_cash_ratio",
              "fraud", "mgmt", "cycle"]
             + [f"score_{c}" for c in SCHOOL_COLS.values()]
             + [f"{p}_{c}" for c in SCHOOL_COLS.values()
                for p in ("buy", "sell_cons", "sell_fair")])
sort_bad = []
for k in sort_keys:
    for od in ("asc", "desc"):
        url = "/securities?" + urllib.parse.urlencode(
            {"sort": k, "order": od, "page_size": 50})
        vals = [it.get(k) for it in api(url)["items"]]
        nums = [v for v in vals if v is not None]
        if nums != sorted(nums, reverse=(od == "desc")):
            sort_bad.append(f"{k}/{od} 乱序:{nums[:4]}")
        # NULL 必须整体沉底：首个 None 之后不得再出现有值
        first_none = next((i for i, v in enumerate(vals) if v is None), None)
        if first_none is not None and any(v is not None for v in vals[first_none + 1:]):
            sort_bad.append(f"{k}/{od} NULL 排在有值之前")
check(f"表头排序键可用({len(sort_keys)}键×升降)", set(), set(sort_bad) if sort_bad else set())

# 详情透传
d = api("/securities/601899")
ev = d.get("events") or {}
w = (d.get("scores") or {}).get("wind") or {}
print("-- 601899 透传 --")
print("events.name:", ev.get("name"), "| fetched_at:", ev.get("fetched_at"))
print("holders groups:", sorted((ev.get("holders") or {}).keys()))
print("wind keys:", sorted(w.keys()))
src = json.load(io.open(LEGACY_DATA_DIR / "data" / "events" / "index.json", encoding="utf-8"))
orig = src["byCode"]["601899"]
missing = set(orig) - set(w)
print(("OK  " if not missing else "FAIL") + f" wind overlay 字段完整: missing={sorted(missing)}")
if missing:
    fails += 1

print("\nRESULT:", "ALL PASSED" if fails == 0 else f"{fails} FAILED")
