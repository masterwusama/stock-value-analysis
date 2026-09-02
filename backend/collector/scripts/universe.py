# -*- coding: utf-8 -*-
"""全市场标的池(A 股沪深京)与批量行情快照。

名单源优先级:
    1) 新浪 ak.stock_zh_a_spot()      一次返回 5500+ 只,含北交所 920/4/8 段(约 17s)
    2) 交易所官网 sh 主板/科创板 + sz A股列表   兜底(北交所官网与东财批量接口易被远端断连)
产出的名单缓存到 data/universe.json,供 fetch_data --all-market 与日更快照复用。

港股通标的单独一条源: load_universe_hk() 走东财 clist(板块码 b:DLMK0146,b:DLMK0144),
一次拉全 ≈621 只并顺带取 f100 行业,缓存到 data/universe_hk.json,供 fetch_data --hk-connect 用。

估值快照走腾讯批量行情(每请求 60 只,约 95 个请求覆盖全市场),单次调用即可刷新
全市场现价/PE/PB/总市值,是"每天全量刷估值、财报按报告期增量"的基础。

用法:
    python universe.py --check              打印名单统计与批量快照抽样
    python universe.py --dump               重新拉取并写 data/universe.json
    python universe.py --dump-hk            重新拉取并写 data/universe_hk.json(港股通)
"""
import argparse
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("no_proxy", "*")     # 本机代理会截断国内数据源连接
os.environ.setdefault("NO_PROXY", "*")

import requests  # noqa: E402

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
sys.path.insert(0, str(HERE))

CN_TZ = timezone(timedelta(hours=8))
UNIVERSE_JSON = BASE / "data" / "universe.json"
HK_UNIVERSE_JSON = BASE / "data" / "universe_hk.json"
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


def _em_clist(fs, fields, quiet=False):
    """东财列表接口分页拉全，返回行 dict 列表（键即 f 字段名）。"""
    rows, page = [], 1
    while True:
        params = {"pn": str(page), "pz": "200", "po": "1", "np": "1",
                  "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2",
                  "fid": "f12", "fs": fs, "fields": fields}
        data = {}
        for attempt in range(4):
            host = EM_CLIST_HOSTS[(page + attempt) % len(EM_CLIST_HOSTS)]
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
        if not total or len(rows) >= total or page > 40:
            break
        page += 1
        time.sleep(0.4)
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
    args = ap.parse_args()

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
