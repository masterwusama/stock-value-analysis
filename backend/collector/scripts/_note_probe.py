# -*- coding: utf-8 -*-
"""附注抽取普查：下载定期报告 PDF、跑 extract_notes，只出统计不落库。

放行门槛（净现金口径改版的前置量测，不达标不进 Phase 2）：
  1. 「确有该科目且 >0」的公司里，其他流动资产附注块锚定率 ≥85%
  2. 锚到的块里「合计」与报表科目闭合率 ≥95%
  3. 单位误读 0 例（合计对不上、且差额恰为 1e3/1e4/1e8 的整数倍）
另外输出「定期存款行名漏抓清单」供人工过一遍白名单是否太窄。

    cd backend/collector/scripts
    python -X utf8 _note_probe.py --sample 200
    python -X utf8 _note_probe.py --codes 603599,601058,000011
"""
import argparse
import io
import json
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf
import requests

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).parent))
from fetch_data import (  # noqa: E402
    COMPANIES_DIR, NOTE_DEPOSIT_ROWS, NOTE_RESTRICTED_HEADS, _note_block,
    _note_factor, _note_num, _note_rows, _stmt_ca, extract_notes, is_chinese_doc,
    note_periods)

INDEX_PATH = COMPANIES_DIR.parent / "index.json"
BOARDS = {"600": "沪主板", "601": "沪主板", "603": "沪主板", "605": "沪主板",
          "688": "科创板", "000": "深主板", "001": "深主板", "002": "深中盘",
          "003": "深主板", "300": "创业板", "301": "创业板", "302": "创业板",
          "920": "北交所"}
# 合计对不上时，差额落在这几个量级上说明是把「万元/千元」当成了「元」
UNIT_STEPS = (("1e8", 1e8), ("1e4", 1e4), ("1e3", 1e3))


def board_of(code: str):
    b = BOARDS.get(code[:3])
    return b if b else ("北交所" if code[:2] in ("43", "83", "87") else "其他")


def stratified_sample(companies, n, seed):
    """按板块配额抽样，板块内按行业排序后等距取——保证行业不被代码顺序挤偏。"""
    pools = defaultdict(list)
    for c in companies:
        if c["market"] == "A":
            pools[board_of(c["code"])].append(c)
    for pool in pools.values():
        pool.sort(key=lambda c: (c.get("industry") or "", c["code"]))
    total = sum(len(p) for p in pools.values())
    picks, frac = [], total / n
    for board, pool in sorted(pools.items()):
        quota = max(8, round(len(pool) / frac)) if len(pool) >= 8 else len(pool)
        step = max(1.0, len(pool) / quota)
        picks += [pool[int(i * step)] for i in range(min(quota, len(pool)))]
    picks.sort(key=lambda c: hash((c["code"], seed)))
    return picks[:n] if len(picks) > n else picks


def pick_latest(reports):
    """每类别取公告日最新的一份（清单本身倒序，取首个命中即可）。"""
    got = {}
    for r in reports or []:
        if r.get("category") in ("年报", "半年报") and r.get("pdf_url"):
            got.setdefault(r["category"], r)
    return list(got.values())


def block_labels(text):
    """锚定块的全部行名 + 块内存款类候选行名，用于人工复核白名单是否太窄。"""
    blk = _note_block(text, "其他流动资产")
    rows = _note_rows(blk) if blk else []
    labels = [lab for lab, _ in rows]
    return rows, sorted({lab for lab in labels
                         if re.search(r"存款|存单|理财|保证金|通知|定期", lab)})


def closure_check(rows, target):
    """返回 (有无合计行, 是否闭合, 单位误读标记)。"""
    tot = next((r[1] for r in rows if r[0].startswith(("合计", "小计"))), None)
    if not tot or len(tot) < 2:
        return False, False, None
    vals = [_note_num(tot[i]) for i in (0, 1)]
    if any(v is None for v in vals):
        return True, False, None
    tol = [max(abs(t) * 1e-4, 1.0) for t in target]
    ok = all(abs(vals[i] - target[i]) <= tol[i] for i in (0, 1))
    if ok:
        return True, True, None
    for tag, step in UNIT_STEPS:      # 把「万元/千元」读成「元」会整体差一个 step
        if all(abs(vals[i] * step - target[i]) <= tol[i] or
               abs(vals[i] - target[i] * step) <= tol[i] for i in (0, 1)):
            return True, False, tag
    return True, False, None


CACHE_DIR = COMPANIES_DIR.parents[3] / "_tmp" / "note_probe"


def pdf_text(url):
    """PDF → 全文，按 URL 落盘缓存（口径还在调，同一批 PDF 要反复跑）。冷读才计时计量。"""
    f = CACHE_DIR / (re.sub(r"\W", "_", url.rsplit("/", 1)[-1]) + ".txt")
    if f.exists():
        return f.read_text(encoding="utf-8"), None, None
    t0 = time.time()
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=90)
    resp.raise_for_status()
    doc = pymupdf.open(stream=resp.content, filetype="pdf")
    text = "".join(p.get_text() for p in doc)
    doc.close()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    return text, round(time.time() - t0, 2), round(len(resp.content) / 1e6, 2)


def probe_one(entry):
    code, name = entry["code"], entry["name"]
    rec = {"code": code, "name": name, "board": board_of(code),
           "industry": entry.get("industry"), "pdfs": []}
    path = COMPANIES_DIR / f"{code}.json"
    if not path.exists():
        rec["skip"] = "无公司JSON"
        return rec
    d = json.loads(path.read_text(encoding="utf-8"))
    anchors = {}
    for row in d.get("balance") or []:
        day = str(row.get("报告日") or "")[:10]
        if day:
            anchors[day] = row
    rec["ca_latest"] = _stmt_ca(anchors, max(anchors)) if anchors else None
    for r in pick_latest(d.get("reports")):
        item = {"category": r["category"], "url": r["pdf_url"]}
        try:
            text, secs, mb = pdf_text(r["pdf_url"])
        except Exception as e:
            item["error"] = type(e).__name__ + ": " + str(e)[:80]
            rec["pdfs"].append(item)
            continue
        item["secs"], item["mb"] = secs, mb
        item["chinese"] = is_chinese_doc(text)
        periods = note_periods(r)
        item["periods"] = list(periods)
        ca = [_stmt_ca(anchors, p) for p in periods]
        item["ca"] = ca
        blk = _note_block(text, "其他流动资产")
        item["anchored"] = bool(blk)
        if blk:
            rows, item["deposit_like"] = block_labels(text)
            item["row_labels"] = [lab for lab, _ in rows]
            if all(v is not None for v in ca):
                target = [v / _note_factor(blk) for v in ca]
                item["has_total"], item["closed"], item["unit_misread"] = \
                    closure_check(rows, target)
        item["notes"] = extract_notes(text, periods, anchors) or {}
        # 受限侧分三层量：锚不到块 = 标题写法不同；有块没货币资金行 = 本年就无受限现金；
        # 有行没数 = 模板读不动。混在一起算「解析率」会把前两层算成失败。
        rb = next((b for b in (_note_block(text, h)
                               for h in NOTE_RESTRICTED_HEADS) if b), "")
        item["rst_block"] = bool(rb)
        item["rst_cash"] = any(l.endswith("货币资金") for l in rb.split("\n"))
        item["rst_parsed"] = any("restrictedCash" in v for v in item["notes"].values())
        rec["pdfs"].append(item)
        time.sleep(0.2)
    return rec


def summarize(recs, out_path):
    ok_recs = [r for r in recs if r.get("pdfs")]
    rows = [(r, p) for r in ok_recs for p in r["pdfs"]]

    def has_pos(p):
        vals = [v for v in p.get("ca") or [] if v is not None]
        return bool(vals) and max(vals) > 0

    usable = [(r, p) for r, p in rows if has_pos(p)]
    anchored = [(r, p) for r, p in usable if p.get("anchored")]
    with_tot = [(r, p) for r, p in anchored if p.get("has_total")]
    closed = [(r, p) for r, p in with_tot if p.get("closed")]
    unit_bad = [(r, p) for r, p in anchored if p.get("unit_misread")]
    en = [(r, p) for r, p in rows if p.get("chinese") is False]
    got_notes = [(r, p) for r, p in rows if (p.get("notes") or {}).values()]
    dep = [(r, p, d) for r, p in rows for d in (p.get("notes") or {}).values()
           if d.get("termDeposit")]
    rst = [(r, p, d) for r, p in rows for d in (p.get("notes") or {}).values()
           if d.get("restrictedCash") is not None]
    secs = [p["secs"] for r, p in rows if p.get("secs")]
    mbs = [p["mb"] for r, p in rows if p.get("mb")]
    fails = Counter(p.get("error", "").split(":")[0] for r, p in rows if p.get("error"))

    def pct(a, b):
        return f"{100.0 * a / b:.1f}%" if b else "n/a"

    print(f"\n普查 {len(recs)} 家 / 有效 {len(ok_recs)} 家，PDF {len(rows)} 份"
          f"（下载+解析 中位 {statistics.median(secs) if secs else 0}s、"
          f"最大 {max(secs, default=0)}s；PDF 中位 {statistics.median(mbs) if mbs else 0}MB）")
    print(f"  下载失败 {sum(fails.values())} 份 {dict(fails)}；英文版 {len(en)} 份")
    print(f"  科目确有且>0 的 PDF {len(usable)} 份")
    print(f"  ① 锚定其他流动资产块          {len(anchored)}/{len(usable)} = "
          f"{pct(len(anchored), len(usable))}   门槛 ≥85%")
    print(f"  ② 锚到且有合计行              {len(with_tot)}/{len(anchored)} = "
          f"{pct(len(with_tot), len(anchored))}")
    print(f"     合计与报表科目闭合          {len(closed)}/{len(with_tot)} = "
          f"{pct(len(closed), len(with_tot))}   门槛 ≥95%")
    print(f"  ③ 单位误读                    {len(unit_bad)} 例            门槛 0 例")
    print(f"  extract_notes 有产出          {len(got_notes)}/{len(rows)}")
    print(f"     含定期存款 {len(dep)} 份 / 含受限货币资金 {len(rst)} 份")
    rb = [(r, p) for r, p in rows if p.get("rst_block")]
    rc = [(r, p) for r, p in rb if p.get("rst_cash")]
    rp = [(r, p) for r, p in rc if p.get("rst_parsed")]
    print(f"  受限侧漏斗：锚到块 {len(rb)}/{len(rows)} → 块内有货币资金行 {len(rc)}/{len(rb)}"
          f" → 读出数 {len(rp)}/{len(rc)} = {pct(len(rp), len(rc))}（真实解析率）")
    no_rst = [(r["code"], r["name"], p["category"]) for r, p in rc
              if not p.get("rst_parsed")]
    print("     有货币资金行却没读出数：" + "、".join(
        f"{c}{n}·{cat}" for c, n, cat in no_rst[:12]))
    per_company = defaultdict(list)
    for r, p in usable:
        per_company[r["code"]].append(p)
    hit = sum(1 for v in per_company.values() if any(x.get("closed") for x in v))
    print(f"  公司口径：有该科目的 {len(per_company)} 家中 {hit} 家闭合 "
          f"= {pct(hit, len(per_company))}")
    by_board = defaultdict(lambda: [0, 0])
    for r, p in anchored:
        b = by_board[r["board"]]
        b[1] += 1
        b[0] += bool(p.get("closed"))
    print("\n  分板块闭合率（闭合/锚到）：")
    for board, (got, tot) in sorted(by_board.items(), key=lambda x: x[1][0] / x[1][1]):
        print(f"    {board}: {got}/{tot} = {pct(got, tot)}")
    bad = [(r["code"], r["name"], r["industry"], p["category"])
           for r, p in anchored if p.get("has_total") and not p.get("closed")]
    print(f"\n  未闭合清单 {len(bad)} 份（逐个看，不批量放过）：")
    for row in bad[:30]:
        print("    " + " / ".join(str(x) for x in row))
    missed = sorted({lab for r, p in anchored for lab in p.get("deposit_like") or []
                     if not any(w in lab for w in NOTE_DEPOSIT_ROWS)})
    print(f"\n  漏抓存款类行名（白名单 {NOTE_DEPOSIT_ROWS} 之外，人工过一遍）：")
    for lab in missed[:40]:
        print(f"    {lab}")
    if out_path:
        out_path.write_text(json.dumps(recs, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        print(f"\n  明细 → {out_path}")
    verdict = (len(anchored) / max(len(usable), 1) >= .85
               and len(closed) / max(len(with_tot), 1) >= .95 and not unit_bad)
    print(f"\n门槛判定：{'放行' if verdict else '不放行——停在 Phase 1'}")
    return verdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--codes", default="")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--seed", type=int, default=20260908)
    ap.add_argument("--out", default=str(COMPANIES_DIR.parents[3] / "_tmp" /
                                         "note_probe.json"))
    args = ap.parse_args()
    idx = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    if args.codes:
        want = {c.strip() for c in args.codes.split(",") if c.strip()}
        targets = [c for c in idx["companies"] if c["code"] in want]
    else:
        targets = stratified_sample(idx["companies"], args.sample, args.seed)
    boards = Counter(board_of(c["code"]) for c in targets)
    print(f"抽样 {len(targets)} 家（板块分布 {dict(boards)}）")
    out_path = Path(args.out) if args.out else None
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        recs = list(ex.map(probe_one, targets))
    print(f"耗时 {time.time() - t0:.0f}s（{args.workers} 线程）")
    return 0 if summarize(recs, out_path) else 1


if __name__ == "__main__":
    raise SystemExit(main())
