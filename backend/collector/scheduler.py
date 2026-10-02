# -*- coding: utf-8 -*-
"""P5 调度器:独立常驻进程,替代 GitHub Actions cron(本机即北京时区)。

用法(在 backend/ 目录):
    python -m collector.scheduler

任务表(全市场 5500 只规模下的两层节奏):
    行情快照: 周一至周六 16:05 / 22:05——只刷腾讯批量估值(分钟级),
             不重抓财务
    全市场深抓: 周六 09:05——财报/定期报告重抓(--resume --max-age 6, 约 4.5h),
             财务数据按季更新,周频足够; 新上市标的也在这一步进池
    农价     : 每天 09:05 / 21:05(生意社每周 2~4 次,一天 2 次足够)
    行业 EDB : 每周日 20:00——量价序列(月/周频为主,周更足够)。2026-10-02 起换免费
             HTTP 源(新浪期货主力连续/东财宏观,收缩为 8 指标),不再依赖 Wind,云端可跑
    估值分位 : 每天 05:05——Wind PE/PB/PS 十年分位,单日 80 次调用,铺完一轮约 1.8 天
    立案公告 : 每周日 10:05——巨潮全文检索「立案告知书/行政处罚决定书」(免 Wind),
             灌 case_event 并单刷 score_daily,「正在立案」抬 ST 高危旗
    其余 Wind 侧(events/valuation 之外的 events job)仍不进调度:本机 Wind 客户端 CLI
             要登录、按量耗积分,是自动链管不到的外部依赖(2026-09-03 09:05 的 agro
             就被当时的 fetch_edb 带成过 failed——edb 自换免费源后该风险已消除)。

Wind 积分约束(每日 1000 分,单价实测 8~11.4 积分/次、cli 不返回余额):2026-10-02 edb
退役 Wind 后,自动任务里只剩 valuation 碰 Wind——靠 --calls-cap 80 的当日调用上限加
data/valuation/state.json 游标,到上限就干净收尾 exit 0(一天铺不完全是预期,次日接着
铺),只有连续多批取数失败才让 job 红;积分预算全额让给它。
要更新 events 跑 python -m collector.run events。

全量抓取期间持 data/.fetch.lock,同时段的 stock job 会自动跳过(不互盖 index.json)。

不想常驻可在 Windows「任务计划程序」注册等价触发器:
    schtasks /Create /TN va-stock /SC WEEKLY /D MON,TUE,WED,THU,FRI,SAT /ST 16:05 ^
      /TR "cmd /c cd /d <项目目录>\\backend && python -m collector.run stock >> collector.log 2>&1"
"""
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler

BACKEND = Path(__file__).resolve().parents[1]

# APScheduler 的丢触发/吞异常只走 logging:不配 handler 时 WARNING 以下全被吞,
# 错过触发只剩一条内部日志。落到 stdout → start.ps1 已重定向到 run/scheduler.out.log。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)


def run_job(name: str):
    print(f"[scheduler] {datetime.now():%F %T} 触发 {name}", flush=True)
    r = subprocess.run(
        [sys.executable, "-m", "collector.run", name],
        cwd=str(BACKEND),
    )
    print(f"[scheduler] {name} 退出码 {r.returncode}", flush=True)


def main():
    # misfire_grace_time 默认只有 1 秒:机器休眠/进程被卡超 1 秒,本轮触发就被静默丢弃。
    # 对本链「晚跑」永远优于「不跑」——宽限 1 小时,同一 job 错过多次只补跑一次(coalesce)。
    s = BlockingScheduler(
        timezone="Asia/Shanghai",
        job_defaults={"misfire_grace_time": 3600, "coalesce": True},
    )
    # 原 cron "0 8,14 * * 1-6" = 北京 16:00/22:00,顺延 5 分钟等数据源落库
    s.add_job(run_job, "cron", args=["stock"], day_of_week="mon-sat",
              hour="16,22", minute=5, id="stock")
    # 全市场财务深抓每周一次(周六盘后时段,避开工作日行情快照);中断可 --resume 接着跑
    s.add_job(run_job, "cron", args=["deep"], day_of_week="sat",
              hour=9, minute=5, id="deep")
    # 原 cron "0 1,13 * * *" = 北京 09:00/21:00
    s.add_job(run_job, "cron", args=["agro"], hour="9,21", minute=5, id="agro")
    # 行业 EDB 每周一次(周日晚上,不与周六全市场深抓挤同一时段)：量价序列以月/周频
    # 为主,日更无意义。2026-10-02 起为免费 HTTP 源(期货主力/东财),无 Wind 依赖
    s.add_job(run_job, "cron", args=["edb"], day_of_week="sun",
              hour=20, minute=0, id="edb")
    # 估值分位每天 05:05 铺一批(单日 80 次调用,约 1.8 天铺完全市场后自动开新一轮)。
    # 挑清晨是躲开行情快照/深抓/农价/EDB 四个时段,也让 Wind 当日额度从凌晨就开始用。
    s.add_job(run_job, "cron", args=["valuation"], hour=5, minute=5, id="valuation")
    # 情绪日报每个交易日 16:30(收盘后 90 分钟:新浪指数日频要拖到 15:35 还没收录
    # 当日,R5 虽有实时兜底但能等到正主就等;周末不跑,休市跳过在采集器内部)。
    # 榜单池数据源只留 15 个交易日,中断超两周记得手动补跑
    s.add_job(run_job, "cron", args=["sentiment"], day_of_week="mon-fri",
              hour=16, minute=30, id="sentiment")
    # 立案/处罚公告每周一次(周日 10:05,在 agro 早场之后、edb 之前,互不挤时段)。
    # 立案是低频事件,周更足够;回灌 --only-case 顺带单刷 score_daily 把「正在立案」
    # 抬成 ST 高危旗(旗发生在 score 行重建里,只灌 case_event 看不到)
    s.add_job(run_job, "cron", args=["case"], day_of_week="sun",
              hour=10, minute=5, id="case")
    print("[scheduler] 已注册: stock(周一~六 16:05/22:05 估值快照) "
          "deep(周六 09:05 全市场财务) agro(每天 09:05/21:05 农价,不碰 Wind) "
          "edb(周日 20:00 行业量价) valuation(每天 05:05 估值分位,两个 Wind 任务按游标限额跑) "
          "sentiment(交易日 16:30 情绪日报) case(周日 10:05 立案/处罚公告)。"
          "Ctrl+C 退出",
          flush=True)
    s.start()


if __name__ == "__main__":
    main()
