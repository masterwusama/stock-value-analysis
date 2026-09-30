# -*- coding: utf-8 -*-
"""盘后情绪日报采集:全市场快照 → 情绪状态机 → 板块效应/候选池 → JSON 工作目录。

理论出处与校准见 docs/使用说明书.md「情绪日报」一节。要点:
    - 状态机 极弱>回暖>极强>普通,阈值与转移矩阵来自 2024-01~2026-09 全样本校准
      (600 股分层抽样回测:极强日涨停股次日竞价溢价 +2.29%/胜率66%,板块效应行业 +3.64%/73%,
       炸板股 -1.59%/20%,极弱后首个普通日 +0.55%/50% 即"陷阱日");
    - 涨停判定用腾讯快照自带的涨停价/跌停价(交易所口径,含四舍五入),不用比例近似;
    - 板块效应 = 同行业涨停≥5家 且 占该行业成分≥2%(行业是证监会大类,计算机类 700 只,
      绝对家数阈值对大行业形同虚设,必须带占比);
    - 榜单池(封单额/首次/最后封板时间/连板数/炸板次数)只保留近 15 个交易日,本轮顺手
      回填窗口内的缺失日,梯队/身位维度的历史完全靠这里逐日落盘积累。

用法(在 backend/ 目录): python -m collector.run sentiment
    或直接 python collector/scripts/fetch_sentiment.py [--force]
产物: collector/data/sentiment/{daily.json, history.json, reports/YYYY-MM-DD.md, pool_*.csv}
"""
import argparse
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

# 本机若配了系统代理,腾讯行情/东财数据中心偶发被代理拦断(实测 ProxyError);
# 行情域名单独直连,不影响其它任务
os.environ["NO_PROXY"] = "*"
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import requests

BACKEND = Path(__file__).resolve().parents[2]
DATA = BACKEND / "collector" / "data"
OUT = DATA / "sentiment"
REPORTS = OUT / "reports"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
EPS = 0.001

# ---- 校准常数(2024-01~2026-09,勿随手感改;积累满 60 交易日后应换滚动分位) ----
TH = dict(r5_weak=-0.05, r5_strong=0.04, dt_panic=30, red_weak=0.25, red_strong=0.60, zt_p90=100)
TRANS = {
    "极弱": {"极弱": 0.45, "回暖": 0.25, "普通": 0.27, "极强": 0.02},
    "回暖": {"极弱": 0.09, "回暖": 0.00, "普通": 0.76, "极强": 0.15},
    "普通": {"极弱": 0.16, "回暖": 0.00, "普通": 0.73, "极强": 0.11},
    "极强": {"极弱": 0.09, "回暖": 0.00, "普通": 0.42, "极强": 0.49},
}
# 板块效应行业涨停股的次日竞价溢价档位(校准样本实测分层)
PREMIUM = {"极强": "+3.6%(胜率73%)", "普通": "+2.5%(67%)", "回暖": "+2.0%(60%)", "极弱": "+2.2%(63%)"}


def _get(url, retries=3, timeout=15):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            r.encoding = "gbk"
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 + 2 * i)
    raise last


def load_universe():
    """宇宙与行业口径唯一来自 index.json(A股),与列表页同源。"""
    idx = json.loads((DATA / "index.json").read_text(encoding="utf-8"))
    rows = [c for c in idx["companies"] if c.get("market") == "A"]
    # 腾讯接口要交易所前缀:A 股宇宙含 339 只北交所(4/8/92 开头,前缀 bj)——老写法
    # 只认 sh/sz,北交所快照拉不到被整只丢弃,红盘率/涨停数少了它们而行业分母仍计
    # (板块效应被系统性压低)。前缀规则统一复用 universe.exchange_symbol。
    import universe as uni_mod
    uni = [{"code": c["code"], "industry": c.get("industry") or "未知",
            # name 不带(腾讯快照 field1 自带,merge 会撞列)
            "tc": uni_mod.exchange_symbol(c["code"])}
           for c in rows]
    sizes = {}
    for u in uni:
        sizes[u["industry"]] = sizes.get(u["industry"], 0) + 1
    return uni, sizes


def fetch_quotes(codes):
    """腾讯批量收盘快照:涨停价/跌停价(47/48)、高低价(33/34)、流通市值(44)直接给。"""
    fields = {1: "name", 3: "price", 4: "prev_close", 5: "open", 6: "volume", 30: "ts",
              31: "chg", 32: "chg_pct", 33: "high", 34: "low", 37: "amount_wan",
              38: "turnover", 44: "float_mcap_yi", 45: "mcap_yi", 47: "limit_up", 48: "limit_dn"}
    recs = []
    for i in range(0, len(codes), 60):
        chunk = codes[i:i + 60]
        txt = _get("https://qt.gtimg.cn/q=" + ",".join(chunk)).text
        for line in txt.strip().split(";"):
            line = line.strip()
            if '="' not in line:
                continue
            f = line.split('="', 1)[1].rstrip('";').split("~")
            if len(f) < 49 or not f[3]:
                continue
            rec = {v: f[k] for k, v in fields.items()}
            rec["code"] = f[2]
            recs.append(rec)
        time.sleep(0.12)
    import pandas as pd
    q = pd.DataFrame(recs)
    for c in ("price", "prev_close", "open", "high", "low", "chg_pct", "amount_wan",
              "turnover", "float_mcap_yi", "mcap_yi", "limit_up", "limit_dn", "volume"):
        q[c] = pd.to_numeric(q[c], errors="coerce")
    q["snap_date"] = q["ts"].str[:8]
    return q.drop_duplicates("code")


def fetch_pools(yyyymmdd):
    """东财榜单池:封单额/封板时间/连板数等富信息,日线快照里没有。"""
    import akshare as ak
    pools = {}
    for name, fn in (("zt", ak.stock_zt_pool_em), ("dt", ak.stock_zt_pool_dtgc_em),
                     ("zha", ak.stock_zt_pool_zbgc_em)):
        try:
            pools[name] = fn(date=yyyymmdd)
        except Exception as e:  # noqa: BLE001 池子挂了不拦日报主体,只是少富信息
            print(f"[sentiment] {name} pool 拉取失败: {e}", flush=True)
            pools[name] = None
        time.sleep(0.4)
    return pools


def backfill_pools(days=15):
    """榜单池只留近 15 个交易日:补齐窗口内缺过的日子,断了别超两周。"""
    import akshare as ak
    done = {p.stem.split("_")[-1] for p in OUT.glob("pool_*.csv")}
    idx = ak.stock_zh_index_daily(symbol="sz399303")
    got = []
    for d in [x.replace("-", "") for x in idx["date"].astype(str)][-days - 2:]:
        if d in done:
            continue
        pools = fetch_pools(d)
        if any(v is not None and len(v) for v in pools.values()):
            for k, v in pools.items():
                if v is not None:
                    v.to_csv(OUT / f"pool_{k}_{d}.csv", index=False, encoding="utf-8-sig")
            got.append(d)
    return got


def build(quotes, uni, sizes, r5, pool_zt=None):
    """状态机 + 两层板块效应(大类/细分) + 三个名单。"""
    q = quotes[(quotes["volume"] > 0) & (quotes["price"] > 0) & (quotes["prev_close"] > 0)].copy()
    lim = q["price"] >= q["limit_up"] - EPS
    limdn = q["price"] <= q["limit_dn"] + EPS
    yz = (q["high"] - q["low"]).abs() <= 0.005
    zha = (q["high"] >= q["limit_up"] - EPS) & ~lim
    ind = dict(stocks=int(len(q)), red_rate=float((q["price"] > q["prev_close"]).mean()),
               zt_count=int(lim.sum()), zha_count=int(zha.sum()),
               dt_count=int(limdn.sum()), yz_dt_count=int((limdn & yz).sum()))
    ind["zha_rate"] = ind["zha_count"] / max(1, ind["zt_count"] + ind["zha_count"])

    # 优先级即判定顺序:极弱 > 极强 > 普通。「回暖」是转折确认、当日盘后判不出
    # (它是"昨日极弱+今日涨"的读法),由 main 里用 history 推导后写回
    if (r5 <= TH["r5_weak"]) or ind["yz_dt_count"] >= 5 or \
       (ind["dt_count"] >= TH["dt_panic"] and ind["red_rate"] < TH["red_weak"]):
        state = "极弱"
    elif (r5 >= TH["r5_strong"]) or (ind["zt_count"] >= TH["zt_p90"] and ind["red_rate"] >= TH["red_strong"]):
        state = "极强"
    else:
        state = "普通"

    m = q.assign(_lim=lim, _zha=zha, _limdn=limdn, _yz=yz)
    import pandas as pd
    m = m.merge(pd.DataFrame(uni), on="code", how="left")
    m["industry"] = m["industry"].fillna("未知")
    lim_rows = m[m["_lim"]].copy()
    # 细分行业取涨停池自带的东财行业(比证监会大类细一档,医药拆到化学制药/医疗服务
    # /医疗器械),顺带把连板数/封单资金并进来——这两项快照里没有
    if pool_zt is not None and len(pool_zt):
        pz = pool_zt[["代码", "所属行业", "连板数", "封板资金"]].copy()
        pz.columns = ["code", "fine_industry", "streak", "seal_yuan"]
        pz["code"] = pz["code"].astype(str)
        lim_rows = lim_rows.merge(pz, on="code", how="left")
    else:
        for c in ("fine_industry", "streak", "seal_yuan"):
            lim_rows[c] = None
    # ①大类:证监会行业,≥5家且≥成分2%(大类粒度差异大,计算机类700家,
    # 绝对家数对大行业形同虚设,必须带占比)
    eff = []
    for ind_name, g in lim_rows.groupby("industry"):
        size = sizes.get(ind_name, 0)
        if len(g) >= 5 and size and len(g) / size >= 0.02:
            eff.append({"industry": ind_name, "limit_count": int(len(g)),
                        "share_of_industry": round(len(g) / size * 100, 2),
                        "amount_yi": round(float(g["amount_wan"].sum()) / 1e4, 1)})
    eff.sort(key=lambda x: -x["limit_count"])
    eff_names = {e["industry"] for e in eff}
    # ②细分:东财行业,≥3家即算(细分盘子小,全市场百余只涨停里同细分扎堆3家
    # 已经是聚集信号;课程口径的新概念效应本质就是这个粒度)
    sectors_fine = []
    fz = lim_rows[lim_rows["fine_industry"].notna()]
    for ind_name, g in fz.groupby("fine_industry"):
        if len(g) >= 3:
            sectors_fine.append({"industry": ind_name, "limit_count": int(len(g)),
                                 "share_of_zt": round(len(g) / max(1, ind["zt_count"]) * 100, 1),
                                 "max_streak": int(g["streak"].max()),
                                 "seal_yi": round(float(g["seal_yuan"].sum()) / 1e8, 1)})
    sectors_fine.sort(key=lambda x: -x["limit_count"])
    fine_names = {e["industry"] for e in sectors_fine}
    cand = lim_rows[~lim_rows["_yz"] &
                    (lim_rows["industry"].isin(eff_names) | lim_rows["fine_industry"].isin(fine_names))]
    cand = cand.sort_values("amount_wan", ascending=False).head(50)
    blacklist = m[m["_zha"]].sort_values("amount_wan", ascending=False).head(50)
    watch = m[m["_limdn"]].sort_values("amount_wan", ascending=False).head(30)

    import math

    def slim(d, cols):
        out = []
        for _, r in d.iterrows():
            row = {}
            for k in cols:
                v = r.get(k)
                if v is None or (isinstance(v, float) and math.isnan(v)):
                    # NaN 必须拦在源头:json.dumps 会写出裸 NaN,MySQL JSON 列拒收
                    row[k] = None
                elif isinstance(v, float):
                    row[k] = round(v, 2)
                else:
                    row[k] = v
            out.append(row)
        return out

    payload = {
        "sectors": eff,
        "sectors_fine": sectors_fine,
        "candidates": slim(cand, ["code", "name", "industry", "fine_industry", "streak",
                                  "price", "chg_pct", "amount_wan", "float_mcap_yi"]),
        "blacklist": slim(blacklist, ["code", "name", "price", "chg_pct", "high", "limit_up"]),
        "watch_dt": slim(watch, ["code", "name", "price", "limit_dn", "high", "low"]),
    }
    return state, ind, payload


def r5_gz2000(snap_date=None):
    """国证2000 五日累计 = 课程口径的"平均股价温度"。

    新浪日频在收盘后有延迟(实测 15:35 仍无当日数据),拿最新一根直接算等于
    用昨天的温度判今天(2026-09-22 实踩:9/21、9/22 两天 R5 同为 +4.30%)。
    因此:双源轮询,最新日期必须等于快照日;都不行就用腾讯实时快照的指数
    现价(15:00 后即收盘价)补最后一根,快照时间戳还对不上才报错。
    """
    import akshare as ak
    need = snap_date.replace("-", "") if snap_date else None
    for fetch in (lambda: ak.stock_zh_index_daily(symbol="sz399303"),
                  lambda: ak.stock_zh_index_daily_tx(symbol="sz399303")):
        try:
            d = fetch()
            dates = d["date"].astype(str).str.replace("-", "")
            if need is None or dates.iloc[-1] == need:
                c = d["close"].astype(float).tolist()
                return c[-1] / c[-6] - 1
        except Exception:  # noqa: BLE001 单源挂了换下一个
            continue
    r = _get("https://qt.gtimg.cn/q=sz399303", retries=2)
    f = r.text.split('="')[1].split("~")
    if need and f[30][:8] != need:
        raise RuntimeError(f"指数数据落后:最新{f[30][:8]}≠快照{need},R5 拒绝用旧值")
    d = ak.stock_zh_index_daily(symbol="sz399303")
    c = d["close"].astype(float).tolist()
    return float(f[3]) / c[-6] - 1


def render_md(date_s, state, prev_state, ind, r5, trans, trap, payload):
    P = PREMIUM.get(state, PREMIUM["普通"])
    L = [f"# 情绪日报 {date_s}", "",
         f"状态:**{state}**(昨日:{prev_state or '—'})|"
         f"红盘率{ind['red_rate']*100:.0f}% 涨停{ind['zt_count']} 炸板率{ind['zha_rate']*100:.0f}% "
         f"跌停{ind['dt_count']} 一字跌停{ind['yz_dt_count']} | 国证2000 R5={r5*100:+.2f}%", ""]
    if trap:
        L += ["> **陷阱日**:昨日极弱、今日转普通——该形态次日涨停溢价实测 +0.55%/胜率50%,全局禁止隔夜。", ""]
    L += ["## 明日预案(转移概率)"]
    L += [f"- P(明日{k})={v*100:.0f}%" for k, v in sorted(trans.items(), key=lambda x: -x[1])]
    L += ["", "## 细分板块效应(东财行业,涨停≥3家)"]
    if payload["sectors_fine"]:
        L += ["| 细分行业 | 涨停家数 | 占涨停% | 最高连板 | 封单合计(亿) |",
              "|---|---:|---:|---:|---:|"]
        L += [f"| {e['industry']} | {e['limit_count']} | {e['share_of_zt']} | "
              f"{e['max_streak']} | {e['seal_yi']} |" for e in payload["sectors_fine"]]
    else:
        L.append("- 无")
    L += ["", f"## 大类板块效应(证监会行业,涨停≥5家且占行业≥2%,次日溢价档 {P})"]
    if payload["sectors"]:
        L += ["| 行业 | 涨停家数 | 占行业% | 成交额(亿) |", "|---|---:|---:|---:|"]
        L += [f"| {e['industry']} | {e['limit_count']} | {e['share_of_industry']} | {e['amount_yi']} |"
              for e in payload["sectors"]]
    else:
        L.append("- 无(散点行情,谨慎追高)")
    L += ["", f"## 候选池(两类板块效应行业内自然涨停 {len(payload['candidates'])} 只)",
          "| 代码 | 名称 | 大类行业 | 细分行业 | 连板 | 收盘 | 涨跌幅% | 成交额(万) | 流通市值(亿) |",
          "|---|---|---|---|---:|---:|---:|---:|---:|"]
    L += [f"| {c['code']} | {c['name']} | {c['industry']} | {c.get('fine_industry') or '-'} | "
          f"{c.get('streak') if c.get('streak') is not None else '-'} | {c['price']} | {c['chg_pct']} | "
          f"{c['amount_wan']:.0f} | {c['float_mcap_yi']} |" for c in payload["candidates"][:20]]
    L += ["", f"## 炸板黑名单(次日禁入,实测-1.6%)共 {ind['zha_count']} 只",
          "、".join(b["name"] for b in payload["blacklist"][:15])]
    L += ["", f"## 跌停观察(一字跌停 {ind['yz_dt_count']} 只,关注打开)",
          "、".join(w["name"] for w in payload["watch_dt"][:15])]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="休市/已生成也强制重算")
    a = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(exist_ok=True)
    hist_path = OUT / "history.json"
    history = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else []
    done = {h["date"] for h in history}

    uni, sizes = load_universe()
    quotes = fetch_quotes([u["tc"] for u in uni])
    snap = quotes["snap_date"].mode().iloc[0]
    today = date.today().strftime("%Y%m%d")
    if snap != today and not a.force:
        print(f"[sentiment] 今日({today})休市,快照停在 {snap},跳过")
        return 0
    date_s = f"{snap[:4]}-{snap[4:6]}-{snap[6:]}"
    if date_s in done and not a.force:
        print(f"[sentiment] {date_s} 已生成,跳过(--force 重算)")
        return 0

    # 榜单池先落盘再建日报:细分板块效应就来自池子的「所属行业」字段
    pool_zt = fetch_pools(snap)["zt"]
    if pool_zt is not None:
        pool_zt.to_csv(OUT / f"pool_zt_{snap}.csv", index=False, encoding="utf-8-sig")
    r5 = r5_gz2000(snap)
    state, ind, payload = build(quotes, uni, sizes, r5, pool_zt)
    # 排除当日自身:--force 重跑最新一天时不能自引用成 prev_state
    prior = [h for h in history if h["date"] != date_s]
    prev_state = prior[-1]["state"] if prior else None
    trans = TRANS.get(state, TRANS["普通"])
    trap = prev_state == "极弱" and state == "普通"
    md = render_md(date_s, state, prev_state, ind, r5, trans, trap, payload)

    daily = {"date": date_s, "state": state, "prev_state": prev_state,
             "indicators": ind, "r5": round(r5, 4), "transitions": trans,
             "trap": trap, "premium_hint": PREMIUM.get(state, PREMIUM["普通"]),
             **payload, "report_md": md,
             "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    (OUT / "daily.json").write_text(json.dumps(daily, ensure_ascii=False, indent=1), encoding="utf-8")

    entry = {"date": date_s, "state": state, "prev_state": prev_state,
             "red_rate": round(ind["red_rate"], 4), "zt": ind["zt_count"],
             "zha_rate": round(ind["zha_rate"], 4), "dt": ind["dt_count"],
             "yz_dt": ind["yz_dt_count"], "r5": round(r5, 4), "trap": trap}
    history = [h for h in history if h["date"] != date_s] + [entry]
    history.sort(key=lambda x: x["date"])
    hist_path.write_text(json.dumps(history[-500:], ensure_ascii=False), encoding="utf-8")
    (REPORTS / f"{date_s}.md").write_text(md, encoding="utf-8")

    missed = backfill_pools()
    if missed:
        print(f"[sentiment] 榜单池回填: {','.join(missed)}", flush=True)
    print(f"[sentiment] {date_s} 状态={state} 涨停{ind['zt_count']} "
          f"板块效应{len(payload['sectors'])} 候选{len(payload['candidates'])} "
          f"黑名单{ind['zha_count']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
