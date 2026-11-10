# -*- coding: utf-8 -*-
"""小微盘投机池(microcap)验证:排雷分级/热度打分离线单测 + 在线端点不变量。

用法(在 backend 目录下):
    python -X utf8 -m scripts.verify_microcap           # 离线单测(不依赖服务)
    python -X utf8 -m scripts.verify_microcap --live    # 追加在线验证(需 uvicorn 已运行)

不变量清单:
- 分级四值封闭(0~3);简称含 ST ⇒ level=3;年报触线(亏损+营收<分板线)⇒ level=3;
- 双创上市<4 完整会计年度触线 ⇒ 降级关注(loss_rev_ex)且不为 3;
- level=0 ⇒ 无任何旗;level=1 ⇒ 只含关注级旗;
- 年报净资产负(gate)⇒ 3;仅最新披露期转负 ⇒ 2 且不 3;
- 面值/市值/审计/立案/近线 2 倍 ⇒ ≥2;
- heat_score ∈ [0,100];可用权重不足 ⇒ None(判不动≠0);全满因子 ⇒ 封顶 100;
- 池界:微盘<25 亿 ≤ 小票<60 亿;北交所/次新不在池;
- 排序 null 恒垫底;分页 total 一致;pool/risk/band/safe_only 筛选精确。
"""
import json
import sys
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.microcap import (  # noqa: E402
    HEAT_BANDS, MICRO_CAP_MAX, SMALL_CAP_MAX, _calendar, _classify, heat_band, heat_score,
)

PASS = FAIL = 0
BASE = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--base=")),
            "http://127.0.0.1:8000")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def mk(**kw):
    """_classify 的最小入参:干净公司打底,按需覆盖。"""
    base = dict(name="某某股份", code="000123", annual_np=5e7, annual_ded=4e7,
                annual_rev=10e8, annual_debt=0.45, list_date=date(2015, 1, 1),
                annual_year=2025, prev_min_np=3e7, latest_np=1e7,
                latest_is_annual=False, latest_eq=8e8, gate_flags=[],
                mcap=20e8, avg20=8.0, price=8.0, industry="化工")
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# 离线单测
# ---------------------------------------------------------------------------

def test_classify_levels():
    check("干净=0无旗", _classify(mk()) == (0, []))
    lv, fl = _classify(mk(name="ST 某某"))
    check("简称ST=3", lv == 3 and "st_named" in fl, str(fl))
    lv, fl = _classify(mk(name="*ST某某"))
    check("星头ST=3", lv == 3, str(fl))
    lv, fl = _classify(mk(annual_np=-5e7, annual_ded=-6e7, annual_rev=1.5e8))
    check("主板亏损+营收<3亿线=3", lv == 3 and "loss_rev_hit" in fl, str(fl))
    lv, fl = _classify(mk(annual_np=-5e7, annual_ded=-6e7, annual_rev=4e8))
    check("主板亏损+营收3~6亿<2倍线=2", lv == 2 and "loss_rev_near" in fl, str(fl))
    lv, fl = _classify(mk(annual_np=-5e7, annual_ded=-6e7, annual_rev=10e8))
    check("亏损但营收在线外=1关注", lv == 1 and "year_loss" in fl, str(fl))
    lv, fl = _classify(mk(annual_np=-5e7, annual_ded=-6e7, annual_rev=10e8,
                          prev_min_np=-3e7))
    check("连续两年亏叠加loss2y", lv == 1 and "loss2y" in fl, str(fl))
    lv, fl = _classify(mk(gate_flags=["neg_equity"]))
    check("年报资不抵债=3", lv == 3 and "neg_equity" in fl, str(fl))
    lv, fl = _classify(mk(latest_eq=-1e7))
    check("仅最新期转负=2", lv == 2 and "negeq_soft" in fl, str(fl))
    lv, fl = _classify(mk(gate_flags=["neg_equity"], latest_eq=-1e7))
    check("年报负+期负不重复挂soft旗", "negeq_soft" not in fl and lv == 3, str(fl))
    lv, fl = _classify(mk(gate_flags=["audit_qualify"]))
    check("审计非标=2", lv == 2 and "audit_qualify" in fl, str(fl))
    lv, fl = _classify(mk(gate_flags=["case_filed"]))
    check("立案=2", lv == 2 and "case_filed" in fl, str(fl))
    lv, fl = _classify(mk(avg20=1.3))
    check("20日均价<1.5=2面值", lv == 2 and "face_value" in fl, str(fl))
    lv, fl = _classify(mk(mcap=5e8))
    check("主板市值5亿<6亿线=2", lv == 2 and "mcap_thin" in fl, str(fl))
    lv, fl = _classify(mk(code="300999", mcap=3.5e8))
    check("双创市值3.5亿<4亿线=2", lv == 2 and "mcap_thin" in fl, str(fl))
    lv, fl = _classify(mk(annual_debt=0.95))
    check("负债率95%=1关注", lv == 1 and "debt90" in fl, str(fl))
    lv, fl = _classify(mk(annual_debt=0.95, industry="货币金融服务"))
    check("金融业高杠杆不罚", "debt90" not in fl, str(fl))
    lv, fl = _classify(mk(latest_np=-2e7, latest_is_annual=False))
    check("最新期转亏=1", lv == 1 and "q_turn_loss" in fl, str(fl))
    lv, fl = _classify(mk(latest_np=-2e7, latest_is_annual=True))
    check("年报期本身亏不算转亏旗", "q_turn_loss" not in fl, str(fl))


def test_classify_exemption():
    # 双创上市不满 4 个完整会计年度:触线降关注(科创板线 1 亿,营收须 <1 亿才算触线)
    lv, fl = _classify(mk(code="688123", annual_np=-5e7, annual_ded=-6e7,
                          annual_rev=0.8e8, list_date=date(2024, 8, 1),
                          annual_year=2025))
    check("科创板豁免窗触线降1", lv == 1 and "loss_rev_ex" in fl and "loss_rev_hit" not in fl,
          str(fl))
    # 满 4 年:照旧 3
    lv, fl = _classify(mk(code="688123", annual_np=-5e7, annual_ded=-6e7,
                          annual_rev=0.8e8, list_date=date(2020, 8, 1),
                          annual_year=2025))
    check("科创板满4年触线=3", lv == 3 and "loss_rev_hit" in fl, str(fl))
    # 豁免窗+已戴帽:确定性事实压过豁免
    lv, _ = _classify(mk(code="300001", name="ST某某", annual_np=-5e7,
                         annual_ded=-6e7, annual_rev=1.5e8,
                         list_date=date(2024, 8, 1), annual_year=2025))
    check("豁免窗但已ST=3", lv == 3)


def test_heat_score():
    full = dict(inflow_strength=0.01, inflow_d5_strength=0.04, turn_pctile=0.8,
                streak=1, r5=0.05, r20=0.10, hot_concept=True)
    q_only = heat_score({**full, "inflow_strength": None, "inflow_d5_strength": None,
                         "hot_concept": False})
    s = heat_score(full)
    check("界内", 0 <= s <= 100 and 0 <= q_only <= 100, f"{s}/{q_only}")
    # 缺资金列 ≠ 判 0:归一化下缺列按可用权重计,不惩罚数据缺失;
    # 强资金流入则必须跑赢纯行情孪生(弱资金稀释分数是设计语义)
    strong = heat_score({**full, "inflow_strength": 0.05, "inflow_d5_strength": 0.12})
    check("强资金跑赢纯行情", strong > q_only, f"{strong} vs {q_only}")
    check("缺资金不冒0:等于纯行情自身", s is not None and q_only is not None)
    check("满因子封顶100", heat_score(dict(inflow_strength=0.05, inflow_d5_strength=0.12,
                                           turn_pctile=1.0, streak=3, r5=0.15,
                                           r20=0.30, hot_concept=True)) == 100.0)
    check("纯行情因子仍有分", q_only is not None and q_only > 0, str(q_only))
    check("判不动=None", heat_score({"turn_pctile": None, "r5": None, "r20": None,
                                     "streak": None, "inflow_strength": None,
                                     "inflow_d5_strength": None,
                                     "hot_concept": False}) is None)
    check("负流入0分不越界", 0 <= heat_score({**full, "inflow_strength": -0.2,
                                             "inflow_d5_strength": -0.5,
                                             "streak": 0, "r5": -0.2, "r20": -0.4}))
    check("band三档", heat_band(80) == "hot" and heat_band(60) == "warm"
          and heat_band(30) == "cool" and heat_band(None) is None)
    check("band常量有序", HEAT_BANDS == ((70.0, "hot"), (50.0, "warm")))


def test_calendar():
    cal = _calendar(date(2026, 11, 10), None, 0.999)
    check("11月=出清窗口", cal["state"] == "cleared")
    check("上一窗=三季报", cal["last_label"] == "2026三季报", str(cal))
    check("下一窗=年报", cal["next_label"] == "2026年报+2027一季报")
    cal = _calendar(date(2026, 4, 15), 0.5, None)
    check("4月中=雷暴进行中", cal["state"] == "revealing" and cal["progress"] == 0.5)
    check("4月窗报告期=上一年报", cal["period"] == "2025-12-31")
    cal = _calendar(date(2026, 4, 15), 0.99, None)
    check("进度≥95%=出清近尾声", cal["state"] == "cleared")
    cal = _calendar(date(2026, 10, 20), 0.4, None)
    check("10月中=三季报雷暴", cal["period"] == "2026-09-30" and cal["state"] == "revealing")
    cal = _calendar(date(2026, 6, 15), None, None)
    check("6月=出清(上一窗年报)", cal["last_label"] == "2025年报+2026一季报")


# ---------------------------------------------------------------------------
# 在线验证
# ---------------------------------------------------------------------------

def get(url):
    from urllib.parse import quote
    with urllib.request.urlopen(BASE + quote(url, safe="/?&=.,%"), timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def test_live():
    ov = get("/api/microcap/overview")
    check("overview池子非空", ov["pools"]["micro"] > 0 and ov["pools"]["small"] > 0,
          str(ov["pools"]))
    check("风险分布加总=池子", sum(ov["risk_dist"].values())
          == ov["pools"]["micro"] + ov["pools"]["small"])
    check("日历字段齐", "state" in ov["calendar"] and "label" in ov["calendar"])
    check("热榜字段齐", isinstance(ov["heat"].get("boards"), list))

    d = get("/api/microcap/board?page_size=200&sort=heat&order=desc")
    check("board有数据", d["total"] > 0, str(d["total"]))
    heats = [it["heat"] for it in d["items"] if it["heat"] is not None]
    check("heat降序单调", heats == sorted(heats, reverse=True))
    tail = [i for i, it in enumerate(d["items"]) if it["heat"] is None]
    check("null垫底", not tail or min(tail) >= len(heats))

    micro = get("/api/microcap/board?pool=micro&page_size=200")
    check("微盘池界<25亿", micro["total"] > 0 and all(
        (it["market_cap"] or 0) < MICRO_CAP_MAX and it["pool"] == "micro"
        for it in micro["items"]))
    small = get("/api/microcap/board?pool=small&page_size=200")
    check("小票池界25-60亿", small["total"] > 0 and all(
        MICRO_CAP_MAX <= (it["market_cap"] or 0) < SMALL_CAP_MAX
        for it in small["items"]))
    check("北交所不进池", all(
        it["code"][:2] not in ("43", "82", "83", "87", "88", "92")
        for it in micro["items"] + small["items"]))

    safe = get("/api/microcap/board?safe_only=true&page_size=200")
    check("safe_only全0级", all(it["risk_level"] == 0 for it in safe["items"]))
    r2 = get("/api/microcap/board?risk=2,3&page_size=200")
    check("risk复选≥2", all(it["risk_level"] >= 2 for it in r2["items"]) and r2["total"] > 0)
    st_rows = [it for it in r2["items"] if "ST" in it["name"].upper()]
    if st_rows:
        check("简称ST必为3级", all(it["risk_level"] == 3 for it in st_rows),
              str([(it["name"], it["risk_level"]) for it in st_rows[:3]]))
    hot = get("/api/microcap/board?band=hot&page_size=200")
    check("band筛选", all(it["heat"] is not None and it["heat"] >= 70 for it in hot["items"]))
    kw = get("/api/microcap/board?keyword=000001&page_size=20")
    check("keyword命中代码", all("000001" in it["code"] for it in kw["items"]))

    p2 = get("/api/microcap/board?page_size=5&page=2")
    check("分页total一致", p2["total"] == d["total"] and len(p2["items"]) <= 5)

    # 作战面板与快捷预设(2026-11-11)
    check("battle字段齐", all(k in ov["battle"] for k in ("premium_hint", "trap", "zt_yd", "ladder")),
          str(ov.get("battle", {}).keys()))
    lad = ov["battle"]["ladder"]
    check("梯队自洽", isinstance(lad["dist"], dict)
          and sum(lad["dist"].values()) == lad["total"]
          and (lad["max"] == 0 or str(lad["max"]) in lad["dist"]), str(lad))
    check("打法提示非空", bool(ov["battle"]["premium_hint"]), str(ov["battle"]["premium_hint"]))
    ztf = get("/api/microcap/board?zt=true&page_size=200")
    check("zt预设全涨停(空集放行)", ztf["total"] == 0
          or all(it["streak"] is not None for it in ztf["items"]))
    brk = get("/api/microcap/board?broke=true&page_size=200")
    check("断板预设", brk["total"] == 0
          or all(it["zt_yesterday"] and it["streak"] is None for it in brk["items"]))
    trn = get("/api/microcap/board?turn=true&page_size=50")
    check("扭亏预设非空且全命中", trn["total"] > 0
          and all(it["turn_profit"] for it in trn["items"]))
    if trn["items"]:
        t0 = trn["items"][0]
        check("扭亏样本含雷区或ST底", t0["risk_level"] >= 1 or "ST" in t0["name"].upper(),
              f"{t0['name']} risk={t0['risk_level']}")

    # 年历层+分布校准+跌停侧(课程规律层,2026-11-11)
    rg = ov["battle"]["regime"]
    check("月历字段齐", all(k in rg for k in ("month", "label", "advice", "small_ok")), str(rg))
    check("月历口径合法", rg["label"] in ("极弱", "极强", "大震", "长征初", "长征", "长征末", "博弈")
          and isinstance(rg["small_ok"], bool))
    cal = ov["battle"]["calibration"]
    check("校准字段齐", all(k in cal for k in ("median_chg", "expect_red", "actual_red")))
    check("预期红盘率界内", cal["expect_red"] is None or 0 <= cal["expect_red"] <= 1,
          str(cal["expect_red"]))
    ds = ov["battle"]["dt_side"]
    check("跌停侧自洽", ds["oneword"] <= ds["n"] and isinstance(ds["negative_spiro"], bool), str(ds))
    check("负螺旋口径=一字≥3", ds["negative_spiro"] == (ds["oneword"] >= 3))
    check("负螺旋分级口径", ds["level"] == (2 if ds["oneword"] >= 5 else 1 if ds["oneword"] >= 3 else 0),
          str(ds))
    lad2 = ov["battle"]["ladder"]
    trail = lad2.get("trail") or []
    check("高度轨迹≤15且界内", len(trail) <= 15 and all(isinstance(h, int) and h >= 0 for h in trail),
          str(trail))
    if trail:
        check("轨迹末位=当日最高板", trail[-1] == lad2["max"], f"{trail[-1]} vs {lad2['max']}")


if __name__ == "__main__":
    test_classify_levels()
    test_classify_exemption()
    test_heat_score()
    test_calendar()
    if "--live" in sys.argv:
        test_live()
    print(f"verify_microcap: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
