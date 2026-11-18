# -*- coding: utf-8 -*-
"""社保及外资持仓·历史期一次性回填(2026-11-18 P0,设计:docs/设计-社保及外资持仓模块.md)。

从日历倒推近 BACKFILL_N+1 个标准报告期(季末 03-31/06-30/09-30/12-31),当期(最新
一个)留给 fetch_inst.py 周任务,其余逐期逐类拉全量 → data/freeholders_{期}.json。
文件一旦落盘即冻结(与设计「历史期不改」一致),已存在则跳过(--force 覆盖)。

用法(backend/): python collector/inst/scripts/backfill_inst.py [--force] [--periods 12]
回灌: 与当期同一张表,import_legacy --only-inst 一并读入。
"""
import argparse
import json
import ssl
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[3]
DATA = BACKEND / "collector" / "inst" / "data"

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126'}
SLEEP = 0.4
RETRY = 3
PAGE = 1000

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

BASE = ('https://datacenter-web.eastmoney.com/api/data/v1/get'
        '?reportName=RPT_F10_EH_FREEHOLDERS&source=WEB&client=WEB'
        '&pageSize=%d&pageNumber=%%d&columns=ALL&filter=%%s' % PAGE)

# 注意:backfill 按 END_DATE=标准季末 过滤,天然只取该期,不加 IS_MAX
TYPES = [
    ("社保", '(HOLDER_NEWTYPE="社保")'),
    ("QFII", '(HOLDER_NEWTYPE="QFII")'),
    ("保险", '(HOLDER_NEWTYPE="保险")'),
    ("养老", '(HOLDER_TYPE="基本养老基金")'),
]

# 行归一从 fetch_inst import(同一份口径,双实现防漂移)


def fetch(url):
    last = None
    for i in range(RETRY):
        try:
            req = urllib.request.Request(url, headers=UA)
            return json.loads(urllib.request.urlopen(
                req, timeout=25, context=ctx).read().decode('utf-8', 'ignore'))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(SLEEP * (i + 2))
    raise RuntimeError('fetch failed: %s (%r)' % (url[:120], last))


def fetch_all(filter_expr):
    rows, page = [], 1
    while True:
        d = fetch(BASE % (page, urllib.parse.quote(filter_expr)))
        r = d.get('result')
        if not r or not r.get('data'):
            break
        rows.extend(r['data'])
        if len(rows) >= r.get('count', 0) or page >= 60:
            break
        page += 1
        time.sleep(SLEEP)
    return rows


def norm_row(r, holder_type):
    from fetch_inst import norm_row as _norm  # 同一份归一口径,双实现防漂移
    x = _norm(r, holder_type)
    x["is_std_period"] = True  # 回填按 END_DATE=标准季末过滤,恒为标准期
    return x


def std_periods(n, today=None):
    """倒推 n 个季末(不含当前未收官季);升序返回。"""
    today = today or date.today()
    out = []
    y, q = today.year, (today.month - 1) // 3 - 1  # 当前季未收官,从上一季末起
    if q < 0:
        q, y = 3, y - 1
    for _ in range(n):
        mm = (q + 1) * 3
        out.append("%04d-%02d-%02d" % (y, mm, {3: 31, 6: 30, 9: 30, 12: 31}[mm]))
        q -= 1
        if q < 0:
            q, y = 3, y - 1
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--periods", type=int, default=12, help="回填期数(不含当期)")
    ap.add_argument("--force", action="store_true", help="已存在的期文件也重拉覆盖")
    args = ap.parse_args()

    periods = std_periods(args.periods + 1)
    current, past = periods[-1], periods[:-1]
    print(f"[backfill] 当期 {current} 留给周任务;回填 {len(past)} 期: {past[0]} → {past[-1]}",
          flush=True)
    DATA.mkdir(parents=True, exist_ok=True)
    for p in past:
        tag = p.replace("-", "")
        out = DATA / f"freeholders_{tag}.json"
        if out.exists() and not args.force:
            print(f"[backfill] {p} 已存在,跳过", flush=True)
            continue
        rows, seen = [], set()
        for tname, fexpr in TYPES:
            raw = fetch_all("(%s)(END_DATE='%s')" % (fexpr, p))
            for r in raw:
                x = norm_row(r, tname)
                key = (x["holder_code"], x["holder_name"], x["secu_code"],
                       x["end_date"], x["shares_type"])
                if key in seen:
                    continue
                seen.add(key)
                rows.append(x)
            print(f"[backfill] {p} {tname}: {len(raw)} 行", flush=True)
            time.sleep(SLEEP)
        payload = {"period": p, "updated_at": None,  # 冻结文件,更新时刻以库 upsert 为准
                   "rows": rows}
        out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                       encoding="utf-8")
        print(f"[backfill] {p}: 落盘 {len(rows)} 行 → {out.name}", flush=True)
    print("[backfill] 完成", flush=True)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
