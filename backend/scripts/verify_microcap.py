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
    DANGER_CAP, HEAT_BANDS, MICRO_CAP_MAX, SMALL_CAP_MAX, _ACT_KEYS, _CK_TIMES, _calendar,
    _classify, danger_score, heat_band, heat_score, plan_checkpoints, plan_moves,
    spec_band, spec_score,
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


def test_danger_spec():
    # 干净温票:几乎无旗
    s, fl = danger_score({"risk_level": 0, "r5": 0.05, "r20": 0.15,
                          "float_mcap": 30e8, "turn_pctile": 0.7, "heat": 80})
    check("干净票danger≈0", s <= 5 and not fl, f"{s}/{fl}")
    # 雷上加热:多旗累加封顶
    s, fl = danger_score({"risk_level": 3, "streak": 6, "r20": 0.6, "r5": 0.25,
                          "float_mcap": 8e8, "turn_pctile": 0.99, "heat": 95,
                          "zha_flag": True})
    check("雷上加热封顶95", s == int(DANGER_CAP) and len(fl) >= 6, f"{s}/{fl}")
    check("确定雷必挂mine3", "mine3" in fl)
    # 分级信号各自可判
    s, fl = danger_score({"risk_level": 0, "zt_yesterday": True, "float_mcap": 25e8})
    check("断板旗", "broke" in fl and s >= 10, f"{s}/{fl}")
    s, fl = danger_score({"risk_level": 0, "dt_today": True, "float_mcap": 25e8})
    check("跌停旗≥20", s >= 20 and "dt_now" in fl, f"{s}/{fl}")
    s, fl = danger_score({"risk_level": 0, "float_mcap": 25e8, "streak": 5})
    check("高位连板旗", "space_high" in fl and s >= 25, f"{s}/{fl}")
    # 乘法语义
    check("spec=heat×(100-d)/100", spec_score(90, 10) == 81.0 and spec_score(90, 60) == 36.0)
    check("heat判不动spec=None", spec_score(None, 10) is None)
    check("同热度危险高spec必低", spec_score(80, 40) < spec_score(80, 5))
    check("冷而安全不冒高", spec_score(10, 0) < 45)
    check("band三档", spec_band(70) == "trade" and spec_band(50) == "watch"
          and spec_band(20) == "rest" and spec_band(None) is None)


def test_plan_moves():
    LV = {"ma5": 31.79, "ma10": 31.09, "lo20": 27.57, "hi20": 31.94, "shrink": 3.1,
          "surge": 6.7, "tma5": 4.5}
    import re
    for args, want_arch in [((3, 0.4, True, LV, True), "board"), ((None, 0.05, True, LV, True), "low"),
                            ((None, 0.3, False, LV, True), "base"), ((1, 0.2, None, LV, True), "board")]:
        arch, moves, sizing, auc = plan_moves(*args)
        check(f"形态判定{want_arch}→{want_arch}", arch == want_arch, arch)
        check(f"预案{arch}条数4~9", 4 <= len(moves) <= 9, str(len(moves)))
        check(f"sizing含仓位阶梯", "首仓" in sizing and "独立止损" in sizing)
        for m in moves:
            if m["k"] not in _ACT_KEYS:
                check(f"动作键合法{m['k']}", False, str(m))
            if not m["t"] or not m["x"]:
                check("预案字段非空", False, str(m))
            # 加仓类指引必须可执行:t+x 含数字(价位/量能/仓位/时间)——防回到定性模糊话
            if m["k"] in ("dip", "add") and not re.search(r"\d", m["t"] + m["x"]):
                check(f"加仓指引含数字({m['t']})", False, str(m))
        # 竞价锚:tma5 在阈值必出且单调(强>枯竭)、含绝对地板
        if args[2] is not None:
            check(f"竞价锚{arch}", auc is not None and auc["strong"] > auc["weak"]
                  and auc["strong"] >= 0.05 and auc["weak"] >= 0.01, str(auc))
            check("竞价锚占日均比口径", abs(auc["strong"] - round(max(4.5 * 0.15, 0.05), 2)) < 0.01,
                  str(auc))
            check("竞价锚三行", len(auc["rows"]) == 3
                  and all(re.search(r"\d", r["t"]) and r["x"] for r in auc["rows"]))
            check("竞价行键合法", all(r["k"] in _ACT_KEYS for r in auc["rows"]))
    # 冷票地板:tma5=0.1 时强承接=0.05(绝对地板生效,占比纲)
    _, _, _, auc_cold = plan_moves(None, 0.05, True, {**LV, "tma5": 0.1}, True)
    check("冷票竞价地板", auc_cold["strong"] == 0.05 and auc_cold["weak"] == 0.01, str(auc_cold))
    # 热票系数生效:tma5=10 时强=1.5/弱=0.3(占日均15%/3%)
    _, _, _, auc_hot = plan_moves(None, 0.05, True, {**LV, "tma5": 10}, True)
    check("热票竞价系数生效", auc_hot["strong"] == 1.5 and auc_hot["weak"] == 0.3, str(auc_hot))
    # tma5 缺失:auction=None 不冒充
    _, _, _, auc_none = plan_moves(None, 0.05, True, {k: v for k, v in LV.items() if k != "tma5"}, True)
    check("无量能基准无竞价锚", auc_none is None)
    # 月历门控:防守月只减不加;长征月非主线不加;主线可加
    _, mv_strict, _, _ = plan_moves(None, 0.05, False, LV, True)
    _, mv_march_no, _, _ = plan_moves(None, 0.05, None, LV, False)
    _, mv_march_yes, _, _ = plan_moves(None, 0.05, None, LV, True)
    check("防守月无dip/add", not any(m["k"] in ("dip", "add") for m in mv_strict))
    check("防守月门控文案", any("只减不加" in m["x"] for m in mv_strict))
    check("长征月非主线无dip/add", not any(m["k"] in ("dip", "add") for m in mv_march_no))
    check("长征月主线可低吸", any(m["k"] == "dip" for m in mv_march_yes))
    # 从严参数:防守月破5日线=clear,其余=half
    _, mv_strict, _, _ = plan_moves(None, 0.05, False, LV, True)
    _, mv_norm, _, _ = plan_moves(None, 0.3, True, LV, True)
    s5 = next(m for m in mv_strict if "5日线" in m["t"] and "破" in m["t"])
    n5 = next(m for m in mv_norm if "5日线" in m["t"] and "破" in m["t"])
    check("防守月破5日=清仓", s5["k"] == "clear", s5["k"])
    check("极强月破5日=减半", n5["k"] == "half", n5["k"])
    # 连板型有断板与加仓窗口(原则不加),低位型有回踩低吸与突破加仓
    _, mv_b, _, _ = plan_moves(2, 0.4, True, LV, True)
    _, mv_l, _, _ = plan_moves(None, 0.04, True, LV, True)
    check("连板型含断板", any("断板" in m["t"] for m in mv_b))
    check("连板型含加仓窗口(原则不加)", any("加仓窗口" in m["t"] and "原则上不加" in m["x"] for m in mv_b))
    check("低位型回踩低吸带区间数字", any("回踩" in m["t"] and "31" in m["t"] and m["k"] == "dip" for m in mv_l))
    check("低位型突破加仓带量能数字", any("站上前高" in m["t"] and "6.7" in m["t"] and m["k"] == "add" for m in mv_l))


def test_checkpoints():
    LV = {"ma5": 31.79, "ma10": 31.09, "lo20": 27.57, "hi20": 31.94,
          "shrink": 3.1, "surge": 6.7, "tma5": 4.5}
    auc = {"strong": 0.68, "weak": 0.14, "tma5": 4.5, "rows": []}
    import re
    for arch in ("low", "base"):
        cks = plan_checkpoints(arch, LV, auc)
        check(f"{arch}四时点齐", [c["time"] for c in cks] == list(_CK_TIMES),
              str([c["time"] for c in cks]))
        check(f"{arch}每时点2~5行", all(2 <= len(c["rows"]) <= 5 for c in cks))
        # 条件覆盖完备性(2026-11-12 审计补):9:30 须含一字/高开>7 与低开2~5 的归属,
        # 10:00 须含「站上开盘价但破5日线」中间态,14:30 须含常态持有,14:55 须含弱势收盘
        by_t = {c["time"]: " ".join(r["c"] for r in c["rows"]) for c in cks}
        check(f"{arch}9:30覆盖一字高开", "高开>7%" in by_t["9:30"])
        check(f"{arch}9:30覆盖低开<5", "低开<5%" in by_t["9:30"])
        check(f"{arch}10:00覆盖中间态", "已破" in by_t["10:00"])
        check(f"{arch}14:30覆盖常态持有", "常态" in by_t["14:30"])
        check(f"{arch}14:55覆盖弱势收盘", "下方" in by_t["14:55"])
        for ck in cks:
            for r in ck["rows"]:
                if not (r["c"] and r["n"] and r["h"]):
                    check(f"{arch}字段非空@{ck['time']}", False, str(r))
                # 每行至少一处(条件或动作)含数字——兜底分支(仍低于开盘价/尾盘跳水)条件
                # 本身定性,但动作列须可执行;全部无数字即视为模糊话
                if not any(re.search(r"\d", r[k]) for k in ("c", "n", "h")):
                    check(f"{arch}行含数字@{ck['time']}", False, str(r))
        # 关键价位在场:9:30有竞价阈值、10:00有5日线、14:30有前高或5日、14:55有次日竞价逻辑
        all_c = " ".join(r["c"] for ck in cks for r in ck["rows"])
        all_n = " ".join(r["n"] for ck in cks for r in ck["rows"])
        check(f"{arch}条件含5日线", "31.79" in all_c, all_c[:80])
        check(f"{arch}尾盘吃次日竞价", "次日竞价" in all_n, all_n[:120])
    cks_noauc = plan_checkpoints("low", LV, None)
    check("无量能锚时9:30退定性", all(r["c"] and r["n"] for r in cks_noauc[0]["rows"]))


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
    # 量纲不变量:median_chg 是比率(涨跌幅单日不可能 >50%)
    check("池内中位量纲=比率", cal["median_chg"] is None or abs(cal["median_chg"]) <= 0.5,
          str(cal["median_chg"]))
    ds = ov["battle"]["dt_side"]
    check("跌停侧自洽", ds["oneword"] <= ds["n"] and isinstance(ds["negative_spiro"], bool), str(ds))
    check("负螺旋口径=一字≥3", ds["negative_spiro"] == (ds["oneword"] >= 3))
    check("负螺旋分级口径", ds["level"] == (2 if ds["oneword"] >= 5 else 1 if ds["oneword"] >= 3 else 0),
          str(ds))
    sp = ov["spec"]
    check("投机分布字段齐", all(k in sp for k in ("trade", "watch", "rest", "danger_median")),
          str(sp))
    check("可上手≤观察≤看戏加总合理", sp["trade"] + sp["watch"] + sp["rest"]
          <= ov["pools"]["micro"] + ov["pools"]["small"], str(sp))
    sb = get("/api/microcap/board?spec_band=trade&page_size=200&sort=spec")
    check("spec_band=trade全≥65", sb["total"] == 0
          or all((it["spec"] or 0) >= 65 and it["spec_band"] == "trade" for it in sb["items"]))
    dg = get("/api/microcap/board?page_size=100&sort=danger&order=desc")
    dgs = [it["danger"] for it in dg["items"] if it["spec"] is not None]
    check("危险度降序单调", dgs == sorted(dgs, reverse=True))
    sps = [it["spec"] for it in get("/api/microcap/board?page_size=100&sort=spec&order=desc")["items"]
           if it["spec"] is not None]
    check("投机分降序单调", sps == sorted(sps, reverse=True))
    ok3 = all((it["danger"] >= 30) for it in dg["items"] if it["risk_level"] == 3)
    check("确定级危险度≥30", ok3, str([(i['name'], i['danger']) for i in dg['items'] if i['risk_level'] == 3][:3]))

    # 复盘·预案端点(2026-11-11)
    pl = get("/api/microcap/plan")
    check("plan四段齐", all(k in pl for k in ("review", "header", "plans", "cheatsheet")))
    check("速查表≥8行", len(pl["cheatsheet"]) >= 8, str(len(pl["cheatsheet"])))
    check("头注含仓位提示", bool(pl["header"]["position_hint"]))
    if pl["review"]["available"]:
        rs = pl["review"]["stats"]
        check("复盘统计字段齐", all(k in rs for k in ("n", "avg_chg", "red_rate", "broken5", "stopped")))
        check("复盘样本>0", rs["n"] > 0, str(rs))
        check("止损计数≤样本", rs["stopped"] <= rs["n"] and rs["broken5"] <= rs["n"])
        # 量纲不变量(2026-11-12 修):avg_chg 必须是比率——涨跌幅类字段若被按百分数
        # 写/读会整体错 100 倍。上界 |avg|≤0.35(单票涨跌停 ±10/20%,均值不可能超)
        check("复盘avg_chg量纲=比率", rs["avg_chg"] is None or abs(rs["avg_chg"]) <= 0.35,
              str(rs["avg_chg"]))
        check("复盘判定分布非单档", len({r["verdict"] for r in pl["review"]["items"]}) >= 2
              or rs["n"] <= 2, str({r["verdict"] for r in pl["review"]["items"]}))
        # 判定与涨跌幅一致:verdict=ok 的必有 chg≥2%,weak 的必有 chg≤-3%(比率)
        for r in pl["review"]["items"]:
            if r["verdict"] == "ok":
                check(f"ok判定量纲({r['name']})", (r["chg"] or 0) >= 0.02, str(r["chg"]))
            if r["verdict"] == "weak":
                check(f"weak判定量纲({r['name']})", (r["chg"] or 0) <= -0.03, str(r["chg"]))
    else:
        check("无快照时诚实降级", bool(pl["review"]["note"]), str(pl["review"]))
    tr_n = sp["trade"]
    if tr_n > 0:
        check("预案全部含可执行买入指引", all(
            any(m["k"] in ("dip", "add") for m in p["moves"]) for p in pl["plans"]))
        check("跳过计数与出卡数自洽", len(pl["plans"]) + pl["header"].get("skipped_no_buy", 0) <= tr_n,
              f"{len(pl['plans'])}+{pl['header'].get('skipped_no_buy')} vs {tr_n}")
        p0 = pl["plans"][0]
        check("预案字段齐", all(k in p0 for k in ("code", "name", "spec", "archetype", "levels", "moves", "risk_note", "sizing")))
        check("live加仓指引含数字", all(
            any(m["k"] not in ("dip", "add") or __import__("re").search(r"\d", m["t"] + m["x"])
                for m in p["moves"]) for p in pl["plans"]))
        check("量能锚存在(历史够时)", all(
            p["levels"]["turn_ma5"] is None or p["levels"]["shrink"] is not None
            for p in pl["plans"]))
        check("竞价锚与量能锚同存同缺", all(
            (p["auction"] is None) == (p["levels"]["turn_ma5"] is None) for p in pl["plans"]))
        check("竞价锚三档单调", all(
            p["auction"] is None or (p["auction"]["strong"] > p["auction"]["weak"]
                                     and len(p["auction"]["rows"]) == 3) for p in pl["plans"]))
        check("live四时点齐", all([c["time"] for c in p["checkpoints"]] == list(_CK_TIMES)
                                  for p in pl["plans"]))
        check("预案全部spec≥65", all((p["spec"] or 0) >= 65 for p in pl["plans"]))
        check("预案按spec降序", [p["spec"] for p in pl["plans"]]
              == sorted((p["spec"] for p in pl["plans"]), reverse=True))
        check("价位锚点齐且涨停>现价", all(
            p["levels"]["limit_up"] is None or p["levels"]["limit_up"] >= (p["levels"]["price"] or 0)
            for p in pl["plans"]))
    else:
        check("无预案时空态", len(pl["plans"]) == 0)
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
    test_danger_spec()
    test_plan_moves()
    test_checkpoints()
    test_calendar()
    if "--live" in sys.argv:
        test_live()
    print(f"verify_microcap: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
