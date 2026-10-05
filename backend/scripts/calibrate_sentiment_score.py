# -*- coding: utf-8 -*-
"""温度锚点校准:用历史数据实测各指标分位,对照 static-v1 软锚。

设计文档 docs/设计-情绪温度与极端度.md §4.2。**2026-10-02 实测数据源边界**:
    - R5:国证2000 指数日线自算——唯一有深历史的精确源(2024-01~2026-09 全窗口);
    - 东财三大池(涨停/炸板/跌停)**均只开放近 ~30 个交易日**,且 akshare 对更早
      日期返回空表不报错——老日期的 0 是假值,绝不能当真样本用;池子三项因此
      只做近 30 日的尾部体检,不采纳为锚点;
    - 一字跌停、红盘率:免费源无历史,保持软锚。
结论存档(2026-10-02 首轮):R5 十锚点偏差尾部 ≤1pt、最差中段 8.7pt,无一超
10pt 采纳阈值 → static-v1 维持原样,报告留作 60 交易日滚动切换时的对照基线。

产物:collector/data/sentiment/calib_series.json(逐日原始序列,断点续跑)
     + calib_report.json(静态锚 vs 实测分位偏差;|偏差|>10pt 的锚点列入待调)。

用法(backend/ 目录): python -X utf8 -m scripts.calibrate_sentiment_score
    [--start 2024-01-01 --end 2026-09-30] [--refresh]
"""
import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

# 与 fetch_sentiment 同因:系统代理拦断行情域,名单独直连
os.environ["NO_PROXY"] = "*"
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "collector" / "scripts"))
import sentiment_score as sscore  # noqa: E402

OUT = BACKEND / "collector" / "data" / "sentiment"
CACHE = OUT / "calib_series.json"
REPORT = OUT / "calib_report.json"
POOL_WINDOW = 30          # 东财三池的有效回溯窗口(交易日)


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
    return {d: closes[pos[d]] / closes[pos[d] - 5] - 1
            for d in days if pos[d] >= 5}


def pool_counts(day):
    """东财三池当日家数;单池失败不拦整体(计 None,分位计算剔除)。"""
    import akshare as ak
    out = {}
    for key, fn in (("zt", ak.stock_zt_pool_em), ("zha", ak.stock_zt_pool_zbgc_em),
                    ("dt", ak.stock_zt_pool_dtgc_em)):
        try:
            df = fn(date=day)
            # 空表与异常同罪:老日期东财返空不报错,当 None 而不是 0
            out[key] = int(len(df)) if len(df) else None
        except Exception:  # noqa: BLE001
            out[key] = None
        time.sleep(0.25)
    return out


def pctile(sorted_vals, q):
    """线性插值分位(numpy 语义,免依赖)。"""
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
    cache.setdefault("r5", {}).update(
        {d: round(v, 6) for d, v in r5_series(idx, days).items()})

    # 池子只拉有效窗口内的日子(更早的必然空表/报错,还容易把无超时的请求挂死)。
    # 缓存值为 None 说明上次拉失败:必须重拉,否则失败日被永久跳过
    pool_days = set(days[-POOL_WINDOW:])
    todo = [d for d in days if d in pool_days
            and (a.refresh or cache.get("zt", {}).get(d) is None)]
    print(f"[calib] 窗口 {a.start}~{a.end} 共 {len(days)} 交易日;R5 全窗口,"
          f"池子窗口 {len(pool_days)} 天(待拉 {len(todo)})", flush=True)
    for i, d in enumerate(todo):
        counts = pool_counts(d)
        for k in ("zt", "zha", "dt"):
            cache.setdefault(k, {})[d] = counts[k]
        if (i + 1) % 10 == 0:
            CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            print(f"[calib] {i + 1}/{len(todo)} 已缓存", flush=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    series = {"r5": [v for v in cache["r5"].values() if v is not None]}
    # 池子样本只取有效窗口内的日期(历史遗留缓存里的老日期假 0 一律丢弃)
    for k in ("zt", "zha", "dt"):
        series[k] = [v for d, v in cache.get(k, {}).items() if d in pool_days and v is not None]
    for k in series:
        series[k].sort()
    # 炸板锚点是「率」不是「家数」:zha/(zt+zha) 同日现算,免得拿 8~30 的家数去比 0.1~0.7 的率
    zt_by_day = {d: v for d, v in cache.get("zt", {}).items() if d in pool_days and v is not None}
    rates = [z / (z + t) for d, z in cache.get("zha", {}).items()
             if d in pool_days and z is not None and zt_by_day.get(d) is not None
             for z, t in [(z, zt_by_day[d])] if z + t > 0]
    series["zha"] = sorted(rates)
    # 空头强度含 2×一字项,历史无源 → 用 dt 单独近似(系统偏左,报告标注);
    # red 无源不参与;short 键名对齐锚点表
    series["short"] = series.pop("dt")

    rows, flagged = [], []
    for key, anchors in sscore.ANCHORS.items():
        vals = series.get(key, [])
        for v, p in anchors:
            if not vals:
                rows.append({"key": key, "value": v, "static": p, "emp": None,
                             "dev_pt": None, "n": 0})
                continue
            n = len([x for x in vals if x <= v])
            # 锚点值低于样本最小值时 n=0,不减 0.5 会出负分位;钳到 [0,1]
            emp = min(1.0, max(0.0, (n - 0.5) / len(vals)))   # 经验 CDF 近似
            dev = p - emp
            row = {"key": key, "value": v, "static": p, "emp": round(emp, 3),
                   "dev_pt": round(dev * 100, 1), "n": len(vals)}
            rows.append(row)
            if abs(dev) > 0.10:
                flagged.append(row)
    rep = {"window": [a.start, a.end],
           "note": "R5=指数日线精确全窗口;zt/zha/short 仅近 %d 交易日尾部体检"
                   "(东财池历史边界)不采纳为锚点;red_rate 无免费历史源未校准;"
                   "short 为跌停单指标近似(一字无源,系统偏左)" % POOL_WINDOW,
           "rows": rows, "flagged_over_10pt": flagged,
           "median_abs_dev_pt": {k: round(statistics.median(
               [abs(r["dev_pt"]) for r in rows if r["key"] == k and r["dev_pt"] is not None]), 1)
               for k in sscore.ANCHORS if any(r["key"] == k and r["dev_pt"] is not None
                                              for r in rows)}}
    REPORT.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[calib] 报告 → {REPORT}")
    print(f"[calib] 各指标静态 vs 实测分位中位绝对偏差(pt): {rep['median_abs_dev_pt']}")
    print(f"[calib] |偏差|>10pt 锚点 {len(flagged)} 个:")
    for r in flagged:
        print(f"    {r['key']} v={r['value']} static={r['static']} emp={r['emp']} dev={r['dev_pt']}pt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
