# -*- coding: utf-8 -*-
"""农产品(生猪链)供需研究·第一层采集(P0:价格/饲料/粮/比价,2026-10-27 起)。

猪周期对中国 CPI 影响权重最大(设计文档同目录系列):生猪价格 ÷ 玉米价格 =
猪粮比,官方有 6:1 盈亏平衡预警线(低于触发收储)。能繁母猪存栏(农业农村部
月度)无结构化免费源记 P2。鸡蛋作为蛋鸡链的另一个观察口顺带收录。

序列清单(8,展示顺序即写入顺序,code 稳定不宜再改):
    价格      LH0_CLOSE+LH0_OI 生猪主力(大商所,2021-01 上市,元/吨;历史短但
              是唯一可交易的猪价序列) / M0_CLOSE+M0_OI 豆粕主力(2000 上市,
              饲料蛋白核心) / C0_CLOSE 玉米主力(2004,能量饲料锚)
              JD0_CLOSE 鸡蛋主力(2013,蛋鸡链观察口)
    自算派生  HOG_FEED 猪粮比 = 生猪(元/公斤) ÷ 玉米(元/公斤),即 LH/C 直比
              (期货口径近似官方猪粮比;6 以下深度亏损触发收储预期,9 以上利润丰厚)

用法(backend/ 目录): python -m collector.run agri  (调度:每天 07:15)
产物: commodity-agri/data/agri.json → import_legacy --only-agri。
注意:与 agro-price(农化制品,#/agro 页)是两个模块,agri 是大宗商品矩阵成员。
"""
import argparse
import json
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
DATA = BACKEND / "collector" / "commodity-agri" / "data"
OUT = DATA / "agri.json"

S_SINA_FUT = "新浪期货-主力连续"


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


def derive(series_map):
    """自算派生:猪粮比(生猪元/吨 ÷ 玉米元/吨,量纲消掉后即元/公斤直比)。"""
    out, notes = {}, []
    lh, c = series_map.get("LH0_CLOSE"), series_map.get("C0_CLOSE")
    if lh and c:
        mc = dict(c)
        out["HOG_FEED"] = sorted(
            (d, round(v / mc[d], 3)) for d, v in lh if d in mc and mc[d])
    else:
        notes.append("猪粮比缺原料(生猪/玉米)")
    return out, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印抓取计划")
    a = ap.parse_args()
    plan = [
        ("价格", "LH0 生猪(2021→) / M0 豆粕(2005→) / C0 玉米(2005→) / JD0 鸡蛋(2013→)"),
        ("自算派生", "HOG_FEED 猪粮比(生猪/玉米;6 以下深度亏损,官方预警线)"),
    ]
    print("[agri] P0 序列计划: %d 组" % len(plan), flush=True)
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
                    print("[agri] %s 失败: %s %s" % (
                        code, type(e).__name__, str(e)[:120]), flush=True)
                    return None
                print("[agri] %s 第%d次失败(%s),8秒后重试" % (
                    code, i + 1, type(e).__name__), flush=True)
                time.sleep(8)
        print("[agri] %s: %s" % (code, _sum(pts)), flush=True)
        return pts

    raw = {}
    lh_pair = grab("生猪组", lambda: tuple(fetch_fut("LH0"))) or (None, None)
    m_pair = grab("豆粕组", lambda: tuple(fetch_fut("M0"))) or (None, None)
    c_pair = grab("玉米组", lambda: tuple(fetch_fut("C0"))) or (None, None)
    jd_pair = grab("鸡蛋组", lambda: tuple(fetch_fut("JD0"))) or (None, None)
    singles = [
        ("LH0_CLOSE", lambda: lh_pair[0]),
        ("LH0_OI", lambda: lh_pair[1]),
        ("M0_CLOSE", lambda: m_pair[0]),
        ("M0_OI", lambda: m_pair[1]),
        ("C0_CLOSE", lambda: c_pair[0]),
        ("JD0_CLOSE", lambda: jd_pair[0]),
    ]
    for code, fn in singles:
        raw[code] = grab(code, fn)
        time.sleep(0.4)
    derived, dnotes = derive({k: v for k, v in raw.items() if v})
    for code, pts in derived.items():
        raw[code] = pts
        print("[agri] %s(派生): %d 点 %s~%s" % (code, len(pts), pts[0][0], pts[-1][0]), flush=True)
    warnings += dnotes

    # 展示清单(顺序=页面顺序);C0_OI/JD0_OI 是中间量不进清单
    SPEC = {
        "LH0_CLOSE":  ("生猪主力收盘", "生猪(主力)", "价格", "养殖", "元/吨", "日",
                       S_SINA_FUT + "(大商所,2021-01 上市;折元/公斤≈÷1000)"),
        "HOG_FEED":   ("猪粮比(生猪/玉米)", "猪粮比", "价格", "比价", "倍", "日",
                       "自算(期货口径近似官方;6=盈亏平衡预警线,<6 触发收储预期,>9 利润丰厚)"),
        "M0_CLOSE":   ("豆粕主力收盘", "豆粕(主力)", "价格", "饲料", "元/吨", "日",
                       S_SINA_FUT + "(大商所,2000 上市;饲料蛋白核心,生猪主要成本)"),
        "C0_CLOSE":   ("玉米主力收盘", "玉米(主力)", "价格", "粮价", "元/吨", "日",
                       S_SINA_FUT + "(大商所,2004 上市;能量饲料锚,猪粮比分母)"),
        "JD0_CLOSE":  ("鸡蛋主力收盘", "鸡蛋(主力)", "价格", "蛋鸡链", "元/500千克", "日",
                       S_SINA_FUT + "(大商所,2013 上市;蛋鸡链观察口,同样吃玉米豆粕成本)"),
        "LH0_OI":     ("生猪主力持仓量", "生猪持仓量", "持仓库存", "期指持仓", "手", "日", S_SINA_FUT),
        "M0_OI":      ("豆粕主力持仓量", "豆粕持仓量", "持仓库存", "期指持仓", "手", "日", S_SINA_FUT),
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
        "commodity": "agri",
        "range": {"begin": min(all_ds), "end": max(all_ds)} if all_ds else None,
        "warnings": warnings,
        "series": series_out,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    total = sum(len(s["points"]) for s in series_out)
    print("[agri] 写盘 %s: %d 序列 %d 点,失败 %d(%s),耗时 %.1fs" % (
        OUT.name, len(series_out), total, len(warnings),
        ",".join(w.split(":")[0] for w in warnings) or "-", time.time() - t0), flush=True)
    failed = len(SPEC) - len(series_out)
    return 1 if failed > len(SPEC) / 2 else 0


if __name__ == "__main__":
    sys.exit(main())
