# -*- coding: utf-8 -*-
"""P5 调度器:独立常驻进程,替代 GitHub Actions cron(本机即北京时区)。

用法(在 backend/ 目录):
    python -m collector.scheduler

任务表(全市场 5500 只规模下的两层节奏):
    行情+组合: 周一至周六 16:05 / 22:05——只刷腾讯批量估值(分钟级),
             不重抓财务; 引擎按交易日幂等跳重
    全市场深抓: 周六 09:05——财报/定期报告重抓(--resume --max-age 6, 约 4.5h),
             财务数据按季更新,周频足够; 新上市标的也在这一步进池
    农价     : 每天 09:05 / 21:05(生意社每周 2~4 次,一天 2 次足够)
    行业 EDB : 每周日 20:00——Wind 客户端量价序列(月/周频为主,周更足够)
    其余 Wind 侧(events job)仍不进调度:本机 Wind 客户端 CLI 要登录、按量耗积分,
             是自动链管不到的外部依赖(2026-09-03 09:05 的 agro 就被 fetch_edb 带成过 failed)。

Wind 积分约束(2026-09-03 余额约 1000 分,EDB 历史删了补不回来):edb 是唯一
自动碰 Wind 的任务,靠三件事兜住——fetch_edb 默认增量窗口(单次 7 调用/共 265 天数据,
同脚本改成固定 365 天窗口是 2607 天,约 10 倍)、写盘前的历史不回撤防线(合并按失败
处理就 exit=4 不写文件)、以及客户端没登录时只会 job failed(不耗积分、不动数据)。
要更新 events 跑 python -m collector.run events;要看 edb 本轮花多少数据量跑
python fetch_edb.py --dry-run(不碰 Wind)。

全量抓取期间持 data/.fetch.lock,同时段的 stock job 会自动跳过(不互盖 index.json)。

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
    # 全市场财务深抓每周一次(周六盘后时段,避开工作日行情快照);中断可 --resume 接着跑
    s.add_job(run_job, "cron", args=["deep"], day_of_week="sat",
              hour=9, minute=5, id="deep")
    # 原 cron "0 1,13 * * *" = 北京 09:00/21:00
    s.add_job(run_job, "cron", args=["agro"], hour="9,21", minute=5, id="agro")
    # 行业 EDB 每周一次(周日晚上,不与周六全市场深抓挤同一时段)：量价序列以月/周频
    # 为主,日更毫无意义却要把 Wind 客户端这个自动链管不到的依赖掺进 agro 链(踩过)
    s.add_job(run_job, "cron", args=["edb"], day_of_week="sun",
              hour=20, minute=0, id="edb")
    print("[scheduler] 已注册: stock(周一~六 16:05/22:05 估值快照) "
          "deep(周六 09:05 全市场财务) agro(每天 09:05/21:05 农价,不碰 Wind) "
          "edb(周日 20:00 行业量价,唯一自动碰 Wind 的任务)。Ctrl+C 退出",
          flush=True)
    s.start()


if __name__ == "__main__":
    main()
