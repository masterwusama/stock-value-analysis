# -*- coding: utf-8 -*-
"""PE / PB / PS 历史分位抓取器(免费源:东财 datacenter,经 akshare stock_value_em)。

2026-10 起替代 Wind 路径的默认源(run.py valuation 不带 --source wind 即走本脚本):
设计见 docs/设计-估值分位免费源替代.md。原计划的乐咕接口(stock_a_indicator_lg)在
akshare 1.18.64 已移除,实施期改用 stock_value_em——单请求返回该股全历史日线
(数据日期/收盘/总市值/总股本/PE(TTM)/市净率/市销率…,实测 2018-01 起、北交所与
亏损股负 PE 均覆盖、约 0.8s/只),宿主是 datacenter-web.eastmoney.com——仓库的
情绪/EDB 任务每天已经在打同一家,礼貌间隔 0.4s 与既有习惯一致。

与 Wind 路径的分工:本脚本只刷 A 股全市场(5553 只,不再过筛——积分约束消失,
Wind 时代「管理分<30/造假分>50 不刷」的覆盖洞顺带补平);港股乐咕/东财都不在
本接口覆盖内,维持 Wind 可选路径(run.py valuation --source wind)或停更。

产物两份(与 prices/ 同一套断点续传范式):
  data/valuation/em/<code>.json  个股序列 {code, updated_at, start, dates, pe, pb, ps}
                                 (紧凑数组、升序;窗口裁剪到「今天-10年-45天」起,
                                  单只 ~60KB;gitignore,换机器重拉)
  data/valuation/latest.json     累计快照 items{"A:600519":{trade_date,fetched_at,round,
                                 src:"em",pe:{pct,days,date},pb:{...},ps:{...}}}
                                 ——与 Wind 路径同构,import_valuation() 直接吃;
                                 两条路径谁后跑谁覆盖,靠 src 字段区分来源。

分位口径(本地算,Wind「近10年分位」同义):
  value  = 序列最新一档的 PE(TTM)/市净率/市销率
  window = 最新观测日前推 10 年内该指标的**正值**样本(≤0 剔除——亏损期分位无意义)。
           注意与 Wind 口径并不同义:Wind 只把「当前值≤0」置空,其分位窗口本身
           含非正值样本;换源瞬间同一标的的分位可能小幅跳变
  pct    = count(window ≤ value)/len(window)×100;days = len(window)(新股短样本自明)
           ——EM 的 days 是「正值样本数」,Wind 的 days 是全部交易日数,前端文案
           已按「样本 N」中性表述
  停更守卫:序列最新日期距今 >45 天(长期停牌/退市边缘)整股不进 items,落库旧行保留。

节奏:一轮全量 ≈ 5553×1.2s ≈ 110 分钟(调度 03:35 起,05:30 前完事,与 09:05 agro
错开)。当日已更新的序列文件默认跳过(重跑即断点续传),--force 全部重拉。

用法(一律走 run.py,跑完自动回灌):
    python run.py valuation                    # 默认:东财免费源全量(断点续传)
    python run.py valuation --source wind ...  # 切回 Wind 原路径(游标/积分上限逻辑原样)
    cd collector; python -X utf8 scripts/fetch_valuation_em.py --probe 5   # 冒烟不落盘
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
EM_DIR = DATA / "valuation" / "em"
LATEST_PATH = DATA / "valuation" / "latest.json"
INDEX_PATH = DATA / "index.json"
SLEEP = 0.4
RETRY = 3
WINDOW_YEARS = 10          # 分位回看窗(年)
KEEP_DAYS = 45             # 序列裁剪时在 10 年外多留的余量
STALE_DAYS = 45            # 停更判定:最新观测日距今天超过此数整股置空
METRICS = ("pe", "pb", "ps")   # pe = PE(TTM)


def load_universe():
    """A 股全集(与 fetch_daily_prices 同一口径:index.json 的 market=A)。"""
    if not INDEX_PATH.exists():
        raise SystemExit("index.json 不存在,先跑一轮抓取")
    idx = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    codes = [c["code"] for c in idx.get("companies") or []
             if (c.get("market") or "A") == "A"]
    if not codes:
        raise SystemExit("index.json 没有 A 股标的")
    return sorted(codes)


def fetch_one(ak, code):
    """单只 → (dates, pe, pb, ps) 升序紧凑数组;空表 None;三次重试后 "FAIL"。"""
    last_err = None
    for attempt in range(RETRY):
        try:
            df = ak.stock_value_em(symbol=code)
            if df is None or df.empty:
                return None
            df = df.sort_values("数据日期")
            dates, pe, pb, ps = [], [], [], []
            for _, r in df.iterrows():
                d = str(r["数据日期"])[:10]
                if not d or d in dates[-1:]:
                    continue
                dates.append(d)
                pe.append(_num(r["PE(TTM)"]))
                pb.append(_num(r["市净率"]))
                ps.append(_num(r["市销率"]))
            if not dates:
                return None
            return dates, pe, pb, ps
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    print(f"  [fail] {code}: {last_err!r}", flush=True)
    return "FAIL"


def _num(v):
    """NaN/None → None,其余 float。"""
    try:
        if v is None or v != v:  # NaN != NaN
            return None
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def atomic_write(path, payload):
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    os.replace(tmp, path)


def trim_window(dates, cols, today):
    """序列裁剪:只留「今天 - 10年 - 45天」起的行(分位窗 + 停更判定余量)。"""
    keep_from = (today - dt.timedelta(days=365 * WINDOW_YEARS + KEEP_DAYS)).isoformat()
    i = 0
    while i < len(dates) and dates[i] < keep_from:
        i += 1
    if i == 0:
        return dates, cols
    return dates[i:], [c[i:] for c in cols]


def percentile(dates, vals, last_date):
    """单指标分位 → (pct, days) 或 None(最新值缺失/≤0/窗口空)。"""
    if not dates:
        return None
    v = vals[-1]
    if v is None or v <= 0:
        return None
    win_from = (dt.date.fromisoformat(last_date)
                - dt.timedelta(days=365 * WINDOW_YEARS)).isoformat()
    window = [x for d, x in zip(dates, vals) if d >= win_from and x is not None and x > 0]
    if not window:
        return None
    pct = round(sum(1 for x in window if x <= v) / len(window) * 100, 1)
    return min(100.0, max(0.0, pct)), len(window)


def build_item(code, dates, cols, today, now):
    """序列 → latest.json 条目;停更/全空返回 None。"""
    pe, pb, ps = cols
    last_date = dates[-1]
    if (today - dt.date.fromisoformat(last_date)).days > STALE_DAYS:
        return None
    item = {"trade_date": last_date, "fetched_at": now, "round": today.strftime("%Y%m%d"),
            "src": "em"}
    for name, vals in (("pe", pe), ("pb", pb), ("ps", ps)):
        got = percentile(dates, vals, last_date)
        if got is not None:
            item[name] = {"pct": got[0], "days": got[1], "date": last_date}
    return item if any(m in item for m in METRICS) else None


def load_local(code):
    """本地序列档 → (dates, cols);空档/坏档 None。"""
    p = EM_DIR / f"{code}.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not d.get("dates"):
        return None
    return d["dates"], [d.get("pe") or [], d.get("pb") or [], d.get("ps") or []]


def main():
    ap = argparse.ArgumentParser(description="PE/PB/PS 十年分位(东财免费源)")
    ap.add_argument("--probe", type=int, default=0, help="只跑前 N 只打印结果,不落盘")
    ap.add_argument("--force", action="store_true", help="当日已更新的文件也重拉(默认跳过)")
    args = ap.parse_args()

    import akshare as ak

    codes = load_universe()
    if args.probe:
        codes = codes[: args.probe]
    EM_DIR.mkdir(parents=True, exist_ok=True)
    today = dt.date.today()
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    items, done, skip, empty, fail = {}, 0, 0, 0, 0
    t0 = time.time()
    total = len(codes)
    for i, code in enumerate(codes, 1):
        path = EM_DIR / f"{code}.json"
        if not args.probe and not args.force and path.exists():
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("updated_at", "").startswith(
                        today.isoformat()):
                    got = load_local(code)
                    if got:
                        item = build_item(code, got[0], got[1], today, now)
                        if item:
                            items[f"A:{code}"] = item
                    skip += 1
                    continue
            except Exception:  # noqa: BLE001  坏档当无档重拉
                pass
        got = fetch_one(ak, code)
        if got != "FAIL" and got is not None:
            time.sleep(SLEEP)
        if got == "FAIL":
            fail += 1
            continue
        if got is None:
            # 空表(退市边缘/源不覆盖):空档占位,断点续传不反复重试,不进 items。
            # 已有非空档时保留原档——瞬时空返回不该毁十年历史序列(次日重拉可自愈)
            if not args.probe and not load_local(code):
                atomic_write(path, {"code": code, "updated_at": now, "start": "",
                                    "dates": [], "pe": [], "pb": [], "ps": []})
            empty += 1
            continue
        dates, cols = trim_window(got[0], got[1:], today)
        item = build_item(code, dates, cols, today, now)
        if item:
            items[f"A:{code}"] = item
        if args.probe:
            m = {k: item.get(k) if item else None for k in METRICS}
            print(f"  {code}  {dates[-1]}  " + "  ".join(
                f"{k}={m[k]['pct']}%(n={m[k]['days']})" if m[k] else f"{k}=—" for k in METRICS),
                flush=True)
        else:
            atomic_write(path, {"code": code, "updated_at": now, "start": dates[0],
                                "dates": dates, "pe": cols[0], "pb": cols[1], "ps": cols[2]})
        done += 1
        if not args.probe and i % 200 == 0:
            el = time.time() - t0
            print(f"  [{i}/{total}] 新拉 {done} 跳过 {skip} 空 {empty} 失败 {fail}"
                  f" · 已用 {el / 60:.0f}m · 预计还要 {el / i * (total - i) / 60:.0f}m",
                  flush=True)
    if args.probe:
        print(f"探针完成:{done} 只(空 {empty} 失败 {fail}),未落盘", flush=True)
        return 0
    # 与 Wind 路径同构的 merge 语义:本轮没刷到的 key(港股 Wind 条目、当日 FAIL、
    # 停更置空)保留旧条目——latest.json 是「全量累计快照」,整文件覆盖会把 Wind 时代
    # 的港股分位抹掉(2026-10-04 首跑真发生过,verify_filters 六项红了才逮到)
    old_items = {}
    if LATEST_PATH.exists():
        try:
            old_items = json.loads(LATEST_PATH.read_text(encoding="utf-8")).get("items") or {}
        except Exception:  # noqa: BLE001  坏档当空档,本轮照写
            pass
    for k, v in old_items.items():
        items.setdefault(k, v)
    atomic_write(LATEST_PATH, {"fetched_at": now, "src": "em", "items": items})
    print(f"完成:latest.json 写 {len(items)} 家 · 新拉 {done} · 跳过 {skip} · 空档 {empty}"
          f" · 失败 {fail} · {time.time() - t0:.0f}s", flush=True)
    # 失败率 >5% 记失败:断档的分位会让列表筛选静默少标的
    return 1 if (fail and fail * 20 > total) else 0


if __name__ == "__main__":
    sys.exit(main())
