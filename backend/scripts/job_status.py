# -*- coding: utf-8 -*-
"""采集任务状态一览:etl_job_log 最近运行记录。

用法(在 backend 目录下):
    python -X utf8 -m scripts.job_status        # 最近 10 条
    python -X utf8 -m scripts.job_status -n 30
    python -X utf8 -m scripts.job_status -n 60 --json   # 机读,给 ops/menu.ps1 用

--json 走 ensure_ascii(默认),中文全成 \\uXXXX,于是调用方不必管控制台代码页:
PowerShell 抓 python 的中文输出会按 GBK 解码,菜单里"上一次 FAILED"这种字
样一乱就没了意义,而它只要 job 名/状态/耗时这三个 ASCII 字段。
"""
import json
import sys
from datetime import datetime, timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models import EtlJobLog

MARK = {"success": "OK    ", "failed": "FAILED", "running": "RUNNING"}


def _secs(r, now):
    """耗时(秒)。仍在跑的用 now 兜底,与文本模式的 `123s+` 同口径。"""
    if not r.started_at:
        return None
    return int(((r.finished_at or now) - r.started_at).total_seconds())


def main():
    n = 10
    if "-n" in sys.argv:
        n = int(sys.argv[sys.argv.index("-n") + 1])
    as_json = "--json" in sys.argv
    with SessionLocal() as db:
        rows = db.execute(
            select(EtlJobLog).order_by(EtlJobLog.id.desc()).limit(n)
        ).scalars().all()
        if as_json:
            print(json.dumps([{
                "id": r.id, "job": r.job_name, "status": r.status,
                "started": r.started_at.isoformat(timespec="seconds") if r.started_at else None,
                "finished": r.finished_at.isoformat(timespec="seconds") if r.finished_at else None,
                "secs": _secs(r, datetime.now()),
                "message": (r.message or "")[:200],
            } for r in rows]))   # 新的在前:调用方取"每个 job 的第一条"就是它最近一次
            return 0
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
