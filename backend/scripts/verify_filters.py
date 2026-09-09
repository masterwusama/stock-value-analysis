# -*- coding: utf-8 -*-
"""P4.5 回归:列表筛选参数 vs 原 index.json 本地语义对答案 + 详情透传字段。

PB 十年分位一档的基线不在 index.json（那是我们自己的产出），而是采集侧的外源产物
data/valuation/latest.json——用它对，才能同时管住取数、回灌与接口三段。

用法(需 API 服务在 :8000 运行): python -X utf8 scripts/verify_filters.py
"""
import datetime as dt
import io
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import LEGACY_DATA_DIR  # 对答案基线 = JSON 工作目录(采集后为 collector/)
from app.api.securities import MIN_PRICE_REF  # 参考值门槛与后端排序表达式同源，不在脚本里重抄

BASE = "http://127.0.0.1:8000/api"
IDX = json.load(io.open(LEGACY_DATA_DIR / "data" / "index.json", encoding="utf-8"))
COLS = {"grahamAgg": "grahamAgg", "grahamDef": "grahamDef", "schloss": "schloss", "buffett": "buffett"}
# 板块前缀(与 app/api/securities.py BOARDS 一致)
BOARDS = {"shMain": ("60",), "szMain": ("00",), "gem": ("30",),
          "star": ("68",), "bj": ("92", "83", "87", "43")}


def api(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.load(r)


def local_flt(fraud_max=None, mgmt_min=None, cap_min=None, cap_max=None, ncr_min=None, ncr_max=None,
              pbp_min=None, pbp_max=None,
              buys=None, sells=None, discount=None,
              market=None, board=None, st=None, industry=None, ex_industry=None):
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
        ind = c.get("industry") or None
        if industry and ind != industry:
            continue
        # 无行业标注的标的不属于任何被排除的行业，必须活下来（后端为此单开 IS NULL 一支）
        if ex_industry and ind is not None and ind in ex_industry:
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
        cap = (c.get("quote") or {}).get("market_cap")
        # 市值门槛：本币元、含边界（与后端 >=/<= 一致），无行情行(NULL)不参与
        if cap_min is not None and (cap is None or cap < cap_min):
            continue
        if cap_max is not None and (cap is None or cap > cap_max):
            continue
        # 净现金/市值门槛：小数比率、含边界，与后端 ScoreDaily.net_cash_ratio 同一列同一单位；
        # 这列算不出来的公司（None）不进区间，和“无行情行不参与市值门槛”同一语义
        ncr = refs.get("netCashRatio")
        if ncr_min is not None and (ncr is None or ncr < ncr_min):
            continue
        if ncr_max is not None and (ncr is None or ncr > ncr_max):
            continue
        # PB 十年分位：值来自外源产物而非 index.json，未覆盖与已置空同样是“无值”，两向门槛都排除
        pbp = VPCT.get("%s:%s" % (c.get("market", "A"), c["code"]))
        if pbp_min is not None and (pbp is None or pbp < pbp_min):
            continue
        if pbp_max is not None and (pbp is None or pbp > pbp_max):
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
    if q.get("ex_industry"):
        q["ex_industry"] = ",".join(q["ex_industry"])
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


# 市值门槛：阈值拿实测市值的中位数/最大值，写死数字会随行情漂移退成空集或全市场
_capped = sorted((c for c in IDX["companies"] if (c.get("quote") or {}).get("market_cap")),
                 key=lambda c: c["quote"]["market_cap"])
CAP_MID = _capped[len(_capped) // 2]["quote"]["market_cap"] if _capped else 1e12
CAP_TOP = _capped[-1]["quote"]["market_cap"] if _capped else 1e12

# 净现金/市值阈值同样现算。取 p50/p90/p99，唯独不取 p75——实测 p75 落在 −0.001 上，
# 正卡在这列的零 crossing 密集区，index.json 与库里差到 1e-6 量级就能把一家顶过边界，
# 集合对账会红在筛选之外的原因上。判空一律 is not None：这列 0 是合法值（现金恰好等于负债）。
_ncrd = sorted((c for c in IDX["companies"]
                if ((c.get("scores") or {}).get("priceRefs") or {}).get("netCashRatio") is not None),
               key=lambda c: c["scores"]["priceRefs"]["netCashRatio"])


def _ncr(p):
    return _ncrd[int(len(_ncrd) * p)]["scores"]["priceRefs"]["netCashRatio"]


NCR_MED, NCR_P90, NCR_P99 = _ncr(0.5), _ncr(0.9), _ncr(0.99)

# PB 十年分位不来自 index.json（那是外源截面，一轮只铺一部分），基线单独读采集产物。
# 空值语义与后端一致：本轮判为不可信的（pct 为 null）与观测日落在最新观测日 -VAL_STALE_DAYS
# 之外的，都当“无值”，设任一门槛即被排除。
VPATH = Path(LEGACY_DATA_DIR) / "data" / "valuation" / "latest.json"
VAL_STALE_DAYS = 45  # 与 app/api/securities.py 同值；这边独立写一份，故意不 import 后端的常量
VPCT = {}
if VPATH.exists():
    _vsrc = json.load(io.open(VPATH, encoding="utf-8"))
    _vdates = [v.get("trade_date") for v in (_vsrc.get("items") or {}).values() if v.get("trade_date")]
    _vtop = max(_vdates) if _vdates else None
    _vfloor = (dt.date.fromisoformat(_vtop) - dt.timedelta(days=VAL_STALE_DAYS)).isoformat() if _vtop else None
    for _k, _it in ((_vsrc.get("items") or {}).items()):
        _p = (_it.get("pb") or {}).get("pct")
        _fresh = _vfloor and _it.get("trade_date") and _it["trade_date"] >= _vfloor
        # 键沿用产物里的 "market:code"：港股是 5 位、A 股 6 位，只用裸代码会互相撞
        VPCT[_k] = _p if (_p is not None and _fresh) else None
else:
    print("SKIP valuation/latest.json 不在，未校验 pbp 档")

# 阈值现算：分位是 0~100 的分布，写死数字会在下一轮铺完后退成空集或近全市场
_pbvals = sorted(v for v in VPCT.values() if v is not None)
if _pbvals:
    PB_MED = _pbvals[len(_pbvals) // 2]
    PB_P90 = _pbvals[int(len(_pbvals) * 0.9)]
    PB_FLOOR = _pbvals[0]

# 行业名一律现取：字典有 123 项且跟着采集源变，写死名字在换版后会退成空集而“测过”
_ind_n = {}
for _c in IDX["companies"]:
    _i = _c.get("industry") or None
    if _i:
        _ind_n[_i] = _ind_n.get(_i, 0) + 1
TOP_IND, TOP_IND2 = sorted(_ind_n, key=_ind_n.get, reverse=True)[:2]
NO_IND = {str(_c["code"]) for _c in IDX["companies"] if not _c.get("industry")}

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
    # 市值区间：单侧 + 双侧 + 与规模维度叠加（A 股专用阈值，不跟港美本币值混比）
    {"cap_min": CAP_MID}, {"cap_max": CAP_MID}, {"cap_min": CAP_MID, "cap_max": CAP_TOP},
    {"cap_min": 5e11, "market": "A"}, {"cap_max": 5e9, "market": "A"},
    {"cap_min": 1e11, "cap_max": 5e11, "st": False, "fraud_max": 40},
    # 净现金/市值区间：单侧 ×2 + 双侧 + 与市场叠加 + 定义分界 0（净现金 vs 净负债，专门捞净负债那侧）
    {"ncr_min": NCR_P90}, {"ncr_max": NCR_MED}, {"ncr_min": NCR_MED, "ncr_max": NCR_P90},
    {"ncr_min": NCR_P99, "market": "US"}, {"ncr_max": 0, "market": "A", "st": False},
    # 行业：单选包含 + 多选排除（含排两个、与市场叠加、包含与排除同一项）
    {"industry": TOP_IND}, {"industry": TOP_IND, "market": "A"},
    {"ex_industry": [TOP_IND]}, {"ex_industry": [TOP_IND, TOP_IND2]},
    {"ex_industry": [TOP_IND], "market": "A"},
    {"ex_industry": [TOP_IND], "st": False, "fraud_max": 40},
    {"industry": TOP_IND, "ex_industry": [TOP_IND]},
]
if _pbvals:
    cases_list += [
        {"pbp_max": PB_MED}, {"pbp_min": PB_P90}, {"pbp_min": PB_FLOOR, "pbp_max": PB_MED},
        {"pbp_max": 5}, {"pbp_max": 20, "market": "A"},
        {"pbp_min": PB_FLOOR, "pbp_max": PB_P90, "cap_min": CAP_MID, "st": False},
    ]
for cs in cases_list:
    label = "&".join(f"{k}={','.join(v) if isinstance(v, list) else v}" for k, v in cs.items())
    check(label, local_flt(**cs), api_flt(cs))

# 门槛边界得闭合（后端把 >= 写成 > 就会掉边界那只，上面按集对比未必命中相等情形）：
# 阈值直接用该券 API 返回的 market_cap 原值，拿库对库，不因 index.json 与库微差误报
if _capped:
    _probe = _capped[len(_capped) // 2]
    _pc, _pm = _probe["code"], _probe.get("market", "A")
    _pv = (api_flt_items({"keyword": _pc, "market": _pm}).get(_pc) or {}).get("market_cap")
    if not _pv:
        print("FAIL 市值边界探针拿不到 market_cap"); fails += 1
    else:
        for _k in ("cap_min", "cap_max"):
            _ok = _pc in api_flt({_k: _pv, "market": _pm})
            print(("OK  " if _ok else "FAIL") + f" 市值边界含等号 {_k}={_pv / 1e8:.2f}亿({_pc})")
            if not _ok:
                fails += 1

# 净现金/市值的等号探针另起一块，不扩用上面那个 for：那块三处都是市值专用（探针取自市值中位数、
# 取的是 market_cap、打印除 1e8），而市值中位数那家完全可能算不出这列比率。
if _ncrd:
    _nprobe = _ncrd[len(_ncrd) // 2]
    _nc, _nm = _nprobe["code"], _nprobe.get("market", "A")
    # 阈值用 API 原值而非列面值：列面按一位小数显示，屏幕上读到的 12.9% 背后可能是 0.128509，
    # 拿 0.129 当门槛会把探针自己筛掉——那是假红。
    _nv = (api_flt_items({"keyword": _nc, "market": _nm}).get(_nc) or {}).get("net_cash_ratio")
    if _nv is None:  # 不能用 not _nv：这列 0 合法，会被当成拿不到值
        print("FAIL 净现金/市值边界探针拿不到 net_cash_ratio"); fails += 1
    else:
        for _k in ("ncr_min", "ncr_max"):
            _ok = _nc in api_flt({_k: _nv, "market": _nm})
            print(("OK  " if _ok else "FAIL") + f" 净现金/市值边界含等号 {_k}={_nv * 100:.2f}%({_nc})")
            if not _ok:
                fails += 1

# PB 十年分位的等号探针同上：阈值取 API 自己返回的分位原值（列面按一位小数显示，
# 屏幕上读到 11.2% 背后可能是 11.2119，拿显示值当门槛会自我排除）。
if _pbvals:
    _bprobe = next((c for c in IDX["companies"]
                    if VPCT.get("%s:%s" % (c.get("market", "A"), c["code"])) is not None), None)
    _bc, _bm = str(_bprobe["code"]), _bprobe.get("market", "A")
    _bv = (api_flt_items({"keyword": _bc, "market": _bm}).get(_bc) or {}).get("pb_pctile")
    if _bv is None:
        print("FAIL PB 分位边界探针拿不到 pb_pctile"); fails += 1
    else:
        for _k in ("pbp_min", "pbp_max"):
            _ok = _bc in api_flt({_k: _bv, "market": _bm})
            print(("OK  " if _ok else "FAIL") + f" PB十年分位边界含等号 {_k}={_bv}({_bc})")
            if not _ok:
                fails += 1
else:
    print("SKIP 估值分位无覆盖行，pbp 档门槛与边界未校验")

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

# 排除类的非法名同样要 400：静默忽略会得到“看着排除了、实际一片没少”的隐形失败。
# 探针用「真名 + 不可能出现在行业名里的后缀」，既保证落在字典外，又不必编一个可能撞名的假名
try:
    api_flt({"ex_industry": [TOP_IND + "×非法名"]})
    print("FAIL bad ex_industry no 400"); fails += 1
except urllib.error.HTTPError as e:
    print(("OK  " if e.code == 400 else "FAIL") + f" bad ex_industry name -> {e.code}")
    if e.code != 400:
        fails += 1

# 非有限浮点不能漏到 SQL：float("NaN") 不抛、pydantic 也认，过去要一路走到 MySQL 驱动才炸成 500。
# 带 ge/le 的参数顺带挡住了 NaN，但净现金/市值刻意不结界、cap_min 只界了下界（inf 照样漏），
# 故三个参数各探两值，期望全部 422 而不是 500。
_nf_bad = []
for _p in ("ncr_min", "ncr_max", "cap_min"):
    for _v in ("NaN", "Infinity"):
        try:
            api(f"/securities?{_p}={_v}&page_size=1")
            _nf_bad.append(f"{_p}={_v} 未被拒")
        except urllib.error.HTTPError as _e:
            if _e.code != 422:
                _nf_bad.append(f"{_p}={_v} -> {_e.code}")
check("非有限浮点在参数层 422（不落到 SQL 变 500）", set(), set(_nf_bad))

# 无行业标注的标的必须活下来：MySQL 的 industry NOT IN (...) 对 NULL 求值为 NULL，
# 少了后端那支 IS NULL，这一批会被整批静默丢掉（集对比也会红，但看不出是这个原因）
if not NO_IND:
    print("WARN index.json 里没有无行业标注的公司，NULL 存活项未真正覆盖")
else:
    _kept = NO_IND & api_flt({"ex_industry": [TOP_IND]})
    _gone = sorted(NO_IND - _kept)
    print(("OK  " if not _gone else "FAIL")
          + f" 排除 {TOP_IND} 后无行业标注的 {len(NO_IND)} 只仍在"
          + ("" if not _gone else f" 丢失={_gone[:8]}"))
    if _gone:
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

# 算不出净现金/市值比率的行（NULL）在两个方向都必须被门槛排除，且不设门槛时它们仍在结果里。
# 期望值拿当次 API 自己返回的空值数算，不拿 index.json 对：后者把“评分行落在 RECENT_DATES 窗口外”
# 的公司也算作有值/无值，会让这条红在筛选之外的原因上。
_n_null = sum(1 for it in all_items.values() if it.get("net_cash_ratio") is None)
_n_exp = len(all_items) - _n_null
_extreme = (("ncr_min", -1e9), ("ncr_max", 1e9))
_n_tots = {k: api("/securities?" + urllib.parse.urlencode({k: v, "page_size": 1}))["total"]
           for k, v in _extreme}
_n_ok = all(t == _n_exp for t in _n_tots.values())
print(("OK  " if _n_ok else "FAIL") + " 净现金/市值门槛两向都排除算不出比率的行: "
      + " ".join(f"{k}={v}->{_n_tots[k]}" for k, v in _extreme)
      + f" 期望={_n_exp}(总 {len(all_items)} − 空值 {_n_null})")
if not _n_ok:
    fails += 1

# PB 十年分位同理：全市场只有铺到的那部分有值，两向门槛都必须把无值行挡在外面。
# 顺带逐家核对字段本身——库里读出来的分位与采集产物不一致，只有 sid 映射错或回灌漏行会造成。
_v_null = sum(1 for it in all_items.values() if it.get("pb_pctile") is None)
_v_exp = len(all_items) - _v_null
_v_extreme = (("pbp_min", 0), ("pbp_max", 100))
_v_tots = {k: api("/securities?" + urllib.parse.urlencode({k: v, "page_size": 1}))["total"]
           for k, v in _v_extreme}
_v_ok = all(t == _v_exp for t in _v_tots.values())
print(("OK  " if _v_ok else "FAIL") + " PB十年分位门槛两向都排除无值的行: "
      + " ".join(f"{k}={v}->{_v_tots[k]}" for k, v in _v_extreme)
      + f" 期望={_v_exp}(总 {len(all_items)} − 空值 {_v_null})")
if not _v_ok:
    fails += 1
_v_bad = []
for _code, _it in all_items.items():
    _want = VPCT.get("%s:%s" % (_it.get("market", "A"), _code))
    _got = _it.get("pb_pctile")
    if (_want is None) != (_got is None) or (_want is not None and abs(_want - _got) > 1e-6):
        _v_bad.append((_code, _want, _got))
print(("OK  " if not _v_bad else "FAIL") + f" pb_pctile 与采集产物逐家一致(空 {len(VPCT) - len(_pbvals)}/有值 {len(_pbvals)})"
      + ("" if not _v_bad else f" 前 5 处={_v_bad[:5]}"))
if _v_bad:
    fails += 1

# 表头排序：前端可发的每个 sort 键都要 200 + 单调有序 + NULL 不占首页。
# 曾经的 bug：列表 COLS 拿流派驼峰键（grahamAgg）当排序键，而后端白名单只有列名，
# 点四派参考价列头直接 400、整表变“加载失败”；前后端键名漂移无人拦截。
sort_keys = (["code", "price", "pe_ttm", "pb", "market_cap", "fair_liq", "net_cash_ratio",
              "pb_pctile", "fraud", "mgmt", "cycle"]
             + [f"score_{c}" for c in SCHOOL_COLS.values()]
             + [f"{p}_{c}" for c in SCHOOL_COLS.values()
                for p in ("buy", "sell_cons", "sell_fair")])
sort_bad = []


def sort_val(it, k):
    """该排序键在这一行上的期望值。

    buy_* 与 fair_liq 排的都是折价率 1 - 现价/参考值（响应里这些字段仍是绝对值），
    故按后端 _discount 同一公式现算，含参考值过低/缺失即 NULL 的那道判定；门槛常量
    直接取后端的 MIN_PRICE_REF，不在这边重抄一遍，免得两边各自改了还互相认为对方错。
    """
    if not (k.startswith("buy_") or k == "fair_liq"):
        return it.get(k)
    ref, price = it.get(k), it.get("price")
    if ref is None or ref < MIN_PRICE_REF or price is None:
        return None
    return 1 - price / ref


for k in sort_keys:
    for od in ("asc", "desc"):
        url = "/securities?" + urllib.parse.urlencode(
            {"sort": k, "order": od, "page_size": 50})
        vals = [sort_val(it, k) for it in api(url)["items"]]
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

# 详情里的估值分位：结构必须齐（date/pe/pb/ps），且与列表接口同一家的 pb_pctile 同值。
# 探针优先挑本轮铺到的那家，否则整块会红在“没数据”而不是“接口错了”上。
_pcode = next(((k.split(":", 1)[1], k.split(":", 1)[0])
               for k, v in VPCT.items() if v is not None), ("601899", "A"))
_dv = api("/securities/%s" % _pcode[0])
_vp = _dv.get("valuationPctile") or {}
_vl = (api_flt_items({"keyword": _pcode[0], "market": _pcode[1]}).get(_pcode[0]) or {}).get("pb_pctile")
_v_ok = bool(_vp.get("date")) and set(_vp) >= {"date", "pe", "pb", "ps"} \
    and (_vp.get("pb") or {}).get("pct") == _vl
print(("OK  " if _v_ok else "FAIL") + f" 详情 valuationPctile 结构齐且与列表同值({_pcode[0]})"
      + ("" if _v_ok else f" 详情={_vp.get('pb')} 列表={_vl}"))
if not _v_ok:
    fails += 1
src = json.load(io.open(LEGACY_DATA_DIR / "data" / "events" / "index.json", encoding="utf-8"))
orig = src["byCode"]["601899"]
missing = set(orig) - set(w)
print(("OK  " if not missing else "FAIL") + f" wind overlay 字段完整: missing={sorted(missing)}")
if missing:
    fails += 1

print("\nRESULT:", "ALL PASSED" if fails == 0 else f"{fails} FAILED")
