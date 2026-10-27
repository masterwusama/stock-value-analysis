# -*- coding: utf-8 -*-
"""碳酸锂供需研究·第一层采集(P0:价格/需求/库存,2026-10-27 起)。

碳酸锂是新能源周期品种(设计文档同目录系列):锂电需求占 80%+,2022 年 60 万→
2024 年 7 万的完整暴跌周期由供给释放主导(澳矿+盐湖+江西云母扩产),当前处于
反弹段。中国定价(广期所 LC 2023-07 上市),无 LME/COMEX 流动合约;现货
(SMM/百川)与锂矿价均付费反爬,电池级/工业级价差记 P2——P0 诚实精简 7 序列。

序列清单(7,展示顺序即写入顺序,code 稳定不宜再改):
    价格      LC0_CLOSE+LC0_OI 碳酸锂主力量仓(广期所,2023-07 上市,元/吨)
    需求      EV_ETF 新能车ETF(新浪 sh515030,2020→,终端需求代理)
              BAT_ETF 电池ETF(新浪 sz159755,2021→,中游电池景气代理)
              LI_PMI 制造业PMI(月度;独立 code 防共码互灌)
    持仓库存  LC_INV 碳酸锂注册仓单(东财,滚动近 3 月——锂的挤仓核心变量)
    宏观因子  LI_DTWEXBGS FRED 美元宽口径(LI_ 前缀;锂精矿美元计价,进口成本端)

用法(backend/ 目录): python -m collector.run lithium  (调度:每天 06:45)
产物: commodity-lithium/data/lithium.json → import_legacy --only-lithium。
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

BACKEND = Path(__file__).resolve().parents[3]
DATA = BACKEND / "collector" / "commodity-lithium" / "data"
OUT = DATA / "lithium.json"

FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s"

S_SINA_FUT = "新浪期货-主力连续"
S_SINA_ETF = "新浪基金(ETF)"
S_EM = "东方财富"
S_FRED = "FRED(圣路易斯联储)"


def _pts(df, dcol, vcol):
    s = df[[dcol, vcol]].dropna()
    out = {}
    for d, v in zip(s[dcol], s[vcol]):
        out[str(d)[:10]] = float(v)
    return sorted(out.items())


def fetch_lc0():
    df = ak.futures_main_sina(symbol="LC0")
    close = _pts(df, "日期", "收盘价")
    oi = _pts(df, "日期", "持仓量")
    return close, oi


def fetch_etf(symbol):
    # 新浪 ETF 历史列名是英文(date/close);东财 push2his 直连当日被断,新浪稳
    return _pts(ak.fund_etf_hist_sina(symbol=symbol), "date", "close")


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


def fetch_lc_inv():
    return _pts(ak.futures_inventory_em(symbol="碳酸锂"), "日期", "库存")


def fetch_fred(sid):
    import requests
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印抓取计划")
    a = ap.parse_args()
    plan = [
        ("价格", "LC0_CLOSE/LC0_OI 碳酸锂量仓(广期所,2023-07 上市)"),
        ("需求", "EV_ETF 新能车ETF(sh515030,2020→) / BAT_ETF 电池ETF(sz159755,2021→)"),
        ("需求", "LI_PMI 制造业PMI(2008→月度,独立code防共码)"),
        ("持仓库存", "LC_INV 碳酸锂注册仓单(滚动3月,挤仓核心变量)"),
        ("宏观因子", "LI_DTWEXBGS FRED直链"),
    ]
    print("[lithium] P0 序列计划: %d 组" % len(plan), flush=True)
    for cat, line in plan:
        print("  [%s] %s" % (cat, line), flush=True)
    if a.dry_run:
        return 0

    t0 = time.time()
    warnings = []

    def _sum(pts):
        if isinstance(pts, tuple):
            return "; ".join("%s点" % (len(x) or 0) for x in pts)
        return "%d 点 %s~%s" % (len(pts), pts[0][0] if pts else "-", pts[-1][0] if pts else "-")

    def grab(code, fn, tries=2):
        pts = None
        for i in range(tries):
            try:
                pts = fn()
                if pts is None:
                    raise ValueError("上游组失败,本序列随组缺席")
                break
            except Exception as e:  # noqa: BLE001
                if i == tries - 1:
                    warnings.append("%s: %s(%s)" % (code, type(e).__name__, str(e)[:80]))
                    print("[lithium] %s 失败: %s %s" % (
                        code, type(e).__name__, str(e)[:120]), flush=True)
                    return None
                print("[lithium] %s 第%d次失败(%s),8秒后重试" % (
                    code, i + 1, type(e).__name__), flush=True)
                time.sleep(8)
        print("[lithium] %s: %s" % (code, _sum(pts)), flush=True)
        return pts

    raw = {}
    lc_pair = grab("碳酸锂组(收盘/持仓)", lambda: tuple(fetch_lc0())) or (None, None)
    singles = [
        ("LC0_CLOSE", lambda: lc_pair[0]),
        ("LC0_OI", lambda: lc_pair[1]),
        ("EV_ETF", lambda: fetch_etf("sh515030")),
        ("BAT_ETF", lambda: fetch_etf("sz159755")),
        ("LI_PMI", fetch_pmi),
        ("LC_INV", fetch_lc_inv),
        ("LI_DTWEXBGS", lambda: fetch_fred("DTWEXBGS")),
    ]
    for code, fn in singles:
        raw[code] = grab(code, fn)
        time.sleep(0.4)

    # 展示清单(顺序=页面顺序)
    SPEC = {
        "LC0_CLOSE":   ("碳酸锂主力收盘", "碳酸锂(主力)", "价格", "期货", "元/吨", "日",
                        S_SINA_FUT + "(广期所LC,2023-07 上市)"),
        "EV_ETF":      ("新能源车ETF(515030)净值", "新能车ETF", "需求", "终端需求", "元", "日",
                        S_SINA_ETF + "(动力电池终端景气代理)"),
        "BAT_ETF":     ("电池ETF(159755)净值", "电池ETF", "需求", "中游景气", "元", "日",
                        S_SINA_ETF + "(电池排产景气代理)"),
        "LC_INV":      ("碳酸锂注册仓单", "广期所仓单", "持仓库存", "交易所库存", "吨", "日",
                        S_EM + "(仅滚动近3月;仓单激增常是挤仓行情的核心变量)"),
        "LC0_OI":      ("碳酸锂主力持仓量", "碳酸锂持仓量", "持仓库存", "期指持仓", "手", "日", S_SINA_FUT),
        "LI_PMI":      ("中国制造业PMI", "制造业PMI", "宏观因子", "景气", "%", "月",
                        S_EM + "(官方口径,当月首日)"),
        "LI_DTWEXBGS": ("美元指数(贸易加权宽口径)", "美元指数(宽)", "宏观因子", "汇率", "点", "日", S_FRED),
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
        "commodity": "lithium",
        "range": {"begin": min(all_ds), "end": max(all_ds)} if all_ds else None,
        "warnings": warnings,
        "series": series_out,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    total = sum(len(s["points"]) for s in series_out)
    print("[lithium] 写盘 %s: %d 序列 %d 点,失败 %d(%s),耗时 %.1fs" % (
        OUT.name, len(series_out), total, len(warnings),
        ",".join(w.split(":")[0] for w in warnings) or "-", time.time() - t0), flush=True)
    failed = len(SPEC) - len(series_out)
    return 1 if failed > len(SPEC) / 2 else 0


if __name__ == "__main__":
    sys.exit(main())
