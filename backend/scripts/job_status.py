# -*- coding: utf-8 -*-
"""采集任务状态一览:etl_job_log 最近运行记录。

用法(在 backend 目录下):
    python -X utf8 -m scripts.job_status        # 最近 10 条
    python -X utf8 -m scripts.job_status -n 30
"""
import sys
from datetime import datetime, timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models import EtlJobLog

MARK = {"success": "OK    ", "failed": "FAILED", "running": "RUNNING"}


def main():
    n = 10
    if "-n" in sys.argv:
        n = int(sys.argv[sys.argv.index("-n") + 1])
    with SessionLocal() as db:
        rows = db.execute(
            select(EtlJobLog).order_by(EtlJobLog.id.desc()).limit(n)
        ).scalars().all()
        if not rows:
            print("etl_job_log 为空(尚未跑过采集)")
            return 0
        print(f"最近 {len(rows)} 次采集运行:")
        print("  id     job            开始               耗时    状态     摘要")
        now = datetime.now()
        stale = []
        for r in rows:
            fin = r.finished_at
            dur = "-"
            if fin and r.started_at:
                dur = f"{int((fin - r.started_at).total_seconds())}s"
            elif r.started_at:
                dur = f"{int((now - r.started_at).total_seconds())}s+"
                if now - r.started_at > timedelta(hours=3):
                    stale.append(r.id)
            msg = (r.message or "").replace("\n", " ")[:46]
            print(f"  {r.id:<6} {r.job_name:<14} {r.started_at:%m-%d %H:%M:%S}   "
                  f"{dur:<7} {MARK.get(r.status, r.status):<6} {msg}")
        if stale:
            print(f"\n[提示] 记录 {stale} 处于 running 超 3 小时,应为进程被强杀留下的残留,"
                  f"\n       可执行下列语句收尾(用 python 一行脚本或客户端):"
                  f"\n       UPDATE etl_job_log SET status='failed', finished_at=NOW()"
                  f" WHERE status='running';")
    return 0


if __name__ == "__main__":
    sys.exit(main())
