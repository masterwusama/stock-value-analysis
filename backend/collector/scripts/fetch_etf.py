# -*- coding: utf-8 -*-
"""ETF 综合评估采集器 → data/etf/*.json

数据面(全部免费源):
  - EM f10 jbgk   : 基金名称/跟踪指数/费率/成立日(服务端渲染 HTML,周级 TTL)
  - 腾讯 spot     : ETF 场内价/规模/52 周 + fundgz 单位净值与盘中估算(折溢价)
  - EM f10 jjcc   : 季报持仓(前十重仓,jsonp+内嵌 HTML 表),相邻季 diff → 调仓
  - 招商基金官网  : ETF 申赎清单(PCF)全量篮子(meta.pcf='cmfchina' 的标的,
                    ~50 只官方数量/金额,权重=金额占比,与单位净值自洽已验证)
  - 国证官网      : 980xxx 定制指数全量成份月度快照(xlsx,官方权重+行业)+指数日线;
                    名录快照(indexList)带上市指数当日 PE/PB,能覆盖则存官方值
  - EM fundmobapi : ETF 公告(移动端接口,失败降级跳过)
  - 估值自算      : 最新官方权重(无则前十重仓) × 腾讯个股 PE/PB(A:39/46 HK:57/58)
                    + index.json scores.divYield → 指数 PE(调和)/PB(调和)/股息率(加权)
                    日序列累积于 index_val/<index>.json,分位样本天数诚实披露

产物:
  data/etf/etf_list.json            名单 + 指数解析元数据(980xxx 代码等)
  data/etf/<code>.json              单只 ETF 全量(info/spot/holdings/changes/anns)
  data/etf/index_val/<index>.json   指数估值序列(dates/pe/pb/dv/close)

口径注:指数估值双口径整体法,权重=官方/PCF 权重的 pe/pb 为「ETF 实际持仓」
口径,市值权重的 pe_mcap/pb_mcap 为「Wind/恒指官方对表」口径(2026-10-26 实测
159263 市值口径 9.02/0.85 vs Wind 9.01/0.85 分毫不差);国证名录官方快照已停用
(其 PB 口径离群,见 fetch_one 注释)。
"""
import os

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"

import argparse
import base64
import datetime as dt
import io
import json
import re
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parents[1]          # backend/collector
DATA = BASE / "data" / "etf"
VAL_DIR = DATA / "index_val"
LIST_JSON = DATA / "etf_list.json"
INDEX_JSON = BASE / "data" / "index.json"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
SLEEP = 0.4
BASIC_TTL_DAYS = 7          # jbgk/指数解析 元数据周刷

F10 = "https://fundf10.eastmoney.com"
TENCENT_URL = "http://qt.gtimg.cn/q="
FUNDMOB_ANN = "https://fundmobapi.eastmoney.com/FundMNewApi/FundMNAnnouncementList"
CNI_INDEX_LIST = "https://www.cnindex.com.cn/index/indexList"
CNI_HIST = "http://hq.cnindex.com.cn/market/market/getIndexDailyDataWithDataFormat"
CNI_SAMPLE_HIST = "https://www.cnindex.com.cn/sample-detail/download-history"

QTR_MAP = {"1季度": "03-31", "2季度": "06-30", "中期": "06-30", "3季度": "09-30",
           "4季度": "12-31", "年度": "12-31"}

# 中证指数官网(公开文件直链,官方月度权重快照+指数收盘/滚动PE 历史)
CSI_CLOSEWEIGHT = ("https://oss-ch.csindex.com.cn/static/html/csindex/"
                   "public/uploads/file/autofile/closeweight/{}closeweight.xls")
CSI_INDEX_PERF = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
# 新浪 ETF 场内日线(轻量直连,避开 akshare 导入的 ~10s 启动成本)
SINA_KLINE = ("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData")

# 招商基金官网接口的签名密钥(前端 JS 静态串,公开披露页;算法见 fetch_cmf_pcf)
CMF_SECRET = "d274d06273f96656442b0728316026a1"
CMF_RID = "01ab90a0d4ce45f3bb35b919099d9da1"
CMF_API = "https://www.cmfchina.com/ws-business-server/fund/getEtfStockList"
EFUNDS_FUND_PAGE = "https://www.efunds.com.cn/fund/{code}_org.shtml"


def _get(url, params=None, headers=None, gbk=False, retries=3, timeout=15):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers or UA, timeout=timeout)
            r.encoding = "gbk" if gbk else r.apparent_encoding or "utf-8"
            if r.status_code == 200:
                return r
            last = RuntimeError(f"HTTP {r.status_code}")
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(1.5 * (i + 1))
    raise last


def atomic_write(path, payload):
    tmp = str(path) + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, str(path))


def _num(v):
    try:
        f = float(str(v).replace(",", "").replace("%", "").strip())
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _txt(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


# ---------------------------------------------------------------------------
# 1) f10 基本概况(周级 TTL)
# ---------------------------------------------------------------------------

def fetch_f10_basic(code):
    """jbgk 服务端渲染页 → 基础信息;抓不到的字段容忍为 None。"""
    r = _get(f"{F10}/jbgk_{code}.html", headers={**UA, "Referer": f"{F10}/{code}.html"})
    html = r.text

    def grab(label):
        m = re.search(rf"{label}</th>\s*<td[^>]*>([^<]*)</td>", html)
        return m.group(1).strip() or None if m else None

    m = re.search(r"基金全称</th>\s*<td[^>]*>([^<]*)</td>", html)
    full_name = m.group(1).strip() if m else None
    m = re.search(r"<span class=\"FundName\">([^<]+)</span>", html) or re.search(
        r"基金简称</th>\s*<td[^>]*>([^<]*)</td>", html)
    name = m.group(1).strip() if m else None
    return {
        "name": name, "full_name": full_name,
        "tracked_index": grab("跟踪标的"),
        "benchmark": grab("业绩比较基准"),
        "mgmt_fee": grab("管理费率"), "custody_fee": grab("托管费率"),
        "inception": grab("成立日期"), "list_date": grab("上市时间"),
        "scale_text": grab("资产规模"),
        "fetched_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# 2) 指数解析:国证名录匹配 → 980xxx 定制代码需 etf_list.meta 手工/官网补
# ---------------------------------------------------------------------------

def cni_catalog():
    """国证名录(1491 只,含当日 PE/PB)→ {名称关键词: row}。"""
    rows, pn = [], 1
    while True:
        r = _get(CNI_INDEX_LIST, params={"channelCode": -1, "rows": 500, "pageNum": pn})
        data = r.json().get("data") or {}
        got = data.get("rows") or []
        rows += got
        if len(rows) >= int(data.get("total") or 0) or not got:
            break
        pn += 1
        time.sleep(SLEEP)
    return rows


def resolve_index(code, basic, meta, catalog):
    """解析跟踪指数 → {key, name, family, cni_code, csi_code, basis}。
    family: cni=国证(官网月度快照) / csi=中证(官网 closeweight 月度快照+收盘历史) /
    hsi=恒生系(招商 PCF) / other。980xxx 国证与中证指数代码靠 etf_list.meta 兜底,
    名录/名字匹配只是辅助。"""
    idx_name = (meta or {}).get("index_name") or (basic or {}).get("tracked_index") or ""
    idx_name = re.sub(r"(指数)?([（(]价格[）)])?$", "", idx_name.strip())
    m = meta or {}
    fam = m.get("family")
    if not fam:
        fam = ("hsi" if ("恒生" in idx_name or "港股通" in idx_name)
               else "cni" if "国证" in idx_name
               else "csi" if "中证" in idx_name else "other")
    cni_code = m.get("index_code") if fam == "cni" else None
    csi_code = m.get("index_code") if fam == "csi" else None
    if not cni_code and fam == "cni" and catalog:
        for row in catalog:
            if idx_name and idx_name.replace("国证", "") in str(row.get("indexname", "")):
                cni_code = str(row.get("indexcode"))
                break
    key = m.get("index_key") or (
        cni_code or csi_code or "IDX_" + re.sub(r"\W", "", idx_name)[:12])
    return {"key": key, "name": idx_name, "family": fam, "cni_code": cni_code,
            "csi_code": csi_code,
            "basis": "official" if (cni_code or csi_code) else "top10"}


# ---------------------------------------------------------------------------
# 3) 场内快照:腾讯 spot + fundgz 净值/估算
# ---------------------------------------------------------------------------

def etf_prefix(code):
    return ("sh" if code.startswith(("5", "6")) else "sz") + code


def fetch_spot(code):
    r = _get(TENCENT_URL + etf_prefix(code), gbk=True)
    m = re.search(r"=\"(.+)\"", r.text.strip())
    parts = m.group(1).split("~") if m else []
    if len(parts) < 50:
        return None

    def num(i):
        return _num(parts[i]) if i < len(parts) else None

    spot = {
        "time": parts[30] if len(parts) > 30 else None,
        "price": num(3), "change_pct": None if num(32) is None else round(num(32) / 100, 4),
        "turnover_wan": num(37),
        "scale_yi": num(45),            # 总市值(亿)≈基金规模
        "week52_high": num(47), "week52_low": num(48),
    }
    try:  # pingzhongdata:单位净值全史(fundgz 不覆盖 ETF)
        g = _get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
                 headers={**UA, "Referer": f"https://fund.eastmoney.com/{code}.html"},
                 retries=2, timeout=12)
        nm = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", g.text)
        trend = json.loads(nm.group(1)) if nm else []
        if trend:
            last = trend[-1]
            nav_date = dt.date.fromtimestamp(last["x"] / 1000).isoformat()
            spot.update({"nav": _num(last["y"]), "nav_date": nav_date,
                         "nav_series": [[dt.date.fromtimestamp(p["x"] / 1000).isoformat(),
                                         p["y"]] for p in trend[-250:]],
                         "premium_pct": None})
            if spot["price"] and last["y"]:
                # 静态折溢价:对最新披露净值(ETF 盘中无免费 IOPV,口径注明)
                spot["premium_pct"] = round((spot["price"] / last["y"] - 1) * 100, 3)
    except Exception:  # noqa: BLE001  净值接口失败不阻断行情
        pass
    return spot


# ---------------------------------------------------------------------------
# 4) f10 季报持仓(前十重仓)+ 相邻季调仓
# ---------------------------------------------------------------------------

def fetch_holdings_f10(code):
    """jjcc jsonp,显式按年拉 3 年(year='' 只回当年)。
    → {quarters: [报告期...], rows: {报告期: [holding...]}, top_weight_cov}。
    表格列数随季度变(新季度带「变动详情」列,权重格按百分数定位而非固定下标)。"""
    quarters, rows = [], {}
    this_year = dt.date.today().year
    for year in (this_year, this_year - 1, this_year - 2):
        r = _get(f"{F10}/FundArchivesDatas.aspx",
                 params={"type": "jjcc", "code": code, "topline": 10, "year": year, "month": ""},
                 headers={**UA, "Referer": f"{F10}/ccmx_{code}.html"})
        m = re.search(r'content:"(.*)",arryear', r.text, re.S)
        if not m:
            continue
        html = m.group(1).replace('\\"', '"')
        for sec in re.split(r"<h4 class='t'>", html)[1:]:
            head = _txt(sec.split("</h4>")[0])
            qm = re.search(r"(\d{4})年(中期|年度|\d季度)", head)
            if not qm or int(qm.group(1)) != year:
                continue
            qdate = f"{qm.group(1)}-{QTR_MAP[qm.group(2)]}"
            holdings = []
            for tr in re.findall(r"<tr>(.*?)</tr>", sec, re.S):
                cells = [_txt(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
                if len(cells) < 5 or not re.fullmatch(r"\d{5,6}", cells[1] or ""):
                    continue
                pct_i = next((i for i, c in enumerate(cells[3:], 3)
                              if re.fullmatch(r"\d+(\.\d+)?%", c or "")), None)
                if pct_i is None:
                    continue
                w = _num(cells[pct_i])
                holdings.append({
                    "code": cells[1].zfill(5) if len(cells[1]) == 5 else cells[1],
                    "name": cells[2],
                    "weight": None if w is None else round(w / 100, 6),
                    "shares_wan": _num(cells[pct_i + 1]) if pct_i + 1 < len(cells) else None,
                    "value_wan": _num(cells[pct_i + 2]) if pct_i + 2 < len(cells) else None})
            if holdings:
                quarters.append(qdate)
                rows[qdate] = holdings
        time.sleep(SLEEP)
    quarters = sorted(set(quarters), reverse=True)
    return {"quarters": quarters, "rows": rows,
            "top_weight_cov": round(sum(h["weight"] or 0 for h in rows.get(quarters[0], [])), 4)
            if quarters else None}


def diff_holdings(prev, cur):
    """相邻两期持仓 → 调入/调出/权重变动。"""
    pm = {h["code"]: h for h in prev or []}
    cm = {h["code"]: h for h in cur or []}
    return {
        "ins": [dict(cm[c], chg=round((cm[c]["weight"] or 0), 6)) for c in cm if c not in pm],
        "outs": [dict(pm[c], chg=round(-(pm[c]["weight"] or 0), 6)) for c in pm if c not in cm],
        "wchg": [{"code": c, "name": cm[c]["name"],
                  "from": pm[c].get("weight"), "to": cm[c].get("weight")}
                 for c in cm if c in pm and cm[c].get("weight") != pm[c].get("weight")],
    }


# ---------------------------------------------------------------------------
# 5) 国证官方:全量成份月度快照 + 指数日线
# ---------------------------------------------------------------------------

def fetch_cni_constituents(cni_code):
    """官方最新成份快照 xlsx(dateStr 对定制指数不生效,每月同一文件,无历史可下;
    历史靠本采集器逐月累积进 etf_holding 表)。
    → {month, rows: [holding...]}"""
    import openpyxl

    for back in range(3):  # 当月未发布时回退前两月
        k = dt.date.today().year * 12 + dt.date.today().month - back - 1
        tag = f"{k // 12}{k % 12 + 1:02d}"
        try:
            r = _get(CNI_SAMPLE_HIST, params={"indexcode": cni_code, "dateStr": tag},
                     timeout=25, retries=2)
            wb = openpyxl.load_workbook(io.BytesIO(r.content))  # read_only 行会残缺,勿开
            ws = wb.active
            holds, month_key = [], None
            for row in ws.iter_rows(min_row=2, values_only=True):
                if not row or not row[1]:
                    continue
                d = str(row[0])[:10]
                month_key = d[:7]
                holds.append({"code": str(row[1]).zfill(6), "name": str(row[2]).strip(),
                              "industry": (str(row[3]).strip() if row[3] else None),
                              "mcap_yi": _num(row[4]),
                              "weight": None if _num(row[5]) is None else round(_num(row[5]) / 100, 6)})
            wb.close()
            if holds:
                return {"month": month_key or tag, "rows": holds}
        except Exception:  # noqa: BLE001  未发布/无文件月份跳过
            continue
        time.sleep(SLEEP)
    return None


def fetch_cni_ohlc(cni_code):
    """指数日线全史(上市起) → [[date, close], ...](估值图的点位底衬)。"""
    r = _get(CNI_HIST, params={"indexCode": cni_code, "startDate": "2015-01-01",
                               "endDate": dt.date.today().isoformat(), "frequency": "day"},
             timeout=25, retries=2)
    data = (r.json().get("data") or {}).get("data") or []
    out = []
    for row in data:
        try:
            out.append([str(row[0])[:10], round(float(row[5]), 4)])
        except (TypeError, ValueError, IndexError):
            continue
    out.sort()
    return out


# ---------------------------------------------------------------------------
# 5.5) 招商基金官网 ETF 申赎清单(PCF):全量篮子
# ---------------------------------------------------------------------------

def fetch_cmf_pcf(code):
    """招商基金官网 getEtfStockList → 全量申赎篮子(恒生系指数的全量成份来源)。

    站点接口走国密签名(secret 为前端 JS 静态串,公开披露页):
      key=sm3(secret)[:16];data=base64(SM4-ECB(json));sig=sm3(secret..data..rid..encrypted)
    响应明文。权重=tdAmount/Σ(tdAmount):各券金额与官方「最小申赎单位资产净值」
    的差恰等于现金部分(2026-10-26 实测 517362.65 vs 517268.14-(-94.51)),自洽。
    清单 T 日开市前发布,取不到时回退前一交易日。"""
    from gmssl import func as gfunc
    from gmssl import sm3
    from gmssl.sm4 import CryptSM4, SM4_ENCRYPT

    key = sm3.sm3_hash(gfunc.bytes_to_list(CMF_SECRET.encode()))[:16].encode()
    for back in range(3):  # 当日清单未出则回退,最多看 3 天(长假后首日)
        day = dt.date.today() - dt.timedelta(days=back)
        payload = {"startDate": day.isoformat(), "productCode": code, "pageNum": 1,
                   "pageSize": 10000000, "isPreview": "", "siteno": "web",
                   "merchantId": 0, "request_id": str(uuid.uuid4())}
        crypt = CryptSM4()
        crypt.set_key(key, SM4_ENCRYPT)
        d64 = base64.b64encode(
            crypt.crypt_ecb(json.dumps(payload, separators=(",", ":")).encode())).decode()
        sig = sm3.sm3_hash(gfunc.bytes_to_list(
            f"secret={CMF_SECRET}data={d64}request_id={CMF_RID}encrypted=true".encode()))
        try:
            r = _get(CMF_API, params={"data": d64, "request_id": CMF_RID, "encrypted": "true"},
                     headers={**UA,
                              "Referer": f"https://www.cmfchina.com/web/fundDetail/{code}/index.html",
                              "tk-trans-merchant-key": "thinkive", "tk-trans-signature": sig})
            lst = ((r.json().get("data") or {}).get("list")) or []
        except Exception:  # noqa: BLE001
            return None
        rows, total = [], 0.0
        for x in lst:
            amt = _num(x.get("tdAmount"))
            if not x.get("stockCode") or not amt or amt <= 0:
                continue
            total += amt
            rows.append({"code": str(x["stockCode"]).zfill(5),
                         "name": (x.get("stockName") or "").strip(),
                         "shares": _num(x.get("stockNum")), "amount": amt})
        if rows:
            for h in rows:
                h["weight"] = round(h["amount"] / total, 6)
            return {"date": day.isoformat(), "rows": rows}
        time.sleep(SLEEP)
    return None


# ---------------------------------------------------------------------------
# 5.6) 中证官网:官方月度权重快照(全量成份)+指数收盘历史
# ---------------------------------------------------------------------------

def fetch_csi_closeweight(csi_code):
    """中证官网 {code}closeweight.xls → 官方全量成份+权重(月度快照,老式 BIFF xls
    需 pandas/xlrd)。输出与国证官方同一形状({month, rows}),回灌同走 official 槽位。"""
    import pandas as pd

    r = _get(CSI_CLOSEWEIGHT.format(csi_code),
             headers={**UA, "Referer": "https://www.csindex.com.cn/"}, timeout=30)
    df = pd.read_excel(io.BytesIO(r.content))
    col_code = next(c for c in df.columns if "成份券代码" in str(c))
    col_name = next(c for c in df.columns if "成份券名称" in str(c))
    col_w = next(c for c in df.columns if "权重" in str(c))
    col_date = next(c for c in df.columns if "日期" in str(c))
    holds = []
    for _, row in df.iterrows():
        w = _num(row[col_w])
        holds.append({"code": str(row[col_code]).split(".")[0].zfill(6),
                      "name": str(row[col_name]).strip(),
                      "industry": None, "mcap_yi": None,
                      "weight": None if w is None else round(w / 100, 6)})
    if not holds:
        return None
    ds = str(df.iloc[0][col_date]).replace("-", "").replace("/", "")
    return {"month": f"{ds[:4]}-{ds[4:6]}", "rows": holds}


def fetch_csi_perf(csi_code):
    """中证官网 index-perf → 指数收盘全史 + 官方滚动 PE 全史。

    接口对大日期窗的返回量不稳定(同参数实测有时给 5 年、有时只给近几日,
    512800/513070 都踩过)——按年分段拉取合并去重,保证完整。
    close 作估值图底衬;官方 PE 与自算市值口径同口径、贴合度实测 ~98.5%
    (无亏损股的池子;含亏损池子如港股通消费官方含亏损会系统性高 ~50%,
    所以 pe_mcap 列只收官方值不混自算)。官方 PB 无公开文件,PB 两列恒自算。"""
    close, pe = {}, {}
    this_year = dt.date.today().year
    for year in range(this_year - 11, this_year + 1):
        try:
            r = _get(CSI_INDEX_PERF, params={"indexCode": csi_code,
                                             "startDate": f"{year}0101",
                                             "endDate": f"{year}1231"},
                     headers={**UA, "Referer": "https://www.csindex.com.cn/"}, timeout=25)
            data = r.json().get("data") or []
        except Exception:  # noqa: BLE001 单年失败不拖垮整段,下轮补
            continue
        for row in data:
            d = str(row.get("tradeDate") or "")
            if len(d) != 8:
                continue
            iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            try:
                if row.get("close") is not None:
                    close[iso] = round(float(row["close"]), 4)
                peg = _num(row.get("peg"))
                if peg and peg > 0:
                    pe[iso] = peg
            except (TypeError, ValueError):
                continue
        time.sleep(0.3)
    return {"close": [[d, close[d]] for d in sorted(close)],
            "pe": [[d, pe[d]] for d in sorted(pe)]}


# ---------------------------------------------------------------------------
# 5.55) 易方达官网 ETF 申赎清单(PCF):服务端渲染 HTML 直读
# ---------------------------------------------------------------------------

def fetch_efunds_pcf(code):
    """易方达官网基金页 etfStocksContainer 表(服务端渲染,无加密)。
    表给数量无金额,权重=数量×腾讯现价占篮子比——与招商 PCF 的官方金额权重
    实测两位小数一致(2026-10-26 同指数双基金交叉验证)。剔除申赎现金行。"""
    from universe import tencent_spot_hk

    r = _get(EFUNDS_FUND_PAGE.format(code=code), timeout=25)
    i = r.text.find("etfStocksContainer")
    if i < 0:
        return None
    seg = r.text[i:i + 300000]
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", seg, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).replace("&nbsp;", "").strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        cells = [c for c in cells if c]
        if (len(cells) >= 3 and cells[0] != "159900"
                and re.fullmatch(r"\d{5,6}", cells[0] or "")):
            rows.append(cells)
    if not rows:
        return None
    hk_codes = [c[0] for c in rows if len(c[0]) == 5]
    q = tencent_spot_hk(hk_codes, quiet=True) if hk_codes else {}
    out, total = [], 0.0
    for c in rows:
        shares = _num(c[2])
        px = (q.get(c[0]) or {}).get("price") if len(c[0]) == 5 else None
        if not shares or not px:
            continue
        amt = shares * px
        total += amt
        out.append({"code": c[0], "name": c[1], "shares": shares, "amount": amt})
    if not out:
        return None
    for h in out:
        h["weight"] = round(h["amount"] / total, 6)
    return {"date": dt.date.today().isoformat(), "rows": out}


# ---------------------------------------------------------------------------
# 6) ETF 公告(EM 移动端,失败降级)
# ---------------------------------------------------------------------------

def fetch_anns(code, limit=30):
    try:
        r = _get(FUNDMOB_ANN, params={"FCODE": code, "pageIndex": 1, "pageSize": limit,
                                      "deviceid": "Wap", "plat": "Wap", "product": "EFund",
                                      "version": "6.2.8"},
                 headers={**UA, "Referer": "https://fund.eastmoney.com/"})
        lst = r.json().get("Datas") or []
        out = []
        for a in lst:
            t = (a.get("TITLE") or "").strip()
            d = (a.get("PUBLISHDATE") or "")[:10]
            if t and d:
                out.append({"date": d, "title": t, "art_code": str(a.get("ID") or a.get("ANN_ID") or "")[:40]})
        return out
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 7) 估值自算:权重 × 腾讯个股 PE/PB + index.json 股息率
# ---------------------------------------------------------------------------

def load_index_scores():
    """index.json → {code: (market, divYield)}。"""
    try:
        d = json.loads(INDEX_JSON.read_text(encoding="utf-8"))
        return {c["code"]: (c.get("market"), (c.get("scores") or {}).get("divYield"))
                for c in d.get("companies", [])}
    except Exception:  # noqa: BLE001
        return {}


def calc_index_valuation(holdings):
    """官方/PCF 权重 → 双口径整体法估值。

    指数权重口径(pe/pb/dv):Σw/Σ(w/PE_i) 调和整体法——权重=官方成份/PCF 篮子,
    即「按 ETF 实际持仓买」的组合估值体验;
    市值权重口径(pe_mcap/pb_mcap):Σ市值/Σ(市值/PE_i)——与 Wind/恒指官方读数
    对表的口径(2026-10-26 双 ETF 实测:159263 市值口径 9.02/0.85 vs Wind 9.01/0.85
    分毫不差)。两口径都用腾讯个股 PE(TTM)/PB,正值样本;单市场指数无汇率混算问题。
    """
    from universe import tencent_spot, tencent_spot_hk

    a_codes = [h["code"] for h in holdings if len(h["code"]) == 6]
    hk_codes = [h["code"] for h in holdings if len(h["code"]) == 5]
    quotes = {}
    if a_codes:
        quotes.update(tencent_spot(a_codes, quiet=True))
    if hk_codes:
        quotes.update(tencent_spot_hk(hk_codes, quiet=True))
    dv_map = load_index_scores()

    w_pe = w_pb = s_w_pe = s_w_pb = 0.0
    w_dv = s_w_dv = w_all = 0.0
    mc_pe = mc_pb = s_mc_pe = s_mc_pb = 0.0
    n_hit = 0
    for h in holdings:
        w = h.get("weight") or 0
        w_all += w
        q = quotes.get(h["code"]) or {}
        pe, pb, mcap = q.get("pe_ttm"), q.get("pb"), q.get("market_cap") or 0
        if pe and pe > 0:
            w_pe += w
            s_w_pe += w / pe
            mc_pe += mcap
            s_mc_pe += mcap / pe
            n_hit += 1
        if pb and pb > 0:
            w_pb += w
            s_w_pb += w / pb
            mc_pb += mcap
            s_mc_pb += mcap / pb
        dv = dv_map.get(h["code"], (None, None))[1]
        if dv is not None and dv >= 0:
            s_w_dv += w
            w_dv += w * dv
    return {
        "pe": round(w_pe / s_w_pe, 3) if s_w_pe > 0 else None,
        "pb": round(w_pb / s_w_pb, 3) if s_w_pb > 0 else None,
        "pe_mcap": round(mc_pe / s_mc_pe, 3) if s_mc_pe > 0 else None,
        "pb_mcap": round(mc_pb / s_mc_pb, 3) if s_mc_pb > 0 else None,
        "dv": round(w_dv / s_w_dv * 100, 3) if s_w_dv > 0 else None,   # 百分数
        "pe_cov": round(w_pe / w_all, 4) if w_all else None,
        "dv_cov": round(s_w_dv / w_all, 4) if w_all else None,
        "n": n_hit,
    }


def append_index_val(meta, day_row, ohlc=None):
    """自算/官方日行累积进序列档;同日 upsert;裁 10 年窗。"""
    VAL_DIR.mkdir(parents=True, exist_ok=True)
    path = VAL_DIR / f"{meta['key']}.json"
    doc = {}
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            doc = {}
    dates = doc.get("dates") or []
    cols = {k: doc.get(k) or [] for k in ("pe", "pb", "dv", "pe_mcap", "pb_mcap")}
    d = day_row["d"]
    if d in dates:  # 同日重跑=以本轮结果为准,全键覆盖(含置 None,如官方值未出)
        i = dates.index(d)
        for k in cols:
            while len(cols[k]) <= i:
                cols[k].append(None)
            cols[k][i] = day_row.get(k)
    else:
        dates.append(d)
        for k in cols:
            cols[k].append(day_row.get(k))
    order = sorted(range(len(dates)), key=lambda i: dates[i])
    dates = [dates[i] for i in order]
    cols = {k: [v[i] for i in order] for k, v in cols.items()}
    keep_from = (dt.date.today() - dt.timedelta(days=365 * 10)).isoformat()
    i0 = 0
    while i0 < len(dates) and dates[i0] < keep_from:
        i0 += 1
    doc = {"index_key": meta["key"], "index_name": meta["name"], "family": meta["family"],
           "method": "calc", "basis": meta["basis"], "updated_at":
               datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
           "dates": dates[i0:], "pe": cols["pe"][i0:], "pb": cols["pb"][i0:],
           "dv": cols["dv"][i0:], "pe_mcap": cols["pe_mcap"][i0:],
           "pb_mcap": cols["pb_mcap"][i0:]}
    if ohlc is not None:
        doc["close"] = ohlc
    atomic_write(path, doc)
    return len(dates[i0:])


def backfill_official_pe(meta, pe_hist):
    """中证官方滚动 PE 历史 → pe_mcap 列回填(仅补缺失日期,不覆盖已有自算行;
    当日官方值在 append 的 day_row 里已优先采用)。同列=同口径(市值整体法),
    缝合差实测 ~1.5%,分位按秩计算对此不敏感。"""
    if not pe_hist:
        return 0
    VAL_DIR.mkdir(parents=True, exist_ok=True)
    path = VAL_DIR / f"{meta['key']}.json"
    doc = {}
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            doc = {}
    dates = doc.get("dates") or []
    cols = {k: doc.get(k) or [] for k in ("pe", "pb", "dv", "pe_mcap", "pb_mcap")}
    have = set(dates)
    added = 0
    for d, v in pe_hist:
        if d in have:
            continue
        dates.append(d)
        cols["pe_mcap"].append(v)
        for k in ("pe", "pb", "dv", "pb_mcap"):
            cols[k].append(None)
        added += 1
    if not added:
        return 0
    order = sorted(range(len(dates)), key=lambda i: dates[i])
    doc = {**{k: doc.get(k) for k in ("index_key", "index_name", "family", "method",
                                      "basis", "updated_at", "close")},
           "dates": [dates[i] for i in order],
           **{k: [cols[k][i] for i in order] for k in cols}}
    atomic_write(path, doc)
    return added


def fetch_etf_hist(code, nav_series):
    """ETF 场内价格史(新浪日线直连)+ 净值史(pingzhongdata,fetch_spot 已带)。
    近 250 个交易日,回灌进 etf_spot 缺失日期,API 层拼价格/净值/指数归一曲线。"""
    symbol = ("sh" if code.startswith(("5", "6")) else "sz") + code
    try:
        r = _get(SINA_KLINE, params={"symbol": symbol, "scale": 240, "ma": "no",
                                     "datalen": 250},
                 headers={**UA, "Referer": "https://finance.sina.com.cn"}, timeout=20)
        kl = json.loads(r.text)
        price = [[x["day"], _num(x["close"])] for x in kl if x.get("day") and _num(x.get("close"))]
    except Exception:  # noqa: BLE001
        price = []
    return {"price": price, "nav": nav_series or []}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def fetch_one(code, meta, catalog, force=False, today=None):
    today = today or dt.date.today()
    out_path = DATA / f"{code}.json"
    old = {}
    if out_path.exists():
        try:
            old = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            old = {}

    # 基础信息周刷
    basic = old.get("info", {})
    if force or not basic.get("tracked_index") or _stale(basic.get("fetched_at"), BASIC_TTL_DAYS):
        basic = fetch_f10_basic(code)
        time.sleep(SLEEP)
    idx = resolve_index(code, basic, meta, catalog)

    spot = fetch_spot(code)
    time.sleep(SLEEP)
    f10_h = fetch_holdings_f10(code)
    time.sleep(SLEEP)

    cni = None
    ohlc = None
    official_pe = []
    if idx["family"] == "csi" and idx.get("csi_code"):
        try:
            cni = fetch_csi_closeweight(idx["csi_code"])   # 同形状复用 official 槽位
        except Exception as e:  # noqa: BLE001
            print(f"  [{code}] 中证权重下载失败: {e!r}", flush=True)
        try:
            perf = fetch_csi_perf(idx["csi_code"])
            ohlc = perf["close"]
            official_pe = perf["pe"]
        except Exception as e:  # noqa: BLE001
            print(f"  [{code}] 中证收盘/官方PE历史失败: {e!r}", flush=True)
        time.sleep(SLEEP)
    elif idx["cni_code"]:
        try:
            cni = fetch_cni_constituents(idx["cni_code"])
        except Exception as e:  # noqa: BLE001
            print(f"  [{code}] 国证成份下载失败: {e!r}", flush=True)
        ohlc = fetch_cni_ohlc(idx["cni_code"])
        time.sleep(SLEEP)

    # 恒生系/其他走 PCF 的 ETF 可选挂中证官方 PE 源(meta.csi_perf=中证指数代码):
    # 持仓仍走 PCF/官网快照,官方 PE 历史回填 pe_mcap 列+指数点位底衬(如 513070
    # 跟踪的中证港股通消费 931454)——csi 家族自己的 perf 在上面分支已取
    if official_pe == [] and not ohlc:
        csi_perf_code = (meta or {}).get("csi_perf")
        if csi_perf_code:
            try:
                perf = fetch_csi_perf(csi_perf_code)
                ohlc = perf["close"]
                official_pe = perf["pe"]
            except Exception as e:  # noqa: BLE001
                print(f"  [{code}] 中证官方PE挂载失败({csi_perf_code}): {e!r}", flush=True)

    # PCF 全量篮子(meta.pcf='cmfchina' 招商加密接口 / 'efunds' 易方达 HTML 直读)
    pcf = None
    pcf_src = (meta or {}).get("pcf")
    try:
        if pcf_src == "cmfchina":
            pcf = fetch_cmf_pcf(code)
        elif pcf_src == "efunds":
            pcf = fetch_efunds_pcf(code)
    except Exception as e:  # noqa: BLE001
        print(f"  [{code}] PCF 拉取失败: {e!r}", flush=True)
    if pcf:
        time.sleep(SLEEP)

    # 估值自算:官方全量 > PCF 全量 > 前十重仓
    if cni and cni.get("rows"):
        base_h = cni["rows"]
        val_basis, val_basis_date = "official", cni["month"]
    elif pcf and pcf.get("rows"):
        base_h = pcf["rows"]
        val_basis, val_basis_date = "pcf", pcf["date"]
    elif f10_h and f10_h["quarters"]:
        q = f10_h["quarters"][0]
        base_h = f10_h["rows"][q]
        val_basis, val_basis_date = "top10", q
    else:
        base_h, val_basis, val_basis_date = [], None, None

    calc = calc_index_valuation(base_h) if base_h else None
    series_days = None
    official_pe_map = dict(official_pe) if official_pe else {}
    if calc:
        # 双口径整体法:指数权重(pe/pb)=ETF 实际持仓体验;市值权重(pe_mcap/pb_mcap)
        # =Wind/恒指官方对表口径。中证系当日 pe_mcap 优先取官方滚动 PE(同口径),
        # 官方历史由 backfill_official_pe 补,分位即刻可用
        row = {"d": today.isoformat(), "pe": calc["pe"], "pb": calc["pb"],
               "dv": calc["dv"],
               # 官方序列存在时 pe_mcap 只收官方值(没有就 None 等下轮)——官方
               # 与自算在含亏损股的池子上差可达 ~50%(513070 实测 18.89 vs 12.85),
               # 混列会让分位拿自算值对官方历史算,系统性失真
               "pe_mcap": (official_pe_map.get(today.isoformat())
                           if official_pe_map else calc["pe_mcap"]),
               "pb_mcap": calc["pb_mcap"]}
        series_days = append_index_val({**idx, "basis": val_basis}, row, ohlc=ohlc)
        if official_pe:
            n_back = backfill_official_pe({**idx, "basis": val_basis}, official_pe)
            if n_back:
                series_days += n_back
                print(f"  [{code}] 官方PE历史回填 {n_back} 日", flush=True)

    anns = fetch_anns(code)

    # 调仓:季 diff兜底(官方/PCF 月度快照无历史,靠逐月累积后 API 层可切月 diff)
    changes = None
    if f10_h and len(f10_h["quarters"]) >= 2:
        a, b = f10_h["quarters"][0], f10_h["quarters"][1]
        changes = {"kind": "quarter", "from": b, "to": a,
                   **diff_holdings(f10_h["rows"][b], f10_h["rows"][a])}

    # 降级旗:期望全量源(pcf/官方快照)没拿到而落回 f10 前十时标记,页面挂黄条
    expected = ("pcf" if pcf_src else
                "official" if (idx.get("cni_code") or idx.get("csi_code")) else None)
    degraded = bool(expected and val_basis != expected)

    hist = fetch_etf_hist(code, (spot or {}).get("nav_series"))

    payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "info": basic, "index": idx, "spot": spot, "degraded": degraded,
        "valuation": {"method": "calc", "basis": val_basis, "basis_date": val_basis_date,
                      "calc": calc, "series_days": series_days},
        "holdings": {"official": cni, "pcf": pcf, "f10": f10_h},
        "hist": hist,
        "changes": changes, "anns": anns,
    }
    atomic_write(out_path, payload)
    n_h = len(base_h)
    print(f"  [{code}] {basic.get('name') or code} | 跟踪 {idx['name']}({idx['key']})"
          f" | 成份 {n_h}({val_basis}) | PE {calc and calc['pe']} DV {calc and calc['dv']}%"
          f" | 序列 {series_days} 日 | 公告 {len(anns) if anns else '降级'}条", flush=True)
    return payload


def _stale(ts, days):
    if not ts:
        return True
    try:
        return (dt.date.today() - dt.date.fromisoformat(ts[:10])).days > days
    except ValueError:
        return True


def main():
    ap = argparse.ArgumentParser(description="ETF 综合评估采集")
    ap.add_argument("--code", help="只跑指定代码")
    ap.add_argument("--force", action="store_true", help="忽略当日缓存重拉")
    args = ap.parse_args()

    DATA.mkdir(parents=True, exist_ok=True)
    seed = {"codes": ["159263", "159545", "159915", "563300", "512800", "513070"], "meta": {
        "159263": {"index_code": "980081"},       # 国证价值100(定制指数,名录查不到)
        "159545": {"pcf": "efunds"},              # 易方达→官网申赎清单 HTML 直读(全量)
        "159915": {"index_code": "399006", "family": "cni"},   # 创业板指(国证官方快照)
        "563300": {"index_code": "932000", "family": "csi"},  # 中证2000(官网权重快照)
        "512800": {"index_code": "399986", "family": "csi"},  # 中证银行(官网权重快照)
        "513070": {"pcf": "efunds", "csi_perf": "931454"},    # 中证港股通消费(PCF 全量+官方PE)
    }}
    if LIST_JSON.exists():
        try:
            seed = json.loads(LIST_JSON.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    codes = [args.code] if args.code else seed.get("codes") or []
    if not codes:
        print("etf_list.json 无 codes,退出")
        return

    print(f"[etf] {datetime.now():%F %T} 开始:{','.join(codes)}", flush=True)
    catalog = cni_catalog()
    print(f"  [国证名录] {len(catalog)} 只指数快照", flush=True)

    today = dt.date.today()
    for code in codes:
        old = {}
        p = DATA / f"{code}.json"
        if p.exists():
            try:
                old = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                old = {}
        if (not args.force and old.get("updated_at", "").startswith(today.isoformat())
                and old.get("valuation", {}).get("series_days")):
            print(f"  [{code}] 当日已更新,跳过(--force 重拉)", flush=True)
            continue
        try:
            payload = fetch_one(code, (seed.get("meta") or {}).get(code), catalog,
                                force=args.force, today=today)
            # 写回 etf_list 富化元数据(指数解析结果固化,避免每次名录匹配)。
            # index_code 区分家族:国证存 cni_code、中证存 csi_code,勿把对方冲成 None
            ix = payload["index"]
            meta = seed.setdefault("meta", {}).setdefault(code, {})
            if ix.get("cni_code"):
                meta["index_code"] = ix["cni_code"]
            elif ix.get("csi_code"):
                meta["index_code"] = ix["csi_code"]
                meta["family"] = "csi"
            meta["index_name"] = ix["name"]
            meta["index_key"] = ix["key"]
            seed["updated_at"] = payload["updated_at"]
            atomic_write(LIST_JSON, seed)
        except Exception as e:  # noqa: BLE001
            print(f"  [{code}] 失败: {e!r}", flush=True)
    print(f"[etf] 完成", flush=True)


if __name__ == "__main__":
    main()
