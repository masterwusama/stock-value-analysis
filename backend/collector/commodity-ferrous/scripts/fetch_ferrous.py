# -*- coding: utf-8 -*-
"""黑色系供需研究·第一层采集(P0:价格/成本/利润/结构,2026-10-27 起)。

黑色系=中国定价权最强的产业链(设计文档同目录系列):地产/基建需求 → 钢材
(螺纹/热卷) → 原料(铁矿双寡头供给/焦煤)。铁矿石普氏指数与港口库存无免费
结构化源记 P2;焦炭与焦煤高度相关,克制不采;铁矿掉期(FE)新浪源坏。

序列清单(10,展示顺序即写入顺序,code 稳定不宜再改):
    价格      RB0_CLOSE+RB0_OI 螺纹主力(上期所,2009-03 上市,元/吨)
              I0_CLOSE+I0_OI 铁矿主力(大商所,2013-10 上市,元/吨)
              JM0_CLOSE+JM0_OI 焦煤主力(大商所,2013-03 上市,元/吨)
              HC0_CLOSE 热卷主力(2014-03,制造业板材)
    自算派生  MILL_MARGIN 钢厂盘面利润 = 螺纹 − 1.6×铁矿 − 0.5×焦煤
              (吨钢原料配比口径;>0 即期钢厂有冶炼利润)
              HC_RB_SPREAD 卷螺差 = 热卷 − 螺纹(制造业 vs 建筑业相对强弱)
    需求      FE_PMI 制造业PMI(月度;独立 code 防共码互灌)

用法(backend/ 目录): python -m collector.run ferrous  (调度:每天 07:05)
产物: commodity-ferrous/data/ferrous.json → import_legacy --only-ferrous。
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
DATA = BACKEND / "collector" / "commodity-ferrous" / "data"
OUT = DATA / "ferrous.json"

S_SINA_FUT = "新浪期货-主力连续"
S_EM = "东方财富"

# 吨钢原料配比:1.6 吨铁矿 + 0.5 吨焦煤(行业通用口径,盘面利润即期近似)
IRON_PER_TON, COAL_PER_TON = 1.6, 0.5


def _pts(df, dcol, vcol):
    s = df[[dcol, vcol]].dropna()
    out = {}
    for d, v in zip(s[dcol], s[vcol]):
        out[str(d)[:10]] = float(v)
    return sorted(out.items())


def fetch_fut(symbol):
    df = ak.futures_main_sina(symbol=symbol)
    close = _pts(df, "日期", "收盘价")
    oi = _pts(df, "日期", "持仓量")
    return close, oi


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


def derive(series_map):
    """自算派生:钢厂盘面利润(三序列按日对齐)、卷螺差。"""
    out, notes = {}, []
    rb, iron, coal = (series_map.get("RB0_CLOSE"), series_map.get("I0_CLOSE"),
                      series_map.get("JM0_CLOSE"))
    if rb and iron and coal:
        mi, mc = dict(iron), dict(coal)
        margin = []
        for d, v in rb:
            if d in mi and d in mc:
                margin.append((d, round(v - IRON_PER_TON * mi[d] - COAL_PER_TON * mc[d], 1)))
        out["MILL_MARGIN"] = sorted(margin)
    else:
        notes.append("钢厂利润缺原料(螺纹/铁矿/焦煤)")
    hc = series_map.get("HC0_CLOSE")
    if hc and rb:
        mr = dict(rb)
        out["HC_RB_SPREAD"] = sorted(
            (d, round(v - mr[d], 1)) for d, v in hc if d in mr)
    else:
        notes.append("卷螺差缺原料(热卷/螺纹)")
    return out, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印抓取计划")
    a = ap.parse_args()
    plan = [
        ("价格", "RB0 螺纹(2009→) / I0 铁矿(2013→) / JM0 焦煤(2013→) 量仓"),
        ("价格", "HC0 热卷(2014→,制造业板材)"),
        ("自算派生", "MILL_MARGIN 钢厂盘面利润(螺纹-1.6铁矿-0.5焦煤) / HC_RB_SPREAD 卷螺差"),
        ("需求", "FE_PMI 制造业PMI(2008→月度,独立code防共码)"),
    ]
    print("[ferrous] P0 序列计划: %d 组" % len(plan), flush=True)
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
                    print("[ferrous] %s 失败: %s %s" % (
                        code, type(e).__name__, str(e)[:120]), flush=True)
                    return None
                print("[ferrous] %s 第%d次失败(%s),8秒后重试" % (
                    code, i + 1, type(e).__name__), flush=True)
                time.sleep(8)
        print("[ferrous] %s: %s" % (code, _sum(pts)), flush=True)
        return pts

    raw = {}
    rb_pair = grab("螺纹组", lambda: tuple(fetch_fut("RB0"))) or (None, None)
    i_pair = grab("铁矿组", lambda: tuple(fetch_fut("I0"))) or (None, None)
    jm_pair = grab("焦煤组", lambda: tuple(fetch_fut("JM0"))) or (None, None)
    hc_pair = grab("热卷组", lambda: tuple(fetch_fut("HC0"))) or (None, None)
    singles = [
        ("RB0_CLOSE", lambda: rb_pair[0]),
        ("RB0_OI", lambda: rb_pair[1]),
        ("I0_CLOSE", lambda: i_pair[0]),
        ("I0_OI", lambda: i_pair[1]),
        ("JM0_CLOSE", lambda: jm_pair[0]),
        ("JM0_OI", lambda: jm_pair[1]),
        ("HC0_CLOSE", lambda: hc_pair[0]),
        ("FE_PMI", fetch_pmi),
    ]
    for code, fn in singles:
        raw[code] = grab(code, fn)
        time.sleep(0.4)
    derived, dnotes = derive({k: v for k, v in raw.items() if v})
    for code, pts in derived.items():
        raw[code] = pts
        print("[ferrous] %s(派生): %d 点 %s~%s" % (code, len(pts), pts[0][0], pts[-1][0]), flush=True)
    warnings += dnotes

    # 展示清单(顺序=页面顺序);HC0_OI 是中间量不进清单
    SPEC = {
        "RB0_CLOSE":   ("螺纹钢主力收盘", "螺纹钢(主力)", "价格", "钢材", "元/吨", "日",
                        S_SINA_FUT + "(上期所,2009-03 上市;地产/基建需求)"),
        "I0_CLOSE":    ("铁矿石主力收盘", "铁矿石(主力)", "价格", "原料", "元/吨", "日",
                        S_SINA_FUT + "(大商所,2013-10 上市;澳巴双寡头供给)"),
        "JM0_CLOSE":   ("焦煤主力收盘", "焦煤(主力)", "价格", "原料", "元/吨", "日",
                        S_SINA_FUT + "(大商所,2013-03 上市;山西安监/澳煤进口是供给变量)"),
        "MILL_MARGIN": ("钢厂盘面利润(螺纹-1.6矿-0.5煤)", "钢厂盘面利润", "价格", "利润", "元/吨", "日",
                        "自算(吨钢配比 1.6 矿+0.5 煤;>0=即期有冶炼利润,持续>500 刺激复产)"),
        "HC_RB_SPREAD": ("卷螺差(热卷-螺纹)", "卷螺差", "价格", "结构", "元/吨", "日",
                         "自算(制造业板材 vs 建筑业长材的相对强弱)"),
        "HC0_CLOSE":   ("热卷主力收盘", "热卷(主力)", "价格", "钢材", "元/吨", "日",
                        S_SINA_FUT + "(上期所,2014-03 上市,制造业板材)"),
        "RB0_OI":      ("螺纹钢主力持仓量", "螺纹持仓量", "持仓库存", "期指持仓", "手", "日", S_SINA_FUT),
        "I0_OI":       ("铁矿石主力持仓量", "铁矿持仓量", "持仓库存", "期指持仓", "手", "日", S_SINA_FUT),
        "JM0_OI":      ("焦煤主力持仓量", "焦煤持仓量", "持仓库存", "期指持仓", "手", "日", S_SINA_FUT),
        "FE_PMI":      ("中国制造业PMI", "制造业PMI", "需求", "景气", "%", "月",
                        S_EM + "(官方口径,当月首日)"),
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
        "commodity": "ferrous",
        "range": {"begin": min(all_ds), "end": max(all_ds)} if all_ds else None,
        "warnings": warnings,
        "series": series_out,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    total = sum(len(s["points"]) for s in series_out)
    print("[ferrous] 写盘 %s: %d 序列 %d 点,失败 %d(%s),耗时 %.1fs" % (
        OUT.name, len(series_out), total, len(warnings),
        ",".join(w.split(":")[0] for w in warnings) or "-", time.time() - t0), flush=True)
    failed = len(SPEC) - len(series_out)
    return 1 if failed > len(SPEC) / 2 else 0


if __name__ == "__main__":
    sys.exit(main())
