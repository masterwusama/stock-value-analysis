# -*- coding: utf-8 -*-
"""温度锚点校准:用 2024-01~2026-09 历史数据实测各指标分位,对照 static-v1 软锚。

设计文档 docs/设计-情绪温度与极端度.md §4.2。可得分位源:
    - R5:国证2000 指数日线自算(精确);
    - 涨停/炸板/跌停家数:东财三大池 date 参数回溯(精确;一字跌停与红盘率
      免费源无历史,保持软锚)。
产物:collector/data/sentiment/calib_series.json(逐日原始序列,可断点续跑)
     + calib_report.json(静态锚 vs 实测分位偏差;|偏差|>10pt 的锚点列入待调)。

用法(backend/ 目录): python -X utf8 -m scripts.calibrate_sentiment_score
    [--start 2024-01-01 --end 2026-09-30] [--refresh]
"""
import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

# 与 fetch_sentiment 同因:系统代理拦断行情域,名单独直连
os.environ["NO_PROXY"] = "*"
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import requests

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "collector" / "scripts"))
import sentiment_score as sscore  # noqa: E402

OUT = BACKEND / "collector" / "data" / "sentiment"
CACHE = OUT / "calib_series.json"
REPORT = OUT / "calib_report.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def trade_days(start, end):
    import akshare as ak
    idx = ak.stock_zh_index_daily(symbol="sz399303")
    days = [d.replace("-", "") for d in idx["date"].astype(str)]
    return [d for d in days if start <= d <= end], idx


def r5_series(idx, days):
    """国证2000 五日累计,逐日(窗口起点前 5 根也参与分母)。"""
    closes = idx["close"].astype(float).tolist()
    dates = [d.replace("-", "") for d in idx["date"].astype(str)]
    pos = {d: i for i, d in enumerate(dates)}
    out = {}
    for d in days:
        i = pos[d]
        if i >= 5:
            out[d] = closes[i] / closes[i - 5] - 1
    return out


def pool_counts(day):
    """东财三池当日家数;单池失败不拦整体(计 None,分位计算剔除)。"""
    import akshare as ak
    out = {}
    for key, fn in (("zt", ak.stock_zt_pool_em), ("zha", ak.stock_zt_pool_zbgc_em),
                    ("dt", ak.stock_zt_pool_dtgc_em)):
        try:
            out[key] = int(len(fn(date=day)))
        except Exception:  # noqa: BLE001
            out[key] = None
        time.sleep(0.25)
    return out


def pctile(sorted_vals, q):
    """线性插值分位(numpy 语义,免依赖)。"""
    import bisect
    n = len(sorted_vals)
    if not n:
        return None
    k = q * (n - 1)
    lo = int(k)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (k - lo) * (sorted_vals[hi] - sorted_vals[lo])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="20240101")
    ap.add_argument("--end", default="20260930")
    ap.add_argument("--refresh", action="store_true", help="忽略缓存日期重拉")
    a = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    days, idx = trade_days(a.start, a.end)
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    r5 = r5_series(idx, days)
    cache.setdefault("r5", {}).update({d: round(v, 6) for d, v in r5.items()})

    todo = [d for d in days if a.refresh or d not in cache.get("zt", {})]
    print(f"[calib] 窗口 {a.start}~{a.end} 共 {len(days)} 个交易日,待拉池 {len(todo)} 天", flush=True)
    for i, d in enumerate(todo):
        counts = pool_counts(d)
        for k in ("zt", "zha", "dt"):
            cache.setdefault(k, {})
            cache[k][d] = counts[k]
        if (i + 1) % 20 == 0:
            CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            print(f"[calib] {i + 1}/{len(todo)} 已缓存", flush=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    import statistics
    series = {"r5": [v for v in cache["r5"].values() if v is not None],
              "zt": [v for v in cache["zt"].values() if v is not None],
              "zha": [v for v in cache["zha"].values() if v is not None],
              # 空头强度含 2×一字项,历史无源 → 用 dt 单独近似(系统偏左,报告标注)
              "short": [v for v in cache["dt"].values() if v is not None]}
    for k in series:
        series[k].sort()

    rows, flagged = [], []
    for key, anchors in sscore.ANCHORS.items():
        vals = series[key]
        for v, p in anchors:
            n = len([x for x in vals if x <= v])
            emp = (n - 0.5) / len(vals) if vals else None   # 经验 CDF(无重复值校正的近似)
            dev = None if emp is None else p - emp
            row = {"key": key, "value": v, "static": p,
                   "emp": round(emp, 3) if emp is not None else None,
                   "dev_pt": round(dev * 100, 1) if dev is not None else None,
                   "n": len(vals)}
            rows.append(row)
            if dev is not None and abs(dev) > 0.10:
                flagged.append(row)
    rep = {"window": [a.start, a.end], "note": "short 为跌停单指标近似(一字无历史源);"
           "red_rate 无免费历史源未校准", "rows": rows,
           "flagged_over_10pt": flagged,
           "median_abs_dev_pt": {k: round(statistics.median(
               [abs(r["dev_pt"]) for r in rows if r["key"] == k and r["dev_pt"] is not None]), 1)
               for k in sscore.ANCHORS}}
    REPORT.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[calib] 报告 → {REPORT}")
    print(f"[calib] 各指标静态 vs 实测分位中位绝对偏差(pt): {rep['median_abs_dev_pt']}")
    print(f"[calib] |偏差|>10pt 锚点 {len(flagged)} 个:")
    for r in flagged:
        print(f"    {r['key']} v={r['value']} static={r['static']} emp={r['emp']} dev={r['dev_pt']}pt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
