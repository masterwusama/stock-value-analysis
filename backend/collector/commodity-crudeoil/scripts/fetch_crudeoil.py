# -*- coding: utf-8 -*-
"""原油供需研究·第一层采集(P0:价格/比价/持仓/能源,2026-10-27 起)。

原油=全球流量大宗之王(设计文档同目录系列):OPEC+ 供给管理 vs 全球需求,
美元计价。免费源边界(当日实测):EIA 周度库存 akshare 源 2025-09 停更、
贝克休斯钻机数 akshare 源 2024-02 停更——均记 P2(EIA 免费 key 可注册);
CFTC 2026 年报中 NYMEX 标准 WTI 市名未匹配到(疑似改名,待考),非商业
持仓走 P2,P0 用东财商业净持仓(1986→,1,941 期)。RB 汽油新浪源坏(1 期),
裂解价差改用取暖油/原油比值(HO 单位无法旁证,比值不依赖绝对单位校准)。

序列清单(10,展示顺序即写入顺序,code 稳定不宜再改):
    价格      CL_MAIN WTI原油主力(新浪外盘,1996→,30年) / OIL_MAIN 布伦特
              (2016→) / SC0_CLOSE+SC0_OI 上海原油量仓(上期能源,2018-03 上市)
              BRENT_WTI 布伦特-WTI价差(自算;正值走阔=美国以外市场更紧张)
    能源炼制  HO_MAIN 取暖油(2021→) / NG_MAIN 天然气(2016→,发电/取暖替代品)
              CRACK_HO 取暖油/原油比价(自算,炼厂馏分油利润的相对水平)
    持仓库存  CFTC_OIL_COMM 商业净持仓(东财纽约原油列,1986→)
    宏观因子  OIL_DTWEXBGS FRED 美元宽口径(OIL_ 前缀独立 code 防互灌)

用法(backend/ 目录): python -m collector.run crudeoil  (调度:每天 06:55)
产物: commodity-crudeoil/data/crudeoil.json → import_legacy --only-crudeoil。
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

# 本机系统代理会拦断行情/东财域名,与 fetch_edb/fetch_gold 同因,单独直连
import os
os.environ["NO_PROXY"] = "*"
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import akshare as ak  # noqa: E402
import requests       # noqa: E402

BACKEND = Path(__file__).resolve().parents[3]
DATA = BACKEND / "collector" / "commodity-crudeoil" / "data"
OUT = DATA / "crudeoil.json"

FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s"

S_SINA_FOREIGN = "新浪外盘期货"
S_SINA_FUT = "新浪期货-主力连续"
S_EM = "东方财富"
S_FRED = "FRED(圣路易斯联储)"


def _pts(df, dcol, vcol):
    s = df[[dcol, vcol]].dropna()
    out = {}
    for d, v in zip(s[dcol], s[vcol]):
        out[str(d)[:10]] = float(v)
    return sorted(out.items())


def _sina_foreign(symbol):
    return _pts(ak.futures_foreign_hist(symbol=symbol), "date", "close")


def fetch_prices():
    return {
        "CL_MAIN": _sina_foreign("CL"),
        "OIL_MAIN": _sina_foreign("OIL"),
        "HO_MAIN": _sina_foreign("HO"),
        "NG_MAIN": _sina_foreign("NG"),
    }


def fetch_sc0():
    df = ak.futures_main_sina(symbol="SC0")
    close = _pts(df, "日期", "收盘价")
    oi = _pts(df, "日期", "持仓量")
    return close, oi


def fetch_cftc_comm():
    df = ak.macro_usa_cftc_merchant_goods_holding()
    return _pts(df.dropna(subset=["纽约原油-净仓位"]), "日期", "纽约原油-净仓位")


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


def derive(series_map):
    """自算派生:布伦特-WTI价差(桶)、取暖油/原油比价(无量纲)。"""
    out, notes = {}, []
    brent, wti = series_map.get("OIL_MAIN"), series_map.get("CL_MAIN")
    if brent and wti:
        m = dict(wti)
        out["BRENT_WTI"] = sorted(
            (d, round(v - m[d], 2)) for d, v in brent if d in m)
    else:
        notes.append("布伦特-WTI缺原料(OIL/CL)")
    ho = series_map.get("HO_MAIN")
    if ho and wti:
        m = dict(wti)
        out["CRACK_HO"] = sorted(
            (d, round(v / m[d], 4)) for d, v in ho if d in m and m[d])
    else:
        notes.append("取暖油/原油比价缺原料(HO/CL)")
    return out, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印抓取计划")
    a = ap.parse_args()
    plan = [
        ("价格", "CL_MAIN WTI(1996→,30年) / OIL_MAIN 布伦特(2016→) 新浪外盘"),
        ("价格", "SC0_CLOSE/SC0_OI 上海原油量仓(2018-03 上市)"),
        ("能源炼制", "HO_MAIN 取暖油(2021→) / NG_MAIN 天然气(2016→) / CRACK_HO 比价(自算)"),
        ("持仓库存", "CFTC_OIL_COMM 商业净持仓(东财,1986→;非商业走 P2,WTI市名待考)"),
        ("宏观因子", "OIL_DTWEXBGS FRED直链"),
    ]
    print("[crudeoil] P0 序列计划: %d 组" % len(plan), flush=True)
    for cat, line in plan:
        print("  [%s] %s" % (cat, line), flush=True)
    if a.dry_run:
        return 0

    t0 = time.time()
    warnings = []

    def _sum(pts):
        if isinstance(pts, dict):
            return "; ".join("%s:%d点" % (k, len(v) or 0) for k, v in pts.items())
        if isinstance(pts, tuple):
            return "; ".join("%s点" % (len(x) or 0) for x in pts)
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
                    print("[crudeoil] %s 失败: %s %s" % (
                        code, type(e).__name__, str(e)[:120]), flush=True)
                    return None
                print("[crudeoil] %s 第%d次失败(%s),8秒后重试" % (
                    code, i + 1, type(e).__name__), flush=True)
                time.sleep(8)
        print("[crudeoil] %s: %s" % (code, _sum(pts)), flush=True)
        return pts

    raw = {}
    fx = grab("新浪外盘组(CL/OIL/HO/NG)", fetch_prices) or {}
    sc_pair = grab("沪原油组(收盘/持仓)", lambda: tuple(fetch_sc0())) or (None, None)
    singles = [
        ("CL_MAIN", lambda: fx.get("CL_MAIN")),
        ("OIL_MAIN", lambda: fx.get("OIL_MAIN")),
        ("HO_MAIN", lambda: fx.get("HO_MAIN")),
        ("NG_MAIN", lambda: fx.get("NG_MAIN")),
        ("SC0_CLOSE", lambda: sc_pair[0]),
        ("SC0_OI", lambda: sc_pair[1]),
        ("CFTC_OIL_COMM", fetch_cftc_comm),
        ("OIL_DTWEXBGS", lambda: fetch_fred("DTWEXBGS")),
    ]
    for code, fn in singles:
        raw[code] = grab(code, fn)
        time.sleep(0.4)
    derived, dnotes = derive({k: v for k, v in raw.items() if v})
    for code, pts in derived.items():
        raw[code] = pts
        print("[crudeoil] %s(派生): %d 点 %s~%s" % (code, len(pts), pts[0][0], pts[-1][0]), flush=True)
    warnings += dnotes

    # 展示清单(顺序=页面顺序)
    SPEC = {
        "CL_MAIN":      ("WTI原油主力", "WTI原油", "价格", "外盘", "美元/桶", "日",
                         S_SINA_FOREIGN + "(1996→,30年)"),
        "OIL_MAIN":     ("布伦特原油", "布伦特", "价格", "外盘", "美元/桶", "日", S_SINA_FOREIGN),
        "BRENT_WTI":    ("布伦特-WTI价差", "布伦特-WTI", "价格", "比价", "美元/桶", "日",
                         "自算(正值走阔=美国以外市场更紧张/制裁与海运风险)"),
        "SC0_CLOSE":    ("上海原油主力收盘", "沪原油(主力)", "价格", "内盘", "元/桶", "日",
                         S_SINA_FUT + "(上期能源SC,2018-03 上市)"),
        "SC0_OI":       ("上海原油主力持仓量", "沪原油持仓量", "价格", "期指持仓", "手", "日", S_SINA_FUT),
        "CRACK_HO":     ("取暖油/原油比价", "取暖油比价", "能源炼制", "炼制利润", "倍", "日",
                         "自算(HO/CL,馏分油炼制利润相对水平,比值不依赖单位校准)"),
        "HO_MAIN":      ("取暖油主力", "取暖油", "能源炼制", "馏分油", "点", "日", S_SINA_FOREIGN),
        "NG_MAIN":      ("天然气主力", "天然气", "能源炼制", "替代能源", "点", "日",
                         S_SINA_FOREIGN + "(发电/取暖替代品,与油价有替代弹性)"),
        "CFTC_OIL_COMM": ("CFTC原油商业净持仓", "CFTC商业净持仓", "持仓库存", "期指持仓", "手", "周",
                          S_EM + "(纽约原油,1986→;非商业细项 P2)"),
        "OIL_DTWEXBGS": ("美元指数(贸易加权宽口径)", "美元指数(宽)", "宏观因子", "汇率", "点", "日", S_FRED),
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
        "commodity": "crudeoil",
        "range": {"begin": min(all_ds), "end": max(all_ds)} if all_ds else None,
        "warnings": warnings,
        "series": series_out,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    total = sum(len(s["points"]) for s in series_out)
    print("[crudeoil] 写盘 %s: %d 序列 %d 点,失败 %d(%s),耗时 %.1fs" % (
        OUT.name, len(series_out), total, len(warnings),
        ",".join(w.split(":")[0] for w in warnings) or "-", time.time() - t0), flush=True)
    failed = len(SPEC) - len(series_out)
    return 1 if failed > len(SPEC) / 2 else 0


if __name__ == "__main__":
    sys.exit(main())
