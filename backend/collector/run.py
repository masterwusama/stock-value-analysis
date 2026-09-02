# -*- coding: utf-8 -*-
"""P5 采集任务驱动:跑原采集脚本(产 JSON)→ 回灌 MySQL(import_legacy --no-clean 增量 upsert)。

用法(在 backend/ 目录):
    python -m collector.run stock     # fetch_data → portfolio_engine → 回灌
    python -m collector.run agro      # fetch_prices → fetch_edb → 回灌
    python -m collector.run events    # Wind 事件(一次性/手动,不进每日调度) → 回灌
    python -m collector.run import    # 跳过抓取,仅手动触发一次回灌
尾部参数透传给该 job 首个采集脚本,如 `python -m collector.run stock --limit 1`。

设计对齐原 GitHub Actions workflow:
    - 抓取步骤失败/超时被杀也继续回灌+调仓已落盘数据(if: always() 语义)
    - 引擎按交易日幂等跳重,同日重复跑不产生重复调仓
    - 每次运行写 etl_job_log(替代 Actions 运行记录页)
"""
import argparse
import os
import subprocess
import sys
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
    "stock": [(SCRIPTS, "fetch_data.py"), (SCRIPTS, "portfolio_engine.py")],
    "agro": [(AGRO_SCRIPTS, "fetch_prices.py"), (AGRO_SCRIPTS, "fetch_edb.py")],
    "events": [(SCRIPTS, "fetch_events.py")],
    "import": [],
}


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


def _import_db():
    """回灌 JSON 工作目录 → MySQL(增量 upsert,不清空)。"""
    env = _env()
    env["LEGACY_DATA_DIR"] = str(COLLECTOR)
    print("[collector] >>> 回灌 MySQL(import_legacy --no-clean)", flush=True)
    subprocess.check_call(
        [sys.executable, "-X", "utf8", "-m", "scripts.import_legacy", "--no-clean"],
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
    for i, (cwd, script) in enumerate(steps):
        try:
            # 仅首个脚本吃透传参数(后续步骤如 portfolio_engine 无自定义参数)
            _run_script(cwd, script, extra if i == 0 else [])
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
        _import_db()
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
