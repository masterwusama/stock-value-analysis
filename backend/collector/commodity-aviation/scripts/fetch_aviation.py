# -*- coding: utf-8 -*-
"""民航景气度·第一层采集(P0:行业量价/成本锚/汇率,2026-11-09 起)。

民航=重资产+高经营杠杆+强周期,与航运四案例组同框架:供给刚性+需求波动。
本层只做行业全口径温度计(民航局口径,含全部承运人,不分公司上市与否);
上市 7 家财务压力表/双排序走读时合成(fin_* 五表),不在此采集。

免费源边界(当日实测):
    新浪宏观库 industry event=20 正班客座率/载运率(2006.2→,20年月度) ✓
    akshare society_traffic_volume 民航行 客运量/同比/RPK/总周转量(月度) ✓
    FRED DJFUELUSGULF 美湾航煤现货(1990→,36年) ✓ —— 国内航煤出厂价
        (发改委月度调价)无免费结构化源,记 P1 人工回填(forecast targets 模式);
        美湾价与国内出厂价联动(税差恒定),趋势/分位用途不受影响,口径写在 source
    FRED DGS10/DEXCHUS 美债10Y+美元兑人民币 ✓
    航司月报(7家 ASK/RPK/客公里收益)、波音交付、机场吞吐量、支线补贴公示
        均记 P1(公告解析/官网无稳定免费 API)。

序列清单(10,展示顺序即写入顺序,code 稳定不宜再改;共码序列 AV_ 前缀防互灌):
    需求  AV_PAX 民航客运量(月) / AV_PAX_YOY 同比 / AV_RPK 旅客周转量 /
          AV_TAT 运输总周转量(含货运,全口径总温度计) / AV_PLF 正班客座率 /
          AV_LF 正班载运率(含货运装载)
    成本  AV_JETFUEL 美湾航煤现货(×42=美元/桶,占航司成本30-40%的燃油锚) /
          AV_JF_CL 航煤/原油比价(自算,馏分油裂解利润代理)
    宏观  AV_DGS10 美债10Y(经营租赁租金锚) / AV_USDCNY 美元兑人民币(美元负债敞口)

用法(backend/ 目录): python -m collector.run aviation  (调度:每天 07:25)
产物: commodity-aviation/data/aviation.json → import_legacy --only-aviation。
"""
import argparse
import calendar
import json
import sys
import time
from datetime import datetime
from pathlib import Path

# 本机系统代理会拦断行情/东财/新浪域名,与 fetch_edb/fetch_gold 同因,单独直连
import os
os.environ["NO_PROXY"] = "*"
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import akshare as ak  # noqa: E402
import requests       # noqa: E402

BACKEND = Path(__file__).resolve().parents[3]
DATA = BACKEND / "collector" / "commodity-aviation" / "data"
OUT = DATA / "aviation.json"

FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s"

S_SINA_MAC = "民航局/新浪宏观库"
S_SINA_MAC_TRAF = "民航局/新浪宏观库(全社会交通量·民航行)"
S_FRED = "FRED(圣路易斯联储)"
S_SINA_FOREIGN = "新浪外盘期货"


def _month_end(s):
    """'2026.9'/'1952.12' → 该月最后一天(月度锚定,与 fetch_edb month_mean 同口径)。"""
    y, m = str(s).split(".")
    y, m = int(y), int(m)
    return "%04d-%02d-%02d" % (y, m, calendar.monthrange(y, m)[1])


def fetch_traffic_air():
    """全社会交通量·民航行 → {AV_PAX/AV_PAX_YOY/AV_RPK/AV_TAT}(月度,1949→早期NaN自然丢弃)。"""
    df = ak.macro_china_society_traffic_volume()
    air = df[df["统计对象"] == "民航"]
    out = {}
    for col, code in (("客运量", "AV_PAX"), ("客运量同比增长", "AV_PAX_YOY"),
                      ("旅客周转量", "AV_RPK"), ("民航总周转量", "AV_TAT")):
        s = air[["统计时间", col]].dropna()
        out[code] = sorted((_month_end(d), float(v)) for d, v in zip(s["统计时间"], s[col]))
    return out


def fetch_load_factor():
    """正班客座率/载运率(2006.2→) → {AV_PLF, AV_LF}。"""
    df = ak.macro_china_passenger_load_factor()
    out = {}
    for col, code in (("客座率", "AV_PLF"), ("载运率", "AV_LF")):
        s = df[["统计时间", col]].dropna()
        out[code] = sorted((_month_end(d), float(v)) for d, v in zip(s["统计时间"], s[col]))
    return out


def fetch_fred(sid):
    r = requests.get(FRED % sid, timeout=30)
    r.raise_for_status()
    rows = []
    for line in r.text.splitlines()[1:]:
        cells = line.split(",")
        if len(cells) != 2 or cells[1] in (".", ""):
            continue
        try:
            rows.append((cells[0], float(cells[1])))
        except ValueError:
            continue
    if not rows:
        raise ValueError(f"FRED {sid} 空返回")
    return rows


def fetch_wti():
    df = ak.futures_foreign_hist(symbol="CL")
    s = df[["date", "close"]].dropna()
    return sorted((str(d)[:10], float(v)) for d, v in zip(s["date"], s["close"]))


def derive(series_map):
    """自算派生:航煤/原油比价(无量纲)。航煤美元/加仑×42=美元/桶,与WTI同单位;
    >1=航煤溢价(馏分油裂解利润强=航空燃油需求旺),<1=折价。日度交集对齐。"""
    out, notes = {}, []
    jf, wti = series_map.get("AV_JETFUEL"), series_map.get("AV_CL")
    if jf and wti:
        m = dict(wti)
        out["AV_JF_CL"] = sorted(
            (d, round(v * 42.0 / m[d], 4)) for d, v in jf if d in m and m[d])
    else:
        notes.append("航煤/原油比价缺原料(AV_JETFUEL/AV_CL)")
    return out, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印抓取计划")
    a = ap.parse_args()
    plan = [
        ("需求", "AV_PAX/AV_PAX_YOY/AV_RPK/AV_TAT 民航行月度(新浪宏观库,1949→)"),
        ("需求", "AV_PLF/AV_LF 正班客座率/载运率(2006.2→,20年)"),
        ("成本", "AV_JETFUEL 美湾航煤现货 FRED(1990→,36年) / AV_JF_CL 比价(自算)"),
        ("宏观", "AV_DGS10 美债10Y / AV_USDCNY 美元兑人民币(FRED)"),
    ]
    print("[aviation] P0 序列计划: %d 组" % len(plan), flush=True)
    for cat, line in plan:
        print("  [%s] %s" % (cat, line), flush=True)
    if a.dry_run:
        return 0

    t0 = time.time()
    warnings = []

    def _sum(pts):
        if isinstance(pts, dict):
            return "; ".join("%s:%d点" % (k, len(v) or 0) for k, v in pts.items())
        return "%d 点 %s~%s" % (len(pts), pts[0][0] if pts else "-", pts[-1][0] if pts else "-")

    def grab(code, fn, tries=2):
        pts = None
        for i in range(tries):
            try:
                pts = fn()
                if pts is None or (isinstance(pts, dict) and not pts):
                    raise ValueError("上游组失败,本序列随组缺席")
                break
            except Exception as e:  # noqa: BLE001
                if i == tries - 1:
                    warnings.append("%s: %s(%s)" % (code, type(e).__name__, str(e)[:80]))
                    print("[aviation] %s 失败: %s %s" % (
                        code, type(e).__name__, str(e)[:120]), flush=True)
                    return None
                print("[aviation] %s 第%d次失败(%s),8秒后重试" % (
                    code, i + 1, type(e).__name__), flush=True)
                time.sleep(8)
        print("[aviation] %s: %s" % (code, _sum(pts)), flush=True)
        return pts

    raw = {}
    traf = grab("民航行交通量组(PAX/YOY/RPK/TAT)", fetch_traffic_air) or {}
    plf = grab("客座率组(PLF/LF)", fetch_load_factor) or {}
    for k, v in {**traf, **plf}.items():
        raw[k] = v or None
    singles = [
        ("AV_JETFUEL", lambda: fetch_fred("DJFUELUSGULF")),
        ("AV_DGS10", lambda: fetch_fred("DGS10")),
        ("AV_USDCNY", lambda: fetch_fred("DEXCHUS")),
        ("AV_CL", fetch_wti),
    ]
    for code, fn in singles:
        raw[code] = grab(code, fn)
        time.sleep(0.4)
    derived, dnotes = derive({k: v for k, v in raw.items() if v})
    for code, pts in derived.items():
        raw[code] = pts
        print("[aviation] %s(派生): %d 点 %s~%s" % (code, len(pts), pts[0][0], pts[-1][0]), flush=True)
    warnings += dnotes

    # 展示清单(顺序=页面顺序)。AV_CL 仅作派生原料,不展示。
    SPEC = {
        "AV_PAX":      ("民航客运量", "民航客运量", "需求", "总量", "亿人", "月",
                        S_SINA_MAC_TRAF + "(月度,全口径含未上市航司)"),
        "AV_PAX_YOY":  ("民航客运量同比", "客运量同比", "需求", "总量", "%", "月",
                        S_SINA_MAC_TRAF),
        "AV_RPK":      ("民航旅客周转量", "旅客周转量RPK", "需求", "周转量", "亿人公里", "月",
                        S_SINA_MAC_TRAF),
        "AV_TAT":      ("民航运输总周转量", "总周转量", "需求", "周转量", "亿吨公里", "月",
                        S_SINA_MAC_TRAF + "(含货运,全口径景气总温度计)"),
        "AV_PLF":      ("正班客座率", "客座率", "需求", "效率", "%", "月",
                        S_SINA_MAC + "(2006.2→,20年;83%一线为供需平衡经验线)"),
        "AV_LF":       ("正班载运率", "载运率", "需求", "效率", "%", "月",
                        S_SINA_MAC + "(含货运装载)"),
        "AV_JETFUEL":  ("美湾航空煤油现货", "航煤现货", "成本", "燃油", "美元/加仑", "周",
                        S_FRED + "(DJFUELUSGULF,1990→,36年;×42=美元/桶。国内出厂价"
                        "无免费结构化源记P1人工,美湾价联动税差恒定)"),
        "AV_JF_CL":    ("航煤/原油比价", "航煤比价", "成本", "燃油", "倍", "日",
                        "自算(航煤桶价/WTI;>1=航煤溢价=馏分裂解利润强=航空燃油需求旺)"),
        "AV_DGS10":    ("美债10年期收益率", "美债10Y", "成本", "租赁", "%", "日",
                        S_FRED + "(飞机经营租赁租金锚,机队扩张融资成本)"),
        "AV_USDCNY":   ("美元兑人民币", "美元/人民币", "成本", "汇率", "元", "周",
                        S_FRED + "(DEXCHUS;美元负债+租金敞口,人民币贬值=汇兑损失)"),
    }
    series_out = []
    for idx, (code, (name, label, cat, group, unit, freq, source)) in enumerate(SPEC.items()):
        pts = raw.get(code)
        if not pts:
            continue
        series_out.append({
            "code": code, "idx": idx, "name": name, "label": label, "cat": cat,
            "group": group, "unit": unit, "freq": freq, "source": source,
            "points": [[d, round(v, 4)] for d, v in pts],
        })
    all_ds = [p[0] for s in series_out for p in s["points"]]
    payload = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "commodity": "aviation",
        "range": {"begin": min(all_ds), "end": max(all_ds)} if all_ds else None,
        "warnings": warnings,
        "series": series_out,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    total = sum(len(s["points"]) for s in series_out)
    print("[aviation] 写盘 %s: %d 序列 %d 点,失败 %d(%s),耗时 %.1fs" % (
        OUT.name, len(series_out), total, len(warnings),
        ",".join(w.split(":")[0] for w in warnings) or "-", time.time() - t0), flush=True)
    failed = len(SPEC) - len(series_out)
    return 1 if failed > len(SPEC) / 2 else 0


if __name__ == "__main__":
    sys.exit(main())
