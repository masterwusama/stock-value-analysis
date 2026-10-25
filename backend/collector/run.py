# -*- coding: utf-8 -*-
"""P5 采集任务驱动:跑原采集脚本(产 JSON)→ 回灌 MySQL(import_legacy --no-clean 增量 upsert)。

用法(在 backend/ 目录):
    python -m collector.run stock     # 日更：腾讯批量刷全市场估值 + 定增/回购明细 → 回灌（分钟级）
    python -m collector.run deep      # 深抓：全市场财务/报告重抓（--resume 增量，数小时）
    python -m collector.run agro      # fetch_prices → 回灌（生意社/中农立华价格，不碰 Wind）
    python -m collector.run edb       # 行业 EDB 量价（免费源:新浪期货主力/东财宏观,周日 20:00 自动,云端可跑）
    python -m collector.run events    # Wind 事件(一次性/手动,不进每日调度) → 回灌
    python -m collector.run etf       # ETF 综合评估(成份/指数估值序列/净值) → 回灌
    python -m collector.run import    # 跳过抓取,仅手动触发一次回灌
尾部参数透传给该 job 首个采集脚本(传了就整体替代该 job 的默认参数),
如 `python -m collector.run stock --codes 600519`。

设计对齐原 GitHub Actions workflow:
    - 抓取步骤失败/超时被杀也继续回灌+调仓已落盘数据(if: always() 语义)
    - 引擎按交易日幂等跳重,同日重复跑不产生重复调仓
    - 每次运行写 etl_job_log(替代 Actions 运行记录页)
"""
import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
COLLECTOR = BACKEND / "collector"
SCRIPTS = COLLECTOR / "scripts"
AGRO_SCRIPTS = COLLECTOR / "agro-price" / "scripts"
AGRO_DATA = COLLECTOR / "agro-price" / "data"

# 独立进程入口(调度器直接调本模块),需自行加载 backend/.env
try:
    from dotenv import load_dotenv
    load_dotenv(BACKEND / ".env")
except ImportError:
    pass

# Wind MCP skill(node cli.mjs + 密钥)不在本仓库,目录由 backend/.env 的 WIND_SKILL_DIR 指定
WIND_SKILL_DIR = os.getenv("WIND_SKILL_DIR")


def _stale_note(code):
    """fetch_prices 用 exit=3 专指“发现新增断档品种”，把名单一起写进 etl_job_log。

    不加这一句，job 日志里就只有 `fetch_prices.py exit=3`，想知道红了的是哪几个
    品种还得去翻 products.json 或 scheduler.out.log —— 而“谁断了”恰恰是要人看的部分。
    """
    if code != 3:
        return None
    try:
        ids = json.loads((AGRO_DATA / "products.json")
                         .read_text(encoding="utf-8")).get("stale_reported") or []
    except Exception:  # noqa: BLE001 名单读不到不该影响失败记录本身
        return "stale=?"
    return "stale=" + ",".join(ids) if ids else None


# job → 采集步骤序列(每项 cwd/脚本);全部步骤跑完后统一回灌
JOBS = {
    # stock ：每日分钟级（只刷腾讯批量行情→估值/价格，不重抓财务）
    # fetch_actions 两页表全量约 25 秒，且与行情无依赖，跟在后面跑；放在本 job 而不是独立
    # job，是因为它没有自己的数据目录游标，每天都会整表重取——独立时刻表只会多一次重复抓取。
    # _refresh_scores 排在最后：定增摊薄那一项的输入就是上一步那份整表快照，日更全市场重算
    # 实测 20 秒（只读本地 companies/，不发请求），跑完 `--only-fresh` 那条回灌顺手把新分写进
    # score_daily（`import_index_snapshot` 本来就干这张表），所以不需要再补一遍导入。
    # 抢不到 .fetch.lock 时它以 exit=2 退出，这一轮日志会红一次——那是「今天没刷分」的正确
    # 记录，不是把分刷坏了。
    "stock": [(SCRIPTS, "fetch_data.py"), (SCRIPTS, "fetch_actions.py"),
              (SCRIPTS, "_refresh_scores.py")],
    # deep  ：全市场财务重抓（季报到账后/周末跑一次，数小时）
    # 末尾同样补一次重算：fetch_data 只给本轮真爬过的公司算分，带 --resume/--max-age 时
    # 大部分公司是跳过的，改了评分公式就得靠这一步铺满全市场。
    "deep": [(SCRIPTS, "fetch_data.py"), (SCRIPTS, "_refresh_scores.py")],
    # agro 只跑生意社价格。行业 EDB 历史上是本机 Wind 客户端 CLI(要登录、耗积分),属于
    # 自动链管不到的外部依赖,2026-09-03 09:05 那轮 agro 曾被 fetch_edb 带成 failed(价格
    # 落盘/回灌照常,但日更状态被 Wind 侧带红)。2026-10-02 起 edb 全量换免费 HTTP 源
    # (新浪期货主力连续/东财宏观,收缩为 8 指标,见 fetch_edb.py 头注),独立成周日 job
    # 自动跑、云端也能跑;仍不并进 agro 链——周/月聚合的低频序列,日更无意义。
    "agro": [(AGRO_SCRIPTS, "fetch_prices.py")],
    "edb": [(AGRO_SCRIPTS, "fetch_edb.py")],
    "events": [(SCRIPTS, "fetch_events.py")],
    # valuation：PE/PB/PS 十年分位。2026-10-04 起默认免费源(东财 datacenter,
    # fetch_valuation_em,全 A 一轮约 110 分钟,调度 03:35);Wind 路径保留为可选
    # (--source wind 切回,积分/游标/批上限逻辑原样,唯一还能刷港股的路径)。
    # 所以是独立 job、每天固定跑一次，不并进 stock/deep。
    "valuation": [(SCRIPTS, "fetch_valuation.py")],
    # sentiment：盘后情绪日报（腾讯批量快照+东财榜单池，约 2~4 分钟，不碰 Wind）。
    # 16:30 独立时段：收盘后 90 分钟,越过指数日频的更新延迟窗口,且与 stock(16:05) 错开；
    # 休市与当日去重采集器自兜
    "sentiment": [(SCRIPTS, "fetch_sentiment.py")],
    # case：证监会立案/处罚公告（巨潮全文检索，全市场聚合翻页 ≈1 分钟，不碰 Wind）。
    # 周更：立案是低频事件，且回答「正在立案」只看近 2 年的公告面；回灌走 --only-case
    # （case_event + 单刷 score_daily——立案抬 ST 高危旗发生在 score 行重建里）
    "case": [(SCRIPTS, "fetch_case.py")],
    # etf：ETF 综合评估（f10 基础/持仓 + 国证官方成份/指数日线 + 腾讯行情/净值 +
    # 估值自算序列累积，每只 ≈15 秒）。回灌走 --only-etf；17:35 独立时段排在
    # stock(16:05/22:05) 与 sentiment(16:30) 之后，保证成份股联动用的是当日新评分。
    "etf": [(SCRIPTS, "fetch_etf.py")],
    "import": [],
}

# 各 job 首脚本的默认参数（调度器只传 job 名，全市场模式在此定版）。
# CLI 透传参数会整体替代默认参数（如 `run.py stock --codes 600519`）。
JOB_DEFAULTS = {
    "stock": ["--all-market", "--snapshot-only", "--quiet"],
    # --hk-connect：港股通名单（≈621 只）跟着深抓一起刷，否则只有今天这一次手动跑，
    # 下周港股财务数据就开始变旧。名单拉不到时 hk_connect_targets 空列表降级，不阻断 A 股
    # --us-indexes：美股五大指数成分并集（≈765 只），降级行为同上；
    # --audit-scope full：年报 + 半年报都解析，净现金/市值 的存款构成只在这两份里披露，
    # 只解析年报会让口径滞后到下一份年报。附注已抽到的公司走缓存不重下，稳态成本可控。
    "deep": ["--all-market", "--hk-connect", "--us-indexes", "--workers", "4",
             "--resume", "--max-age", "6", "--chunk", "400", "--flush-every", "200",
             "--audit-scope", "full", "--quiet"],
    # 免费源(em)是默认路径且无需默认参数;Wind 路径的批上限默认在 main() 的 dispatch 里
    # 补(--source wind 时自动追加,当年 56 次/天 ≈780 积分的预算纪律不变)
    "valuation": [],
}

# 回灌面：日更只动了 index.json（行情/评分），财务文件按 mtime 增量；
# 深抓回灌全量（财务期表与报告都要重写）
IMPORT_DEFAULTS = {
    "stock": ["--only-fresh", "26"],
    # 情绪日报只灌自己两张表;不带这杆会跑成全量导入(实测多花 5 分钟)
    "sentiment": ["--only-sentiment"],
    # 立案公告：先灌 case_event 再单刷 score_daily（旗在 score 行重建里贴）
    "case": ["--only-case"],
    # ETF 综合评估六表（序列档全量重放幂等，不追删）
    "etf": ["--only-etf"],
}


# 全量抓取持锁时跳过调度（fetch_data 的 index.json 为全量重写,两进并行会互盖条目）
FETCH_LOCK = COLLECTOR / "data" / ".fetch.lock"
FETCH_LOCK_STALE = 45 * 60

# 挂死护栏:单脚本墙钟上限(秒)。采集脚本卡死(网络栈/CLI 挂住)会永久占住 job,
# etl_job_log 停在 running 无从分辨死活。上限取历史最长一轮(deep 4.2h、stock 10min、
# import 4min、valuation 3min)再放宽约一倍;events 手动跑全市场,量级同 deep。
# agro 早上 10min、晚间 100ppi 变慢后 30min 跑不完(9-18/9-19 连续两晚被杀,自然时长
# 未知),按最坏估计放宽到 2h;21:05 起跑至多到 23:05,与 stock(22:05)无锁冲突。
SCRIPT_TIMEOUT = {
    "stock": 2 * 3600,
    "deep": 8 * 3600,
    "events": 8 * 3600,
    "valuation": 3 * 3600,
    "import": 2 * 3600,
    "edb": 3600,
    "sentiment": 1800,
    "agro": 2 * 3600,
    "case": 1800,   # 全量回填 28 页 ≈1 分钟，增量更少；放宽到与 sentiment 同档
    "etf": 900,     # 每只 ≈15 秒（国证月度快照+3 年持仓+序列），多只线性涨,留足余量
}


def _pid_alive(pid: int) -> bool:
    """判活（与 fetch_data._pid_alive 同口径）：Windows 下 os.kill 会把 OpenProcess
    失败包装成 SystemError,不能用 except OSError 兜住。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            return bool(ok) and code.value == 259
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False
    except Exception:
        return True


def fetch_locked() -> bool:
    """data/.fetch.lock 是否被一个还活着的抓取进程持有。

    锁 mtime 由 fetch_data 的看门线程每 5 分钟刷新（与 index flush 解耦），所以
    「PID 报活 + 心跳超时」只剩 PID 被无关进程复用一种解释，按无锁放行；
    fetch_data.acquire_lock 同一口径，且用 O_EXCL 原子建锁消掉 check-then-write 竞态。
    """
    try:
        pid = int(FETCH_LOCK.read_text(encoding="utf-8").strip() or 0)
        age = time.time() - FETCH_LOCK.stat().st_mtime
    except (OSError, ValueError):
        return False
    if age >= FETCH_LOCK_STALE:
        return False
    return _pid_alive(pid)


def _env():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"  # 子脚本中文 print 防 GBK 断管
    if WIND_SKILL_DIR:  # 未配时不塞 None,让子脚本走自己的仓库相对兜底
        env["WIND_SKILL_DIR"] = WIND_SKILL_DIR
    return env


def _run_script(cwd, script, extra, timeout=None):
    print(f"[collector] >>> {script} {' '.join(extra)} (cwd={cwd})", flush=True)
    # timeout 到点杀掉直接子进程:fetch_data 的进程池 worker 读到管道 EOF 会自行退出
    subprocess.check_call(
        [sys.executable, script, *extra], cwd=str(cwd), env=_env(), timeout=timeout
    )


def _import_db(job=""):
    """回灌 JSON 工作目录 → MySQL(增量 upsert,不清空)。"""
    env = _env()
    env["LEGACY_DATA_DIR"] = str(COLLECTOR)
    extra = IMPORT_DEFAULTS.get(job, [])
    print("[collector] >>> 回灌 MySQL(import_legacy --no-clean %s)" % " ".join(extra), flush=True)
    subprocess.check_call(
        [sys.executable, "-X", "utf8", "-m", "scripts.import_legacy", "--no-clean", *extra],
        cwd=str(BACKEND), env=env, timeout=SCRIPT_TIMEOUT["import"],
    )


def main():
    ap = argparse.ArgumentParser(description="采集任务驱动")
    ap.add_argument("job", choices=list(JOBS), help="要跑的 job")
    ap.add_argument("extra", nargs=argparse.REMAINDER, help="透传给首个采集脚本的参数")
    args = ap.parse_args()
    extra = [a for a in args.extra if a != "--"]

    # 延迟导入:collector.run 在 backend cwd 下 app 包可直接 import
    sys.path.insert(0, str(BACKEND))
    from app.db import SessionLocal
    from app.models import EtlJobLog

    db = SessionLocal()
    log = EtlJobLog(job_name=args.job, started_at=datetime.now(), status="running")
    db.add(log)
    db.commit()

    status, messages = "success", []
    steps = JOBS[args.job]
    # valuation 双源 dispatch:默认免费源(东财);--source wind 整体切回 Wind 原路径
    # (含港股过筛池;当年 56 次/天的积分预算作为默认参数自动补上,显式传了就不重复加)
    if args.job == "valuation":
        wind = any(v == "wind" for k, v in zip(extra, extra[1:]) if k == "--source")
        if not wind:
            steps = [(SCRIPTS, "fetch_valuation_em.py")]
        elif "--calls-cap" not in extra:
            extra = extra + ["--calls-cap", "56"]
    if any(s == "fetch_data.py" for _, s in steps) and fetch_locked():
        # 全量抓取进行中：不跑采集也不回灌（避开与持锁进程抢写 index.json）
        msg = "跳过：全量抓取持锁中（data/.fetch.lock）"
        print(f"[collector] {msg}", flush=True)
        log.finished_at = datetime.now()
        log.status = "success"
        log.message = msg
        db.commit()
        db.close()
        return 0
    for i, (cwd, script) in enumerate(steps):
        try:
            # 仅首个脚本吃参数：尾部透传参数优先，其次该 job 的默认参数；后续脚本一律空参
            # （fetch_data 的 --all-market/--snapshot-only 传给 fetch_actions 会被 argparse 拒掉）
            argv = (extra or JOB_DEFAULTS.get(args.job, [])) if i == 0 else []
            _run_script(cwd, script, argv, timeout=SCRIPT_TIMEOUT.get(args.job))
        except subprocess.TimeoutExpired:
            # 挂死被护栏击杀:记 failed 但不 traceback(栈在子进程日志里),后续脚本不跑,
            # 已落盘数据照旧回灌——与 CalledProcessError 同一条 always() 语义
            status = "failed"
            limit = SCRIPT_TIMEOUT.get(args.job)
            messages.append(f"{script} 挂死护栏:超时被杀(>{limit}s)")
            print(f"[collector] {script} 超过 {limit}s 被杀(挂死护栏)", flush=True)
            break
        except subprocess.CalledProcessError as e:
            status = "failed"
            messages.append(f"{script} exit={e.returncode}")
            note = _stale_note(e.returncode)
            if note:
                messages.append(note)
            if script == "_refresh_scores.py" and e.returncode == 3:
                messages.append("有公司 compute_scores 失败保留旧分（家数/异常看本轮日志）")
            traceback.print_exc()
            break  # 抓取失败不再跑链上后续脚本,但仍回灌已落盘数据
        except Exception:
            status = "failed"
            messages.append(f"{script} crashed")
            traceback.print_exc()
            break
    # always() 语义:部分失败也回灌已落盘 JSON(避免又一次整天为空)
    try:
        _import_db(args.job)
    except Exception as e:
        status = "failed"
        messages.append(f"import_db failed: {e}")

    # 口径改了只动一侧是这套双实现架构最安静的坏法(2026-09-30 自检项)：公式探针
    # 秒级(0.3s/79 组合)，挂在动了 fetch/评分的 job 尾上当闸门；红了一条独立
    # etl_job_log(job_name=parity)，status.ps1 的运维自检会盯它——不把采集 job 本身
    # 染红，采集成功与口径漂移是两个信号。
    if args.job in ("stock", "deep"):
        try:
            subprocess.run(
                [sys.executable, "-X", "utf8", str(SCRIPTS / "_score_formula_check.py")],
                cwd=str(BACKEND), timeout=300, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            name = getattr(e, "returncode", None)
            note = f"exit={name}" if name else str(e)[:200]
            db.add(EtlJobLog(job_name="parity", started_at=datetime.now(),
                             finished_at=datetime.now(), status="failed",
                             message=f"{args.job} 链尾公式探针未过({note})；评分双实现口径漂移，跑 _score_check.py 定位"))
            db.commit()
            print(f"[collector] 公式探针未过({note})——已记 parity failed", flush=True)

    log.finished_at = datetime.now()
    log.status = status
    log.message = "; ".join(messages) or None
    db.commit()
    secs = (log.finished_at - log.started_at).total_seconds()
    print(f"[collector] {args.job} -> {status} ({secs:.0f}s)", flush=True)
    db.close()
    return 0 if status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
