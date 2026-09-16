# -*- coding: utf-8 -*-
"""股本事件采集器：定向增发 + 回购（东财数据中心，全市场两张表）。

产物（与 valuation/edb 同一套采集工作目录）：
  data/actions/latest.json   全量明细快照 {fetched_at, sources, seo:[...], buyback:[...]}

这里只做「取全 + 归一列名 + 标注销三态」，不挑「最近一次」——一次抓取就把两张表整个
存下来，取哪一笔留给查询侧（import_legacy → share_action → API 的 ROW_NUMBER）。
把筛选留在源侧的话，日后改口径得重抓一次网络。

数据源（实测 2026-09-16，两个都是免登录 JSON 接口，无需 Wind 积分）：
  定增  reportName=RPT_SEO_DETAIL        5,884 行 / 3,029 家
  回购  reportName=RPTA_WEB_GETHGLIST_NEW  5,513 行 / 2,936 家
分页 pageSize=500、sortColumns 用各自的日期列倒序，12 页拿完，整轮 ~25 秒。

口径（都是逐列量出来的，不是按名称猜的）：
  - 定增只认「非公开募集」：RPT_SEO_DETAIL 全表 5,884 行 / 3,029 家，SEO_TYPE 只有 1
    （5,510 行 / 2,684 家）和 2（374 行，「战略配售,网上定价发行」那类公开形态）两种；
    SEO_TYPE=1 下 ISSUE_WAY 实测只有三种——网下询价配售 2,840、网下定价发行 2,631、
    吸收合并 39。吸收合并是换股并购不是募股，只涉 6 家，剔掉，得 5,471 行 / 2,678 家。
    另两条候选规则本轮量出来是「只要 SEO_TYPE=1」5,510 行 / 2,684 家、「再要求 ISSUE_WAY
    含网下」5,471 行 / 2,678 家，与取用的这条并排写在 self-report 里；将来东财加了新发行
    方式，这三个数会对不齐，那时再改口径。
  - 定增无稳定自然键：(代码, 发行日, 发行量) 有 4 组重复，加上发行价后 5,881/5,884 —— 剩
    3 组是全字段一致的源侧重复行。键就取「代码|发行日|发行量|发行价」，重复行在 upsert 时
    自然塌成一条。20 行 ISSUE_DATE 为空，键里的日期退到 上市日 → 新增股份上市日 →「nodate」。
  - 回购进度码：006=实施完成（4,926 行全部有 FINISHDATE，已回购/拟回购中位 0.98）、
    004=进行中（370 行都有已回购数量、无 FINISHDATE，中位 0.59）、005=终止（124 行，
    REMARK 里 24 行明写「终止」，仅 75 行买过）、001/002/003/007/008=方案阶段（无已回购数）。
  - 已回购 vs 拟回购是两族列，不能混：REPURNUM/REPURAMOUNT/HGJG_VAG（成交均价）三列在
    方案阶段的 93 行里**全空**，只有真买过才落值；ZJSL/ZJJE/ZJJG 与它们 100% 等价，但
    方案阶段会退化成计划值（89/89 行 ZJJE==JHJE_VAG），所以已回购一律读 REPUR* 那一族。
    拟回购数量/价格用 REPURNUMCAP / REPURPRICECAP（与 JHJG_VAG 99.6% 一致）。
  - 「是否注销」没有结构化列：68 列里唯一的开关 SFFHSP 与用途文本交叉后 1/0 都同时落在
    注销与不注销两类里（不是注销标记），REMARK2 整列全空。只能读 REPUROBJECTIVE 全文，分三态：
      注销   —— 处置句里只有注销/减少注册资本
      非注销 —— 处置句里只有别的去向（股权激励 / 员工持股 / 转债 / 出售转让 / 「是为维护公司价值」）
      待核   —— 两种并列（「用于股权激励或减少注册资本」）、或通篇只讲动机不讲处置、或没有用途文本
    判据是**逐句**的：含「未能/届时/三年内/使用完毕/未完成出售…」的兜底整句丢掉（那 1,160 行
    是「提了注销但只在兜底从句里」，按全文匹配会把它们误判成注销）；同句里注销与去向并列 → 待核。
    两个坑都实测过：把 股东权益/市值管理 这类**动机词**当去向词，中国东航「回购股票将全部予以
    注销」和中国中冶会一起掉进待核；更早那版按位置切主句，则把东航判成待核、把三鑫医疗
    「用于转换…可转换为股票的公司债券」这种写全了的可转债漏成待核。
    v7 实测分布：全表 5,513 行 → 注销 628 家 / 非注销 2,052 家 / 待核 1,159 家；
    按公司只取最近一笔 → 421 / 1,758 / 757。用途原文（截 600 字）一并落库，详情页展示，
    判错了人能直接看出来。

守卫：行数比上一轮掉到 70% 以下就拒绝覆盖（半张表会让「最近一次」悄悄回退成旧一笔），
  同时要求实收行数 == 接口回报的 count，缺一页就报错，不把截断的快照写进库。

用法：
  python fetch_actions.py                # 全量抓两张表，落 data/actions/latest.json
  python fetch_actions.py --probe 2      # 只取前 2 页，跑一遍口径自检但不落盘
  python fetch_actions.py --samples 10   # 自检时多打 N 家注销三态的原文供人工核
"""
import argparse
import datetime as dt
import io
import json
import os
import re
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
STOCK_ROOT = os.path.abspath(os.path.join(HERE, ".."))
DATA_DIR = os.path.join(STOCK_ROOT, "data")
OUT_DIR = os.path.join(DATA_DIR, "actions")
LATEST_PATH = os.path.join(OUT_DIR, "latest.json")

URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HDR = {"Referer": "https://data.eastmoney.com/", "User-Agent": "Mozilla/5.0"}
PAGE_SIZE = 500
TIMEOUT = 40
RETRY = 3
MIN_RATIO = 0.7          # 比上轮行数掉这么多就拒绝覆盖

SEO_REPORT = "RPT_SEO_DETAIL"
BUYBACK_REPORT = "RPTA_WEB_GETHGLIST_NEW"

# 回购进度码 → 中文标签（语义核对见模块注释）
# 003/007/008 三码合计 21 行，只核到「都没有已回购实数、REMARK 是方案阶段措辞」这一步，
# 细分语义没敢瞎猜：003 不写成东财页面上的「双方实施」（那要看它的原始定义，我们只有数据）。
PROGRESS = {
    "001": "董事会预案", "002": "股东大会通过", "003": "方案阶段",
    "004": "实施中", "005": "已终止", "006": "实施完成",
    "007": "方案终止", "008": "未实施",
}
# 兜底句引导词：一句里出现这些词，讲的是「万一没派上用场怎么办」，整句不作为注销证据
CATCH = re.compile(u"未能|届时|逾期|三年内|未使用|剩余|若未|期满|到期|使用完毕|转让完毕|"
                   u"变更为|调整用途|未实施完毕|未完成出售")
# 处置去向的两类词：注销（含减资）vs 不注销（激励/转债/卖出）
# 「减少注册资本」中间会插字：红塔证券原文是「用于减少公司注册资本」，写死会漏判（实测）。
CANCEL = re.compile(u"注销|减少.{0,6}注册资本|减资")
# 去向词只认「说明这批股份拿去干什么」的写法。股东权益 / 市值管理 / 维护投资者利益 这类是
# **动机**，几乎每份回购公告的前缀都有，算进证据会把东航「回购股票将全部予以注销」这种
# 真注销判成「注销+他用途并列 → 待核」（v6 实测挂在中国东航、中国中冶两家上）。
DEST = re.compile(u"股权激励|员工持股|期权|限制性股票|持股计划|可转换|可转债|债转股|转股|出售|转让"
                  u"|是为维护公司价值|用于维护公司价值|旨在维护公司价值")
SENT = re.compile(u"[。；;]")
PURPOSE_LABEL = [
    (u"股权激励", re.compile(u"股权激励|期权|限制性股票")),
    (u"员工持股计划", re.compile(u"员工持股")),
    (u"可转债", re.compile(u"可转债|可转换公司债")),
    (u"维护价值及股东权益", re.compile(u"维护公司价值|股东权益")),
    (u"市值管理", re.compile(u"市值管理")),
]
CODE6 = re.compile(r"^\d{6}$")


def fetch_page(report, sort_col, page):
    """取一页，返回 (rows, total_count, pages)。网络抖动用 RETRY 兜，重试仍失败才抛。"""
    params = {"reportName": report, "columns": "ALL", "sortColumns": sort_col,
              "sortTypes": "-1", "pageSize": str(PAGE_SIZE), "pageNumber": str(page),
              "source": "WEB", "client": "WEB"}
    last = None
    for attempt in range(RETRY):
        try:
            r = requests.get(URL, params=params, headers=HDR, timeout=TIMEOUT)
            j = r.json()
            res = j.get("result") or {}
            rows = res.get("data") or []
            if rows or page > 1:
                return rows, int(res.get("count") or 0), int(res.get("pages") or 0)
            last = RuntimeError(u"首页空返回: %s" % str(j)[:160])
        except Exception as e:  # noqa: BLE001  接口偶发 5xx / 非 JSON，一律重试
            last = e
        time.sleep(1.0 + attempt)
    raise last


def fetch_table(report, sort_col, max_pages=None):
    """按页铺完整张表，返回 (rows, count, pages_walked)。count 对不上就抛，别交半张表。"""
    rows, page, count, pages = [], 1, 0, 0
    while True:
        got, count, npages = fetch_page(report, sort_col, page)
        rows.extend(got)
        pages = npages
        if max_pages and page >= max_pages:
            return rows, len(rows), page
        if page >= (npages or 1) or not got:
            break
        page += 1
    if len(rows) != count:
        raise RuntimeError(u"%s 分页实收 %d 行 ≠ 接口回报 %d 行，疑似漏页" % (report, len(rows), count))
    return rows, count, pages


def d(v):
    """'2026-09-10 00:00:00' → '2026-09-10'；空 → None。"""
    s = str(v or "").strip()
    return s[:10] if len(s) >= 10 and s[:4].isdigit() else None


def num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def first_date(row, keys):
    for k in keys:
        v = d(row.get(k))
        if v:
            return v
    return None


def one_line(v, limit=None):
    s = re.sub(u"\\s+", u" ", unicode_safe(v)).strip()
    return s[:limit] if limit else s


def unicode_safe(v):
    if v is None:
        return u""
    return v if isinstance(v, str) else str(v)


# ---------- 定增 ----------

def norm_seo(rows):
    """RPT_SEO_DETAIL → 我们的行。只保留定向增发（SEO_TYPE=1 且非吸收合并）。"""
    out, skipped = [], {"type2": 0, "merge": 0, "badcode": 0}
    for r in rows:
        code = unicode_safe(r.get("SECURITY_CODE"))
        if not CODE6.match(code):
            skipped["badcode"] += 1
            continue
        way = one_line(r.get("ISSUE_WAY"))
        if str(r.get("SEO_TYPE")) != "1":
            skipped["type2"] += 1
            continue
        if u"吸收合并" in way:
            skipped["merge"] += 1
            continue
        issue = d(r.get("ISSUE_DATE"))
        listing = d(r.get("ISSUE_LISTING_DATE")) or d(r.get("NEW_ISSUE_LIST_DATE"))
        price, qty = num(r.get("ISSUE_PRICE")), num(r.get("ISSUE_NUM"))
        key_date = issue or listing or "nodate"
        out.append({
            "kind": "seo",
            "code": code,
            "name": one_line(r.get("SECURITY_NAME_ABBR") or r.get("SECURITY_SHORT_NAME")),
            "src_id": u"%s|%s|%s|%s" % (code, key_date, qty if qty is not None else "",
                                        price if price is not None else ""),
            "issue_date": issue,
            "listing_date": listing,
            "plan_notice_date": d(r.get("PLAN_NOTICE_DATE")) or d(r.get("PARD_NOTICE_DATE")),
            "price": price,
            "num": qty,
            "raise_funds": num(r.get("TOTAL_RAISE_FUNDS")),
            "apply_price": num(r.get("APPLY_PRICE")),
            "price_before": num(r.get("PRE_CLOSE_PRICE")),
            "raise_ratio": num(r.get("NEW_LIMITED_RATIO")),
            "share_before": num(r.get("ISSUE_SHARE_BEFORE")),
            "share_after": num(r.get("ISSUE_SHARE_AFTER")),
            "way": way,
            "lockin": one_line(r.get("LOCKIN_PERIOD")),
            "target": one_line(r.get("ISSUE_OBJECT"), 300),
            "market": one_line(r.get("TRADE_MARKET")),
            "updated_at": first_date(r, ("UPDATED_DATE", "ISSUE_LISTING_DATE")),
        })
    return out, skipped


# ---------- 回购 ----------

def cancel_status(text):
    """三态注销判定，返回 (status, 首条处置句)。判据出处见模块注释「是否注销没有结构化列」。

    逐句判而不是「切一刀看主句」：兜底安排可能写在任意位置，按位置切会把
    「为维护公司价值…（第 200 字）本次回购股份用于注销」这种正文一起切掉。
    """
    t = re.sub(u"\\s+", u" ", unicode_safe(text)).strip()
    if not t:
        return u"待核", t
    n_cancel = n_dest = 0
    first = u""
    for s in SENT.split(t):
        s = s.strip(u",， ")
        if not s or CATCH.search(s):
            continue                       # 兜底句：讲的是没用完怎么办，不是这批股份的处置
        hc, hd = bool(CANCEL.search(s)), bool(DEST.search(s))
        if hc or hd:
            n_cancel += int(hc)
            n_dest += int(hd)              # 同句并列（「用于股权激励或减少注册资本」）两边都记 → 待核
            first = first or s
    if n_cancel and n_dest:
        return u"待核", first
    if n_cancel:
        return u"注销", first
    if n_dest:
        return u"非注销", first
    return u"待核", first


def purpose_label(text):
    for label, pat in PURPOSE_LABEL:
        if pat.search(unicode_safe(text)):
            return label
    return None


def norm_buyback(rows):
    """RPTA_WEB_GETHGLIST_NEW → 我们的行，全进度码都留（取哪一笔交给查询侧）。"""
    out, skipped = [], {"badcode": 0}
    for r in rows:
        code = unicode_safe(r.get("DIM_SCODE"))
        if not CODE6.match(code):
            skipped["badcode"] += 1
            continue
        prog = unicode_safe(r.get("REPURPROGRESS"))
        obj = r.get("REPUROBJECTIVE")
        status, head = cancel_status(obj)
        done_num, done_amt = num(r.get("REPURNUM")), num(r.get("REPURAMOUNT"))
        avg = num(r.get("HGJG_VAG"))
        src = unicode_safe(r.get("REPURCODE")) or first_date(r, ("DIM_DATE", "UPDATEDATE")) or ""
        out.append({
            "kind": "buyback",
            "code": code,
            "name": one_line(r.get("SECURITYSHORTNAME") or r.get("SECURITY_NAME_ABBR")),
            "src_id": u"%s|%s" % (code, src),
            # 实测 171 行（董事会预案/股东大会通过那类）NOTICEDATE 为空而 DIM_DATE 必有值
            # （决议公告当天即方案披露日），故公告日退到决议日，原始两值都另存着可回溯
            "notice_date": d(r.get("NOTICEDATE")) or d(r.get("DIM_DATE")),
            "dim_date": d(r.get("DIM_DATE")),
            "start_date": d(r.get("REPURSTARTDATE")),
            "end_date": d(r.get("REPURENDDATE")),
            "finish_date": d(r.get("FINISHDATE")),
            "updated_at": d(r.get("UPDATEDATE")),
            "progress": prog,
            "progress_label": PROGRESS.get(prog, prog),
            "finished": prog == "006",
            "plan_price_cap": num(r.get("REPURPRICECAP")),
            "plan_num_lower": num(r.get("REPURNUMLOWER")),
            "plan_num_cap": num(r.get("REPURNUMCAP")),
            "plan_amount_lower": num(r.get("REPURAMOUNTLOWER")),
            "plan_amount_cap": num(r.get("REPURAMOUNTLIMIT")),
            "done_num": done_num,
            "done_amount": done_amt,
            "done_price": avg if avg is not None else (
                round(done_amt / done_num, 4) if done_amt and done_num else None),
            "done_price_high": num(r.get("REPURPRICECAP1")),
            "done_price_low": num(r.get("REPURPRICELOWER1")),
            "purpose": purpose_label(obj),
            "cancel_type": status,
            "objective": one_line(obj, 600),
            # 判据所依据的那一句处置句。cancel_type 读的是全文，objective 只存 600 字，
            # 实测有一行的判据句在 600 字之外——只留原文就复现不出自己的标签，故单独存一句。
            "evidence": one_line(head, 200),
        })
    return out, skipped


# ---------- 落盘与守卫 ----------

def load_prev():
    if not os.path.exists(LATEST_PATH):
        return None
    try:
        with io.open(LATEST_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001  旧快照坏了不该挡住新一轮
        return None


def guard(prev, sources):
    """行数比上轮掉太多 / 家数崩塌 → 拒绝覆盖。返回拒绝原因列表（空表示放行）。"""
    if not prev:
        return []
    reasons = []
    old_src = prev.get("sources") or {}
    for key in ("seo", "buyback"):
        o, n = old_src.get(key) or {}, sources.get(key) or {}
        for field in ("rows_raw", "companies"):
            ov, nv = o.get(field), n.get(field)
            if not ov or nv is None:
                continue
            if nv < ov * MIN_RATIO:
                reasons.append(u"%s.%s %d → %d（低于上轮 %d%%）" % (key, field, ov, nv, MIN_RATIO * 100))
    return reasons


def dedupe(rows):
    seen, out = set(), []
    for r in rows:
        if r["src_id"] in seen:
            continue
        seen.add(r["src_id"])
        out.append(r)
    return out, len(rows) - len(out)


def report(seo, buyback, skipped, probe, seo_raw):
    print(u"\n===== 自检 =====")
    print(u"定增 %d 行 / %d 家（剔除 公开增发 %d、吸收合并 %d、异常代码 %d、源侧重复 %d）%s"
          % (len(seo), len({r["code"] for r in seo}), skipped["seo"]["type2"],
             skipped["seo"]["merge"], skipped["seo"]["badcode"], skipped["seo"]["dup"],
             u"［探针模式，未落盘］" if probe else u""))
    print(u"  候选规则对照（三条数应一致，不一致 = 东财加了新发行方式，该重看口径）：")
    for tag, s in ((u"只要 SEO_TYPE=1", lambda r: str(r.get("SEO_TYPE")) == "1"),
                   (u"+ 排除吸收合并", lambda r: str(r.get("SEO_TYPE")) == "1"
                    and u"吸收合并" not in unicode_safe(r.get("ISSUE_WAY"))),
                   (u"+ 要求含网下", lambda r: str(r.get("SEO_TYPE")) == "1"
                    and u"吸收合并" not in unicode_safe(r.get("ISSUE_WAY"))
                    and u"网下" in unicode_safe(r.get("ISSUE_WAY")))):
        keep = [r for r in seo_raw if s(r)]
        print(u"    %-16s %5d 行 / %5d 家" % (tag, len(keep), len({r.get("SECURITY_CODE") for r in keep})))
    print(u"回购 %d 行 / %d 家" % (len(buyback), len({r["code"] for r in buyback})))
    prog = {}
    for r in buyback:
        b = prog.setdefault(r["progress"], [0, 0])
        b[0] += 1
        if r["done_num"]:
            b[1] += 1
    print(u"  进度码 行数/其中已回购实数: " + u"  ".join(
        u"%s(%s) %d/%d" % (k, PROGRESS.get(k, u"?"), v[0], v[1]) for k, v in sorted(prog.items())))
    cs = {}
    for r in buyback:
        cs[r["cancel_type"]] = cs.get(r["cancel_type"], 0) + 1
    print(u"  注销三态（行）: " + u"  ".join(u"%s %d" % (k, v) for k, v in sorted(cs.items())))
    for st in (u"注销", u"非注销", u"待核"):
        print(u"  注销三态（家·%s）: %d" % (st, len({r["code"] for r in buyback if r["cancel_type"] == st})))
    for key, rows in ((u"定增", seo), (u"回购", buyback)):
        null_id = sum(1 for r in rows if not r["src_id"])
        print(u"  %s src_id 空值 %d，去重后 %d 行" % (key, null_id, len({r["src_id"] for r in rows})))
    nod = sum(1 for r in seo if not r["issue_date"])
    print(u"  定增 ISSUE_DATE 空 %d 行（键里已退到上市日/nodate）" % nod)


def report_samples(buyback, n):
    if not n:
        return
    print(u"\n===== 注销判定抽样（每态 %d 家，印的是判据所依据的处置句）=====" % max(1, n // 3))
    for st in (u"注销", u"非注销", u"待核"):
        pool = [r for r in buyback if r["cancel_type"] == st]
        pick, seen = [], set()
        for r in pool:
            if r["code"] in seen:
                continue
            seen.add(r["code"])
            pick.append(r)
            if len(pick) >= max(1, n // 3):
                break
        print(u"--- %s ---" % st)
        for r in pick:
            print(u"  %s %s [%s/%s] %s" % (
                r["code"], r["name"], r["progress_label"], r["purpose"] or u"未指名用途",
                r["evidence"] or (r["objective"][:60] + u"…（无处置句）")))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", type=int, default=0, help=u"只取前 N 页，跑自检但不落盘")
    ap.add_argument("--samples", type=int, default=0, help=u"打印 N 家注销三态抽样供人工核")
    args = ap.parse_args()

    t0 = time.time()
    seo_raw, seo_count, seo_pages = fetch_table(SEO_REPORT, "ISSUE_DATE", args.probe or None)
    buy_raw, buy_count, buy_pages = fetch_table(BUYBACK_REPORT, "DIM_DATE", args.probe or None)
    print(u"抓取：定增 %d/%d 行 %d 页，回购 %d/%d 行 %d 页，耗时 %.1fs"
          % (len(seo_raw), seo_count, seo_pages, len(buy_raw), buy_count, buy_pages, time.time() - t0))

    seo, sk_seo = norm_seo(seo_raw)
    buyback, sk_buy = norm_buyback(buy_raw)
    seo, dup_seo = dedupe(seo)
    buyback, dup_buy = dedupe(buyback)
    sk_seo["dup"] = dup_seo
    sk_buy["dup"] = dup_buy
    report(seo, buyback, {"seo": sk_seo, "buyback": sk_buy}, args.probe, seo_raw)
    report_samples(buyback, args.samples)

    sources = {
        "seo": {"report": SEO_REPORT, "rows_raw": len(seo_raw), "count": seo_count,
                "pages": seo_pages, "rows_kept": len(seo),
                "companies": len({r["code"] for r in seo})},
        "buyback": {"report": BUYBACK_REPORT, "rows_raw": len(buy_raw), "count": buy_count,
                    "pages": buy_pages, "rows_kept": len(buyback),
                    "companies": len({r["code"] for r in buyback})},
    }
    if args.probe:
        print(u"\n探针模式：不落盘。")
        return 0

    prev = load_prev()
    reasons = guard(prev, sources)
    if reasons:
        print(u"\n[拒绝覆盖] 保留旧快照 %s" % LATEST_PATH)
        for r in reasons:
            print(u"  - " + r)
        return 1

    payload = {
        "fetched_at": dt.datetime.now().replace(microsecond=0).isoformat(sep=u" "),
        "sources": sources,
        "seo": seo,
        "buyback": buyback,
    }
    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    tmp = LATEST_PATH + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, LATEST_PATH)
    print(u"\n已写 %s（定增 %d 行 / 回购 %d 行，%.1f KB，%.1fs）"
          % (LATEST_PATH, len(seo), len(buyback),
             os.path.getsize(LATEST_PATH) / 1024.0, time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
