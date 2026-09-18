# -*- coding: utf-8 -*-
"""A 股个股日线（后复权收盘价）回补 —— 收益侧验证的数据面。

为 scripts/rr_validity.py（R 的收益侧回测）供数：按「信号日 → 其后 1/2/3 年」计算点对点
总收益，需要任意日期的复权收盘价。选**后复权** hfq 而不是前复权 qfq：qfq 以最新价为锚，
每次分红除权都会重写整段历史，存量文件与新拉段落会接不上；hfq 锚在上市日，新数据只会
追加、历史不变，断点续传天然成立。同一段区间内两点间的涨跌幅，qfq 与 hfq 数学上相等，
存 hfq 不损失任何收益口径。

数据源用**新浪**（akshare stock_zh_a_daily，单请求全历史，实测 0.6s/只）：东财 kline
（push2his）按 IP 限流，2026-09-18 全量首拉即被拒连（RemoteDisconnected，同机其它
东财主机正常）；腾讯 fqkline 单次上限 640 根、全历史要拼 4 次请求。新浪 hfq 与东财 hfq
锚点不同（末收绝对值不同）——点对点收益只依赖序列内部一致性，跨源混档才会出问题，
所以统一用新浪一源。北交所代码新浪覆盖不全，抓不到的写成空档占位（收益回测里判不动）。

产物：data/prices/<code>.json = {code, updated_at, start, dates: [...], closes: [...]}
（紧凑数组，日期与收盘价一一对应、升序）。约 5,500 只 × 2019 年至今 ≈ 1,800 交易日，
单只 ~30KB、全量 ~170MB——该目录 gitignore，换机器重拉即可。

节奏：一次性回补（≈75 分钟，0.35s 间隔 + 3 次重试）；已存在的文件默认跳过（断点续传），
--refresh 时只从各文件最后一天续拉。不进调度器——收益侧回测是离线研究，不需要日更；
哪天要接实时，把本脚本挂进 run.py 的 JOBS 再议。

用法（在 backend/collector 目录下）：
    python -X utf8 scripts/fetch_daily_prices.py --limit 3     # 冒烟
    python -X utf8 scripts/fetch_daily_prices.py               # 全量回补（可中断重跑）
    python -X utf8 scripts/fetch_daily_prices.py --refresh     # 从各文件末日续拉
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
PRICE_DIR = DATA / "prices"
INDEX_PATH = DATA / "index.json"
START_DEFAULT = "20190101"      # 面板信号年 2021 的披露在 2022 年、前瞻窗最远到 2025，
                                # 2019 起步留足余量；再往前对收益验证没有增量
SLEEP = 0.35
RETRY = 3


def load_universe():
    codes = []
    if INDEX_PATH.exists():
        idx = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        codes = [c["code"] for c in idx.get("companies") or []
                 if (c.get("market") or "A") == "A"]
    if not codes:
        raise SystemExit("index.json 不存在或没有 A 股标的，先跑一轮抓取")
    return sorted(codes)


def sina_symbol(code):
    """6 位代码 → 新浪符号（sh/sz 前缀；北交所 4/8/9 开头给 bj，新浪覆盖不全、抓不到落空档）。"""
    if code.startswith(("4", "8", "9")):
        return "bj" + code
    return ("sh" if code.startswith(("6", "9")) else "sz") + code


def fetch_one(ak, code, start, end="20991231"):
    """单只 → (dates, closes)；新浪日线（单请求全历史），升序。空表返回 None（退市/不覆盖）。"""
    last_err = None
    for attempt in range(RETRY):
        try:
            df = ak.stock_zh_a_daily(symbol=sina_symbol(code),
                                     start_date=start, end_date=end, adjust="hfq")
            if df is None or df.empty:
                return None
            dates = [str(x)[:10] for x in df["date"]]
            closes = [float(x) for x in df["close"]]
            if len(dates) != len(closes) or not dates:
                return None
            return dates, closes
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    print(f"  [fail] {code}: {last_err!r}", flush=True)
    return "FAIL"


def atomic_write(path, payload):
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="A 股日线（hfq 收盘）回补，只写 data/prices/")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 只（冒烟）")
    ap.add_argument("--refresh", action="store_true",
                    help="已存在的文件从最后一天续拉（默认跳过）")
    ap.add_argument("--start", default=START_DEFAULT, help="回补起点 YYYYMMDD")
    args = ap.parse_args()

    import akshare as ak

    codes = load_universe()
    if args.limit:
        codes = codes[: args.limit]
    PRICE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    done = skip = empty = fail = 0
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        path = PRICE_DIR / f"{code}.json"
        start = args.start
        existing = None
        if path.exists():
            if not args.refresh:
                skip += 1
                continue
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if existing.get("dates"):
                    start = existing["dates"][-1].replace("-", "")
            except Exception:  # noqa: BLE001  坏档当无档重拉
                existing = None
        got = fetch_one(ak, code, start)
        time.sleep(SLEEP)
        if got == "FAIL":
            fail += 1
            continue
        if got is None:
            # 空表：老代码（退市/更名）没有行情。写成空档占位，断点续传不会反复重试
            atomic_write(path, {"code": code, "updated_at": now, "start": start,
                                "dates": [], "closes": []})
            empty += 1
            continue
        dates, closes = got
        if existing and existing.get("dates") and dates:
            # 续拉段与存量拼接：东财 start_date 含当日，交界日去重
            if dates[0] <= existing["dates"][-1]:
                dates = dates[1:]
                closes = closes[1:]
        merged_dates = (existing["dates"] + dates) if existing else dates
        merged_closes = (existing["closes"] + closes) if existing else closes
        atomic_write(path, {"code": code, "updated_at": now,
                            "start": (merged_dates[0] if merged_dates else start),
                            "dates": merged_dates, "closes": merged_closes})
        done += 1
        if i % 100 == 0:
            el = time.time() - t0
            eta = el / i * (len(codes) - i)
            print(f"  [{i}/{len(codes)}] 新拉 {done} 跳过 {skip} 空 {empty} 失败 {fail}"
                  f" · 已用 {el / 60:.0f}m · 预计还要 {eta / 60:.0f}m", flush=True)
    total = done + skip + empty + fail
    print(f"完成：{total} 只 · 新拉 {done} · 跳过 {skip} · 空档 {empty} · 失败 {fail}"
          f" · {time.time() - t0:.0f}s", flush=True)
    # 失败率 >5% 记失败：断档的日线会让收益验证静默丢样本
    return 1 if (fail and fail * 20 > total) else 0


if __name__ == "__main__":
    sys.exit(main())
