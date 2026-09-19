# -*- coding: utf-8 -*-
"""A 股主营构成采集（东财 F10 BusinessAnalysis）→ data/zygc/<code>.json

数据源：emweb PageAjax（akshare 的 stock_zygc_em 封装已失效，直接打同一接口）。
内容：产品/行业/地区三个维度的收入构成，多报告期（截留最近 8 期），含分部毛利率——
详情页「主营业务构成」与业务细分标记的数据面（bm_validity 预研 Phase 1）。

节奏：不进调度器。财报披露后手动跑一轮（全量 ≈45 分钟，0.3s 间隔）；已存在的文件
默认跳过（断点续传），--refresh 整体重抓（上游是整表覆盖，没有真正的增量点）。
全部 A 股；无构成数据（银行/部分新股）写成空档占位，下一轮不再重试。

用法（在 backend/collector 目录下）:
    python -X utf8 scripts/fetch_zygc.py --limit 3      # 冒烟
    python -X utf8 scripts/fetch_zygc.py                # 全量（可中断重跑）
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
ZYGC_DIR = DATA / "zygc"
INDEX_PATH = DATA / "index.json"
KEEP_REPORTS = 8
SLEEP = 0.3
RETRY = 3
URL = "https://emweb.securities.eastmoney.com/PC_HSF10/BusinessAnalysis/PageAjax"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://emweb.securities.eastmoney.com/"}


def load_universe():
    if not INDEX_PATH.exists():
        raise SystemExit("index.json 不存在，先跑一轮抓取")
    idx = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    codes = [c["code"] for c in idx.get("companies") or []
             if (c.get("market") or "A") == "A"]
    if not codes:
        raise SystemExit("index.json 里没有 A 股标的")
    return sorted(codes)


def sec_code(code):
    return ("SH" if code.startswith(("6", "9")) else "SZ") + code


def fetch_one(S, code):
    """→ [{report_date,type,name,income,ratio,gm,rank}]；无构成 → None；失败 → 'FAIL'。"""
    last_err = None
    for attempt in range(RETRY):
        try:
            j = S.get(URL, params={"code": sec_code(code)}, timeout=15).json()
            rows = j.get("zygcfx") or []
            if not rows:
                return None
            # 截留最近 KEEP_REPORTS 个报告期
            days = sorted({str(r.get("REPORT_DATE") or "")[:10] for r in rows}, reverse=True)
            keep = set(days[:KEEP_REPORTS])
            out = []
            for r in rows:
                rd = str(r.get("REPORT_DATE") or "")[:10]
                if rd not in keep:
                    continue
                out.append({
                    "report_date": rd,
                    "type": int(r.get("MAINOP_TYPE") or 0),
                    "name": str(r.get("ITEM_NAME") or "")[:100],
                    "income": r.get("MAIN_BUSINESS_INCOME"),
                    "ratio": r.get("MBI_RATIO"),
                    "gm": r.get("GROSS_RPOFIT_RATIO"),   # 源字段拼写即为 RPOFIT
                    "rank": r.get("RANK"),
                })
            return out
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
    ap = argparse.ArgumentParser(description="A 股主营构成采集（东财 F10）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 只（冒烟）")
    ap.add_argument("--refresh", action="store_true", help="重抓已存在的文件（默认跳过）")
    args = ap.parse_args()

    codes = load_universe()
    if args.limit:
        codes = codes[: args.limit]
    ZYGC_DIR.mkdir(parents=True, exist_ok=True)
    S = requests.Session()
    S.headers.update(HEADERS)
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    done = skip = empty = fail = 0
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        path = ZYGC_DIR / f"{code}.json"
        if path.exists() and not args.refresh:
            skip += 1
            continue
        got = fetch_one(S, code)
        time.sleep(SLEEP)
        if got == "FAIL":
            fail += 1
            continue
        if got is None:
            atomic_write(path, {"code": code, "updated_at": now, "rows": []})
            empty += 1
        else:
            atomic_write(path, {"code": code, "updated_at": now, "rows": got})
            done += 1
        if i % 100 == 0:
            el = time.time() - t0
            eta = el / i * (len(codes) - i)
            print(f"  [{i}/{len(codes)}] 新拉 {done} 跳过 {skip} 空 {empty} 失败 {fail}"
                  f" · 已用 {el / 60:.0f}m · 预计还要 {eta / 60:.0f}m", flush=True)
    total = done + skip + empty + fail
    print(f"完成：{total} 只 · 新拉 {done} · 跳过 {skip} · 空档 {empty} · 失败 {fail}"
          f" · {time.time() - t0:.0f}s", flush=True)
    return 1 if (fail and fail * 20 > total) else 0


if __name__ == "__main__":
    sys.exit(main())
