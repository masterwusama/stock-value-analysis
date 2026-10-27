# -*- coding: utf-8 -*-
"""黄金供需研究·第一层采集(P0:价格/微观/持仓/储备/宏观因子,2026-10-27 起)。

设计文档:docs/设计-大宗商品供需模块-黄金.md。全部免费 HTTP 源,不碰 Wind,
云端可跑。2026-10-27 当日逐接口实测(akshare 1.19.1 / FRED / GPR 裸 GET 均 200)。

序列清单(20,展示顺序即写入顺序,code 稳定不宜再改——导入按 idx 还原展示序):
    价格      XAU_USD 伦敦金现货(新浪外盘,2006→,替代已收费的 LBMA 定盘价)
              GC_MAIN COMEX主力 / SGE_AU9999 上海金(2016-12→) / SHGOLD_PM 上海金基准价(晚盘)
              AU0_CLOSE+AU0_OI 沪金主力量仓(2008→) / USDCNY_FIX 中间价 / GOLD_SILVER 金银比(自算)
    持仓库存  SPDR_TON SPDR黄金ETF持仓(2004-11→) / SPDR_IMPLIED 隐含金价=总价值/持仓(自算,
              实测与COMEX价差<0.1%,当源质量交叉校验) / COMEX_INV COMEX库存(东财EMI00069026)
              CFTC_COMM_NET 商业净持仓周度(1986→;akshare 无黄金非商业口径,细项走 CFTC 官网 zip,P1)
    官方储备  CN_GOLD_WAN 中国官方黄金储备(万盎司,1978→月度,央行口径)
    宏观因子  DFII10 10Y TIPS实际利率 / T10YIE 盈亏平衡通胀 / DTWEXBGS 美元宽口径
              (FRED fredgraph.csv 免 key 直链) / VIXCLS / US10Y 名义(东财) / GPR 地缘风险(月度)
    自算派生  SGE_PREMIUM SGE对伦敦金溢价率(中国实物需求温度计,三序列按日对齐内连接)

降级策略:单序列抓取失败只记进 json 的 warnings 并跳过该序列(库中旧点由
--no-clean upsert 语义保留),失败面超过一半才让整轮 job 变红(exit=1)——
金十/新浪偶发 5xx 不该带崩全模块。日期一律 YYYY-MM-DD 文本。

用法(backend/ 目录): python -m collector.run gold   (调度:每天 06:05,scheduler.py)
产物: commodity-gold/data/gold.json → import_legacy --only-gold 回灌 cmd_indicator/cmd_value。
"""
import argparse
import io
import json
import sys
import time
from datetime import datetime
from pathlib import Path

# 本机系统代理会拦断行情/东财域名,与 fetch_edb/fetch_sentiment 同因,单独直连
import os
os.environ["NO_PROXY"] = "*"
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import akshare as ak  # noqa: E402
import pandas as pd   # noqa: E402
import requests       # noqa: E402

BACKEND = Path(__file__).resolve().parents[3]
DATA = BACKEND / "collector" / "commodity-gold" / "data"
OUT = DATA / "gold.json"

TON_OZ = 32150.7466  # 1 公吨 = 32150.7466 金衡盎司
OZ_PER_GRAM_TROY = 31.1034768  # 1 金衡盎司 = 31.1034768 克

FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s"
GPR_URL = "https://www.matteoiacoviello.com/gpr_files/data_gpr_export.xls"
CFTC_URL = "https://www.cftc.gov/files/dea/history/deacot%d.zip"
LBMA_URL = "https://cdn.lbma.org.uk/downloads/LBMA-London-Vault-Holdings-Data-%s-%d.xlsx"

S_SINA_FOREIGN = "新浪外盘期货"
S_SGE = "上海黄金交易所官网"
S_SINA_FUT = "新浪期货-主力连续"
S_JIN10 = "金十数据(SPDR)"
S_EM = "东方财富"
S_FRED = "FRED(圣路易斯联储)"
S_GPR = "Caldara-Iacoviello GPR"
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
    return {
        "XAU_USD": _sina_foreign("XAU"),
        "GC_MAIN": _sina_foreign("GC"),
        "XAG_USD": _sina_foreign("XAG"),  # 中间量,金银比的原料,不直接入清单
    }


def fetch_sge():
    au = _pts(ak.spot_hist_sge(symbol="Au99.99"), "date", "close")
    bm = ak.spot_golden_benchmark_sge()
    bmpts = _pts(bm, "交易时间", "晚盘价")  # 下午盘基准价与国际定盘同窗,口径注记在 source
    return au, bmpts


def fetch_au0():
    df = ak.futures_main_sina(symbol="AU0")
    close = _pts(df, "日期", "收盘价")
    oi = _pts(df, "日期", "持仓量")
    return close, oi


def fetch_usdcny():
    # 美元列口径是 100 美元兑人民币(671.62),统一折成 6.7162
    df = ak.currency_boc_safe()
    df = df.dropna(subset=["美元"]).copy()
    df["cny"] = pd.to_numeric(df["美元"], errors="coerce") / 100.0
    return _pts(df.dropna(subset=["cny"]), "日期", "cny")


def fetch_spdr():
    df = ak.macro_cons_gold()
    df = df[df["商品"] == "黄金"]
    ton = _pts(df, "日期", "总库存")
    df2 = df.copy()
    df2["impl"] = pd.to_numeric(df2["总价值"], errors="coerce") / (
        pd.to_numeric(df2["总库存"], errors="coerce") * TON_OZ)
    implied = _pts(df2, "日期", "impl")
    return ton, [(d, round(v, 2)) for d, v in implied]


def fetch_comex_inv():
    return _pts(ak.futures_comex_inventory(symbol="黄金"), "日期", "COMEX黄金库存量-吨")


def fetch_cftc():
    df = ak.macro_usa_cftc_merchant_goods_holding()
    return _pts(df.dropna(subset=["黄金-净仓位"]), "日期", "黄金-净仓位")


def fetch_cn_reserve():
    # 新浪该表的"统计时间"是 YYYY.M 文本(如 1978.12 / 1993.1),归一成当月首日
    # 的零填充 ISO 日期——py3.10 的 fromisoformat 不认单 digit 月
    df = ak.macro_china_foreign_exchange_gold()
    df = df.dropna(subset=["黄金储备"]).copy()

    def _norm(t):
        y, _, m = str(t).strip().partition(".")
        return "%04d-%02d-01" % (int(y), int(m or 1))

    df["月日"] = df["统计时间"].map(_norm)
    return _pts(df, "月日", "黄金储备")


def fetch_fred(sid):
    r = requests.get(FRED % sid, timeout=30)
    r.raise_for_status()
    rows = []
    for line in r.text.splitlines()[1:]:
        cells = line.split(",")
        if len(cells) != 2 or cells[1] in (".", ""):
            continue  # FRED 缺测值写 '.'
        try:
            rows.append((cells[0], float(cells[1])))
        except ValueError:
            continue
    if not rows:
        raise ValueError(f"FRED {sid} 空返回")
    return rows


def fetch_us10y():
    df = ak.bond_zh_us_rate()
    return _pts(df, "日期", "美国国债收益率10年")


def fetch_gpr():
    r = requests.get(GPR_URL, timeout=60)
    r.raise_for_status()
    df = pd.read_excel(io.BytesIO(r.content), engine="xlrd")
    col_date = next(c for c in df.columns if str(c).strip().lower() in ("month", "date", "月份"))
    col_gpr = next(c for c in df.columns if str(c).strip() == "GPR")
    s = df[[col_date, col_gpr]].dropna()
    return [(str(pd.Timestamp(d).date()), float(v)) for d, v in zip(s[col_date], s[col_gpr])]


def _cftc_year(year):
    """CFTC legacy 年报 zip → 黄金非商业净持仓 [(YYYY-MM-DD, 净多-净空)]。

    annual.txt 是带引号头的 CSV;GOLD 行市名固定,日期在列 2,非商业多/空在列 8/9
    (futures-only 口径,与东财商业持仓互为镜像)。实测 2026-10-27 裸 GET 200。
    """
    import csv as _csv
    import zipfile as _zf
    r = requests.get(CFTC_URL % year, timeout=60)
    r.raise_for_status()
    zf = _zf.ZipFile(io.BytesIO(r.content))
    txt = zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")
    out = {}
    for row in _csv.reader(io.StringIO(txt)):
        # 老年文件(≤2014)分隔符是「", "」逗号带空格,市名列拖着尾随空格——strip 后再认
        if len(row) < 10 or row[0].strip() != "GOLD - COMMODITY EXCHANGE INC.":
            continue
        try:
            out[row[2].strip()] = float(row[8]) - float(row[9])
        except (ValueError, IndexError):
            continue
    return sorted(out.items())


def fetch_cftc_nc(prev_pts):
    """非商业净持仓全序列。增量路径:旧 gold.json 里已有 < 当年 的点直接沿用,
    只重抓当年 zip(每周五发布);首跑/旧档缺失才回填 1986→ 全部年 zip(~40 个)。
    自我合并上一轮产物,不引入第二个状态文件。
    """
    import datetime as _dt
    year = _dt.date.today().year
    cut = "%04d-01-01" % year
    head = [(d, v) for d, v in (prev_pts or []) if d < cut]
    # 旧档完整(最早点回到 1986 上半年)才走增量;残缺/缺失一律全量回填自愈
    if head and head[0][0] <= "1986-06-30":
        return sorted({**dict(head), **dict(_cftc_year(year))}.items())
    merged = {}
    for y in range(1986, year + 1):
        merged.update(dict(_cftc_year(y)))
        time.sleep(0.4)
    return sorted(merged.items())


def fetch_lbma_vault():
    """LBMA 伦敦金库月度黄金库存(吨)。单文件即全史(2016-07→);文件名带数据月
    英文名,每月第 5 个营业日发布——倒着试近 4 个月,首个 200 即用。"""
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
        out = []
        for i in range(len(df)):
            m, koz = df.iat[i, 0], df.iat[i, 1]
            s = str(m).strip()
            if len(s) != 7 or s[4] != "-" or not s[:4].isdigit():
                continue  # 跳过表头/合计行,只认 2026-09 形状
            try:
                tonnes = float(koz) * 1000.0 / TON_OZ
            except (TypeError, ValueError):
                continue
            out.append(("%s-01" % s, round(tonnes, 1)))
        if out:
            return sorted(out)
    raise ValueError("近4个月 LBMA 金库文件均 404")


def derive(series_map):
    """自算派生序列:金银比、SGE 溢价。原料缺失时返回 None(记 warning)。"""
    out, notes = {}, []
    xau, xag = series_map.get("XAU_USD"), series_map.get("XAG_USD")
    if xau and xag:
        m = dict(xag)
        out["GOLD_SILVER"] = sorted(
            (d, round(v / m[d], 2)) for d, v in xau if d in m and m[d])
    else:
        notes.append("金银比缺原料(XAU/XAG)")
    sge, cny = series_map.get("SGE_AU9999"), series_map.get("USDCNY_FIX")
    if xau and sge and cny:
        mc, ms = dict(cny), dict(sge)
        prem = []
        for d, v in xau:
            if d in mc and d in ms and v > 0:
                london_cny_g = v * mc[d] / OZ_PER_GRAM_TROY
                prem.append((d, round((ms[d] / london_cny_g - 1) * 100, 2)))
        out["SGE_PREMIUM"] = sorted(prem)
    else:
        notes.append("SGE溢价缺原料(XAU/中间价/SGE)")
    return out, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印本轮抓取计划(序列/源/口径),不发请求")
    a = ap.parse_args()
    plan = [
        ("价格", "XAU_USD 伦敦金现货 新浪外盘(2006→)"),
        ("价格", "GC_MAIN COMEX主力 新浪外盘 / SGE_AU9999+SHGOLD_PM SGE官网(2016→)"),
        ("价格", "AU0_CLOSE/AU0_OI 沪金量仓 新浪(2008→) / USDCNY_FIX 中间价 BOC"),
        ("持仓库存", "SPDR_TON+SPDR_IMPLIED 金十(2004→) / COMEX_INV 东财 / CFTC_COMM_NET 东财周度(1986→)"),
        ("持仓库存P1", "CFTC_NC_NET CFTC官网年报zip增量合并(1986→) / LONDON_VAULT LBMA金库月度"),
        ("官方储备", "CN_GOLD_WAN 中国官方黄金储备 新浪月度(1978→)"),
        ("宏观因子", "DFII10/T10YIE/DTWEXBGS/VIXCLS FRED直链 / US10Y 东财 / GPR 月度xls"),
        ("自算派生", "GOLD_SILVER 金银比 / SGE_PREMIUM SGE溢价率"),
    ]
    print("[gold] P0 序列计划: %d 组" % len(plan), flush=True)
    for cat, line in plan:
        print("  [%s] %s" % (cat, line), flush=True)
    if a.dry_run:
        return 0

    t0 = time.time()
    warnings = []

    # CFTC 非商业净持仓的增量合并要读上一轮产物(当年 zip 只含当年,往年点沿用旧档)
    prev_cftc = None
    if OUT.exists():
        try:
            _prev = json.loads(OUT.read_text(encoding="utf-8"))
            prev_cftc = next((s["points"] for s in _prev.get("series", [])
                              if s["code"] == "CFTC_NC_NET"), None)
        except Exception:  # noqa: BLE001 旧档读不了就走全量回填路径
            prev_cftc = None


    def _sum(pts):
        """进度摘要。组返回值是 dict/tuple(多序列),点级明细只对 list 打。"""
        if isinstance(pts, dict):
            return "; ".join("%s:%d点" % (k, len(v) or 0) for k, v in pts.items())
        if isinstance(pts, tuple):
            return "; ".join("%s点" % (len(x) or 0) for x in pts)
        return "%d 点 %s~%s" % (len(pts), pts[0][0] if pts else "-", pts[-1][0] if pts else "-")

    def grab(code, fn, tries=2):
        """抓一层序列/组。瞬时网络错(ChunkedEncodingError/超时/空返回)隔 8 秒
        整层重试一次(2026-10-27 实测 SGE 组与 FRED 均出现过一发即愈的瞬断),
        仍失败才记 warning 降级——库中旧点由 --no-clean upsert 语义保留。"""
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
                    print("[gold] %s 失败: %s %s" % (
                        code, type(e).__name__, str(e)[:120]), flush=True)
                    return None
                print("[gold] %s 第%d次失败(%s),8秒后重试" % (
                    code, i + 1, type(e).__name__), flush=True)
                time.sleep(8)
        print("[gold] %s: %s" % (code, _sum(pts)), flush=True)
        return pts

    raw = {}
    # 一调多序列的源(新浪外盘三连/SGE两表/沪金两列/SPDR两列)先整组取一次再分发,
    # 避免每个 lambda 各拉一遍整组
    fx = grab("新浪外盘组(XAU/GC/XAG)", fetch_prices) or {}
    sge_pair = grab("SGE组(Au99.99/基准价)", lambda: tuple(fetch_sge())) or (None, None)
    au0_pair = grab("沪金组(收盘/持仓)", lambda: tuple(fetch_au0())) or (None, None)
    spdr_pair = grab("SPDR组(持仓/隐含价)", lambda: tuple(fetch_spdr())) or (None, None)
    singles = [
        ("XAU_USD", lambda: fx.get("XAU_USD")),
        ("GC_MAIN", lambda: fx.get("GC_MAIN")),
        ("XAG_USD", lambda: fx.get("XAG_USD")),
        ("SGE_AU9999", lambda: sge_pair[0]),
        ("SHGOLD_PM", lambda: sge_pair[1]),
        ("AU0_CLOSE", lambda: au0_pair[0]),
        ("AU0_OI", lambda: au0_pair[1]),
        ("USDCNY_FIX", fetch_usdcny),
        ("SPDR_TON", lambda: spdr_pair[0]),
        ("SPDR_IMPLIED", lambda: spdr_pair[1]),
        ("COMEX_INV", fetch_comex_inv),
        ("CFTC_COMM_NET", fetch_cftc),
        ("CFTC_NC_NET", lambda: fetch_cftc_nc(prev_cftc)),
        ("LONDON_VAULT", fetch_lbma_vault),
        ("CN_GOLD_WAN", fetch_cn_reserve),
        ("DFII10", lambda: fetch_fred("DFII10")),
        ("T10YIE", lambda: fetch_fred("T10YIE")),
        ("DTWEXBGS", lambda: fetch_fred("DTWEXBGS")),
        ("VIXCLS", lambda: fetch_fred("VIXCLS")),
        ("US10Y", fetch_us10y),
        ("GPR", fetch_gpr),
    ]
    for code, fn in singles:
        raw[code] = grab(code, fn)
        time.sleep(0.4)
    derived, dnotes = derive({k: v for k, v in raw.items() if v})
    for code, pts in derived.items():
        raw[code] = pts
        print("[gold] %s(派生): %d 点 %s~%s" % (code, len(pts), pts[0][0], pts[-1][0]), flush=True)
    warnings += dnotes

    # 展示清单(顺序=页面顺序);XAG_USD 是中间量不进清单
    SPEC = {
        "XAU_USD":       ("伦敦金现货", "伦敦金(XAU)", "价格", "外盘", "美元/盎司", "日", S_SINA_FOREIGN),
        "GC_MAIN":       ("COMEX黄金主力", "COMEX主力", "价格", "外盘", "美元/盎司", "日", S_SINA_FOREIGN),
        "SGE_AU9999":    ("上海金Au99.99", "SGE Au99.99", "价格", "内盘", "元/克", "日", S_SGE),
        "SHGOLD_PM":     ("上海金基准价(晚盘)", "上海金基准价", "价格", "内盘", "元/克", "日", S_SGE),
        "AU0_CLOSE":     ("沪金主力收盘", "沪金(主力)", "价格", "内盘", "元/克", "日", S_SINA_FUT),
        "GOLD_SILVER":   ("金银比(伦敦金/伦敦银)", "金银比", "价格", "比价", "倍", "日", "自算(新浪外盘XAU/XAG)"),
        "SGE_PREMIUM":   ("SGE对伦敦金溢价率", "SGE溢价率", "价格", "比价", "%", "日",
                          "自算(SGE/(XAU×中间价折算)-1)"),
        "USDCNY_FIX":    ("人民币兑美元中间价", "USDCNY中间价", "价格", "汇率", "元", "日", S_CNFX),
        "SPDR_TON":      ("SPDR黄金ETF持仓", "SPDR持仓", "持仓库存", "ETF持仓", "吨", "日", S_JIN10),
        "SPDR_IMPLIED":  ("SPDR隐含金价", "SPDR隐含金价", "持仓库存", "ETF持仓", "美元/盎司", "日",
                          "自算(总价值/持仓,盎司换算)"),
        "COMEX_INV":     ("COMEX黄金库存", "COMEX库存", "持仓库存", "交易所库存", "吨", "日",
                          S_EM + "(EMI00069026)"),
        "CFTC_COMM_NET": ("CFTC黄金商业净持仓", "CFTC商业净持仓", "持仓库存", "期指持仓", "手", "周",
                          S_EM + "(商品商业持仓口径)"),
        "CFTC_NC_NET":   ("CFTC黄金非商业净持仓", "CFTC非商业净持仓", "持仓库存", "期指持仓", "手", "周",
                          "CFTC官网(legacy futures-only,1986→,镜像于商业持仓)"),
        "LONDON_VAULT":  ("伦敦金库黄金库存", "伦敦金库库存", "持仓库存", "伦敦金库", "吨", "月",
                          "LBMA London Vault Data(月度,滞后1月;该xlsx口径始于2023-02)"),
        "AU0_OI":        ("沪金主力持仓量", "沪金持仓量", "持仓库存", "期指持仓", "手", "日", S_SINA_FUT),
        "CN_GOLD_WAN":   ("中国官方黄金储备", "中国黄金储备", "官方储备", "央行", "万盎司", "月",
                          "新浪(央行口径,月度7日前后)"),
        "DFII10":        ("美国10年期TIPS实际利率", "10Y实际利率", "宏观因子", "利率", "%", "日", S_FRED),
        "T10YIE":        ("美国10年期盈亏平衡通胀", "盈亏平衡通胀", "宏观因子", "利率", "%", "日", S_FRED),
        "US10Y":         ("美债10年期名义收益率", "10Y名义利率", "宏观因子", "利率", "%", "日", S_EM),
        "DTWEXBGS":      ("美元指数(贸易加权宽口径)", "美元指数(宽)", "宏观因子", "汇率", "点", "日", S_FRED),
        "VIXCLS":        ("VIX恐慌指数", "VIX", "宏观因子", "风险", "点", "日", S_FRED),
        "GPR":           ("地缘政治风险指数", "GPR地缘风险", "宏观因子", "风险", "点", "月", S_GPR),
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
        "commodity": "gold",
        "range": {"begin": min(all_ds), "end": max(all_ds)} if all_ds else None,
        "warnings": warnings,
        "series": series_out,
    }
    DATA.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    total = sum(len(s["points"]) for s in series_out)
    print("[gold] 写盘 %s: %d 序列 %d 点,失败 %d(%s),耗时 %.1fs" % (
        OUT.name, len(series_out), total, len(warnings),
        ",".join(w.split(":")[0] for w in warnings) or "-", time.time() - t0), flush=True)
    # 失败过半才让 job 变红:单序列偶发失败是常态,交给次日自愈
    failed = len(SPEC) - len(series_out)
    return 1 if failed > len(SPEC) / 2 else 0


if __name__ == "__main__":
    sys.exit(main())
