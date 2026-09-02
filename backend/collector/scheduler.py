# -*- coding: utf-8 -*-
"""P5 调度器:独立常驻进程,替代 GitHub Actions cron(本机即北京时区)。

用法(在 backend/ 目录):
    python -m collector.scheduler

任务表(与原 workflow 双时段防漏触发策略一致):
    行情+组合: 周一至周六 16:05 / 22:05(收盘主更 + 兜底,引擎按交易日幂等跳重)
    农价+EDB : 每天 09:05 / 21:05(生意社每周 2~4 次,一天 2 次足够)
    Wind 事件: 不进调度(一次性抓取,手动 python -m collector.run events)

不想常驻可在 Windows「任务计划程序」注册等价触发器:
    schtasks /Create /TN va-stock /SC WEEKLY /D MON,TUE,WED,THU,FRI,SAT /ST 16:05 ^
      /TR "cmd /c cd /d <项目目录>\\backend && python -m collector.run stock >> collector.log 2>&1"
"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler

BACKEND = Path(__file__).resolve().parents[1]


def run_job(name: str):
    print(f"[scheduler] {datetime.now():%F %T} 触发 {name}", flush=True)
    r = subprocess.run(
        [sys.executable, "-m", "collector.run", name],
        cwd=str(BACKEND),
    )
    print(f"[scheduler] {name} 退出码 {r.returncode}", flush=True)


def main():
    s = BlockingScheduler(timezone="Asia/Shanghai")
    # 原 cron "0 8,14 * * 1-6" = 北京 16:00/22:00,顺延 5 分钟等数据源落库
    s.add_job(run_job, "cron", args=["stock"], day_of_week="mon-sat",
              hour="16,22", minute=5, id="stock")
    # 原 cron "0 1,13 * * *" = 北京 09:00/21:00
    s.add_job(run_job, "cron", args=["agro"], hour="9,21", minute=5, id="agro")
    print("[scheduler] 已注册: stock(周一~六 16:05/22:05) agro(每天 09:05/21:05)。Ctrl+C 退出",
          flush=True)
    s.start()


if __name__ == "__main__":
    main()
