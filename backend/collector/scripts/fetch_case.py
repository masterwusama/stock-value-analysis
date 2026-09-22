# -*- coding: utf-8 -*-
"""证监会立案/处罚公告采集（巨潮全文检索，全市场聚合翻页——不逐家、不耗 Wind）。

数据源：巨潮资讯 fulltextSearch 接口（与定期报告同一通道，免登录免积分，Wind 积分紧张
时这里是唯一能跑的公告面）。按关键词检索公告标题，返回公告编号/代码/标题/时间：
    立案告知书 / 立案调查  → kind=立案
    行政处罚决定书        → kind=处罚（用于判定立案是否已公开落地）
撤销/结案公告巨潮几乎不发（实测「撤销立案」全库仅 1 条且是刑事逮捕撤销），「正在立案」
只能定义为「近 2 年有立案告知书、其后未见同主体处罚决定书」——多标不可避免，消费侧
（import_legacy 抬 ST 高危旗、gate.case_filed）的旗语文案如实说「未见处罚」而不是
「确认进行中」。

主体判定（宁多标 related）：标题含 子公司/控股股东/实控人/董事/监事/高管/股东/参股
→ related（公司自身以外的主体被立案，不进 ST 预警）；否则 self——「公司及相关当事人
收到…」这类不带主体词的算 self，因为上市公司自身被立案才是 ST 判定要件，宁可让 related
侧多背记录，不让 self 侧漏标。

产物：data/case/case_anns.json（整文件权威，import_legacy --only-case 回灌 case_event）
    {updated_at, anns:[{annId, code, name, date, kind, subject, title}]}
增量按「该类已有最新公告日 −14 天」截页合并（公告偶有迟登，留重叠不丢行）；--full 全量
回填。全量约 28 页（立案 869 + 处罚 1845 条，pageSize=100），1 分钟内跑完。

用法（在 backend/collector 目录下）:
    python -X utf8 scripts/fetch_case.py           # 周更增量（默认，重叠 14 天）
    python -X utf8 scripts/fetch_case.py --full    # 全量回填
    python -X utf8 scripts/fetch_case.py --probe   # 只看各关键词总量，不落盘
"""
import argparse
import datetime as dt
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
CASE_PATH = DATA / "case" / "case_anns.json"
INDEX_PATH = DATA / "index.json"

URL = "http://www.cninfo.com.cn/new/fulltextSearch/full"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "http://www.cninfo.com.cn/"}
# 关键词 → kind。立案调查的「进展公告」也命中（月度披露本身就是「还在查」的证据），同记立案
KEYWORDS = [("立案告知书", "立案"), ("立案调查", "立案"), ("行政处罚决定书", "处罚")]
PAGE_SIZE = 100
SLEEP = 0.6
RETRY = 3
OVERLAP_DAYS = 14
# 主体限定词：命中即 related（公司以外的主体）。顺序无关，任一命中即判
RELATED_KW = ("子公司", "控股股东", "实控人", "实际控制人", "董事", "监事",
              "高管", "高级管理人员", "股东", "参股")


def subject_of(title):
    return "related" if any(k in title for k in RELATED_KW) else "self"


def load_universe():
    if not INDEX_PATH.exists():
        raise SystemExit("index.json 不存在，先跑一轮抓取")
    idx = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    return {c["code"]: (c.get("name") or "") for c in idx.get("companies") or []
            if (c.get("market") or "A") == "A"}


def fetch_pages(kw, kind, stop_before=None):
    """按关键词翻页，返回 {annId: ann}。stop_before(date) 返回 True 时提前收页（增量）。"""
    out, page = {}, 1
    while True:
        r = None
        for attempt in range(RETRY):
            try:
                r = requests.get(URL, params={
                    "searchkey": kw, "sdate": "", "edate": "", "isfulltext": "false",
                    "sortName": "pubdate", "sortType": "desc",
                    "pageNum": page, "pageSize": PAGE_SIZE,
                }, headers=HEADERS, timeout=25)
                break
            except requests.RequestException:
                if attempt == RETRY - 1:
                    raise
                time.sleep(2 * (attempt + 1))
        anns = (r.json() if r else {}).get("announcements") or []
        if not anns:
            break
        oldest = None
        for a in anns:
            code = (a.get("secCode") or "").strip()
            title = re.sub(r"</?em>", "", a.get("announcementTitle") or "")
            day = dt.date.fromtimestamp(a["announcementTime"] / 1000) if a.get("announcementTime") else None
            ann_id = str(a.get("announcementId") or "")
            if not (code and title and day and ann_id):
                continue
            oldest = day if oldest is None else min(oldest, day)
            out[ann_id] = {"annId": ann_id, "code": code, "name": a.get("secName") or "",
                           "date": day.isoformat(), "kind": kind,
                           "subject": subject_of(title), "title": title[:240]}
        if stop_before and oldest and stop_before(oldest):
            break
        total = (r.json() or {}).get("totalRecordNum") or 0
        if page * PAGE_SIZE >= total:
            break
        page += 1
        time.sleep(SLEEP)
    return out


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    ap = argparse.ArgumentParser(description="证监会立案/处罚公告采集（巨潮全文检索）")
    ap.add_argument("--full", action="store_true", help="全量回填（默认增量：只拉到已有最新日−14 天）")
    ap.add_argument("--probe", action="store_true", help="只看各关键词总量，不落盘")
    args = ap.parse_args()

    if args.probe:
        for kw, kind in KEYWORDS:
            n = fetch_pages(kw, kind, stop_before=lambda d: True)  # 只拉第一页
            print(f"{kw}: 首页 {len(n)} 条（kind={kind}）")
        return 0

    universe = load_universe()
    old = {"anns": []}
    if CASE_PATH.exists():
        old = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    merged = {a["annId"]: a for a in old.get("anns") or []}
    for kw, kind in KEYWORDS:
        stop = None
        if not args.full:
            kind_max = max((a["date"] for a in merged.values() if a["kind"] == kind), default=None)
            if kind_max:
                cut = dt.date.fromisoformat(kind_max) - dt.timedelta(days=OVERLAP_DAYS)
                stop = lambda d, cut=cut: d < cut  # noqa: E731  该类已见过的更早记录直接收页
        got = fetch_pages(kw, kind, stop_before=stop)
        # 巨潮覆盖沪深京全市场，只留标的池内的 A 股
        kept = {k: v for k, v in got.items() if v["code"] in universe}
        print(f"{kw}: 拉到 {len(got)} 条，池内 A 股 {len(kept)} 条")
        merged.update(kept)
    anns = sorted(merged.values(), key=lambda a: (a["date"], a["annId"]), reverse=True)
    CASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CASE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"updated_at": dt.datetime.now().isoformat(timespec="seconds"),
                               "anns": anns}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, CASE_PATH)
    n_case = sum(1 for a in anns if a["kind"] == "立案" and a["subject"] == "self")
    print(f"合计 {len(anns)} 条（其中 self 立案 {n_case} 条）→ 已写 {CASE_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
