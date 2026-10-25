# -*- coding: utf-8 -*-
"""ETF 综合评估(etf)验证:评分合成纯函数离线单测 + 在线端点不变量。

用法(在 backend 目录下):
    python -X utf8 -m scripts.verify_etf           # 离线单测(不依赖服务)
    python -X utf8 -m scripts.verify_etf --live    # 追加在线验证(需 uvicorn 已运行)

不变量清单:
- _pct 只计正值样本、≤v 占比、空窗/非正值返 None;
- _tier_of_stock:gate 命中或 ST 高危 ⇒ avoid;strong 需 R≥78 且 trap≤20;
  buy≥65、watch≥45、R 缺失 ⇒ na(与掘金台阈值同源);
- detail:tier 封闭五值、总分 0~100、持仓权重和 ≤1.05、估值序列三数组等长、
  changes.kind 与 holdings.source 一致(official 才允许 month)、close 序列二元组;
- list:名单内每只都有 score/tier/sample_days,估值序列 ≥1 日(采集已跑过)。
"""
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.etf import (  # noqa: E402
    BUY_MIN, STRONG_MIN, WATCH_MIN, _pct, _tier_of_stock,
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


def S(**kw):
    """score_daily 行最小假对象:未给列一律 None。"""
    base = dict(gate=None, gate_flags=None, st_risk=None, recommend=None,
                trap=None, fraud=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_pct():
    check("正值占比", _pct([1.0, 2.0, 3.0, 4.0], 2.0) == (50.0, 4))
    check("最大值=100", _pct([1.0, 2.0], 2.0) == (100.0, 2))
    check("最小值>0", _pct([1.0, 2.0], 0.5)[0] == 0.0)
    check("负样本不计入", _pct([-1.0, None, 2.0, 2.0], 2.0) == (100.0, 2))
    check("空窗返None", _pct([], 1.0) is None)
    check("v非正返None", _pct([1.0], 0.0) is None)


def test_tier_of_stock():
    check("gate→avoid", _tier_of_stock(S(gate=True, recommend=90)) == "avoid")
    check("ST高危→avoid", _tier_of_stock(S(st_risk=2, recommend=90)) == "avoid")
    check("strong", _tier_of_stock(S(recommend=STRONG_MIN, trap=20)) == "strong")
    check("trap>20不得strong", _tier_of_stock(S(recommend=90, trap=21)) == "buy")
    check("buy线", _tier_of_stock(S(recommend=BUY_MIN)) == "buy")
    check("watch线", _tier_of_stock(S(recommend=WATCH_MIN)) == "watch")
    check("线下na", _tier_of_stock(S(recommend=44.0)) == "na")
    check("无R→na", _tier_of_stock(S()) == "na")


def get(url):
    from urllib.parse import quote
    with urllib.request.urlopen(BASE + quote(url, safe="/?&=.,%"), timeout=30) as r:
        return __import__("json").loads(r.read().decode("utf-8"))


def test_live():
    tiers = {"strong", "buy", "watch", "na", "avoid"}
    lst = get("/api/etf/list")
    check("list非空", len(lst["items"]) >= 1)
    for it in lst["items"]:
        tag = f"{it['code']} {it['name']}"
        check(f"{tag} tier封闭", it["tier"] in tiers)
        check(f"{tag} 分数域", it["score"] is None or 0 <= it["score"] <= 100)
        check(f"{tag} 序列≥1日", (it["sample_days"] or 0) >= 1, str(it["sample_days"]))

        d = get(f"/api/etf/detail?code={it['code']}")
        h, v, sc = d["holdings"], d["valuation"], d["score"]
        check(f"{tag} 权重和", (h["weight_covered"] or 0) <= 1.05, str(h["weight_covered"]))
        check(f"{tag} 分层权重键", set(h["tier_weight"]) == tiers)
        tier_sum = sum(h["tier_weight"].values())
        check(f"{tag} 分层权重≈覆盖", abs(tier_sum - (h["weight_covered"] or 0)) < 0.02,
              f"{tier_sum} vs {h['weight_covered']}")
        n = len(v["dates"])
        check(f"{tag} 序列等长", len(v["pe_series"]) == n and len(v["pb_series"]) == n
              and len(v["dv_series"]) == n)
        check(f"{tag} 样本天数一致", v["sample_days"] == n)
        check(f"{tag} 成份行有分层", len(h["rows"]) == h["count"]
              and all(r["tier"] in tiers for r in h["rows"]))
        dist = h.get("industry_dist") or []
        check(f"{tag} 行业分布守恒", dist and
              abs(sum(x["weight"] for x in dist) - (h["weight_covered"] or 0)) < 0.02
              and sum(x["count"] for x in dist) == h["count"],
              f"{sum(x['weight'] for x in dist)} vs {h['weight_covered']}")
        ch = d.get("changes")
        if ch:
            # month 一定来自官方月度;官方不足两期时回退 f10 季 diff(kind=quarter)是合法态
            check(f"{tag} 调仓口径",
                  not (ch["kind"] == "month" and h["source"] != "official"),
                  f"{ch['kind']} vs {h['source']}")
            check(f"{tag} 调仓不重叠",
                  not ({x["code"] for x in ch["ins"]} & {x["code"] for x in ch["outs"]}))
        check(f"{tag} 评分理由或风险", len(sc["reasons"]) + len(sc["risks"]) >= 1)
        check(f"{tag} close二元组", all(len(x) == 2 for x in v["close_series"][:5]))


if __name__ == "__main__":
    test_pct()
    test_tier_of_stock()
    if "--live" in sys.argv:
        test_live()
    print(f"verify_etf: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
