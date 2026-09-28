# -*- coding: utf-8 -*-
"""status.ps1 的运维盲区兜底（2026-09-28）：打印每个 job 最近一次状态（failed 标红）
与 tailscale serve 指向（非 8000 标红）。独立小脚本，避免在 PowerShell 里拼 SQL。
"""
import io
import json
import subprocess
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


def jobs():
    try:
        sys.path.insert(0, ".")
        from sqlalchemy import text

        from app.db import SessionLocal

        db = SessionLocal()
        rows = db.execute(text(
            "SELECT job_name, status, started_at FROM etl_job_log j "
            "WHERE id = (SELECT MAX(id) FROM etl_job_log WHERE job_name = j.job_name)"
        )).mappings().all()
        db.close()
        bad = [r for r in rows if r["status"] != "success"]
        for r in sorted(rows, key=lambda x: x["job_name"]):
            mark = "[OK] " if r["status"] == "success" else "[FAIL]"
            print(f"  {mark} {r['job_name']:<10} {r['status']:<8} {r['started_at']}")
        return len(bad)
    except Exception as e:  # noqa: BLE001
        print(f"  (job 状态读取失败: {e})")
        return -1


def serve():
    try:
        out = subprocess.run(
            ["C:/Program Files/Tailscale/tailscale.exe", "serve", "status"],
            capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception as e:  # noqa: BLE001
        print(f"  [?] serve 状态读取失败: {e}")
        return 1
    if not out:
        print("  [FAIL] serve 未配置——外网不可达!")
        return 1
    if "8000" in out:
        print("  [OK] 外网入口指向 8000（生产 API）")
        return 0
    print("  [FAIL] serve 指向异常（非 8000）——疑似被改到开发端口:")
    for ln in out.splitlines()[:2]:
        print("        " + ln)
    return 1


if __name__ == "__main__":
    print("== 最近采集任务（每 job 最近一次） ==")
    nbad = jobs()
    print("== 外网入口（tailscale serve） ==")
    nserve = serve()
    sys.exit(0 if (nbad <= 0 and nserve == 0) else 1)
