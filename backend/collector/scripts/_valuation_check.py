# -*- coding: utf-8 -*-
"""采集侧守卫回归：windcode 映射、外源表选取、置空判定（零积分、不连数据库）。

这些判定只能在响应回来之后才看得见，真跑一次要烧积分且覆盖面不受控，故用
一次性抓取实测过的四种畸形回执做固定样本：
1) 港股回码去掉前导零、美股回码带交易所后缀 → 必须还能对回我们的 key；
2) 同一响应里两张表的观测日不同（实测一张 09-09 的 PB、另一张 08-30 的 PE）
   → 逐指标只从自己那张表取值，不跨表拼行；
3) 亏损股 Wind 照样给分位（万科A PE_TTM=-0.42 给 26.5%）→ 置空；
4) 序列停更在过去 / 分位数越界 / 观测日在未来 → 全部置空。

用法：python -X utf8 collector/scripts/_valuation_check.py
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_valuation import (  # noqa: E402
    clean_value, extract_metric, match_key, to_wind,
)

fails = []


def check(tag, cond, got=""):
    print(("  ok   " if cond else "  FAIL ") + tag + ("  " + str(got) if got != "" else ""))
    if not cond:
        fails.append(tag)


TODAY = dt.date(2026, 9, 9)
MAX_LAG = 10

# ---------- 1. windcode 双向映射 ----------
check("沪主/深主/科创后缀", to_wind("600519", "A") == "600519.SH"
      and to_wind("000001", "A") == "000001.SZ" and to_wind("688981", "A") == "688981.SH")
check("北交四段前缀走 .BJ",
      all(to_wind(c, "A").endswith(".BJ") for c in ("920001", "430047", "832000", "873122")),
      [to_wind(c, "A") for c in ("920001", "430047", "832000", "873122")])
check("港股补零五位、美股裸码",
      to_wind("00700", "HK") == "00700.HK" and to_wind("AAPL", "US") == "AAPL")

SENT = {"00700.HK": "HK:00700", "AAPL": "US:AAPL", "000001.SZ": "A:000001"}
check("港股回码去前导零仍能对上（0700.HK）", match_key("0700.HK", SENT) == "HK:00700",
      match_key("0700.HK", SENT))
check("美股回码带交易所后缀仍能对上（AAPL.O）", match_key("AAPL.O", SENT) == "US:AAPL",
      match_key("AAPL.O", SENT))
check("对不上的回码不猜（返回 None）", match_key("ZZZZ.HK", SENT) is None)


# ---------- 2. 多表混合观测日：逐指标各取各的表 ----------
def _tbl(names, rows):
    return {"columns": [{"name": n} for n in names], "rows": rows}


# 表一：PB 新鲜（09-09），顺带给了一个停在 08-30 的 PE 列；表二：PE 自己的表，日期同样旧
MIXED = {"data": {"data": [
    _tbl([u"Wind代码", u"日期", u"市净率LF", u"最新市净率近10年分位数", u"市净率最大序号"],
         [["000001.SZ", "2026-09-09", 0.48, 9.2745, 2427],
          ["000002.SZ", "2026-09-09", 0.37, 11.2119, 2427]]),
    _tbl([u"Wind代码", u"日期", u"市盈率TTM", u"最新市盈率近10年分位数", u"市盈率最大序号"],
         [["000001.SZ", "2026-08-30", 5.22, 30.2968, 2410]]),
]}}
pb = extract_metric(MIXED, u"市盈率")
check("市盈率只从自己那张表取（不跨表拼行）", set(pb) == {"000001.SZ"}, sorted(pb))
check("取到的是该表自己的观测日与样本数",
      pb["000001.SZ"]["date"] == "2026-08-30" and pb["000001.SZ"]["days"] == 2410
      and pb["000001.SZ"]["ratio"] == 5.22, pb["000001.SZ"])
# 分位与比率两列都含指标词，选列时必须避开分位/序号列，否则拿 30.2968 当比率的旧值
check("比率列不含分位与序号",
      extract_metric(MIXED, u"市净率")["000001.SZ"]["ratio"] == 0.48)
check("无该指标表时返回空（不拿别家表凑）", extract_metric(MIXED, u"市销率") == {})


# ---------- 3/4. 置空判定 ----------
def _v(**kw):
    base = {"pct": 50.0, "days": 2400, "date": "2026-09-09", "ratio": 12.0, "wind_code": "X"}
    base.update(kw)
    return base


ENTRY = {"key": "A:000002", "code": "000002", "market": "A", "ratio": {"pe_ttm": -0.42, "pb": 0.37}}
PE = ("pe", u"市盈率", "pe_ttm")
r = clean_value(_v(pct=26.5, ratio=-0.42), ENTRY, "pe", TODAY, MAX_LAG)
check("负 PE 的分位置空（万科A 实测 26.5%）", r["pct"] is None, r)
r = clean_value(_v(pct=26.5, ratio=None), ENTRY, "pe", TODAY, MAX_LAG)
check("外源没给比率列时退回行情快照比率", r["pct"] is None, r)
r = clean_value(_v(pct=11.2119, ratio=0.37), ENTRY, "pb", TODAY, MAX_LAG)
check("同一家 PB 正常给值", r["pct"] == 11.2119 and r["days"] == 2400, r)

for tag, got in (
    ("分位数 >100", _v(pct=100.4)),
    ("分位数 <0", _v(pct=-1.0)),
    ("比率 0（EPS 恰为零）", _v(pct=20.0, ratio=0)),
    ("观测日停在 2024", _v(date="2024-08-30")),
    ("观测日在未来", _v(date="2026-09-12")),
    ("观测日缺失", _v(date=None)),
    ("分位数非数字", _v(pct="不适用")),
):
    check(u"置空：%s" % tag, clean_value(got, ENTRY, "pb", TODAY, MAX_LAG)["pct"] is None)

check("滞后 1 天不算停更（港美股收盘本就晚一天）",
      clean_value(_v(date="2026-09-08"), ENTRY, "pb", TODAY, MAX_LAG)["pct"] == 50.0)
check("滞后恰在门槛上仍给值",
      clean_value(_v(date=str(TODAY - dt.timedelta(days=MAX_LAG))), ENTRY, "pb", TODAY, MAX_LAG)["pct"] == 50.0)
check("样本数脏值不影响分位落库",
      clean_value(_v(days="--"), ENTRY, "pb", TODAY, MAX_LAG)["days"] is None)
check("可信行保留外源回码（溯源用）",
      clean_value(_v(), {"key": "A:x", "code": "x", "market": "A", "ratio": {}},
                  "pb", TODAY, MAX_LAG)["wind_code"] == "X")

print("\n%s" % ("全部通过" if not fails else "失败 %d 项: %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
