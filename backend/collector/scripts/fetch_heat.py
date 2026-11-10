# -*- coding: utf-8 -*-
"""盘后资金热度采集:东财个股主力资金流排行 + 概念板块排行 + 热门概念成分 → JSON 工作目录。

为小微盘投机池(#/microcap)补齐「资金热度/热门题材」两个维度——涨停生态
(连板/封单/炸板)由 sentiment job 的榜单池覆盖,资金流与概念排行是仅剩缺口。
三步全走东财免费源(采集端只落时点工件,页面值一律 API 读时合成,与 sentiment
同一套「防口径漂移」纪律):
    1. stock_individual_fund_flow_rank ×2(今日/5日):全市场主力净流入,各一次一份;
    2. stock_sector_fund_flow_rank(概念资金流):板块排行;融资融券/深股通/重仓系
       是成份通道不是题材,按名单+规则滤掉再取 top N;
    3. 热门概念 top N 的成分名单(stock_board_concept_cons_em):题材→个股映射,
       供池内股挂概念徽章/热度加分。成分表慢变,滚动合并进 members.json——
       7 天内拉过的板直接复用旧映射,新进榜的板当日拉,断了保留旧值不空写。

产物( collector/data/heat/ ):
    flow_{YYYYMMDD}.json    个股资金流快照(全市场,code 索引)
    boards_{YYYYMMDD}.json  概念板块排行快照(过滤后)
    members.json            概念→成分 滚动映射(带 updated_at)
    latest.json             指针:最新交易日/各件齐全度(API 降级标注读这里)

幂等:当日 flow/boards 已存在即跳过;休市日(腾讯参考快照的成交日不是今天)跳过;
15:30 前手动跑只拿盘中数据,同样跳过(--force 强制)。清理:快照只留近 20 个
交易日,与榜单池 15 日窗同量级。
用法(在 backend/ 目录): python -m collector.run heat
    或直接 python collector/scripts/fetch_heat.py [--force] [--top 15]
"""
import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

# 本机若配了系统代理,腾讯行情/东财数据中心偶发被代理拦断(fetch_sentiment 同款处理)
os.environ["NO_PROXY"] = "*"
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

import requests

BACKEND = Path(__file__).resolve().parents[2]
OUT = BACKEND / "collector" / "data" / "heat"
KEEP_DAYS = 20          # 快照滚动保留窗(个交易日文件数)
MEMBER_TTL_DAYS = 7     # 成分映射复用窗:7 天内拉过的板不重拉
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# 通道类伪板块:是成份/融资通道不是题材,进热榜只会污染排名。名单+规则双保险:
# 含「重仓」的(基金/社保/QFII 重仓)与含「股通」的一律滤掉;ST板块保留——
# 摘帽行情本身就是小微盘的投机题材之一
CHANNEL_BOARDS = {
    "融资融券", "转融券标的", "深股通", "沪股通", "标普道琼斯中国", "富时罗素",
    "MSCI中国", "创业板综", "深成500", "沪深300_样本", "中证500成份", "AB股", "AH股", "B股",
}


def _is_channel(name: str) -> bool:
    return name in CHANNEL_BOARDS or "重仓" in name or "股通" in name


def _retry(fn, tries=3, wait=2.0, label=""):
    """东财接口偶发限流:指数退避重试,穷尽后抛给调用方按降级纪律处理。"""
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if i < tries - 1:
                time.sleep(wait * (i + 1))
    raise RuntimeError(f"{label} 重试 {tries} 次仍失败: {last}")


def _atomic_write(path: Path, payload: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def _session_date() -> str:
    """最近成交日(YYYYMMDD):腾讯参考快照(上证指数)的时间戳字段是交易所口径,
    休市日它停在上一交易日——heat 因此自然休市跳过,不依赖 sentiment 先跑。"""
    txt = requests.get("https://qt.gtimg.cn/q=sh000001", headers=UA, timeout=15).text
    ts = txt.split('="', 1)[1].split("~")[30]  # field30=时间 yyyymmddhhmmss,同 fetch_quotes
    return ts[:8]


def _load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def fetch_flow() -> dict:
    """个股主力资金流(今日+5日两榜合并,code 索引)。金额元,占比%。"""
    import akshare as ak

    def _one(indicator):
        df = _retry(lambda: ak.stock_individual_fund_flow_rank(indicator=indicator),
                    label=f"fund_flow_rank[{indicator}]")
        time.sleep(1.0)
        return df

    out = {}
    for ind, tag in (("今日", "t"), ("5日", "d5")):
        df = _one(ind)
        suffix = "今日" if ind == "今日" else "5日"
        for _, r in df.iterrows():
            code = str(r["代码"]).zfill(6)
            rec = out.setdefault(code, {"name": str(r["名称"]), "price": None, "chg_pct": None})
            v = r["最新价"]
            if tag == "t":
                rec["price"] = None if v in ("-", "") else float(v)
                rec["chg_pct"] = None if r["今日涨跌幅"] in ("-", "") else float(r["今日涨跌幅"])
            rec[f"inflow_{tag}"] = None if r[f"{suffix}主力净流入-净额"] in ("-", "") \
                else float(r[f"{suffix}主力净流入-净额"])
            rec[f"inflow_{tag}_pct"] = None if r[f"{suffix}主力净流入-净占比"] in ("-", "") \
                else float(r[f"{suffix}主力净流入-净占比"])
    return out


def fetch_boards(top: int) -> list:
    """概念板块排行(过滤通道类后取 top N,保持东财净流入降序原序)。"""
    import akshare as ak

    df = _retry(lambda: ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="概念资金流"),
                label="sector_fund_flow_rank")
    rows = []
    for _, r in df.iterrows():
        name = str(r["名称"])
        if _is_channel(name):
            continue
        rows.append({
            "name": name,
            "chg_pct": None if r["今日涨跌幅"] in ("-", "") else float(r["今日涨跌幅"]),
            "inflow": None if r["今日主力净流入-净额"] in ("-", "") else float(r["今日主力净流入-净额"]),
            "inflow_pct": None if r["今日主力净流入-净占比"] in ("-", "") else float(r["今日主力净流入-净占比"]),
            "top_stock": str(r.get("今日主力净流入最大股") or "") or None,
        })
    return rows[:top]


def fetch_members(board_names: list, members: dict) -> tuple:
    """热门概念→成分映射:7 天内拉过的板复用旧值,新板/过期板才发请求。
    返回 (新 members, 本次仍缺映射的板名单)——缺的板 API 端成分数亮「?」。"""
    import akshare as ak

    missing = []
    today = datetime.now().astimezone()
    for i, name in enumerate(board_names):
        old = members.get(name)
        if old:
            age = today - datetime.fromisoformat(old["updated_at"])
            if age.days < MEMBER_TTL_DAYS:
                continue
        try:
            # 成分接口是东财限流的重点照顾对象(实测排行分页拉完就被掐),
            # 退避给足:3/8/20 秒;单板挂了冷却 20 秒再走下一板,避免连环被拒
            df = _retry(lambda: ak.stock_board_concept_cons_em(symbol=name), tries=3,
                        wait=3.0, label=f"cons[{name}]")
            codes = [str(c).zfill(6) for c in df["代码"].tolist()]
            members[name] = {
                "codes": codes,
                "updated_at": today.isoformat(timespec="seconds"),
            }
            print(f"[heat] 成分 {name}: {len(codes)} 只", flush=True)
            time.sleep(1.2)
        except Exception as e:  # noqa: BLE001 单板挂了不影响整轮,保留旧映射
            print(f"[heat] 成分拉取失败 {name}: {e}", flush=True)
            if not old:
                missing.append(name)
            time.sleep(20)
    return members, missing


def _ship(fresh_files: list) -> None:
    """采集成功后把工件推到服务端(cloud VM):云端 IP 段被东财 push2 集群 TCP 层
    拉黑(2026-11-10 实测,含全部数字镜像;datacenter-web/push2ex/gtimg 不受影响),
    资金流排行只能由可达侧采集。云端 16:45 的自跑因此退化为兜底(失败不写 latest,
    不会覆盖这里推过去的真数据)。目标可用 VA_HEAT_SHIP_TARGET 覆盖;推送失败只
    打日志不失败 job——采集成功≠分发必须成功,latest 停旧时 API 有降级横幅。"""
    import shutil
    import subprocess

    target = os.environ.get(
        "VA_HEAT_SHIP_TARGET",
        "ubuntu@100.65.58.108:/opt/stock-value-analysis/backend/collector/data/heat/",
    )
    if not shutil.which("scp"):
        print("[heat] 无 scp,跳过 ship", flush=True)
        return
    found = [str(p) for p in fresh_files if Path(p).exists()]
    if not found:
        return
    try:
        r = subprocess.run(["scp", "-o", "ConnectTimeout=10", *found, target],
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            print(f"[heat] ship → {target} ({len(found)} 件)", flush=True)
        else:
            print(f"[heat] ship 失败: {r.stderr.strip()[:200]}", flush=True)
    except Exception as e:  # noqa: BLE001 分发失败不影响采集本身
        print(f"[heat] ship 异常: {e}", flush=True)


def cleanup_snapshots():
    """快照滚动清理:flow_/boards_ 各只留近 KEEP_DAYS 个文件(members/latest 永存)。"""
    for prefix in ("flow_", "boards_"):
        snaps = sorted(OUT.glob(f"{prefix}*.json"))
        for p in snaps[:-KEEP_DAYS]:
            p.unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="休市/盘中/已生成也强制重抓")
    ap.add_argument("--top", type=int, default=15, help="热门概念 top N(默认 15)")
    a = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    latest = _load_json(OUT / "latest.json", {})

    snap = _session_date()
    today = date.today().strftime("%Y%m%d")
    if snap != today and not a.force:
        print(f"[heat] 今日({today})休市,快照停在 {snap},跳过")
        return 0
    now = datetime.now()
    if now.hour < 15 or (now.hour == 15 and now.minute < 30):
        if not a.force:
            print(f"[heat] 盘中({now:%H:%M}),资金流未定盘,15:30 后再跑(--force 强制)")
            return 0
    date_compact = snap
    date_iso = f"{snap[:4]}-{snap[4:6]}-{snap[6:]}"

    flow_path = OUT / f"flow_{date_compact}.json"
    boards_path = OUT / f"boards_{date_compact}.json"
    have_snap = flow_path.exists() and boards_path.exists()
    # 当日快照已齐但成分还有缺(latest.missing_members 非空):只补成分,排行不重拉
    # ——成分接口被东财限流是常态,当日自愈窗口就在这里
    if have_snap and not a.force:
        pending = latest.get("missing_members") or []
        if not pending:
            print(f"[heat] {date_iso} 已生成且成分齐全,跳过(--force 重抓)")
            return 0
        members = _load_json(OUT / "members.json", {})
        members, missing = fetch_members(pending, members)
        _atomic_write(OUT / "members.json", json.dumps(members, ensure_ascii=False))
        _atomic_write(OUT / "latest.json", json.dumps({**latest, "missing_members": missing,
                                                       "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")},
                                                      ensure_ascii=False))
        _ship([OUT / "members.json", OUT / "latest.json"])
        print(f"[heat] {date_iso} 成分补拉完成,仍缺{len(missing)}板", flush=True)
        return 0

    flow, boards, missing = {}, [], []
    try:
        flow = fetch_flow()
        flow_path.write_text(json.dumps(flow, ensure_ascii=False), encoding="utf-8")
        print(f"[heat] 个股资金流 {len(flow)} 只", flush=True)
    except Exception as e:  # noqa: BLE001 排行挂了本轮缺资金维度,latest 标注供 API 降级
        print(f"[heat] 个股资金流拉取失败: {e}", flush=True)
    try:
        boards = fetch_boards(a.top)
        boards_path.write_text(json.dumps(boards, ensure_ascii=False), encoding="utf-8")
        print(f"[heat] 概念板块 top{len(boards)}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[heat] 概念板块拉取失败: {e}", flush=True)

    if boards:
        members = _load_json(OUT / "members.json", {})
        members, missing = fetch_members([b["name"] for b in boards], members)
        _atomic_write(OUT / "members.json", json.dumps(members, ensure_ascii=False))

    if flow or boards:
        _atomic_write(OUT / "latest.json", json.dumps({
            "date": date_iso,
            "flow": bool(flow),
            "boards": bool(boards),
            "missing_members": missing,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }, ensure_ascii=False))
    else:
        # 全失败不落 latest:保留现有(可能是 ship 推来的)状态,不然云端自跑一次
        # 失败就会把推过来的真数据盖成「源未更新」
        print(f"[heat] {date_iso} 全部拉取失败,不覆盖 latest.json", flush=True)

    fresh = [p for p in (flow_path, boards_path) if p.exists()]
    if fresh:
        _ship([*fresh, OUT / "members.json", OUT / "latest.json"])
    cleanup_snapshots()
    print(f"[heat] {date_iso} 完成:资金流{'✓' if flow else '✗'} "
          f"板块{'✓' if boards else '✗'} 成分缺{len(missing)}板", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
