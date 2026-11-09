# -*- coding: utf-8 -*-
"""民航景气度·第一层采集(P0:行业量价/成本锚/汇率,2026-11-09 起)。

民航=重资产+高经营杠杆+强周期,与航运四案例组同框架:供给刚性+需求波动。
本层只做行业全口径温度计(民航局口径,含全部承运人,不分公司上市与否);
上市 7 家财务压力表/双排序走读时合成(fin_* 五表),不在此采集。

免费源边界(当日实测):
    新浪宏观库 industry event=20 正班客座率/载运率(2006.2→,20年月度) ✓
    akshare society_traffic_volume 民航行 客运量/同比/RPK/总周转量(月度) ✓
    FRED DJFUELUSGULF 美湾航煤现货(1990→,36年) ✓
    生意社「3号航空煤油商品报价动态」栏目(news/list-14--10151,日更,翻页≈29页
        ≈2020→今) ✓ —— 国内航煤现货自动化的唯一免费路:列表拿 detail URL,
        正文 pn_data 行解析交易商报价取当日中位数(元/吨,市场口径,与发改委
        出厂价有税差/渠道差但趋势联动;agro 模块 urllib+ssl ctx 绕反爬先例)。
        发改委出厂价本身仍无免费结构化源(记 P1 人工);增量=每天列表 p1 比对
        本地最新日期,回填(--backfill-jf)=每月首篇 detail 采样月度锚点
    FRED DGS10/DEXCHUS 美债10Y+美元兑人民币 ✓
    航司月报(7家 ASK/RPK/客公里收益)、波音交付、机场吞吐量、支线补贴公示
        均记 P1(公告解析/官网无稳定免费 API)。

序列清单(11,展示顺序即写入顺序,code 稳定不宜再改;共码序列 AV_ 前缀防互灌):
    需求  AV_PAX 民航客运量(月) / AV_PAX_YOY 同比 / AV_RPK 旅客周转量 /
          AV_TAT 运输总周转量(含货运,全口径总温度计) / AV_PLF 正班客座率 /
          AV_LF 正班载运率(含货运装载)
    成本  AV_JF_CN 国内航煤现货(生意社交易商报价中位数,元/吨) /
          AV_JETFUEL 美湾航煤现货(×42=美元/桶,占航司成本30-40%的燃油锚) /
          AV_JF_CL 航煤/原油比价(自算,馏分油裂解利润代理)
    宏观  AV_DGS10 美债10Y(经营租赁租金锚) / AV_USDCNY 美元兑人民币(美元负债敞口)

用法(backend/ 目录): python -m collector.run aviation  (调度:每天 07:35)
        首次/补史: python collector/commodity-aviation/scripts/fetch_aviation.py --backfill-jf
产物: commodity-aviation/data/aviation.json → import_legacy --only-aviation。
"""
import argparse
import calendar
import json
import re
import ssl
import sys
import time
import urllib.request
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


# ---- 生意社 3号航空煤油商品报价动态(国内航煤现货自动化的唯一免费路) ----
# requests 直连生意社会被 SSL 握手阶段 RST;urllib+宽松 ssl ctx+UA+Referer
# 可稳定通过(agro-price 模块 2026-10 起的成熟先例,同款模式)。
_SUNSIRS_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "Chrome/126 Safari/537.36")
_SUNSIRS_CTX = ssl.create_default_context()
_SUNSIRS_CTX.check_hostname = False
_SUNSIRS_CTX.verify_mode = ssl.CERT_NONE
SUNSIRS_COL = "https://www.100ppi.com/news/list-14--10151-%d.html"


def _sunsirs_fetch(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": _SUNSIRS_UA,
                              "Referer": "https://www.100ppi.com/"})
            return urllib.request.urlopen(req, timeout=25,
                                          context=_SUNSIRS_CTX).read().decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.2 * (i + 1))
    raise RuntimeError("sunsirs fetch failed: %s (%r)" % (url, last))


def _sunsirs_list(page):
    """栏目列表页 → [(date, detail_url)](每页≈50条,日更;page>~29 回绕到首页)。"""
    html = _sunsirs_fetch(SUNSIRS_COL % page)
    out = []
    for m in re.finditer(
            r'href="(https?://www\.100ppi\.com/news/(detail-(\d{8})-\d+\.html))"'
            r'[^>]*>[^<]*航空煤油[^<]*</a>', html):
        d = m.group(3)
        out.append(("%s-%s-%s" % (d[:4], d[4:6], d[6:8]), m.group(1)))
    return out


def _sunsirs_detail_price(url):
    """报价动态正文 pn_data 行 → 当日报价中位数(元/吨)。

    行结构:<li class='pn_data'><span class='title_w180l20'>交易商</span>
    <span class='title_w110l30'>国产</span><span class='title_w140'>山东省</span>
    <span class='title_w100'>8000元/吨</span></li>。同日多交易商取中位数
    (地区/渠道差天然存在,中位数比均值抗单一挂单离群)。
    """
    html = _sunsirs_fetch(url)
    vals = [float(v) for v in re.findall(
        r"pn_data[\s\S]{0,600}?title_w100'>([\d.]+)元/吨", html)]
    if not vals:
        raise ValueError("pn_data 无报价行: %s" % url)
    vals.sort()
    n = len(vals)
    med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
    return round(med, 1), n


def fetch_jf_cn(existing=None, backfill=False, max_pages=29):
    """国内航煤现货(生意社) → [(date, price)]。

    增量(默认):列表 p1 取最新,只抓本地缺的日期(平时 0~1 篇 detail);
    回填(--backfill-jf):翻满列表,每月取首篇 detail 采样月度锚点(≈72 请求,
    兼顾历史深度与生意社抓取压力;日频密度从接入日起由增量累积)。
    existing: 本地已有点位 dict(date→1),由 main 传入。
    """
    existing = existing or {}
    want = {}   # date -> url
    pages = range(1, max_pages + 1) if backfill else range(1, 2)
    seen_month = set()
    for page in pages:
        items = _sunsirs_list(page)
        if not items:
            break
        for d, u in items:
            if backfill:
                if d[:7] in seen_month:       # 每月首篇(列表按日期倒序)
                    continue
                seen_month.add(d[:7])
                want[d] = u
            else:
                if d not in existing:
                    want[d] = u
        if not backfill:
            break
        time.sleep(0.4)
    if backfill:
        # 本地已有的月不再重抓(断点续传:已入库月度锚点不重复请求)
        have_months = {d[:7] for d in existing}
        want = {d: u for d, u in want.items() if d[:7] not in have_months}
    if not want:
        return []
    if len(want) > 100:
        want = dict(sorted(want.items())[:100])   # 单轮上限护栏
    pts, fails = [], []
    for d, u in sorted(want.items()):
        try:
            v, n = _sunsirs_detail_price(u)
            pts.append((d, v))
            print("[aviation] AV_JF_CN %s: %.1f 元/吨(%d 条挂单)" % (d, v, n), flush=True)
        except Exception as e:  # noqa: BLE001
            fails.append("%s:%s" % (d, type(e).__name__))
        time.sleep(0.4)
    if fails:
        print("[aviation] AV_JF_CN 失败 %d 篇(%s)" % (
            len(fails), ",".join(fails)[:120]), flush=True)
    return sorted(pts)


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
    ap.add_argument("--backfill-jf", action="store_true",
                    help="AV_JF_CN 回填模式:生意社栏目翻满列表,每月首篇采样"
                         "月度锚点(≈72 请求;默认增量只抓本地缺的日期)")
    a = ap.parse_args()
    plan = [
        ("需求", "AV_PAX/AV_PAX_YOY/AV_RPK/AV_TAT 民航行月度(新浪宏观库,1949→)"),
        ("需求", "AV_PLF/AV_LF 正班客座率/载运率(2006.2→,20年)"),
        ("成本", "AV_JF_CN 国内航煤现货(生意社交易商中位数,增量/可回填)"),
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

    # AV_JF_CN(生意社):增量合并语义(本轮新点+本地旧点),不走 grab 的组失败降级
    jf_old = {}
    if OUT.exists():
        try:
            _old = json.loads(OUT.read_text(encoding="utf-8"))
            jf_old = {p[0]: p[1] for s in _old.get("series", [])
                      if s["code"] == "AV_JF_CN" for p in s["points"]}
        except Exception:  # noqa: BLE001 旧档损坏按空处理,重抓
            jf_old = {}
    try:
        jf_new = fetch_jf_cn(jf_old, backfill=a.backfill_jf)
        jf_merged = dict(jf_old)
        jf_merged.update(dict(jf_new))
        raw["AV_JF_CN"] = sorted(jf_merged.items()) or None
        print("[aviation] AV_JF_CN: 本轮 +%d,累计 %d 点 %s~%s" % (
            len(jf_new), len(jf_merged),
            min(jf_merged) if jf_merged else "-", max(jf_merged) if jf_merged else "-"), flush=True)
    except Exception as e:  # noqa: BLE001 生意社整链失败:留旧点(若有),否则序列缺席
        warnings.append("AV_JF_CN: %s(%s)" % (type(e).__name__, str(e)[:80]))
        raw["AV_JF_CN"] = sorted(jf_old.items()) or None
        print("[aviation] AV_JF_CN 失败(留旧 %d 点): %s %s" % (
            len(jf_old), type(e).__name__, str(e)[:120]), flush=True)

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
        "AV_JF_CN":    ("国内航煤现货(生意社)", "国内航煤", "成本", "燃油", "元/吨", "日",
                        "生意社商品动态(3号航空煤油交易商报价中位数,2024-07→;月度锚点"
                        "+日频累积。⚠弱源:当前仅山东智恒达单一交易商长期挂单 8000 元/吨"
                        "僵持,与国际价脱节,代表性弱——多挂单方入场/变价后序列自动生效;"
                        "发改委出厂价与隆众快讯均在登录墙后记 P1"),
        "AV_JETFUEL":  ("美湾航空煤油现货", "美湾航煤", "成本", "燃油", "美元/加仑", "周",
                        S_FRED + "(DJFUELUSGULF,1990→,36年;×42=美元/桶,与国际航煤联动)"),
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
    # 序列继承:本轮源失败的序列从上轮 json 原样带过来(点与元数据照抄,idx 仍按
    # SPEC 常量所以顺序稳定)。2026-11-09 三连跑实测:新浪 81 页/FRED 在连续
    # 密集请求下交替瞬断,不继承会让 json 反复丢序列(库里无损但档不稳定)。
    # SPEC 增删序列时继承的旧 idx 可能与新枚举错位——那时需重跑全量。
    if OUT.exists():
        try:
            _prev = json.loads(OUT.read_text(encoding="utf-8"))
            have = {s["code"] for s in series_out}
            for s_old in _prev.get("series", []):
                if s_old["code"] in have or s_old["code"] not in SPEC:
                    continue
                series_out.append(s_old)
                warnings.append("%s: 继承上轮 %d 点(本轮源失败)" % (
                    s_old["code"], len(s_old.get("points", []))))
                print("[aviation] %s(继承): %d 点 %s~%s" % (
                    s_old["code"], len(s_old.get("points", [])),
                    s_old["points"][0][0] if s_old.get("points") else "-",
                    s_old["points"][-1][0] if s_old.get("points") else "-"), flush=True)
        except Exception:  # noqa: BLE001 上轮档读不了就按本轮结果写
            pass
    series_out.sort(key=lambda s: s["idx"])
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
