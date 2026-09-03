# -*- coding: utf-8 -*-
"""全市场标的池(A 股沪深京)与批量行情快照。

名单源优先级:
    1) 新浪 ak.stock_zh_a_spot()      一次返回 5500+ 只,含北交所 920/4/8 段(约 17s)
    2) 交易所官网 sh 主板/科创板 + sz A股列表   兜底(北交所官网与东财批量接口易被远端断连)
产出的名单缓存到 data/universe.json,供 fetch_data --all-market 与日更快照复用。

港股通标的单独一条源: load_universe_hk() 走东财 clist(板块码 b:DLMK0146,b:DLMK0144),
一次拉全 ≈621 只并顺带取 f100 行业,缓存到 data/universe_hk.json,供 fetch_data --hk-connect 用。

美股五大指数成分另走 load_universe_us(): 标普500(GitHub datasets) + 纳指100/费半/纳指生物
(Nasdaq 官方 WeightingData) + 道指30(维基模板),行业与代码存在性由东财全美股表补齐,
缓存到 data/universe_us.json,供 fetch_data --us-indexes 用。

估值快照走腾讯批量行情(每请求 60 只,约 95 个请求覆盖全市场),单次调用即可刷新
全市场现价/PE/PB/总市值,是"每天全量刷估值、财报按报告期增量"的基础。

用法:
    python universe.py --check              打印名单统计与批量快照抽样
    python universe.py --dump               重新拉取并写 data/universe.json
    python universe.py --dump-hk            重新拉取并写 data/universe_hk.json(港股通)
    python universe.py --dump-us            重新拉取并写 data/universe_us.json(美股指数成分)
    python universe.py --check-us           只看美股成分统计
"""
import argparse
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

os.environ.setdefault("no_proxy", "*")     # 本机代理会截断国内数据源连接
os.environ.setdefault("NO_PROXY", "*")

import requests  # noqa: E402

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(HERE))

CN_TZ = timezone(timedelta(hours=8))
UNIVERSE_JSON = BASE / "data" / "universe.json"
HK_UNIVERSE_JSON = BASE / "data" / "universe_hk.json"
US_UNIVERSE_JSON = BASE / "data" / "universe_us.json"
TENCENT_BATCH = 60          # 腾讯行情单请求代码数
TENCENT_URL = "http://qt.gtimg.cn/q="

# 东财列表接口（港股通成分用）：抓取高峰期 push2 / 33.push2 会被远端直接掐断
# （实测连吃 4 次 RemoteDisconnected），同一套参数 push2delay 一次即通，故三个子域轮着退避
EM_CLIST_HOSTS = ("https://push2delay.eastmoney.com", "https://push2.eastmoney.com",
                  "https://33.push2.eastmoney.com")
EM_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/center/gridlist.html",
}
GGT_BOARD = "b:DLMK0146,b:DLMK0144"        # 港股通成分（沪港通 + 深港通），实测 621 只
GGT_FIELDS = "f12,f14,f2,f9,f20,f23,f100"  # f12 代码 / f14 名称 / f100 所属行业

# ---- 美股指数成分源（探测记录见 docs/使用说明书.md §8）----
# 东财美股 clist 只有 push2delay 子域可用（push2 / 33.push2 一律 RemoteDisconnected），
# 所以美股查询必须固定单主机：跟着三子域轮询等于每页白跑两轮退避，而且四试全落在
# 死子域时整页丢失。m:105 纳斯达克 / m:106 纽交所 / m:107 美交所，分三段拉。
# 不带 t:1（普通股）过滤：那样会把 ASML/TSM/PDD/SNY 这批 ADS 存托凭证全滤掉（实测 24 只查无），
# 代价是表里混进 ETF/权证、行数撑到一万三千多（三段市别 total 相加），多花几十秒且必须抬高 max_pages。
US_EM_FS = ("m:105", "m:106", "m:107")
US_EM_HOSTS = (EM_CLIST_HOSTS[0],)
US_EM_PAGES = 200
US_EM_FIELDS = "f12,f14,f100,f20"
SP500_CSV_URL = ("https://raw.githubusercontent.com/datasets/s-and-p-500-companies/"
                 "main/data/constituents.csv")
NASDAQ_WEIGHT_URL = "https://indexes.nasdaqomx.com/Index/WeightingData"
WIKI_RAW_URL = "https://en.wikipedia.org/w/index.php?title={}&action=raw"
DJIA_TEMPLATE = "Template:Dow Jones Industrial Average companies"
# Nasdaq 官方成分接口认的指数代号；值是页面展示名（成分并集里用英文键，稳定且好过滤）
NASDAQ_INDEXES = {"NDX": "纳斯达克100", "SOX": "费城半导体", "NBI": "纳指生物科技"}
# 海外源：GitHub / api.nasdaq.com 可直连，但 en.wikipedia.org 与 indexes.nasdaqomx.com
# 直连必超时，必须走本机代理（可用 VA_OVERSEAS_PROXY 覆盖）
OVERSEAS_PROXY = os.environ.get("VA_OVERSEAS_PROXY", "http://127.0.0.1:1080")
_OVERSEAS_PROXY_ONLY = set()   # 直连已试过且失败的域名，本次进程内不再白等
OVERSEAS_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}


def market_of(code: str) -> str:
    """按代码段判定市场板块:A 股统一 market='A',板块信息在 code 前缀里。"""
    code = str(code).zfill(6)
    if code.startswith("688"):
        return "SH-STAR"
    if code.startswith("30"):
        return "SZ-ChiNext"
    if code.startswith(("4", "8", "92")):
        return "BJ"
    return "A"


def exchange_symbol(code: str) -> str:
    """腾讯/新浪行情的带交易所前缀代码。9→沪,0/2/3→深,4/8/92→北交所。"""
    code = str(code).zfill(6)
    if code.startswith("92") or code.startswith(("4", "8")):
        return "bj" + code
    if code.startswith(("6", "9")):
        return "sh" + code
    return "sz" + code


def _from_sina():
    """新浪全市场快照:代码/名称(含北交所)。"""
    import akshare as ak
    df = ak.stock_zh_a_spot()
    out = []
    for _, row in df.iterrows():
        code = str(row["代码"]).strip()
        num = code[-6:].zfill(6)
        if not num.isdigit():
            continue
        name = str(row["名称"]).strip()
        if not name:            # ST/*ST 等前缀股保留,只剔空名
            continue
        out.append((num, name))
    return out


def _from_exchanges():
    """交易所官网名单(沪主板 + 科创板 + 深 A),北交所缺失时仅作兜底。"""
    import akshare as ak
    out = []
    for fn in (lambda: ak.stock_info_sh_name_code(symbol="主板A股"),
               lambda: ak.stock_info_sh_name_code(symbol="科创板"),
               lambda: ak.stock_info_sz_name_code(symbol="A股列表")):
        try:
            df = fn()
        except Exception as e:
            print("  [交易所名单] 一段失败,继续: %s" % repr(e)[:80], flush=True)
            continue
        code_col = next(c for c in df.columns if "代码" in str(c))
        name_col = next(c for c in df.columns if "简称" in str(c) or "名称" in str(c))
        out.extend((str(r[code_col]).zfill(6), str(r[name_col]).strip())
                   for _, r in df.iterrows())
    return out


def load_universe(force=False, include_bj=True, quiet=False):
    """返回 [(code, name)] 全市场 A 股名单,按代码升序。

    force=True 忽略缓存重新联网拉取;结果落 data/universe.json。
    """
    if not force and UNIVERSE_JSON.exists():
        data = json.loads(UNIVERSE_JSON.read_text(encoding="utf-8"))
        rows = [(c["code"], c["name"]) for c in data.get("companies") or []]
        if not include_bj:
            rows = [r for r in rows if not r[0].startswith(("4", "8", "92"))]
        return rows

    pairs, src = [], "sina"
    try:
        pairs = _from_sina()
    except Exception as e:
        if not quiet:
            print("  [universe] 新浪名单失败: %s" % repr(e)[:90], flush=True)
    if len(pairs) < 3000:
        try:
            merged = {c: n for c, n in pairs}
            for c, n in _from_exchanges():
                merged.setdefault(c, n)
            pairs = sorted(merged.items())
            src = "sina+exchanges" if merged else "exchanges"
        except Exception as e:
            if not quiet:
                print("  [universe] 交易所名单也失败: %s" % repr(e)[:90], flush=True)
    if not pairs:
        raise RuntimeError("全市场名单拉取失败(新浪/交易所三源均不可用)")

    seen, rows = set(), []
    for c, n in sorted(set(pairs)):
        if c in seen or not n:
            continue
        if not include_bj and c.startswith(("4", "8", "92")):
            continue
        seen.add(c)
        rows.append((c, n))

    UNIVERSE_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(UNIVERSE_JSON) + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump({"updated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
                   "source": src, "count": len(rows),
                   "companies": [{"code": c, "name": n} for c, n in rows]},
                  f, ensure_ascii=False, indent=1)
    os.replace(tmp, str(UNIVERSE_JSON))
    if not quiet:
        bj = sum(1 for c, _ in rows if c.startswith(("4", "8", "92")))
        print("  [universe] %s 共 %d 只(北交所 %d)" % (src, len(rows), bj), flush=True)
    return rows


def _em_clist(fs, fields, quiet=False, max_pages=40, hosts=EM_CLIST_HOSTS):
    """东财列表接口分页拉全，返回行 dict 列表（键即 f 字段名）。

    max_pages 默认 40 够用（港股通 621 只 / 7 页），但全美股表一万三千多行要 140 页——
    单页实际上限是 100 行（pz 给再大也只回 100，实测），所以美股调用必须抬高这个上限。
    拉不完直接抛错而不能返回部分：结果按代码倒序排，断在中途等于整段字母区间的股票全没有，
    调用方只看到“部分代码查无此股”，很难归因到分页（实测踩过一次）。
    """
    rows, page, total = [], 1, None
    while True:
        params = {"pn": str(page), "pz": "200", "po": "1", "np": "1",
                  "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2",
                  "fid": "f12", "fs": fs, "fields": fields}
        data = {}
        for attempt in range(4):
            host = hosts[(page + attempt) % len(hosts)]
            try:
                r = requests.get(host + "/api/qt/clist/get", params=params,
                                 headers=EM_HEADERS, timeout=15)
                r.encoding = "utf-8"           # 不指定时 requests 会猜成 GBK，中文行业变乱码
                data = (r.json() or {}).get("data") or {}
                if data.get("diff"):
                    break
            except Exception as e:
                if not quiet:
                    print("  [东财名单] 页 %d 第 %d 次失败: %s" % (page, attempt + 1, repr(e)[:70]),
                          flush=True)
                time.sleep(2 * (attempt + 1))
        batch = data.get("diff") or []
        if isinstance(batch, dict):            # 单条时东财返回对象而非数组
            batch = [batch]
        if not batch:
            break
        rows.extend(batch)
        total = int(data.get("total") or 0)
        if not total or len(rows) >= total or page > max_pages:
            break
        page += 1
        time.sleep(0.4)
    if total and len(rows) < total:
        raise RuntimeError(f"东财名单分页中断（{fs}）：{len(rows)}/{total} 行，"
                           f"页上限 {max_pages}，请抬大或缩小查询范围")
    return rows


def load_universe_hk(force=False, quiet=False):
    """港股通标的名单 [(code, name, industry)]，代码统一 5 位。

    行业随名单一次拿全（f100），所以逐只抓取阶段不需要额外请求。
    5 位与 A 股 6 位天然不重名，companies/<code>.json 不会互相覆盖；
    正因为这点，任何把港股代码 zfill(6) 的写法都是数据事故（00700→000700 覆盖模塑科技）。
    结果落 data/universe_hk.json；联网失败但有缓存时回退缓存（宁用旧名单也不报错中断）。
    """
    if not force and HK_UNIVERSE_JSON.exists():
        data = json.loads(HK_UNIVERSE_JSON.read_text(encoding="utf-8"))
        return [(c["code"], c["name"], c.get("industry"))
                for c in data.get("companies") or []]
    try:
        rows = _em_clist(GGT_BOARD, GGT_FIELDS, quiet=quiet)
    except Exception as e:
        rows = []
        if not quiet:
            print("  [港股通] 拉取异常: %s" % repr(e)[:90], flush=True)
    if len(rows) < 300:       # 名单腰斩多是被限流截断，宁用旧缓存也不写坏文件
        if HK_UNIVERSE_JSON.exists():
            if not quiet:
                print("  [港股通] 本次只拿到 %d 行，回退旧缓存" % len(rows), flush=True)
            return load_universe_hk(force=False, quiet=quiet)
        raise RuntimeError(f"港股通名单只拿到 {len(rows)} 行（历史约 620）且无旧缓存可用")

    seen, out = set(), []
    for r in rows:
        code = str(r.get("f12") or "").strip().zfill(5)
        name = str(r.get("f14") or "").strip()
        if not code.strip("0") or not name or code in seen:
            continue
        seen.add(code)
        out.append((code, name, str(r.get("f100") or "").strip() or None))
    out.sort()

    HK_UNIVERSE_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(HK_UNIVERSE_JSON) + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump({"updated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
                   "source": "eastmoney_ggt", "count": len(out),
                   "companies": [{"code": c, "name": n, "industry": ind} for c, n, ind in out]},
                  f, ensure_ascii=False, indent=1)
    os.replace(tmp, str(HK_UNIVERSE_JSON))
    if not quiet:
        no_ind = sum(1 for _, _, ind in out if not ind)
        print("  [港股通] 共 %d 只（无行业 %d）" % (len(out), no_ind), flush=True)
    return out


# 道指30 公司名 → 代码：S&P DJI 不开放机读成分表，而成分名写在维基导航模板里。
# 只映射名字不写数，下次调仓出现未收录公司名会直接报错而不是静默漏股。
DJIA_NAME_TICKER = {
    "3M": "MMM", "Alphabet": "GOOGL", "Amazon": "AMZN", "American Express": "AXP",
    "Amgen": "AMGN", "Apple": "AAPL", "Boeing": "BA", "Caterpillar": "CAT",
    "Chevron": "CVX", "Cisco": "CSCO", "Coca-Cola": "KO", "Disney": "DIS",
    "Goldman Sachs": "GS", "Home Depot": "HD", "Honeywell Technologies": "HON",
    "IBM": "IBM", "Johnson & Johnson": "JNJ", "JPMorgan Chase": "JPM",
    "McDonald's": "MCD", "Merck": "MRK", "Microsoft": "MSFT", "Nike": "NKE",
    "Nvidia": "NVDA", "Procter & Gamble": "PG", "Salesforce": "CRM",
    "Sherwin-Williams": "SHW", "Travelers": "TRV", "UnitedHealth": "UNH",
    "Visa": "V", "Walmart": "WMT",
}


def _overseas(method, url, quiet=False, **kw):
    """海外源取数：先直连，失败再走本机代理（并把该主机记为“只认代理”）。

    GitHub 与 api.nasdaq.com 实测可直连；en.wikipedia.org 与 indexes.nasdaqomx.com
    直连必定 ConnectTimeout。本模块开头设了 no_proxy='*'（国内源会被代理截断，不能改），
    但显式传 proxies 参数仍会生效——这一点很反直觉，改造成依赖环境变量就会静默失效。
    主机缓存是必需的：nasdaq_weight() 会连发十几个日期候选，每次都先白等一轮直连
    会把“拉名单”拖成分钟级。
    """
    kw.setdefault("timeout", (6, 25))       # 连不上就早退，别等满 25 秒
    kw.setdefault("headers", OVERSEAS_HEADERS)
    host = urlsplit(url).netloc
    if host not in _OVERSEAS_PROXY_ONLY:
        try:
            return requests.request(method, url, **kw)
        except Exception as e:
            if not quiet:
                print("  [海外源] %s 直连失败(%s)，改走代理 %s" % (
                    host, repr(e)[:44], OVERSEAS_PROXY), flush=True)
            _OVERSEAS_PROXY_ONLY.add(host)
    return requests.request(method, url, proxies={"http": OVERSEAS_PROXY,
                                                  "https": OVERSEAS_PROXY}, **kw)


def sp500_rows(quiet=False):
    """标普500 成分 [(代码, 名称)]，约 503 只（CSV 还带 GICS 行业，我们用东财行业就够用）。"""
    import csv
    r = _overseas("GET", SP500_CSV_URL, quiet=quiet)
    r.raise_for_status()
    out = []
    for row in csv.DictReader(io.StringIO(r.text)):
        sym = str(row.get("Symbol") or "").strip().upper()
        if sym:
            out.append((sym, str(row.get("Security") or "").strip()))
    if len(out) < 450:        # 上游改列名/只推了部分文件时不能当成正常名单用
        raise RuntimeError(f"标普500 只解析到 {len(out)} 行（应约 503）")
    return out


def nasdaq_weight(index_id, quiet=False, max_back=12):
    """Nasdaq 官方指数成分 [(代码, 名称)] + 权重日。

    WeightingData 是日频快照（实测 NBI 2026-09-02=248 / 08-31=249 / 06-30=251，
    能看到调仓历史），所以从当天往前逐日试、跳过周末即可；DJIA/SPX 不在此平台（回空）。
    """
    today = datetime.now(CN_TZ).date()
    last_err = None
    for back in range(max_back):
        day = today - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        date_str = day.strftime("%Y-%m-%d")
        try:
            r = _overseas("POST", NASDAQ_WEIGHT_URL, quiet=quiet,
                          headers={**OVERSEAS_HEADERS,
                                   "Referer": "https://indexes.nasdaqomx.com/Index/Weighting/" + index_id,
                                   "X-Requested-With": "XMLHttpRequest",
                                   "Accept": "application/json, text/javascript, */*; q=0.01"},
                          data={"id": index_id, "tradeDate": date_str, "timeOfDay": "Close"})
            rows = (r.json() or {}).get("aaData") or []
        except Exception as e:
            last_err = repr(e)[:60]
            continue
        out = [(str(x.get("Symbol") or "").strip().upper(), str(x.get("Name") or "").strip())
               for x in rows if str(x.get("Symbol") or "").strip()]
        if len(out) >= 5:
            return out, date_str
        last_err = f"{date_str} 返回 {len(out)} 行"
    raise RuntimeError(f"Nasdaq 成分 {index_id} 拉不到（{last_err}）")


def djia_rows(quiet=False):
    """道指30 [(代码, 公司名)]：拉维基导航模板的公司名再查表转代码。"""
    r = _overseas("GET", WIKI_RAW_URL.format(quote(DJIA_TEMPLATE, safe="")), quiet=quiet)
    r.raise_for_status()
    names = re.findall(r"^\*\s+\[\[(?:[^\]|]+\|)?([^\]]+)\]\]", r.text, re.M)
    names = [n.strip() for n in names if n.strip()]
    if len(names) < 25:
        raise RuntimeError(f"道指模板只解析到 {len(names)} 家公司（应为 30）")
    unknown = [n for n in names if n not in DJIA_NAME_TICKER]
    if unknown:
        raise RuntimeError("道指名单出现未收录公司名，请补 DJIA_NAME_TICKER: %s" % unknown)
    return [(DJIA_NAME_TICKER[n], n) for n in names]


def em_us_table(quiet=False):
    """东财全美股表 {代码: {name, industry, market_cap}}：行业与名单存在性校验都靠它。

    分三段市别拉而不用 m:105,m:106,m:107 合查：合查接不上其他过滤条件，且分市别拉能
    让单段总量变小、分页失败时只丢一段（现在有中断报错保护，不会静默丢）。
    """
    out = {}
    for fs in US_EM_FS:
        rows = _em_clist(fs, US_EM_FIELDS, quiet=quiet, max_pages=US_EM_PAGES,
                         hosts=US_EM_HOSTS)
        for r in rows:
            code = str(r.get("f12") or "").strip().upper()
            if not code:
                continue
            cap = r.get("f20")
            out[code] = {"name": str(r.get("f14") or "").strip(),
                         "industry": str(r.get("f100") or "").strip() or None,
                         "market_cap": cap if isinstance(cap, (int, float)) else None}
    if len(out) < 9000:      # 与港股同样的腰斩保护：宁可读旧缓存也不写坏文件
        raise RuntimeError(f"东财全美股表只拿到 {len(out)} 只（应≈13800）")
    return out


def _em_lookup(em, code):
    """按东财代码形态查表：带级股票两边写法不同，先试变体。

    实测东财把伯克希尔 B 写成 BRK_B，而标普名单/腾讯行情写 BRK.B（互不认账）；
    统一按东财形态落盘（东财财务接口是五个数据项里的四个），只留腾讯快照一处做变体重试。
    返回 (实际使用的代码, 行)；都查不到返回 (原代码, None)，由调用方记入 unverified。
    """
    for cand in (code, code.replace(".", "-"), code.replace("-", "."),
                 code.replace(".", "_"), code.replace("-", "_")):
        if cand in em:
            return cand, em[cand]
    return code, None


def load_universe_us(force=False, quiet=False):
    """美股五大指数成分并集 [(code, name, industry, indexes)]。

    indexes 是该股所属的指数键列表（SP500/NDX/DJIA/SOX/NBI），一只股可以同时属多个。
    任何一路成分源失败就整体抛错（宁可不更新也不落残缺名单）；有缓存时上面就返回了。
    只拿到部分名单会直接反映到第二天的抓取目标上（少一只就是静默少一只）。
    代码统一用东财形态（带级股 “.” → “_”，实测落盘为 BRK_B），
    下游东财财务接口直接认它，只有腾讯行情一处要反向试写法变体。
    """
    if not force and US_UNIVERSE_JSON.exists():
        data = json.loads(US_UNIVERSE_JSON.read_text(encoding="utf-8"))
        return [(c["code"], c["name"], c.get("industry"), list(c.get("indexes") or []))
                for c in data.get("companies") or []]

    members, as_of, broken = {}, {}, []
    today = datetime.now(CN_TZ).strftime("%Y-%m-%d")

    def add(label, rows, date=None):
        as_of[label] = date or today
        for code, name in rows:
            rec = members.setdefault(code, {"name": name, "indexes": []})
            if not rec["name"]:
                rec["name"] = name
            if label not in rec["indexes"]:
                rec["indexes"].append(label)

    for label, fetch in (("SP500", lambda: sp500_rows(quiet)),
                         ("DJIA", lambda: djia_rows(quiet))):
        try:
            add(label, fetch())
        except Exception as e:
            broken.append(f"{label}: {repr(e)[:80]}")
    for key in NASDAQ_INDEXES:
        try:
            rows, date = nasdaq_weight(key, quiet=quiet)
            add(key, rows, date)
        except Exception as e:
            broken.append(f"{key}: {repr(e)[:80]}")

    try:
        em = em_us_table(quiet=quiet)
    except Exception as e:
        broken.append(f"东财全美股表: {repr(e)[:80]}")
        em = {}

    if broken:
        # 开头已经“有缓存就直读缓存”，走到这里要么是没缓存、要么是 --dump-us 强制重拉，
        # 两种情况都不能拿一份缺胳膊少腿的名单去覆盖/冒充成品名单
        raise RuntimeError("美股指数成分拉取失败: " + "; ".join(broken))

    # 存东财代码形态并补行业；查不到的保留原代码并记进 unverified（抓不到会在日志里暴露）
    resolved, unverified = {}, []
    for code, rec in members.items():
        real, row = _em_lookup(em, code) if em else (code, None)
        if row is None:
            unverified.append(code)
        item = resolved.setdefault(real, {"name": rec["name"], "indexes": [],
                                          "industry": (row or {}).get("industry")})
        item["indexes"] = sorted(set(item["indexes"]) | set(rec["indexes"]))
        if not item["name"] and row:
            item["name"] = row["name"]

    out = [(c, v["name"] or c, v["industry"], v["indexes"]) for c, v in sorted(resolved.items())]
    US_UNIVERSE_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(US_UNIVERSE_JSON) + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump({"updated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
                   "source": "sp500_datasets+nasdaq_weighting+djia_wikipedia+eastmoney_us_table",
                   "as_of": as_of,
                   "counts": {k: sum(1 for _, _, _, ix in out if k in ix)
                              for k in ("SP500", "NDX", "DJIA", "SOX", "NBI")},
                   "unverified": sorted(unverified),
                   "count": len(out),
                   "companies": [{"code": c, "name": n, "industry": ind, "indexes": ix}
                                 for c, n, ind, ix in out]},
                  f, ensure_ascii=False, indent=1)
    os.replace(tmp, str(US_UNIVERSE_JSON))
    if not quiet:
        no_ind = sum(1 for _, _, ind, _ in out if not ind)
        print("  [美股成分] 并集 %d 只（缺行业 %d，东财查无 %d）%s" % (
            len(out), no_ind, len(unverified), unverified[:6]), flush=True)
        print("  [美股成分] 权重日: %s" % as_of, flush=True)
    return out


def tencent_spot(codes, quiet=False):
    """批量行情:{code: {price, change_pct, pe_ttm, pb, market_cap,
    float_market_cap, turnover_rate, time}};字段与 fetch_data.fetch_snapshot 对齐。"""
    import fetch_data as fd
    out = {}
    codes = [str(c).zfill(6) for c in codes]
    batches = [codes[i:i + TENCENT_BATCH] for i in range(0, len(codes), TENCENT_BATCH)]
    for bi, batch in enumerate(batches, 1):
        q = ",".join(exchange_symbol(c) for c in batch)
        items = []
        for attempt in range(3):
            try:
                r = requests.get(TENCENT_URL + q, timeout=15)
                r.encoding = "gbk"
                items = [x for x in r.text.strip().split(";") if "~" in x]
                if len(items) >= max(1, len(batch) // 2):
                    break
            except Exception:
                time.sleep(1.5 * (attempt + 1))
        for item in items:
            parts = item.split("~")
            if len(parts) < 50:
                continue

            def num(i):
                try:
                    return float(parts[i])
                except (TypeError, ValueError, IndexError):
                    return None

            code = str(parts[2]).zfill(6)
            out[code] = {
                "name": parts[1].strip() or None,
                "price": num(3),
                "change_pct": None if num(32) is None else round(num(32) / 100.0, 6),
                "pe_ttm": num(39),
                "pb": num(46),
                "market_cap": None if num(45) is None else round(num(45) * 1e8, 2),
                "float_market_cap": None if num(44) is None else round(num(44) * 1e8, 2),
                "turnover_rate": None if num(38) is None else round(num(38) / 100.0, 6),
                "time": fd.format_quote_time(parts[30]),
            }
        if not quiet and bi % 20 == 0:
            print("  [腾讯行情] %d/%d 批" % (bi, len(batches)), flush=True)
        time.sleep(0.25)
    return out


def main():
    ap = argparse.ArgumentParser(description="全市场标的池")
    ap.add_argument("--force", action="store_true", help="忽略缓存重拉名单")
    ap.add_argument("--no-bj", action="store_true", help="不含北交所")
    ap.add_argument("--dump", action="store_true", help="重拉并覆盖 universe.json")
    ap.add_argument("--dump-hk", action="store_true", help="重拉并覆盖 universe_hk.json(港股通)")
    ap.add_argument("--check-hk", action="store_true", help="只看港股通名单统计")
    ap.add_argument("--dump-us", action="store_true",
                    help="重拉并覆盖 universe_us.json(美股五大指数成分)")
    ap.add_argument("--check-us", action="store_true", help="只看美股成分名单统计")
    args = ap.parse_args()

    if args.dump_us or args.check_us:
        us = load_universe_us(force=args.dump_us)
        data = json.loads(US_UNIVERSE_JSON.read_text(encoding="utf-8"))
        print("美股成分并集 %d 只" % len(us))
        print("  各指数: %s" % data.get("counts"))
        print("  权重日: %s" % data.get("as_of"))
        no_ind = sum(1 for _, _, ind, _ in us if not ind)
        print("  缺行业 %d，东财查无 %s" % (no_ind, data.get("unverified")))
        for c, n, ind, ix in us[:6]:
            print("  %s %-24s %-8s %s" % (c, n[:24], ind, "+".join(ix)))
        print("  ...")
        for c, n, ind, ix in us[-3:]:
            print("  %s %-24s %-8s %s" % (c, n[:24], ind, "+".join(ix)))
        return

    if args.dump_hk or args.check_hk:
        hk = load_universe_hk(force=args.dump_hk)
        no_ind = sum(1 for _, _, ind in hk if not ind)
        print("港股通名单 %d 只（缺行业 %d）" % (len(hk), no_ind))
        for c, n, ind in hk[:5]:
            print("  %s %-12s %s" % (c, n, ind))
        print("  ...")
        for c, n, ind in hk[-3:]:
            print("  %s %-12s %s" % (c, n, ind))
        return

    rows = load_universe(force=args.force or args.dump,
                         include_bj=not args.no_bj)
    print("名单 %d 只" % len(rows))
    spot = tencent_spot([c for c, _ in rows[:120]], quiet=True)
    print("抽样 120 只 → 行情返回 %d 只" % len(spot))
    for code in list(spot)[:5]:
        s = spot[code]
        print("  %s %-8s price=%-8s pe=%-8s pb=%-6s mcap=%.2f亿" % (
            code, s["name"], s["price"], s["pe_ttm"], s["pb"],
            (s["market_cap"] or 0) / 1e8))
    miss = [c for c, _ in rows[:120] if c not in spot]
    print("未返回行情(前 120 中) %d 只 %s" % (len(miss), miss[:8]))


if __name__ == "__main__":
    main()
