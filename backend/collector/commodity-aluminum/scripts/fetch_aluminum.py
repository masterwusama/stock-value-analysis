# -*- coding: utf-8 -*-
"""铝供需研究·第一层采集(P0:价格/成本/需求/库存,2026-10-27 起)。

铝与金银铜的定价逻辑不同(设计文档同目录系列):供给约束主导——中国占全球产量
约 60%,电解铝产能受红线约束;成本锚=氧化铝(1.93 吨产 1 吨电解铝)+电力;
需求端新能源车轻量化/光伏边框对冲地产走弱。定价权在 LME/SHFE,CFTC 铝合约
流动性极低(非商业多仅数百手,实测 2026-10-27)故不采 CFTC。

序列清单(9,展示顺序即写入顺序,code 稳定不宜再改):
    价格      AHD_MAIN LME铝三月(新浪外盘 AHD,2016→,美元/吨) / AL0_CLOSE+AL0_OI
              沪铝量仓(2005→,最长序列) / AL_SHFE_LME 沪伦比(自算,>1=内盘溢价)
    成本      AO0_CLOSE+AO0_OI 氧化铝量仓(广期所,2023-06 上市,元/吨——电解铝
              直接原料,成本锚)
    需求      AL_PMI 制造业PMI(月度;独立 code 防与铜页 CN_PMI 共码互灌)
    持仓库存  SHFE_AL_INV 沪铝库存(东财,仅滚动近 3 月——LME 铝库存无免费
              结构化源,页面 footer 诚实注明)
    宏观因子  AL_DTWEXBGS FRED 美元宽口径(AL_ 前缀独立 code 防互灌)

用法(backend/ 目录): python -m collector.run aluminum  (调度:每天 06:35)
产物: commodity-aluminum/data/aluminum.json → import_legacy --only-aluminum。
"""
import argparse
import json
import re
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
import pandas as pd   # noqa: E402
import requests       # noqa: E402

BACKEND = Path(__file__).resolve().parents[3]
DATA = BACKEND / "collector" / "commodity-aluminum" / "data"
OUT = DATA / "aluminum.json"

FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s"

S_SINA_FOREIGN = "新浪外盘期货"
S_SINA_FUT = "新浪期货-主力连续"
S_EM = "东方财富"
S_FRED = "FRED(圣路易斯联储)"
S_CNFX = "中国外汇交易中心中间价(BOC镜像)"


def _pts(df, dcol, vcol):
    s = df[[dcol, vcol]].dropna()
    out = {}
    for d, v in zip(s[dcol], s[vcol]):
        out[str(d)[:10]] = float(v)
    return sorted(out.items())


def fetch_prices():
    return {"AHD_MAIN": _pts(ak.futures_foreign_hist(symbol="AHD"), "date", "close")}


def fetch_fut(symbol):
    df = ak.futures_main_sina(symbol=symbol)
    close = _pts(df, "日期", "收盘价")
    oi = _pts(df, "日期", "持仓量")
    return close, oi


def fetch_usdcny():
    df = ak.currency_boc_safe()
    df = df.dropna(subset=["美元"]).copy()
    df["cny"] = pd.to_numeric(df["美元"], errors="coerce") / 100.0
    return _pts(df.dropna(subset=["cny"]), "日期", "cny")


def fetch_pmi():
    df = ak.macro_china_pmi()
    s = df[["月份", "制造业-指数"]].dropna()

    def _norm(t):
        m = re.match(r"(\d{4})年(\d{1,2})月", str(t).strip())
        return "%04d-%02d-01" % (int(m.group(1)), int(m.group(2))) if m else None

    out = {}
    for t, v in zip(s["月份"], s["制造业-指数"]):
        d = _norm(t)
        if d:
            out[d] = float(v)
    if not out:
        raise ValueError("PMI 归一后为空")
    return sorted(out.items())


def fetch_shfe_inv():
    return _pts(ak.futures_inventory_em(symbol="沪铝"), "日期", "库存")


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
    """自算派生:沪伦比(沪铝元/吨 ÷ (LME铝美元/吨×中间价))。"""
    out, notes = {}, []
    lme, al, cny = (series_map.get("AHD_MAIN"), series_map.get("AL0_CLOSE"),
                    series_map.get("USDCNY_FIX"))
    if lme and al and cny:
        mc, mu = dict(cny), dict(al)
        ratio = []
        for d, v in lme:
            if d in mc and d in mu and v > 0:
                ratio.append((d, round(mu[d] / (v * mc[d]), 4)))
        out["AL_SHFE_LME"] = sorted(ratio)
    else:
        notes.append("沪伦比缺原料(AHD/中间价/沪铝)")
    return out, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印抓取计划")
    a = ap.parse_args()
    plan = [
        ("价格", "AHD_MAIN LME铝三月(新浪外盘,2016→)"),
        ("价格", "AL0_CLOSE/AL0_OI 沪铝量仓(2005→) / AL_SHFE_LME 沪伦比(自算)"),
        ("成本", "AO0_CLOSE/AO0_OI 氧化铝量仓(广期所,2023-06 上市)"),
        ("需求", "AL_PMI 制造业PMI(2008→月度,独立code防共码)"),
        ("持仓库存", "SHFE_AL_INV 沪铝库存(滚动3月)"),
        ("宏观因子", "AL_DTWEXBGS FRED直链"),
    ]
    print("[aluminum] P0 序列计划: %d 组" % len(plan), flush=True)
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
                    print("[aluminum] %s 失败: %s %s" % (
                        code, type(e).__name__, str(e)[:120]), flush=True)
                    return None
                print("[aluminum] %s 第%d次失败(%s),8秒后重试" % (
                    code, i + 1, type(e).__name__), flush=True)
                time.sleep(8)
        print("[aluminum] %s: %s" % (code, _sum(pts)), flush=True)
        return pts

    raw = {}
    fx = grab("新浪外盘组(AHD)", fetch_prices) or {}
    al_pair = grab("沪铝组(收盘/持仓)", lambda: tuple(fetch_fut("AL0"))) or (None, None)
    ao_pair = grab("氧化铝组(收盘/持仓)", lambda: tuple(fetch_fut("AO0"))) or (None, None)
    singles = [
        ("AHD_MAIN", lambda: fx.get("AHD_MAIN")),
        ("AL0_CLOSE", lambda: al_pair[0]),
        ("AL0_OI", lambda: al_pair[1]),
        ("AO0_CLOSE", lambda: ao_pair[0]),
        ("AO0_OI", lambda: ao_pair[1]),
        ("USDCNY_FIX", fetch_usdcny),
        ("AL_PMI", fetch_pmi),
        ("SHFE_AL_INV", fetch_shfe_inv),
        ("AL_DTWEXBGS", lambda: fetch_fred("DTWEXBGS")),
    ]
    for code, fn in singles:
        raw[code] = grab(code, fn)
        time.sleep(0.4)
    derived, dnotes = derive({k: v for k, v in raw.items() if v})
    for code, pts in derived.items():
        raw[code] = pts
        print("[aluminum] %s(派生): %d 点 %s~%s" % (code, len(pts), pts[0][0], pts[-1][0]), flush=True)
    warnings += dnotes

    # 展示清单(顺序=页面顺序);USDCNY_FIX 是中间量不进清单
    SPEC = {
        "AHD_MAIN":    ("LME铝(三月)", "LME铝", "价格", "外盘", "美元/吨", "日", S_SINA_FOREIGN),
        "AL0_CLOSE":   ("沪铝主力收盘", "沪铝(主力)", "价格", "内盘", "元/吨", "日", S_SINA_FUT),
        "AL_SHFE_LME": ("沪伦比(沪铝/LME×中间价)", "沪伦比", "价格", "比价", "倍", "日",
                        "自算(>1=内盘溢价=中国需求强/进口亏损)"),
        "AL0_OI":      ("沪铝主力持仓量", "沪铝持仓量", "价格", "期指持仓", "手", "日", S_SINA_FUT),
        "AO0_CLOSE":   ("氧化铝主力收盘", "氧化铝(主力)", "成本", "原料", "元/吨", "日",
                        S_SINA_FUT + "(广期所AO,2023-06 上市;1.93 吨氧化铝产 1 吨电解铝)"),
        "AO0_OI":      ("氧化铝主力持仓量", "氧化铝持仓量", "成本", "原料", "手", "日", S_SINA_FUT),
        "AL_PMI":      ("中国制造业PMI", "制造业PMI", "需求", "景气", "%", "月", S_EM + "(官方口径,当月首日)"),
        "SHFE_AL_INV": ("沪铝库存", "沪铝库存", "持仓库存", "交易所库存", "吨", "日",
                        S_EM + "(仅滚动近3月;LME铝库存无免费结构化源)"),
        "AL_DTWEXBGS": ("美元指数(贸易加权宽口径)", "美元指数(宽)", "宏观因子", "汇率", "点", "日", S_FRED),
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
        "commodity": "aluminum",
        "range": {"begin": min(all_ds), "end": max(all_ds)} if all_ds else None,
        "warnings": warnings,
        "series": series_out,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    total = sum(len(s["points"]) for s in series_out)
    print("[aluminum] 写盘 %s: %d 序列 %d 点,失败 %d(%s),耗时 %.1fs" % (
        OUT.name, len(series_out), total, len(warnings),
        ",".join(w.split(":")[0] for w in warnings) or "-", time.time() - t0), flush=True)
    failed = len(SPEC) - len(series_out)
    return 1 if failed > len(SPEC) / 2 else 0


if __name__ == "__main__":
    sys.exit(main())
