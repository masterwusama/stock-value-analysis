# -*- coding: utf-8 -*-
"""PE / PB / PS 近十年历史分位抓取器（Wind，分批滚动铺全市场）。

数据源：wind-mcp-skill/scripts/cli.mjs → analytics_data.get_financial_data
  问句里逗号列 100 个 windcode，要「最新市盈率/市净率/市销率近10年分位数」。返回列式表，
  每指标一张表，列含 `最新<指标>在过去10年的分位数`、`过去10年<指标>最大序号`（样本交易日
  数）、`日期`、`Wind代码`。

四条实测硬约束（2026-09-09 逐条探过，直接决定这里的批大小与调用数）：
  - 单表硬截 100 行：问沪深300（300 家）只回 100 行 → 每批固定 100 家；
  - 一次最多 2 张表：一次问 3 个分位只回 PE+PB，PS 被**静默**丢弃（warnings 为空）
    → 每批两次调用（①PE+PB ②PS），否则 PS 永远缺；
  - 分位不能当筛选条件：stock_screener 问「PB 十年分位<5%」回「没找到数据」
    → 只能逐批取回本地落表，筛选走我们自己的库；
  - 问句里要得过的列会「提取数量超限」→ 每次只列 2~3 个指标。

windcode（实测）：A 股 920/43/83/87/88→.BJ、6/9→.SH、其余→.SZ；港股五位数字 + ".HK"
  但回码会去掉前导零（送 00323.HK 回 0323.HK）；美股裸代码自动定位交易所
  （AAPL→AAPL.O、PBR→PBR.N），不用两个后缀都试一遍。

产物（采集工作目录，与 events/edb 同一套）：
  data/valuation/latest.json  累计快照 {items:{"A:600519":{pe,pb,ps,trade_date}}, ...}
  data/valuation/state.json   滚动游标 {round_id, batch_index, day, calls_today}
落库不在这里：run.py 每个 job 末尾统一跑 import_legacy --no-clean，由 import_valuation()
  读 latest.json upsert 进 valuation_pctile —— 别配 --only-fresh，那是 companies/*.json
  的 mtime 增量窗，本产物不在里面。

口径与守卫（为什么有的格子是空的）：
  - 亏损股 Wind 照样给分位（万科A PE_TTM=-0.42 → 「近10年分位 26.5%」，隆基 -11.9 →
    40.0%），这类分位没有意义：比率本身 ≤0 时该指标置空（优先用 Wind 附表里的比率列，
    没有就用我们自己的行情快照兜底）；
  - 上面两家的 PE 序列日期停在 2024-08-30、样本数 1937 而非 2427 —— 序列已经不动了还在
    给分位：外源日期距抓取日 > --max-lag-days 时置空；
  - 分位数只收 0~100；
  - 新股样本天然短（688981 1494 天、300750 2004 天、920002.BJ 556 天）→ 一并落 days，
    前端写明「样本 N 个交易日」，不假装都是满十年。

积分：Wind 按调用次数扣，cli 不回报余额，单价只能按“当天积分 ÷ 成功调用数”反推。2026-09-10
  首轮实测 72 次调用把当天 1000 积分打光（回执「余额不足，请先充值」）⇒ ≈13.9 积分/次，
  比接入前按 7 次调用估的 8~11.4 贵一档。全市场 6939 只 = 70 批 × 2 = 140 次/轮，默认
  --calls-cap 56 次/天（≈780 积分）= 28 批 = 2800 家，约 2.5 天一轮；留下的 ≈220 积分是给
  每周日的 edb 和手工探针的——两个 Wind 任务抢的是同一个池子，上限设到 80 就没 edb 的了。
  当日达到上限、或收到额度类回执都算预期收尾（exit 0，别让 etl_job_log 天天红）；
  只有连续多批取数失败才 exit 1。要把一轮压进一天：--calls-cap 0。
  标的按 A→HK→US 定序，所以额度不够时先补 A 股 = 把 cap 设成剩下的 A 股批次数 × 2。

用法：
  python fetch_valuation.py --probe 20      # 只抓一批前 20 家，验列名/行数/守卫，不写文件
  python fetch_valuation.py                 # 按游标滚动铺（调度每天跑一次）
  python fetch_valuation.py --calls-cap 0   # 一轮铺完全市场
  python fetch_valuation.py --reset         # 丢弃游标，从第一批重铺
"""
import argparse
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STOCK_ROOT = os.path.abspath(os.path.join(HERE, ".."))
REPO = os.path.abspath(os.path.join(STOCK_ROOT, ".."))
# [collector 改造] Wind skill 在博客仓，仓库相对路径失效，用 WIND_SKILL_DIR 环境变量定位
SKILL_DIR = os.environ.get("WIND_SKILL_DIR") or os.path.join(REPO, ".agents", "skills", "wind-mcp-skill")
CLI = os.path.join("scripts", "cli.mjs")

DATA_DIR = os.path.join(STOCK_ROOT, "data")
OUT_DIR = os.path.join(DATA_DIR, "valuation")
LATEST_PATH = os.path.join(OUT_DIR, "latest.json")
STATE_PATH = os.path.join(OUT_DIR, "state.json")

SERVER, TOOL = "analytics_data", "get_financial_data"
BATCH = 100              # Wind 单表硬截 100 行
SLEEP = 1.8              # 调用间隔：Wind 连续调用易瞬时空返回
TIMEOUT = 300            # 单次调用超时（秒）
CALLS_CAP = 56           # 默认每日调用上限（56 × 实测 13.9 积分 ≈ 780，给 edb 与手工留余量）
MAX_LAG_DAYS = 10        # 外源日期距抓取日超过这么多天 = 序列已停更
FAIL_STOP = 5            # 连续失败批数：整轮退出，不把失败扩散到后面的批次

# 指标 → (Wind 列名关键字, 我们行情快照里对应字段的键)。PS 本地无对应字段，只能靠 Wind 附表。
METRICS = (
    ("pe", u"市盈率", "pe_ttm"),
    ("pb", u"市净率", "pb"),
    ("ps", u"市销率", None),
)
LOCAL_KEY = {m[0]: m[2] for m in METRICS}
# 一次调用最多 2 张表 → 分成两问
QUERIES = (
    (("pe", "pb"), u"查询下列股票最新交易日的最新市盈率近10年分位数、最新市净率近10年分位数：%s"),
    (("ps",), u"查询下列股票最新交易日的最新市销率近10年分位数：%s"),
)
CALLS_PER_BATCH = len(QUERIES)
MARKET_ORDER = {"A": 0, "HK": 1, "US": 2}
QUOTA_PAT = re.compile(u"积分|额度|quota|RATE_LIMIT|rate limit|insufficient|余额不足", re.I)


class WindQuota(Exception):
    """额度类回执：当日不该再打，干净收尾。"""


def to_wind(code, market):
    """我们的 code → Wind windcode（规则见模块注释）。"""
    if market == "HK":
        return code + ".HK"
    if market == "US":
        return code
    if code.startswith(("920", "43", "83", "87", "88")):
        return code + ".BJ"
    if code.startswith(("6", "9")):
        return code + ".SH"
    return code + ".SZ"


def load_universe():
    """data/index.json → [{key, code, market, name, ratio}]，按 (市场, 代码) 定序。

    定序必须稳定：游标存的是 batch_index，两批之间标的顺序变了会让有些标的在同一轮里
    被铺两次、另一些一次都没铺到。
    """
    with io.open(os.path.join(DATA_DIR, "index.json"), encoding="utf-8") as f:
        index = json.load(f)
    out = []
    for c in index.get("companies") or []:
        code, market = c.get("code"), c.get("market") or "A"
        if not code:
            continue
        q = c.get("quote") or {}
        out.append({
            "key": "%s:%s" % (market, code), "code": code, "market": market,
            "name": c.get("name") or code,
            "ratio": {"pe_ttm": q.get("pe_ttm"), "pb": q.get("pb")},
        })
    out.sort(key=lambda e: (MARKET_ORDER.get(e["market"], 9), e["code"]))
    return out


def _extract_json(s):
    """从可能混入升级横幅/告警的 stdout 中截出首个 JSON 子串。"""
    if not s:
        return None
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b <= a:
        return None
    return s[a:b + 1]


def call_wind(question, uniq, retry=1):
    """一次 get_financial_data；额度类错误抛 WindQuota（不重试，重试只会再烧一次）。"""
    last = None
    for attempt in range(retry + 1):
        try:
            return _call_once(question, uniq if attempt == 0 else "%s-r%d" % (uniq, attempt))
        except WindQuota:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retry:
                time.sleep(SLEEP * 2)
    raise last


def _call_once(question, uniq):
    pf = os.path.join("scripts", "request-%s.json" % uniq)
    pfull = os.path.join(SKILL_DIR, pf.replace("/", os.sep))
    with io.open(pfull, "w", encoding="utf-8") as f:
        f.write(json.dumps({"question": question}, ensure_ascii=False))
    try:
        r = subprocess.run(
            ["node", CLI, "call", SERVER, TOOL, "@" + pf],
            cwd=SKILL_DIR, capture_output=True, text=True,
            encoding="utf-8", timeout=TIMEOUT,
        )
    finally:
        try:
            os.remove(pfull)
        except OSError:
            pass
    env = json.loads(_extract_json(r.stdout) or "null")
    if env is None:
        raise RuntimeError("no JSON in stdout; head=%r tail=%r"
                           % ((r.stdout or "")[:120], (r.stderr or "")[-120:]))
    blob = json.dumps(env, ensure_ascii=False)
    if QUOTA_PAT.search(blob):
        raise WindQuota(blob[:300])
    if env.get("ok") is False:
        raise RuntimeError("wind envelope error: %s" % blob[:240])
    content = env.get("content")
    txt = (content or [{}])[0].get("text") if content else None
    if not txt:
        raise RuntimeError("empty content (likely throttled): %s" % blob[:160])
    payload = json.loads(txt) if isinstance(txt, str) else txt
    if not isinstance(payload, dict):
        return {}
    if QUOTA_PAT.search(json.dumps(payload, ensure_ascii=False)[:400]):
        raise WindQuota(json.dumps(payload, ensure_ascii=False)[:300])
    return payload


def _find_col(cols, need, avoid=()):
    """按列名片段挑列名。指标分位列 = [指标词, '分位数']；样本数列 = [指标词, '最大', '序号']。"""
    for name in cols:
        if all(t in name for t in need) and not any(t in name for t in avoid):
            return name
    return None


def extract_metric(payload, kw):
    """payload → {windcode: {pct, days, date, ratio}}，只从该指标自己的那张表取。

    一张表会顺带别家指标的列，且同一响应里各表的日期可能不一致（实测一张给 09-09 的 PB、
    另一张给 08-30 的 PE）—— 所以逐指标各选一张表：优先「分位列 + 序号列」齐全的，
    宁可少取也不跨表拼行。
    """
    tables = ((payload.get("data") or {}).get("data")) or []
    best = None
    for t in tables:
        cols = [(c.get("name") or "") for c in (t.get("columns") or [])]
        pct = _find_col(cols, (kw, u"分位数"))
        date = u"日期" if u"日期" in cols else _find_col(cols, (u"日期",))
        if not pct or not date:
            continue
        days = _find_col(cols, (kw, u"最大", u"序号"))
        ratio = _find_col(cols, (kw,), avoid=(u"分位数", u"序号", u"最大"))
        score = (1 if days else 0) + (1 if ratio else 0)
        if best is None or score > best[0]:
            best = (score, cols, t, pct, days, date, ratio)
    if not best:
        return {}
    _, cols, table, pct, days, date, ratio = best
    i_pct, i_date = cols.index(pct), cols.index(date)
    i_days = cols.index(days) if days else None
    i_ratio = cols.index(ratio) if ratio else None
    i_code = cols.index(u"Wind代码") if u"Wind代码" in cols else None
    out = {}
    for row in table.get("rows") or []:
        def cell(i):
            return row[i] if i is not None and i < len(row) else None
        wc = str(cell(i_code) or "").strip().upper()
        if wc:
            out[wc] = {"pct": cell(i_pct), "days": cell(i_days),
                       "date": cell(i_date), "ratio": cell(i_ratio)}
    return out


def match_key(wc, by_sent):
    """Wind 回码 → 我们的 key。港股回码会去掉前导零，美股会带上交易所后缀。"""
    hit = by_sent.get(wc)
    if hit:
        return hit
    base, _, suffix = wc.partition(".")
    if suffix == "HK" and base.isdigit():
        return by_sent.get("%05d.HK" % int(base))
    return by_sent.get(base)


def as_date(v):
    m = re.search(r"\d{4}-\d{2}-\d{2}", str(v or ""))
    return dt.date.fromisoformat(m.group(0)) if m else None


def clean_value(got, entry, metric, today, max_lag):
    """Wind 一行 → 可信值；不可信时 pct 为 None（行仍落库，格子显示 -）。"""
    blank = {"pct": None, "days": None, "date": None, "wind_code": got.get("wind_code")}
    try:
        pct = float(got.get("pct"))
    except (TypeError, ValueError):
        return blank
    if not 0.0 <= pct <= 100.0:
        return blank
    d = as_date(got.get("date"))
    if d is None or not 0 <= (today - d).days <= max_lag:
        return blank
    # 亏损置空：Wind 附表带比率列就用它，没带则退回我们行情快照里的同口径比率
    ratio = got.get("ratio")
    local = LOCAL_KEY.get(metric)
    if ratio is None and local:
        ratio = (entry.get("ratio") or {}).get(local)
    try:
        if ratio is not None and float(ratio) <= 0:
            return blank
    except (TypeError, ValueError):
        pass
    try:
        days = int(got.get("days")) if got.get("days") is not None else None
    except (TypeError, ValueError):
        days = None
    return {"pct": round(pct, 4), "days": days, "date": d.isoformat(),
            "wind_code": got.get("wind_code")}


def read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return default


def write_json(path, obj):
    """先写 .tmp 再换名：中途被杀不该留下半截 JSON（下轮会当成损坏文件从零开始铺）。"""
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True))
    os.replace(tmp, path)


def persist(items, round_id, state, idx, calls_today, today):
    write_json(LATEST_PATH, {
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "round_id": round_id, "source": "%s.%s" % (SERVER, TOOL),
        "window": u"近10年", "items": items,
    })
    write_json(STATE_PATH, dict(state, round_id=round_id, batch_index=idx,
                                day=today.isoformat(), calls_today=calls_today))


def fetch_batch(batch, today, max_lag, uniq):
    """一批（≤100 家）→ (results, calls, errors)。results[key][metric] = 值或 None。"""
    by_sent = {to_wind(e["code"], e["market"]).upper(): e["key"] for e in batch}
    codes = sorted(by_sent, key=lambda w: by_sent[w])
    found = {e["key"]: {} for e in batch}
    calls, errors = 0, []
    for n, (metrics, tmpl) in enumerate(QUERIES):
        calls += 1
        payload = call_wind(tmpl % ",".join(codes), "%s-%d" % (uniq, n))
        for metric, kw, _ in METRICS:
            if metric not in metrics:
                continue
            hits = extract_metric(payload, kw)
            if not hits:
                errors.append(u"%s 无表（问句未被识别或超 2 表）" % metric)
            elif len(hits) < len(batch):
                errors.append(u"%s 表只回 %d/%d 行" % (metric, len(hits), len(batch)))
            for wc, got in hits.items():
                key = match_key(wc, by_sent)
                if key:
                    got["wind_code"] = wc
                    found[key][metric] = got
    results = {}
    for e in batch:
        results[e["key"]] = {
            metric: (clean_value(found[e["key"]][metric], e, metric, today, max_lag)
                     if metric in found[e["key"]] else None)
            for metric, _, _ in METRICS
        }
    return results, calls, errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--calls-cap", type=int, default=CALLS_CAP, help="本次进程允许的调用数（0=不限）")
    ap.add_argument("--probe", type=int, default=0, help="只抓一批前 N 家并打印明细，不落盘")
    ap.add_argument("--batch", type=int, default=BATCH, help="每批家数（Wind 单表硬截 100）")
    ap.add_argument("--max-lag-days", type=int, default=MAX_LAG_DAYS)
    ap.add_argument("--reset", action="store_true", help="丢弃游标，从第一批重铺")
    args = ap.parse_args()

    universe = load_universe()
    if not universe:
        print("[valuation] data/index.json 里没有标的，退出")
        return 1
    if not os.path.isfile(os.path.join(SKILL_DIR, CLI)):
        # 直跑本脚本时 WIND_SKILL_DIR 由 collector/run.py 从 backend/.env 注入，缺了会退到
        # 不存在的仓库相对路径——必须在这里挡住，否则每个批次抛 FileNotFoundError 并推游标
        print("[valuation] Wind skill 不存在：%s（请用 run.py valuation 跑，或先设 WIND_SKILL_DIR）"
              % os.path.join(SKILL_DIR, CLI), flush=True)
        return 1
    if not args.probe:
        os.makedirs(OUT_DIR, exist_ok=True)  # 首轮还没有 data/valuation/，write_json 不建目录
    today = dt.date.today()
    batches = [universe[i:i + args.batch] for i in range(0, len(universe), args.batch)]
    state = read_json(STATE_PATH, {}) if not args.reset else {}
    if state.get("day") != today.isoformat():
        state = dict(state, day=today.isoformat(), calls_today=0)
    calls_today = int(state.get("calls_today") or 0)
    round_id = int(state.get("round_id") or 1)
    if args.probe:
        idx = 0
    else:
        idx = min(int(state.get("batch_index") or 0), len(batches))
        if idx >= len(batches):
            # 上一轮铺完 → 开新一轮，游标归零（latest.json 继续累计，本轮没铺到的标的留旧值）
            idx, round_id = 0, round_id + 1
    items = read_json(LATEST_PATH, {"items": {}}).get("items") or {}

    print("[valuation] %d 家 / %d 批%s，从第 %d 批起，今日已用 %d 次，cap=%d" % (
        len(universe), len(batches), u"（探针）" if args.probe else "",
        idx + 1, calls_today, args.calls_cap), flush=True)
    t0 = time.time()
    done, consecutive, stop_reason, hard_fail = 0, 0, None, False
    while idx < len(batches):
        if args.calls_cap and calls_today + CALLS_PER_BATCH > args.calls_cap:
            stop_reason = u"当日调用已达上限（%d/%d）" % (calls_today, args.calls_cap)
            break
        batch = batches[idx]
        if args.probe:
            batch = batch[:args.probe]
        try:
            results, calls, errors = fetch_batch(
                batch, today, args.max_lag_days, "val-%s-b%d" % (today.strftime("%m%d"), idx))
        except WindQuota as e:
            stop_reason = u"Wind 额度回执，本轮收尾：%s" % str(e)[:160]
            break
        except OSError as e:  # 本地进程没起来（skill 目录/node 缺失）：一次积分都没烧，别推游标
            stop_reason, hard_fail = u"Wind CLI 无法启动，游标停在第 %d 批：%s" % (
                idx + 1, str(e)[:120]), True
            break
        except Exception as e:  # noqa: BLE001
            consecutive += 1
            print("[valuation] 批 %d 失败（连续 %d）：%s" % (idx + 1, consecutive, str(e)[:200]),
                  flush=True)
            if consecutive >= FAIL_STOP:
                stop_reason, hard_fail = u"连续 %d 批失败，整轮退出" % consecutive, True
                break
            idx += 1
            time.sleep(SLEEP)
            continue
        calls_today += calls
        consecutive = 0
        covered = sum(1 for v in results.values()
                      if any(x and x["pct"] is not None for x in v.values()))
        for key, vals in results.items():
            market, _, code = key.partition(":")
            entry = items.get(key) or {"code": code, "market": market}
            dates = [v["date"] for v in vals.values() if v and v["date"]]
            entry.update({"fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
                          "round": round_id, "trade_date": max(dates) if dates else None})
            entry.update(vals)
            items[key] = entry
        done += 1
        msg = "[valuation] 批 %d/%d · %s %s..%s · %d/%d 家有分位 · 调用 %d · %.0fs" % (
            idx + 1, len(batches), batch[0]["market"], batch[0]["code"], batch[-1]["code"],
            covered, len(batch), calls_today, time.time() - t0)
        if errors:
            msg += u" · 注意: " + u"; ".join(errors[:3])
        print(msg, flush=True)
        idx += 1
        if args.probe:
            print(json.dumps([{k: v[k]} for k in ("code", "name", "trade_date", "pe", "pb", "ps")
                               for v in list(items.values())[:6]], ensure_ascii=False, indent=1))
            return 0
        persist(items, round_id, state, idx, calls_today, today)
        time.sleep(SLEEP)

    if not args.probe:
        persist(items, round_id, state, idx, calls_today, today)
    have_pb = sum(1 for v in items.values() if (v.get("pb") or {}).get("pct") is not None)
    print("[valuation] 本轮跑完 %d 批 / 用去 %d 次调用 · 累计 %d 家有 PB 分位 · %s" % (
        done, calls_today, have_pb,
        stop_reason or (u"全市场已铺完" if idx >= len(batches) else u"游标停在第 %d 批" % (idx + 1))),
        flush=True)
    return 1 if hard_fail else 0


if __name__ == "__main__":
    sys.exit(main())
