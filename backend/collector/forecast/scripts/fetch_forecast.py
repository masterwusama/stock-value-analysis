# -*- coding: utf-8 -*-
"""业绩预测模块·价格补充采集(2026-10-28 起,首批标的:紫金矿业/广信股份)。

本模块不重复采已有序列:锌(ZN0_CLOSE)/碳酸锂(LC0_CLOSE)/铜(CU0_CLOSE)/金
(SGE_AU9999)/银(AG0_CLOSE)等在 commodity 各商品组,农化全链在 agro 两表——
预测引擎按 code/product_id 直接引用。这里只补两类:

    1. 动力煤基准价(生意社基准价日报列表 news/list--1217,增量累积——该列表
       分页是当日商品分组轮转非时间倒序,只能逐日累积,不能回填;郑商所 ZC 期货
       2022-12 后流动性死亡不可用,2026-10-27 实测新浪 ZC0 止于 2022-12-30)
    2. manual_series(铜精矿TC等人工维护序列)——从 targets.json 原样并入

用法(backend/): python -m collector.run forecast   (调度:每天 07:05)
产物: forecast/data/forecast.json → import_legacy --only-forecast
      (价格→cmd_indicator/cmd_value,commodity='forecast';标的数据→fc_target 表)
"""
import argparse
import io
import json
import os
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

os.environ["NO_PROXY"] = "*"
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)

BACKEND = Path(__file__).resolve().parents[3]
DATA = BACKEND / "collector" / "forecast" / "data"
TARGETS = DATA / "targets.json"
OUT = DATA / "forecast.json"

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126'}
SLEEP = 0.4
RETRY = 3
BENCH_PAGES = 8  # 基准价列表只翻前几页拿当日新行(见模块头注)

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def fetch(url, referer=None):
    last = None
    for i in range(RETRY):
        try:
            h = dict(UA)
            if referer:
                h['Referer'] = referer
            req = urllib.request.Request(url, headers=h)
            return urllib.request.urlopen(req, timeout=25, context=ctx).read().decode('utf-8', 'ignore')
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(SLEEP * (i + 2))
    raise RuntimeError('fetch failed: %s (%r)' % (url, last))


def parse_bench_row(html, name):
    """基准价日报行 → [(date, price)];行内 detail 链接自带完整 YYYYMMDD。"""
    out = []
    for m in re.finditer(
            r'detail-(\d{8})-\d+\.html"[^>]*>\s*\d{1,2}月\d{1,2}日\s*生意社\s*'
            + re.escape(name) + r'基准价为\s*([\d.]+)\s*元/吨', html):
        d = m.group(1)
        out.append(('%s-%s-%s' % (d[:4], d[4:6], d[6:8]), float(m.group(2))))
    return out


def fetch_coal_bench():
    rows, empty = [], 0
    for page in range(1, BENCH_PAGES + 1):
        try:
            html = fetch('https://www.100ppi.com/news/list--1217-%d.html' % page,
                         referer='https://www.100ppi.com/')
        except Exception as e:  # noqa: BLE001
            print('[forecast] 基准价页 %d 失败: %r' % (page, e), flush=True)
            continue
        if '基准价为' not in html:
            if len(html) < 5000:  # 壳页(200 无内容)按瞬断处理,不计数
                time.sleep(SLEEP)
                continue
            empty += 1
            if empty >= 3:
                break
        else:
            empty = 0
        rows += parse_bench_row(html, '动力煤')
        time.sleep(SLEEP)
    # 同日去重取末值
    dedup = {}
    for d, v in rows:
        dedup[d] = v
    return sorted(dedup.items())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    print('[forecast] 补充序列: 动力煤基准价(增量累积) + manual_series(TC等)", '
          '既有锌/锂/铜/金/银/农化由各商品模块供给', flush=True)
    if a.dry_run:
        return 0

    t0 = time.time()
    warnings = []
    series = []

    coal = fetch_coal_bench()
    if coal:
        series.append({
            'code': 'COAL_BENCH', 'idx': 0, 'name': '动力煤(生意社基准价)', 'label': '动力煤',
            'cat': '能源成本', 'group': '煤炭', 'unit': '元/吨', 'freq': '日',
            'source': '生意社基准价日报(增量累积,始于2026-10-28)',
            'points': [[d, v] for d, v in coal],
        })
        print('[forecast] 动力煤基准价: %d 点 %s~%s' % (len(coal), coal[0][0], coal[-1][0]), flush=True)
    else:
        warnings.append('COAL_BENCH: 本轮零收获(列表页无动力煤行或全壳页)')

    # manual_series 原样并入(人工维护,不发包)
    try:
        tj = json.loads(TARGETS.read_text(encoding='utf-8'))
        for i, ms in enumerate(tj.get('manual_series', [])):
            series.append({
                'code': ms['code'], 'idx': 1 + i, 'name': ms['name'],
                'label': ms['name'], 'cat': 'manual', 'group': '人工维护',
                'unit': ms.get('unit', ''), 'freq': ms.get('freq', '月'),
                'source': '人工维护(' + ms.get('note', '')[:40] + ')',
                'points': [[d, v] for d, v, *_ in ms.get('points', [])],
            })
            print('[forecast] manual %s: %d 点' % (ms['code'], len(ms.get('points', []))), flush=True)
    except Exception as e:  # noqa: BLE001
        warnings.append('manual_series: %r' % e)

    all_ds = [p[0] for s in series for p in s['points']]
    payload = {
        'updated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
        'commodity': 'forecast',
        'range': {'begin': min(all_ds), 'end': max(all_ds)} if all_ds else None,
        'warnings': warnings,
        'series': series,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    print('[forecast] 写盘 %s: %d 序列 %d 点,%.1fs' % (
        OUT.name, len(series), len(all_ds), time.time() - t0), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
