# -*- coding: utf-8 -*-
"""etf job 晚间自愈补跑(scheduler 每半小时 18:00~23:30 触点调用)。

背景:APScheduler 的 misfire 宽限只有 1 小时——机器休眠/进程挂掉跨过 17:35,
当日 etf 就静默缺失,序列出现断档。本脚本查 etl_job_log:工作日 18 点后若
「今日无 etf success」则补跑一次 `collector.run etf`(幂等,重复跑只覆盖同日行)。

覆盖范围有意只做 etf:它便宜(全名单 ~40 秒)、纯增量、不碰 .fetch.lock;
stock/deep 等重 job 的补跑牵涉锁与时长,仍靠 status.ps1 红绿人工判断。
周末不补(17:35 例行跑过,值与周五相同,补跑无意义)。
"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]


def need_catchup() -> bool:
    now = datetime.now()
    if now.weekday() >= 5 or now.hour < 18:
        return False
    sys.path.insert(0, str(BACKEND))
    from sqlalchemy import text

    from app.db import SessionLocal

    db = SessionLocal()
    try:
        row = db.execute(text(
            "SELECT COUNT(*) FROM etl_job_log "
            "WHERE job_name = 'etf' AND status = 'success' "
            "AND DATE(started_at) = CURDATE()"
        )).scalar_one()
        return row == 0
    finally:
        db.close()


def main():
    if not need_catchup():
        return 0
    print("[etf-heal] 今日 etf 无 success,补跑", flush=True)
    return subprocess.call([sys.executable, "-m", "collector.run", "etf"],
                           cwd=str(BACKEND))


if __name__ == "__main__":
    sys.exit(main())
