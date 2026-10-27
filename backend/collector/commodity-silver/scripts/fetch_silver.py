# -*- coding: utf-8 -*-
"""白银供需研究·第一层采集(P0:价格/微观/持仓/工业需求代理,2026-10-27 起)。

设计文档:docs/设计-大宗商品供需模块-白银.md(当日实测源)。全部免费 HTTP 源,
不碰 Wind,云端可跑。与 fetch_gold 同款骨架:组级 8s 退避重试,单序列失败降级
留旧数据(--no-clean upsert 语义),失败过半才 exit=1。

序列清单(19,展示顺序即写入顺序,code 稳定不宜再改——导入按 idx 还原展示序):
    价格      XAG_USD 伦敦银现货(新浪外盘,2006→) / SI_MAIN COMEX白银主力
              AG0_CLOSE+AG0_OI 沪银量仓(2012→) / AGT_CLOSE SGE Ag(T+D)(2016-12→,
              列名是英文 date/close,元/千克)
    自算派生  AG_GSR 金银比(XAU/XAG,XAU 为中间量不入清单)
              AG_PREMIUM 白银SGE溢价(Ag(T+D) vs XAG×中间价折算元/千克)
    持仓库存  SLV_TON+SLV_IMPLIED 白银ETF持仓/隐含银价(金十 macro_cons_silver,
              2014-12→,总库存吨×总价值美元) / COMEX_AG_INV 东财(2021→)
              CFTC_AG_COMM 东财商业净持仓(1986→) / CFTC_AG_NC 官网 zip 非商业
              LONDON_VAULT_AG LBMA 金库白银列(单文件含 2016-07 起全史,老行是
              datetime 形状——比黄金版只认 "2026-09" 字符串多收 2016-2023 段)
    工业需求  PS_MAIN 多晶硅主力(广期所,2024-12→,元/吨) / PV_ETF 光伏ETF 515790
              ZN0_CLOSE+PB0_CLOSE 沪锌沪铅(~70% 矿产银是铅锌铜金伴生,供给端代理)
    宏观因子  AG_DFII10/AG_DTWEXBGS FRED 直链(白银金融属性端同源因子)。
              FRED 共码序列必须加 AG_ 前缀:cmd_indicator 主键是 cmd_code,
              与黄金版同码互灌会把 commodity 字段翻掉(金银比同理用 AG_GSR)

用法(backend/ 目录): python -m collector.run silver  (调度:每天 06:15,scheduler.py)
产物: commodity-silver/data/silver.json → import_legacy --only-silver 回灌 cmd_*。
"""
import argparse
import io
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
import pandas as pd   # noqa: E402
import requests       # noqa: E402

BACKEND = Path(__file__).resolve().parents[3]
DATA = BACKEND / "collector" / "commodity-silver" / "data"
OUT = DATA / "silver.json"

TON_OZ = 32150.7466  # 1 公吨 = 32150.7466 金衡盎司
OZ_PER_GRAM_TROY = 31.1034768

FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s"
CFTC_URL = "https://www.cftc.gov/files/dea/history/deacot%d.zip"
LBMA_URL = "https://cdn.lbma.org.uk/downloads/LBMA-London-Vault-Holdings-Data-%s-%d.xlsx"
EM_KLINE = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.515790"
            "&fields1=f1&fields2=f51,f53&klt=101&fqt=1&beg=19900101&end=20991231")

S_SINA_FOREIGN = "新浪外盘期货"
S_SGE = "上海黄金交易所官网"
S_SINA_FUT = "新浪期货-主力连续"
S_JIN10 = "金十数据(白银ETF)"
S_EM = "东方财富"
S_FRED = "FRED(圣路易斯联储)"
S_CNFX = "中国外汇交易中心中间价(BOC镜像)"
S_EM_FUND = "东方财富(515790)"


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
    # XAU 是金银比中间量,不进清单
    return {
        "XAG_USD": _sina_foreign("XAG"),
        "SI_MAIN": _sina_foreign("SI"),
        "XAU_USD": _sina_foreign("XAU"),
    }


def fetch_agt():
    # SGE 白银延期:列名与黄金表不同(英文 date/close),单位元/千克
    df = ak.spot_hist_sge(symbol="Ag(T+D)")
    return _pts(df, "date", "close")


def fetch_fut(symbol):
    df = ak.futures_main_sina(symbol=symbol)
    close = _pts(df, "日期", "收盘价")
    oi = _pts(df, "日期", "持仓量")
    return close, oi


def fetch_usdcny():
    # 美元列口径是 100 美元兑人民币,统一折成 6.7162
    df = ak.currency_boc_safe()
    df = df.dropna(subset=["美元"]).copy()
    df["cny"] = pd.to_numeric(df["美元"], errors="coerce") / 100.0
    return _pts(df.dropna(subset=["cny"]), "日期", "cny")


def fetch_slv():
    df = ak.macro_cons_silver()
    df = df[df["商品"] == "白银"]
    ton = _pts(df, "日期", "总库存")
    df2 = df.copy()
    df2["impl"] = pd.to_numeric(df2["总价值"], errors="coerce") / (
        pd.to_numeric(df2["总库存"], errors="coerce") * TON_OZ)
    implied = _pts(df2, "日期", "impl")
    return ton, [(d, round(v, 3)) for d, v in implied]


def fetch_comex_inv():
    return _pts(ak.futures_comex_inventory(symbol="白银"), "日期", "COMEX白银库存量-吨")


def fetch_cftc():
    df = ak.macro_usa_cftc_merchant_goods_holding()
    return _pts(df.dropna(subset=["白银-净仓位"]), "日期", "白银-净仓位")


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


def fetch_pv_etf():
    r = requests.get(EM_KLINE, timeout=30)
    r.raise_for_status()
    kl = (r.json().get("data") or {}).get("klines") or []
    if not kl:
        raise ValueError("东财 515790 klines 空")
    return [(x.split(",")[0], float(x.split(",")[1])) for x in kl]


def _cftc_year(year):
    """CFTC legacy 年报 zip → 白银非商业净持仓。列位与黄金同(多 8/空 9),
    市名 SILVER;老年文件(≤2014)分隔符「", "」要 strip。"""
    import csv as _csv
    import zipfile as _zf
    r = requests.get(CFTC_URL % year, timeout=60)
    r.raise_for_status()
    zf = _zf.ZipFile(io.BytesIO(r.content))
    txt = zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")
    out = {}
    for row in _csv.reader(io.StringIO(txt)):
        if len(row) < 10 or row[0].strip() != "SILVER - COMMODITY EXCHANGE INC.":
            continue
        try:
            out[row[2].strip()] = float(row[8]) - float(row[9])
        except (ValueError, IndexError):
            continue
    return sorted(out.items())


def fetch_cftc_nc(prev_pts):
    """非商业净持仓增量合并:旧档 < 当年 的点沿用,只重抓当年 zip;旧档残缺
    (最早点晚于 1986 年末)转全量回填自愈。白银 legacy 首点实测在 1986 年内。"""
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


def fetch_lbma_vault_ag():
    """LBMA 伦敦金库白银(月度,吨)。单文件即全史(2016-07→);月份列两种形状:
    近期行是 "2026-09" 字符串,老行是 datetime——统一归一 YYYY-MM-01,
    因此白银版能收全 2016 段(黄金版只认字符串形状,只有 2023-02 后的点)。"""
    import datetime as _dt
    import calendar
    today = _dt.date.today()
    for back in range(1, 5):
        ref = today - _dt.timedelta(days=30 * back)
        url = LBMA_URL % (calendar.month_name[ref.month], ref.year)
        r = requests.get(url, timeout=40)
        if r.status_code != 200:
            continue
        df = pd.read_excel(io.BytesIO(r.content), header=None)
        out = {}
        for i in range(len(df)):
            m, koz = df.iat[i, 0], df.iat[i, 2]  # 列 0=月份, 列 1=Gold, 列 2=Silver
            s = None
            if isinstance(m, str) and len(m) == 7 and m[4] == "-" and m[:4].isdigit():
                s = m
            elif hasattr(m, "strftime"):
                s = m.strftime("%Y-%m")
            if not s:
                continue  # 表头/合计行
            try:
                tonnes = float(koz) * 1000.0 / TON_OZ
            except (TypeError, ValueError):
                continue
            out["%s-01" % s] = round(tonnes, 1)
        if out:
            return sorted(out.items())
    raise ValueError("近4个月 LBMA 金库文件均 404")


def derive(series_map):
    """自算派生:金银比、白银SGE溢价(元/千克口径直比)。原料缺失记 warning。"""
    out, notes = {}, []
    xau, xag = series_map.get("XAU_USD"), series_map.get("XAG_USD")
    if xau and xag:
        m = dict(xag)
        # code 用 AG_GSR 而非黄金版的 GOLD_SILVER:cmd_value 主键 (cmd_code, data_date),
        # 同 code 两商品互灌会互相覆盖/commodity 翻转(见 import_silver 头注)
        out["AG_GSR"] = sorted(
            (d, round(v / m[d], 2)) for d, v in xau if d in m and m[d])
    else:
        notes.append("金银比缺原料(XAU/XAG)")
    agt, cny = series_map.get("AGT_CLOSE"), series_map.get("USDCNY_FIX")
    if xag and agt and cny:
        mc, ma = dict(cny), dict(agt)
        prem = []
        for d, v in xag:
            if d in mc and d in ma and v > 0:
                # 伦敦银折元/千克 = XAG(美元/盎司)×中间价/31.1035×1000
                london_cny_kg = v * mc[d] / OZ_PER_GRAM_TROY * 1000.0
                prem.append((d, round((ma[d] / london_cny_kg - 1) * 100, 2)))
        out["AG_PREMIUM"] = sorted(prem)
    else:
        notes.append("白银SGE溢价缺原料(XAG/中间价/Ag(T+D))")
    return out, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印本轮抓取计划(序列/源/口径),不发请求")
    a = ap.parse_args()
    plan = [
        ("价格", "XAG_USD 伦敦银 / SI_MAIN COMEX主力 新浪外盘(XAU 伴抓算金银比)"),
        ("价格", "AG0_CLOSE/AG0_OI 沪银量仓(2012→) / AGT_CLOSE SGE Ag(T+D)(2016→)"),
        ("自算派生", "GOLD_SILVER 金银比 / AG_PREMIUM 白银SGE溢价率"),
        ("持仓库存", "SLV_TON+SLV_IMPLIED 金十(2014→) / COMEX_AG_INV 东财 / CFTC_AG_COMM 东财(1986→)"),
        ("持仓库存", "CFTC_AG_NC 官网zip增量合并(1986→) / LONDON_VAULT_AG LBMA白银列(2016→全史)"),
        ("工业需求", "PS_MAIN 多晶硅(2024→) / PV_ETF 515790 / ZN0+PB0 沪锌沪铅(伴生矿代理)"),
        ("宏观因子", "DFII10/DTWEXBGS FRED直链"),
    ]
    print("[silver] P0 序列计划: %d 组" % len(plan), flush=True)
    for cat, line in plan:
        print("  [%s] %s" % (cat, line), flush=True)
    if a.dry_run:
        return 0

    t0 = time.time()
    warnings = []

    # CFTC 非商业增量合并要读上一轮产物
    prev_cftc = None
    if OUT.exists():
        try:
            _prev = json.loads(OUT.read_text(encoding="utf-8"))
            prev_cftc = next((s["points"] for s in _prev.get("series", [])
                              if s["code"] == "CFTC_AG_NC"), None)
        except Exception:  # noqa: BLE001 旧档读不了就走全量回填路径
            prev_cftc = None

    def _sum(pts):
        if isinstance(pts, dict):
            return "; ".join("%s:%d点" % (k, len(v) or 0) for k, v in pts.items())
        if isinstance(pts, tuple):
            return "; ".join("%s点" % (len(x) or 0) for x in pts)
        return "%d 点 %s~%s" % (len(pts), pts[0][0] if pts else "-", pts[-1][0] if pts else "-")

    def grab(code, fn, tries=2):
        """组级 8s 退避重试,仍败记 warning 降级(库中旧点由 upsert 保留)。"""
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
                    print("[silver] %s 失败: %s %s" % (
                        code, type(e).__name__, str(e)[:120]), flush=True)
                    return None
                print("[silver] %s 第%d次失败(%s),8秒后重试" % (
                    code, i + 1, type(e).__name__), flush=True)
                time.sleep(8)
        print("[silver] %s: %s" % (code, _sum(pts)), flush=True)
        return pts

    raw = {}
    fx = grab("新浪外盘组(XAG/SI/XAU)", fetch_prices) or {}
    ag_pair = grab("沪银组(收盘/持仓)", lambda: tuple(fetch_fut("AG0"))) or (None, None)
    slv_pair = grab("SLV组(持仓/隐含价)", lambda: tuple(fetch_slv())) or (None, None)
    zn_close = grab("沪锌收盘", lambda: fetch_fut("ZN0")[0])
    pb_close = grab("沪铅收盘", lambda: fetch_fut("PB0")[0])
    ps_pair = grab("多晶硅组(收盘/持仓)", lambda: tuple(fetch_fut("PS0"))) or (None, None)
    singles = [
        ("XAG_USD", lambda: fx.get("XAG_USD")),
        ("SI_MAIN", lambda: fx.get("SI_MAIN")),
        ("XAU_USD", lambda: fx.get("XAU_USD")),
        ("AGT_CLOSE", fetch_agt),
        ("USDCNY_FIX", fetch_usdcny),
        ("AG0_CLOSE", lambda: ag_pair[0]),
        ("AG0_OI", lambda: ag_pair[1]),
        ("SLV_TON", lambda: slv_pair[0]),
        ("SLV_IMPLIED", lambda: slv_pair[1]),
        ("COMEX_AG_INV", fetch_comex_inv),
        ("CFTC_AG_COMM", fetch_cftc),
        ("CFTC_AG_NC", lambda: fetch_cftc_nc(prev_cftc)),
        ("LONDON_VAULT_AG", fetch_lbma_vault_ag),
        ("PS_MAIN", lambda: ps_pair[0]),
        ("PS_OI", lambda: ps_pair[1]),
        ("PV_ETF", fetch_pv_etf),
        ("ZN0_CLOSE", lambda: zn_close),
        ("PB0_CLOSE", lambda: pb_close),
        # FRED 共码序列用 AG_ 前缀独立 code(见头注防互灌)
        ("AG_DFII10", lambda: fetch_fred("DFII10")),
        ("AG_DTWEXBGS", lambda: fetch_fred("DTWEXBGS")),
    ]
    for code, fn in singles:
        raw[code] = grab(code, fn)
        time.sleep(0.4)
    derived, dnotes = derive({k: v for k, v in raw.items() if v})
    for code, pts in derived.items():
        raw[code] = pts
        print("[silver] %s(派生): %d 点 %s~%s" % (code, len(pts), pts[0][0], pts[-1][0]), flush=True)
    warnings += dnotes

    # 展示清单(顺序=页面顺序);XAU_USD/USDCNY_FIX/PS_OI 是中间量不进清单
    SPEC = {
        "XAG_USD":        ("伦敦银现货", "伦敦银(XAG)", "价格", "外盘", "美元/盎司", "日", S_SINA_FOREIGN),
        "SI_MAIN":        ("COMEX白银主力", "COMEX白银", "价格", "外盘", "美元/盎司", "日", S_SINA_FOREIGN),
        "AG0_CLOSE":      ("沪银主力收盘", "沪银(主力)", "价格", "内盘", "元/千克", "日", S_SINA_FUT),
        "AGT_CLOSE":      ("SGE白银Ag(T+D)", "SGE Ag(T+D)", "价格", "内盘", "元/千克", "日", S_SGE),
        "AG_GSR":         ("金银比(伦敦金/伦敦银)", "金银比", "价格", "比价", "倍", "日",
                           "自算(新浪外盘XAU/XAG;与黄金版同口径,code 独立防互灌)"),
        "AG_PREMIUM":     ("白银SGE溢价率", "SGE银溢价率", "价格", "比价", "%", "日",
                           "自算(Ag(T+D)/(XAG×中间价折算元/千克)-1,含交易费率)"),
        "AG0_OI":         ("沪银主力持仓量", "沪银持仓量", "价格", "期指持仓", "手", "日", S_SINA_FUT),
        "SLV_TON":        ("白银ETF(SLV)持仓", "SLV持仓", "持仓库存", "ETF持仓", "吨", "日", S_JIN10),
        "SLV_IMPLIED":    ("SLV隐含银价", "SLV隐含银价", "持仓库存", "ETF持仓", "美元/盎司", "日",
                           "自算(总价值/持仓,盎司换算)"),
        "COMEX_AG_INV":   ("COMEX白银库存", "COMEX银库存", "持仓库存", "交易所库存", "吨", "日",
                           S_EM + "(东财白银口径)"),
        "CFTC_AG_COMM":   ("CFTC白银商业净持仓", "CFTC商业净持仓", "持仓库存", "期指持仓", "手", "周",
                           S_EM + "(商品商业持仓口径)"),
        "CFTC_AG_NC":     ("CFTC白银非商业净持仓", "CFTC非商业净持仓", "持仓库存", "期指持仓", "手", "周",
                           "CFTC官网(legacy futures-only,1986→)"),
        "LONDON_VAULT_AG": ("伦敦金库白银库存", "伦敦金库银库存", "持仓库存", "伦敦金库", "吨", "月",
                            "LBMA London Vault Data(白银列;2016-07 起,滞后1月)"),
        "PS_MAIN":        ("多晶硅主力收盘", "多晶硅(主力)", "工业需求", "光伏排产", "元/吨", "日",
                           S_SINA_FUT + "(广期所PS,2024-12 上市)"),
        "PV_ETF":         ("光伏ETF(515790)净值", "光伏ETF", "工业需求", "光伏景气", "元", "日", S_EM_FUND),
        "ZN0_CLOSE":      ("沪锌主力收盘", "沪锌(主力)", "工业需求", "伴生矿", "元/吨", "日", S_SINA_FUT),
        "PB0_CLOSE":      ("沪铅主力收盘", "沪铅(主力)", "工业需求", "伴生矿", "元/吨", "日", S_SINA_FUT),
        "AG_DFII10":      ("美国10年期TIPS实际利率", "10Y实际利率", "宏观因子", "利率", "%", "日", S_FRED),
        "AG_DTWEXBGS":    ("美元指数(贸易加权宽口径)", "美元指数(宽)", "宏观因子", "汇率", "点", "日", S_FRED),
    }
    series_out = []
    for idx, (code, (name, label, cat, group, unit, freq, source)) in enumerate(SPEC.items()):
        pts = raw.get(code)
        if not pts:
            continue  # 失败序列本轮缺席:库中旧点由 upsert 语义保留,不删
        series_out.append({
            "code": code, "idx": idx, "name": name, "label": label, "cat": cat,
            "group": group, "unit": unit, "freq": freq, "source": source,
            "points": [[d, round(v, 4)] for d, v in pts],
        })
    all_ds = [p[0] for s in series_out for p in s["points"]]
    payload = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "commodity": "silver",
        "range": {"begin": min(all_ds), "end": max(all_ds)} if all_ds else None,
        "warnings": warnings,
        "series": series_out,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    total = sum(len(s["points"]) for s in series_out)
    print("[silver] 写盘 %s: %d 序列 %d 点,失败 %d(%s),耗时 %.1fs" % (
        OUT.name, len(series_out), total, len(warnings),
        ",".join(w.split(":")[0] for w in warnings) or "-", time.time() - t0), flush=True)
    failed = len(SPEC) - len(series_out)
    return 1 if failed > len(SPEC) / 2 else 0


if __name__ == "__main__":
    sys.exit(main())
