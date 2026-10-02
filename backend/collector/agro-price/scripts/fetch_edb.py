# -*- coding: utf-8 -*-
"""EDB 行业量价采集(2026-10-02 起:全免费 HTTP 源,不再依赖 Wind 客户端,云端可跑)。

背景与收缩决策(用户拍板):原 44 个 Wind EDB 指标中,实测无稳定免费源的 34 个
整体删除——中汽协产销、CCFI/SCFI(航交所无接口)、卓创开工率、秦港 Q5500、
Mysteel 社会库存、统计局产量与地产累计值(官方库 data.stats.gov.cn 反爬拦截)、
海关商品级进口(接口只有总量)、LME 外盘符号未验成。保留 10 个并全部换源,
新指标代码与老 Wind S 码完全不同,历史不混拼;老行由 import_edb 的全量清单
清理删除(edb.json 即本模块唯一清单)。四个源一次调用返回全量历史,免费源
没有积分经济学,周任务整份重写 edb.json,增量窗口/前值缓冲一并退役:

    新浪期货主力连续(日频折周): 沪铝 AL0 / 氧化铝 AO0 / 天然橡胶 RU0 / 螺纹钢 RB0
        —— 替代原现货口径(A00 现货/现货胶/螺纹现货),期现基差存在,趋势与
           分位用途不受影响,口径写在 source 字段;氧化铝 2023-06 上市,历史天然短
    波罗的海系指数(日频折周): BDI + BDTI(macro_china_freight_index 一次两列)
    70城新房价格同比(月频): 东财 new_house_price,70城简单平均自算(口径=自算均值)
    沪铝主力月均(月频): AL0 日频自算月均(原 Wind「铝锭月均」现货口径随现货退役)

用法(backend/ 目录): python -m collector.run edb   (调度:周日 20:00,scheduler.py)
产物: agro-price/data/edb.json → import_legacy --no-clean 回灌两表。
"""
import argparse
import io
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

# 本机系统代理会拦断行情/东财域名,与 fetch_sentiment 同因,单独直连
os.environ["NO_PROXY"] = "*"
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import akshare as ak  # noqa: E402

BACKEND = Path(__file__).resolve().parents[3]
DATA = BACKEND / "collector" / "agro-price" / "data"
OUT = DATA / "edb.json"

# 分类定义(展示顺序)。2026-10-02 收缩后:auto/coal 两类整体删除。
CATS = [
    {"id": "alu", "name": "电解铝"},
    {"id": "shipping", "name": "航运"},
    {"id": "tire", "name": "轮胎橡胶"},
    {"id": "realestate", "name": "地产链"},
    {"id": "steel", "name": "钢铁"},
]

# 期货主力连续:symbol -> (code, 源名, 展示名, 分类, 分组, 单位)
FUTURES = {
    "AL0": ("AL_MAIN", "沪铝主力连续", "沪铝(主力连续)", "alu", "价格", "元/吨"),
    "AO0": ("AO_MAIN", "氧化铝主力连续", "氧化铝(主力连续)", "alu", "成本", "元/吨"),
    "RU0": ("RU_MAIN", "天然橡胶主力连续", "天然橡胶(主力连续)", "tire", "原料价格", "元/吨"),
    "RB0": ("RB_MAIN", "螺纹钢主力连续", "螺纹钢(主力连续)", "steel", "价格", "元/吨"),
}
SRC_FUTURES = "新浪期货-主力连续"
SRC_FREIGHT = "东方财富-波罗的海系运价指数"
SRC_HOUSE = "东方财富-70城房价(简单平均自算)"


def fetch_futures(symbol):
    """新浪主力连续全量日线 → [(YYYY-MM-DD, 收盘)]。"""
    df = ak.futures_main_sina(symbol=symbol)
    s = df[["日期", "收盘价"]].dropna()
    return [(d.isoformat() if hasattr(d, "isoformat") else str(d)[:10], float(v))
            for d, v in zip(s["日期"], s["收盘价"])]


def fetch_freight():
    """东财宏观-运价指数表(降序) → {code: [(date, val)]},BDI 与 BDTI 一次拿。"""
    df = ak.macro_china_freight_index()
    out = {}
    for col, code in (("波罗的海综合运价指数BDI", "BDI"),
                      ("油轮运价指数原油运价指数BDTI", "BDTI")):
        s = df[["截止日期", col]].dropna()
        pts = [(str(d)[:10], float(v)) for d, v in zip(s["截止日期"], s[col])]
        pts.sort()
        out[code] = pts
    return out


def fetch_house70():
    """70城新房价格同比指数 → 70城简单平均,指数-100 转同比%(月频)。"""
    df = ak.macro_china_new_house_price()
    col = "新建商品住宅价格指数-同比"
    s = df.dropna(subset=[col])
    g = s.groupby("日期")[col].mean()
    return [(d.isoformat() if hasattr(d, "isoformat") else str(d)[:10],
             round(float(v) - 100.0, 2)) for d, v in sorted(g.items())]


def collapse_weekly(points):
    """日频→每周最后一点(ISO 周)。同周后写覆盖前写,周内数据更新整周重写。"""
    last = {}
    for d, v in points:
        key = date.fromisoformat(d).isocalendar()[:2]
        last[key] = (d, v)
    return sorted(last.values())


def month_mean(points):
    """日频→月均,锚该月最后一个交易日(月均口径写在指标名里)。"""
    acc, last_d = {}, {}
    for d, v in points:
        acc.setdefault(d[:7], []).append(v)
        last_d[d[:7]] = d
    return [(last_d[m], round(sum(vs) / len(vs), 2)) for m, vs in sorted(acc.items())]


def _ind(code, name, label, cat, group, unit, freq, source, points):
    return {"code": code, "name": name, "label": label, "unit": unit,
            "freq": freq, "source": source, "group": group, "points": points}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印本轮抓取计划(源/指标/预计点数口径),不发请求")
    a = ap.parse_args()

    print("[edb] 免费源清单: 期货主力×%d(折周) + BDI/BDTI(折周) + 70城同比(月) "
          "+ 沪铝月均(自算)" % len(FUTURES), flush=True)
    if a.dry_run:
        for sym, (code, name, cat, *_r) in FUTURES.items():
            print("  [plan] %s %s(%s) 新浪期货全量日线折周" % (code, name, cat))
        print("  [plan] BDI/BDTI 东财运价指数表全量折周")
        print("  [plan] HOUSE70_YOY 东财70城月度同比均值")
        print("  [plan] AL_MAIN_MA 沪铝日线自算月均")
        return 0

    t0 = time.time()
    futures_daily = {}
    for sym in FUTURES:
        futures_daily[sym] = fetch_futures(sym)
        print("[edb] %s: %d 个交易日 %s~%s" % (
            sym, len(futures_daily[sym]),
            futures_daily[sym][0][0], futures_daily[sym][-1][0]), flush=True)
        time.sleep(0.5)
    freight = fetch_freight()
    for code, pts in freight.items():
        print("[edb] %s: %d 点 %s~%s" % (code, len(pts), pts[0][0], pts[-1][0]), flush=True)
    house = fetch_house70()
    print("[edb] HOUSE70: %d 个月 %s~%s" % (len(house), house[0][0], house[-1][0]), flush=True)

    by_cat = {c["id"]: [] for c in CATS}
    for sym, (code, name, label, cat, group, unit) in FUTURES.items():
        by_cat[cat].append(_ind(code, name, label, cat, group, unit, "周",
                                SRC_FUTURES, collapse_weekly(futures_daily[sym])))
    # 沪铝月均与沪铝周频同源,口径差异(月均=日频算术平均)已写进名称与 source
    by_cat["alu"].append(_ind(
        "AL_MAIN_MA", "沪铝主力月均", "沪铝(主力月均)", "alu", "价格", "元/吨", "月",
        "自算月均(新浪期货-主力连续)", month_mean(futures_daily["AL0"])))
    for code, label in (("BDI", "BDI(干散货)"), ("BDTI", "BDTI(原油运输)")):
        by_cat["shipping"].append(_ind(
            code, "波罗的海%s" % ("干散货指数" if code == "BDI" else "原油运输指数"),
            label, "shipping", "散货油运", "点", "周", SRC_FREIGHT,
            collapse_weekly([p for p in freight[code] if p[0] >= "1995-01-01"])))
    by_cat["realestate"].append(_ind(
        "HOUSE70_YOY", "70城新房价格同比", "70城新房同比", "realestate", "价格",
        "%", "月", SRC_HOUSE, house))

    cats_out = []
    for c in CATS:
        inds = by_cat[c["id"]]
        ds = [p[0] for i in inds for p in i["points"]]
        cats_out.append({
            "id": c["id"], "name": c["name"],
            "range": {"begin": min(ds), "end": max(ds)} if ds else None,
            "indicators": inds,
        })
    all_ds = [p[0] for i in (ind for c in cats_out for ind in c["indicators"])
              for p in i["points"]]
    payload = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "range": {"begin": min(all_ds), "end": max(all_ds)},
        "categories": cats_out,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    total = sum(len(i["points"]) for c in cats_out for i in c["indicators"])
    print("[edb] 写盘 %s: %d 类 %d 指标 %d 点,耗时 %.1fs" % (
        OUT.name, len(cats_out),
        sum(len(c["indicators"]) for c in cats_out), total, time.time() - t0), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
