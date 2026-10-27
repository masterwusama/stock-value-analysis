# -*- coding: utf-8 -*-
"""铜供需研究·第一层采集(P0:价格/比价/需求/持仓,2026-10-27 起)。

设计思路同 gold/silver(设计文档 docs/设计-大宗商品供需模块-白银.md §1 的铜注:
铜是流量主导工业金属,定价核心=全球制造业景气与中国需求,占全球消费约 55%)。
全部免费源;与 fetch_silver 同款骨架(组级 8s 退避/单序列降级留旧/失败过半才红)。

序列清单(10,展示顺序即写入顺序,code 稳定不宜再改——导入按 idx 还原展示序):
    价格      CAD_MAIN LME铜三月(新浪外盘 CAD,2016→,美元/吨) / HG_MAIN COMEX铜主力
              (美元/磅,新浪 HG) / CU0_CLOSE+CU0_OI 沪铜量仓(2005→,最长国内序列)
              CU_GOLD 铜金比(自算 CAD/XAU;XAU 为中间量不入清单)
    需求      SHFE_LME 沪伦比(自算 沪铜元/吨 ÷ (LME美元/吨×中间价);>1=内盘溢价=
              中国需求强/进口亏损) / CN_PMI 制造业PMI(月度,2008→,倒序表需归一)
    持仓库存  CFTC_CU_NC 官网 zip 非商业净持仓(市名 COPPER- #1,1986→)
              SHFE_CU_INV 沪铜库存(东财,仅滚动近 3 月——LME/COMEX 铜库存无免费
              结构化源,页面 footer 诚实注明,这是铜模块的已知弱项)
    宏观因子  CU_DTWEXBGS FRED 美元宽口径(CU_ 前缀独立 code 防与金银互灌)

用法(backend/ 目录): python -m collector.run copper  (调度:每天 06:25,scheduler.py)
产物: commodity-copper/data/copper.json → import_legacy --only-copper 回灌 cmd_*。
"""
import argparse
import io
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
import pandas as pd   # noqa: E402
import requests       # noqa: E402

BACKEND = Path(__file__).resolve().parents[3]
DATA = BACKEND / "collector" / "commodity-copper" / "data"
OUT = DATA / "copper.json"

FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s"
CFTC_URL = "https://www.cftc.gov/files/dea/history/deacot%d.zip"

S_SINA_FOREIGN = "新浪外盘期货"
S_SINA_FUT = "新浪期货-主力连续"
S_EM = "东方财富"
S_FRED = "FRED(圣路易斯联储)"
S_CNFX = "中国外汇交易中心中间价(BOC镜像)"


def _pts(df, dcol, vcol):
    """DataFrame → [(YYYY-MM-DD, float)],丢 NaN 按日升序。"""
    s = df[[dcol, vcol]].dropna()
    out = {}
    for d, v in zip(s[dcol], s[vcol]):
        out[str(d)[:10]] = float(v)
    return sorted(out.items())


def _sina_foreign(symbol):
    return _pts(ak.futures_foreign_hist(symbol=symbol), "date", "close")


def fetch_prices():
    # XAU 是铜金比中间量,不进清单
    return {
        "CAD_MAIN": _sina_foreign("CAD"),
        "HG_MAIN": _sina_foreign("HG"),
        "XAU_USD": _sina_foreign("XAU"),
    }


def fetch_cu0():
    df = ak.futures_main_sina(symbol="CU0")
    close = _pts(df, "日期", "收盘价")
    oi = _pts(df, "日期", "持仓量")
    return close, oi


def fetch_usdcny():
    df = ak.currency_boc_safe()
    df = df.dropna(subset=["美元"]).copy()
    df["cny"] = pd.to_numeric(df["美元"], errors="coerce") / 100.0
    return _pts(df.dropna(subset=["cny"]), "日期", "cny")


def fetch_pmi():
    # 倒序表(最新在头),月份是"2026年09月份"文本 → 当月首日
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


def fetch_shfe_inv():
    return _pts(ak.futures_inventory_em(symbol="沪铜"), "日期", "库存")


def fetch_fred(sid):
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


def _cftc_year(year):
    """CFTC legacy 年报 zip → 铜非商业净持仓。铜市名历史三种格式(1986 无后缀
    COPPER / 2000-2021 COPPER-GRADE #1 / 现 COPPER- #1),统一用 startswith 前缀
    匹配——该文件里铜只有一个市场,无歧义;老年文件分隔符「", "」要 strip。
    列位与金银同(非商业多 8/空 9)。"""
    import csv as _csv
    import zipfile as _zf
    r = requests.get(CFTC_URL % year, timeout=60)
    r.raise_for_status()
    zf = _zf.ZipFile(io.BytesIO(r.content))
    txt = zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")
    out = {}
    for row in _csv.reader(io.StringIO(txt)):
        if len(row) < 10 or not row[0].strip().upper().startswith("COPPER"):
            continue
        try:
            out[row[2].strip()] = float(row[8]) - float(row[9])
        except (ValueError, IndexError):
            continue
    return sorted(out.items())


def fetch_cftc_nc(prev_pts):
    """非商业净持仓增量合并:旧档 < 当年 的点沿用,只重抓当年 zip;旧档残缺
    (最早点晚于 1986 年末)转全量回填自愈。"""
    import datetime as _dt
    year = _dt.date.today().year
    cut = "%04d-01-01" % year
    head = [(d, v) for d, v in (prev_pts or []) if d < cut]
    if head and head[0][0] <= "1986-12-31":
        return sorted({**dict(head), **dict(_cftc_year(year))}.items())
    merged = {}
    for y in range(1986, year + 1):
        merged.update(dict(_cftc_year(y)))
        time.sleep(0.4)
    return sorted(merged.items())


def derive(series_map):
    """自算派生:铜金比(CAD/XAU 美元价直比)、沪伦比(沪铜÷(LME×中间价))。"""
    out, notes = {}, []
    cad, xau = series_map.get("CAD_MAIN"), series_map.get("XAU_USD")
    if cad and xau:
        m = dict(xau)
        out["CU_GOLD"] = sorted(
            (d, round(v / m[d], 3)) for d, v in cad if d in m and m[d])
    else:
        notes.append("铜金比缺原料(CAD/XAU)")
    cu, cny = series_map.get("CU0_CLOSE"), series_map.get("USDCNY_FIX")
    if cad and cu and cny:
        mc, mu = dict(cny), dict(cu)
        ratio = []
        for d, v in cad:
            if d in mc and d in mu and v > 0:
                lme_cny_t = v * mc[d]  # LME 折元/吨
                ratio.append((d, round(mu[d] / lme_cny_t, 4)))
        out["SHFE_LME"] = sorted(ratio)
    else:
        notes.append("沪伦比缺原料(CAD/中间价/沪铜)")
    return out, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印本轮抓取计划,不发请求")
    a = ap.parse_args()
    plan = [
        ("价格", "CAD_MAIN LME铜 / HG_MAIN COMEX铜(美分/磅) 新浪外盘(XAU 伴抓算铜金比)"),
        ("价格", "CU0_CLOSE/CU0_OI 沪铜量仓(2005→) / CU_GOLD 铜金比(自算)"),
        ("需求", "SHFE_LME 沪伦比(自算) / CN_PMI 制造业PMI(2008→月度)"),
        ("持仓库存", "CFTC_CU_NC 官网zip增量合并(1986→) / SHFE_CU_INV 沪铜库存(滚动3月)"),
        ("宏观因子", "CU_DTWEXBGS FRED直链"),
    ]
    print("[copper] P0 序列计划: %d 组" % len(plan), flush=True)
    for cat, line in plan:
        print("  [%s] %s" % (cat, line), flush=True)
    if a.dry_run:
        return 0

    t0 = time.time()
    warnings = []

    prev_cftc = None
    if OUT.exists():
        try:
            _prev = json.loads(OUT.read_text(encoding="utf-8"))
            prev_cftc = next((s["points"] for s in _prev.get("series", [])
                              if s["code"] == "CFTC_CU_NC"), None)
        except Exception:  # noqa: BLE001 旧档读不了就走全量回填路径
            prev_cftc = None

    def _sum(pts):
        if isinstance(pts, dict):
            return "; ".join("%s:%d点" % (k, len(v) or 0) for k, v in pts.items())
        if isinstance(pts, tuple):
            return "; ".join("%s点" % (len(x) or 0) for x in pts)
        return "%d 点 %s~%s" % (len(pts), pts[0][0] if pts else "-", pts[-1][0] if pts else "-")

    def grab(code, fn, tries=2):
        pts = None
        for i in range(tries):
            try:
                pts = fn()
                if pts is None or (isinstance(pts, dict) and not pts):
                    raise ValueError("上游组失败,本序列随组缺席")
                break
            except Exception as e:  # noqa: BLE001
                if i == tries - 1:
                    warnings.append("%s: %s(%s)" % (code, type(e).__name__, str(e)[:80]))
                    print("[copper] %s 失败: %s %s" % (
                        code, type(e).__name__, str(e)[:120]), flush=True)
                    return None
                print("[copper] %s 第%d次失败(%s),8秒后重试" % (
                    code, i + 1, type(e).__name__), flush=True)
                time.sleep(8)
        print("[copper] %s: %s" % (code, _sum(pts)), flush=True)
        return pts

    raw = {}
    fx = grab("新浪外盘组(CAD/HG/XAU)", fetch_prices) or {}
    cu_pair = grab("沪铜组(收盘/持仓)", lambda: tuple(fetch_cu0())) or (None, None)
    singles = [
        ("CAD_MAIN", lambda: fx.get("CAD_MAIN")),
        ("HG_MAIN", lambda: fx.get("HG_MAIN")),
        ("XAU_USD", lambda: fx.get("XAU_USD")),
        ("CU0_CLOSE", lambda: cu_pair[0]),
        ("CU0_OI", lambda: cu_pair[1]),
        ("USDCNY_FIX", fetch_usdcny),
        ("CN_PMI", fetch_pmi),
        ("CFTC_CU_NC", lambda: fetch_cftc_nc(prev_cftc)),
        ("SHFE_CU_INV", fetch_shfe_inv),
        ("CU_DTWEXBGS", lambda: fetch_fred("DTWEXBGS")),
    ]
    for code, fn in singles:
        raw[code] = grab(code, fn)
        time.sleep(0.4)
    derived, dnotes = derive({k: v for k, v in raw.items() if v})
    for code, pts in derived.items():
        raw[code] = pts
        print("[copper] %s(派生): %d 点 %s~%s" % (code, len(pts), pts[0][0], pts[-1][0]), flush=True)
    warnings += dnotes

    # 展示清单(顺序=页面顺序);XAU_USD/USDCNY_FIX 是中间量不进清单
    SPEC = {
        "CAD_MAIN":    ("LME铜(三月)", "LME铜", "价格", "外盘", "美元/吨", "日", S_SINA_FOREIGN),
        "HG_MAIN":     ("COMEX铜主力", "COMEX铜", "价格", "外盘", "美分/磅", "日", S_SINA_FOREIGN),
        "CU0_CLOSE":   ("沪铜主力收盘", "沪铜(主力)", "价格", "内盘", "元/吨", "日", S_SINA_FUT),
        "CU_GOLD":     ("铜金比(LME铜/伦敦金)", "铜金比", "价格", "比价", "倍", "日",
                        "自算(美元价直比;越高=市场越押周期/增长)"),
        "CU0_OI":      ("沪铜主力持仓量", "沪铜持仓量", "价格", "期指持仓", "手", "日", S_SINA_FUT),
        "SHFE_LME":    ("沪伦比(沪铜/LME×中间价)", "沪伦比", "需求", "比价", "倍", "日",
                        "自算(>1=内盘溢价=中国需求强/进口亏损)"),
        "CN_PMI":      ("中国制造业PMI", "制造业PMI", "需求", "景气", "%", "月", S_EM + "(官方口径,当月首日)"),
        "CFTC_CU_NC":  ("CFTC铜非商业净持仓", "CFTC非商业净持仓", "持仓库存", "期指持仓", "手", "周",
                        "CFTC官网(legacy futures-only,COPPER- #1,1986→)"),
        "SHFE_CU_INV": ("沪铜库存", "沪铜库存", "持仓库存", "交易所库存", "吨", "日",
                        S_EM + "(仅滚动近3月;LME/COMEX铜库存无免费结构化源)"),
        "CU_DTWEXBGS": ("美元指数(贸易加权宽口径)", "美元指数(宽)", "宏观因子", "汇率", "点", "日", S_FRED),
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
        "commodity": "copper",
        "range": {"begin": min(all_ds), "end": max(all_ds)} if all_ds else None,
        "warnings": warnings,
        "series": series_out,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    total = sum(len(s["points"]) for s in series_out)
    print("[copper] 写盘 %s: %d 序列 %d 点,失败 %d(%s),耗时 %.1fs" % (
        OUT.name, len(series_out), total, len(warnings),
        ",".join(w.split(":")[0] for w in warnings) or "-", time.time() - t0), flush=True)
    failed = len(SPEC) - len(series_out)
    return 1 if failed > len(SPEC) / 2 else 0


if __name__ == "__main__":
    sys.exit(main())
