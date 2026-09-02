# -*- coding: utf-8 -*-
"""P5 采集任务驱动:跑原采集脚本(产 JSON)→ 回灌 MySQL(import_legacy --no-clean 增量 upsert)。

用法(在 backend/ 目录):
    python -m collector.run stock     # 日更：腾讯批量刷全市场估值 → 调仓 → 回灌（分钟级）
    python -m collector.run deep      # 深抓：全市场财务/报告重抓（--resume 增量，数小时）
    python -m collector.run agro      # fetch_prices → fetch_edb → 回灌
    python -m collector.run events    # Wind 事件(一次性/手动,不进每日调度) → 回灌
    python -m collector.run import    # 跳过抓取,仅手动触发一次回灌
尾部参数透传给该 job 首个采集脚本(传了就整体替代该 job 的默认参数),
如 `python -m collector.run stock --codes 600519`。

设计对齐原 GitHub Actions workflow:
    - 抓取步骤失败/超时被杀也继续回灌+调仓已落盘数据(if: always() 语义)
    - 引擎按交易日幂等跳重,同日重复跑不产生重复调仓
    - 每次运行写 etl_job_log(替代 Actions 运行记录页)
"""
import argparse
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

# 独立进程入口(调度器直接调本模块),需自行加载 backend/.env
try:
    from dotenv import load_dotenv
    load_dotenv(BACKEND / ".env")
except ImportError:
    pass

# Wind MCP skill(node cli.mjs + 密钥)仍留在博客仓库;如整体搬迁用环境变量改指
WIND_SKILL_DIR = os.getenv(
    "WIND_SKILL_DIR",
    r"<wind-mcp-skill 目录>",
)

# job → 采集步骤序列(每项 cwd/脚本);全部步骤跑完后统一回灌
JOBS = {
    # stock ：每日分钟级（只刷腾讯批量行情→估值/价格，不重抓财务）
    "stock": [(SCRIPTS, "fetch_data.py"), (SCRIPTS, "portfolio_engine.py")],
    # deep  ：全市场财务重抓（季报到账后/周末跑一次，数小时）
    "deep": [(SCRIPTS, "fetch_data.py"), (SCRIPTS, "portfolio_engine.py")],
    "agro": [(AGRO_SCRIPTS, "fetch_prices.py"), (AGRO_SCRIPTS, "fetch_edb.py")],
    "events": [(SCRIPTS, "fetch_events.py")],
    "import": [],
}

# 各 job 首脚本的默认参数（调度器只传 job 名，全市场模式在此定版）。
# CLI 透传参数会整体替代默认参数（如 `run.py stock --codes 600519`）。
JOB_DEFAULTS = {
    "stock": ["--all-market", "--snapshot-only", "--quiet"],
    # --hk-connect：港股通名单（≈621 只）跟着深抓一起刷，否则只有今天这一次手动跑，
    # 下周港股财务数据就开始变旧。名单拉不到时 hk_connect_targets 空列表降级，不阻断 A 股
    "deep": ["--all-market", "--hk-connect", "--workers", "4", "--resume", "--max-age", "6",
             "--chunk", "400", "--flush-every", "200", "--audit-scope", "annual",
             "--quiet"],
}

# 回灌面：日更只动了 index.json（行情/评分），财务文件按 mtime 增量；
# 深抓回灌全量（财务期表与报告都要重写）
IMPORT_DEFAULTS = {
    "stock": ["--only-fresh", "26"],
}


# 全量抓取持锁时跳过调度（fetch_data 的 index.json 为全量重写,两进并行会互盖条目）
FETCH_LOCK = COLLECTOR / "data" / ".fetch.lock"
FETCH_LOCK_STALE = 45 * 60


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
    """data/.fetch.lock 是否被一个还活着的抓取进程持有。"""
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
    env["WIND_SKILL_DIR"] = WIND_SKILL_DIR
    return env


def _run_script(cwd, script, extra):
    print(f"[collector] >>> {script} {' '.join(extra)} (cwd={cwd})", flush=True)
    subprocess.check_call(
        [sys.executable, script, *extra], cwd=str(cwd), env=_env()
    )


def _import_db(job=""):
    """回灌 JSON 工作目录 → MySQL(增量 upsert,不清空)。"""
    env = _env()
    env["LEGACY_DATA_DIR"] = str(COLLECTOR)
    extra = IMPORT_DEFAULTS.get(job, [])
    print("[collector] >>> 回灌 MySQL(import_legacy --no-clean %s)" % " ".join(extra), flush=True)
    subprocess.check_call(
        [sys.executable, "-X", "utf8", "-m", "scripts.import_legacy", "--no-clean", *extra],
        cwd=str(BACKEND), env=env,
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
            # 仅首个脚本吃参数(后续步骤如 portfolio_engine 无自定义参数)
            _run_script(cwd, script, (extra if i == 0 else []) or JOB_DEFAULTS.get(args.job, []))
        except subprocess.CalledProcessError as e:
            status = "failed"
            messages.append(f"{script} exit={e.returncode}")
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
