# -*- coding: utf-8 -*-
"""掘金台(pick)验证:合成逻辑离线单测 + 在线端点不变量。

用法(在 backend 目录下):
    python -X utf8 -m scripts.verify_pick           # 离线单测(不依赖服务)
    python -X utf8 -m scripts.verify_pick --live    # 追加在线验证(需 uvicorn 已运行)

不变量清单:
- tier 五值封闭;avoid ⇒ pick/channel/reasons 全空;
- strong ⇒ 无 gate、无 ST 高危、fraud≤40、pick≥78、市值≥30 亿 CNY 当量;
- buy(stub 通道)⇒ pick≥55;buy(R 通道)⇒ pick≥65;
- watch ⇒ pick≥45;
- 现价<买点 ⇒ 折价为负;折价越深两锚取越深者;
- 排序 null 恒垫底;分页 total 一致;market/tier 筛选精确。
"""
import sys
import urllib.request
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.pick import (  # noqa: E402
    BUY_MIN, STRONG_CAP_FLOOR, STRONG_MIN, STUB_BUY_MIN, WATCH_MIN,
    _discounts, _expected_annual, _report_late, _synth,
)

PASS = FAIL = 0
# 服务地址:--base= 覆盖(官方栈默认 8000,临时验证可指向其他端口)
BASE = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--base=")),
            "http://127.0.0.1:8000")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def S(**kw):
    """score_daily 行的最小假对象:未给的列一律 None/False。"""
    base = dict(gate=False, gate_flags=None, st_risk=None, st_flags=None,
                fraud=None, trap=None, interim_dip=None,
                recommend=None, value=None, growth=None,
                score_schloss=None, score_graham_def=None,
                div_yield=None, net_cash_ratio=None,
                cyclical=False, cycle_trend=None,
                bm_light=None, bm_pricing=None)
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# 离线单测
# ---------------------------------------------------------------------------

def test_expected_annual():
    check("5月起应有上一年报", _expected_annual(date(2026, 10, 12)) == date(2025, 12, 31))
    check("4月底仍是前年年报", _expected_annual(date(2026, 4, 30)) == date(2024, 12, 31))
    check("5月1日切换", _expected_annual(date(2026, 5, 1)) == date(2025, 12, 31))
    check("1月仍是前年", _expected_annual(date(2026, 1, 1)) == date(2024, 12, 31))


def test_report_late():
    today = date(2026, 10, 12)
    check("不滞后", _report_late(date(2025, 12, 31), today) == 0)
    check("缺一年=12月", _report_late(date(2024, 12, 31), today) == 12)
    check("未来报告期=0", _report_late(date(2026, 6, 30), today) == 0)
    check("空=0", _report_late(None, today) == 0)


def test_discounts():
    ds, dg, best, anchor = _discounts(8.0, 10.0, 12.0)
    check("折价为负", abs(ds + 0.2) < 1e-9 and dg is not None and abs(dg + 1 / 3) < 1e-9)
    check("取最深者", abs(best + 1 / 3) < 1e-9 and anchor == "graham_def")
    ds, dg, best, anchor = _discounts(15.0, 10.0, 12.0)
    check("溢价为正", abs(ds - 0.5) < 1e-9 and abs(dg - 0.25) < 1e-9 and anchor == "graham_def")
    ds, dg, best, anchor = _discounts(5.0, None, 6.0)
    check("单锚可用", ds is None and best is not None and anchor == "graham_def")
    check("双缺返空", _discounts(5.0, None, None) == (None, None, None, None))
    check("买价非正视同缺失", _discounts(5.0, 0, -1) == (None, None, None, None))


def mk(disc_p, disc_g, price=10.0):
    """从折价反推买价,构造 _synth 用的 (disc, anchor) 对。"""
    bs = price / (1 + disc_p) if disc_p is not None else None
    bg = price / (1 + disc_g) if disc_g is not None else None
    return _discounts(price, bs, bg)[2:]


def test_synth_avoid():
    for label, s in [
        ("gate", S(gate=True, gate_flags=["risk_warning"], recommend=70)),
        ("ST高危", S(st_risk=2, recommend=70)),
        ("造假过线", S(fraud=41, recommend=70)),
    ]:
        out = _synth(s, 10.0, 100e8, *mk(-0.2, None), pb_pctile=10, report_late=0)
        check(f"回避:{label}", out["tier"] == "avoid" and out["pick"] is None
              and out["channel"] is None and out["reasons"] == []
              and len(out["risks"]) >= 1, str(out))


def test_synth_tiers():
    # 强推:高分 R + 深折价 + 无任何灰带
    out = _synth(S(recommend=80, value=80, growth=80), 10.0, 100e8, *mk(-0.15, None),
                 pb_pctile=10, report_late=0)
    check("强推成立", out["tier"] == "strong" and out["pick"] >= STRONG_MIN, str(out["pick"]))
    # pick≥78 但无过硬项(无折价/分位/股息)→ 降 buy
    out = _synth(S(recommend=80, value=80, growth=80), 10.0, 100e8, *mk(None, None),
                 pb_pctile=None, report_late=0)
    check("无过硬项降buy", out["tier"] == "buy", out["tier"])
    # 关注旗压过强推
    out = _synth(S(recommend=80, value=80, growth=80, st_risk=1, st_flags="price_low"),
                 10.0, 100e8, *mk(-0.15, None), pb_pctile=10, report_late=0)
    check("ST关注不得强推", out["tier"] != "strong", out["tier"])
    # 市值不足不得强推
    out = _synth(S(recommend=80, value=80, growth=80), 10.0, 10e8, *mk(-0.15, None),
                 pb_pctile=10, report_late=0)
    check("小市值不得强推", out["tier"] != "strong", out["tier"])
    # 港股市值折算后够强推线
    out = _synth(S(recommend=80, value=80, growth=80), 10.0,
                 STRONG_CAP_FLOOR / 0.92, *mk(-0.15, None), pb_pctile=10, report_late=0)
    check("港股市值当量生效", out["tier"] == "strong", out["tier"])
    # 烟蒂通道:无 R,施洛斯分高 + 深折价
    out = _synth(S(score_schloss=85), 10.0, 100e8, *mk(-0.2, None), pb_pctile=None, report_late=0)
    check("烟蒂入围", out["channel"] == "stub" and out["pick"] is not None
          and out["pick"] <= 65, str(out))
    # 烟蒂折价不够深 → 不入围
    out = _synth(S(score_schloss=85), 10.0, 100e8, *mk(-0.05, None), pb_pctile=None, report_late=0)
    check("浅折价不入烟蒂", out["channel"] is None and out["pick"] is None, str(out))
    # 灰带扣分:fraud 30 → −4;trap 15 → −5
    base = _synth(S(recommend=70, value=70, growth=70), 10.0, 100e8, *mk(None, None),
                  pb_pctile=None, report_late=0)
    gray = _synth(S(recommend=70, value=70, growth=70, fraud=30, trap=15),
                  10.0, 100e8, *mk(None, None), pb_pctile=None, report_late=0)
    check("灰带扣分方向", gray["pick"] < base["pick"],
          f"{gray['pick']} vs {base['pick']}")
    # 年报滞后 >15 月扣更多
    late = _synth(S(recommend=70, value=70, growth=70), 10.0, 100e8, *mk(None, None),
                  pb_pctile=None, report_late=16)
    check("滞后扣分", late["pick"] < base["pick"], str(late["pick"]))
    # 理由可解释性:强推至少 2 条理由
    out = _synth(S(recommend=80, value=80, growth=80, div_yield=0.05),
                 10.0, 100e8, *mk(-0.15, None), pb_pctile=10, report_late=0)
    check("理由>=2", len(out["reasons"]) >= 2, str(out["reasons"]))


# ---------------------------------------------------------------------------
# 在线验证(服务已在运行时)
# ---------------------------------------------------------------------------

def get(url):
    from urllib.parse import quote
    with urllib.request.urlopen(BASE + quote(url, safe="/?&=.,%"), timeout=30) as r:
        return __import__("json").loads(r.read().decode("utf-8"))


def test_live():
    stars = get("/api/pick/stars?limit=20")
    meta, stats = stars["meta"], stars["stats"]
    check("meta日期齐全", bool(meta["trade_date"]) and bool(meta["quote_date"]))
    check("统计覆盖A+HK", set(stats) == {"A", "HK"})
    total = sum(m["strong"] + m["buy"] + m["watch"] + m["na"] + m["avoid"]
                for m in stats.values())
    check("统计加总=count", total == meta["count"], f"{total} vs {meta['count']}")

    for it in stars["stars"]:
        tag = f"{it['code']} {it['name']}"
        check(f"强推 {tag} 无gate", not it["gate"] and it["st_risk"] != 2
              and (it["fraud"] is None or it["fraud"] <= 40))
        check(f"强推 {tag} 分数线", it["pick"] is not None and it["pick"] >= STRONG_MIN)
        cap_cny = (it["market_cap"] or 0) * (0.92 if it["market"] == "HK" else 1)
        check(f"强推 {tag} 规模", cap_cny >= STRONG_CAP_FLOOR)
        check(f"强推 {tag} 有理由", len(it["reasons"]) >= 1)

    d = get("/api/pick/board?page_size=200&sort=pick&order=desc")
    check("board分页", len(d["items"]) == 200 and d["total"] >= 200)
    picks = [it["pick"] for it in d["items"] if it["pick"] is not None]
    check("pick降序单调", picks == sorted(picks, reverse=True))
    tail = [it for it in d["items"] if it["pick"] is None]
    check("null垫底", all(d["items"].index(it) >= len(picks) for it in tail))

    tiers = {"strong", "buy", "watch", "na", "avoid"}
    all_ok = True
    for it in d["items"]:
        if it["tier"] not in tiers:
            all_ok = False
        if it["tier"] == "avoid" and it["pick"] is not None:
            all_ok = False
        if it["tier"] == "buy" and it["channel"] == "stub" and (it["pick"] or 0) < STUB_BUY_MIN:
            all_ok = False
        if it["tier"] == "buy" and it["channel"] == "R" and (it["pick"] or 0) < BUY_MIN:
            all_ok = False
        if it["tier"] == "watch" and (it["pick"] or 0) < WATCH_MIN:
            all_ok = False
        if it["price"] and it["buy_schloss"] and it["discount_schloss"] is not None:
            sign = -1 if it["price"] < it["buy_schloss"] else 1
            if sign * it["discount_schloss"] < 0:
                all_ok = False
    check("200样本不变量", all_ok)

    hk = get("/api/pick/board?market=HK&page_size=50")
    check("market筛选", all(it["market"] == "HK" for it in hk["items"]) and hk["total"] > 0)
    two = get("/api/pick/board?tier=strong,buy&page_size=50")
    check("tier复选", all(it["tier"] in ("strong", "buy") for it in two["items"]))

    import urllib.error
    try:
        get("/api/pick/board?tier=stub")
        check("非法tier=400", False)
    except urllib.error.HTTPError as e:
        check("非法tier=400", e.code == 400)

    kw = get("/api/pick/board?keyword=宏桥")
    check("keyword命中", kw["total"] >= 1 and all(
        "宏桥" in it["name"] or "宏桥" in str(it.get("research") or "") for it in kw["items"]))


if __name__ == "__main__":
    test_expected_annual()
    test_report_late()
    test_discounts()
    test_synth_avoid()
    test_synth_tiers()
    if "--live" in sys.argv:
        test_live()
    print(f"verify_pick: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
