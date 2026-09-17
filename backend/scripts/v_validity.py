# -*- coding: utf-8 -*-
"""价值综合分 V 的分项回测：七项先验权重（定稿指表）→ 结构核验、无价格块的判别效度、与现成分的重叠度。

为什么要跑它：V 是三轴的最后一轴，上线后和巴菲特分、陷阱分、成长分挤在同一个列表页。四派分
已经在做「便宜 + 质量」这件事，V 必须回答「凭什么再来一列」，所以三件事先用数据钉死：
① 七项加权出来的 V 在结构上是不是两块互相淹没或互相抵消（便宜的和质量的会不会就是同一批公司）；
② 不带价格的那半边（质量与股东回报）排不排得出其后「变坏」的高低（判别效度）；
③ V 是不是四派分换了一层皮（重叠度：ρ>0.85 就不该单开一列）。

**为什么便宜块没有判决书：本库量不了它。** `quote_daily` 自 2026-09-02 起才有行情，全库没有
历史市值，任何「当时的价格 → 后来的结局」都拼不出来；`valuation_pctile` 每家只有今天这一行，
也不能往回喂。所以甲拆成三条腿，只有第二条进判决书：
- **甲-1 结构核验**（当期截面，全市场，七项全可测）：V 同时带着便宜与质量两块信息
  —— ρ(V, 便宜块) 与 ρ(V, 质量块) 都 ≥0.30（任一块被另一块淹没 ⇒ V 只是它的别名）；
  分布不塌（p90−p10 ≥ 15 分，否则一列只是常数）；两块的 ρ 不接近 −1（互相抵消）。
- **甲-2 判别效度**（A 股面板 2021~2023，**只用不含价格的 C+D 块**）：五分位对「其后转亏率」
  与「其后减值≥5%净资产率」两条腿，各自要单调不升（相邻档容 2pp）+ 首末差 ≥5pp / ≥2pp，
  外加转亏率逐年同向。这一过量的是「V 的质量那半边确实指向未来变坏」。
- **甲-3 前视对照**（同一批面板，A+B 块配**今天的市值**）：今天的市值里已经装着信号日之后
  所有消息，所以它与未来结局的关系**不是预测**，判决书上不记它的过与否。它只回答一个形状
  问题：便宜档在后面是更好还是更坏？若更坏（便宜块与坏结局同向），V 就不该把两块加权相加，
  而要取小或相乘——这是本脚本唯一允许它参与的决定。

判定线在 `verdict()`：甲（甲-1 ∧ 甲-2）+ 乙（max|ρ(V, 四派/T/造假/周期)| ≤ 0.85，与 G 同一条尺）
+ 丙（每项覆盖 ≥60%、无一项打满/打零 >50%、三市场中位差 ≤20 分）。
乙不过且 V 同时与 ⑦ 管理分也 >0.85 ⇒ 整轴不上（那是重复信息，不是新轴）。

**权重先验声明、不拟合**，与 G 同一条纪律：拟合只会把权重编成对这段历史的过拟合，而这段历史
只覆盖 FY2016 之后（财务行被 40 期窗口钳住）。

**指表改过三版再加一次定稿合并，判据一条没动**（改的都是输入端，结局口径从头到尾是 loss/imp5
那两条）：第一版把便宜度算成「市值 ÷ 账面锚」的折扣率并夹到 0，A 块三项打零 87%~99% —— 一项在
九成公司身上都是同一个数就不是分项了；第二版换成 `ln(锚/市值)` 且不夹 0，A 块活了，但净流动资产
与净现金两个锚打零 74%/82%（见排除清单 ①），股东回报那项收成「连续三年分红」后仍打满 52.2%；
第三版是 A 块只留一个账面锚、股东回报改成数近五年回钱年数，全量跑四条线全过；定稿再按 ⑥ 的合并
纪律摘掉两个 r>0.5 的重复项（流动比率、商誉+无形/总资产），12 分还给同块兄弟，块间 65/32/3 不动。

**定稿指表与全量实测**（2026-09-17 跑，面板 A 股 5486 家 / 16373 条观测 / 信号年 2021~2023，
截面 6909 家＝A 5553＋港股 592＋美股 764）：

| 分项 | 块 | 权重 | 锚点(0→100) | 覆盖 | 打满 | 打零 |
| --- | --- | --- | --- | --- | --- | --- |
| ln(有形账面价值/市值) | A | 30 | −1.5→0.5 | 97.7% | 1.5% | 37.4% |
| 盈利收益率（锚点年报/市值） | B | 25 | 0→0.10 | 100.0% | 4.0% | 29.5% |
| 现金股息率（近 3 年均值） | B | 10 | 0→0.05 | 85.9% | 4.5% | 25.3% |
| ROE 近 5 年中位 | C | 14 | 0→0.20 | 99.8% | 8.7% | 18.9% |
| 资产负债率（反向） | C | 10 | 0.90→0.30 | 100.0% | 31.1% | 4.2% |
| 净现比（近 5 年配对求和） | C | 8 | 0.8→2.5 | 76.5% | 17.7% | 22.9% |
| 近 5 年回钱年数占比 | D | 3 | 0→1 | 86.8% | 38.6% | 17.8% |

- **甲-1 结构 PASS**：ρ(便宜块, 质量块)=+0.473，ρ(V, 便宜块)=+0.938，ρ(V, 质量块)=+0.719，
  V 跨度 p10 8.8→p90 53.2＝44.4 分。
- **甲-2 判别效度 PASS**：C+D 块五分位 → 其后转亏率 25.4%→15.1%→10.6%→7.4%→5.8%（Q1−Q5
  =+19.7pp，线 ≥5pp）、减值≥5%净资产率 37.3%→3.8%（+33.5pp，线 ≥2pp），两腿单调不升、
  逐年 3/3 同向、ρ(C+D, 其后没转亏)=+0.182。
- **甲-3（不进判决）**：便宜块 Q1 转亏 21.2% → Q5 9.7%、减值 26.7% → 10.4%，方向对（贵更坏），
  中段 Q2 触底后不再降 ⇒ **合成形状取加权相加**，不用 min/相乘。
- **乙 PASS**：max|ρ(V, 现成分)|=0.789（施洛斯），四派其余 0.412/0.556/0.595，陷阱分 −0.199、
  造假 −0.182、周期 +0.033、成长 +0.428、管理分 +0.511（退轴条件未触发）。
- **丙 PASS**：七项覆盖 76.5%~100%，打满/打零最高 38.6%；三市场 V 中位 A 29.4 / 港 41.0 /
  美 27.2，跨市场中位差 13.8 分（线 ≤20）。
- **⑥**：合并后已无 |r|>0.5 的分项对。

定稿指表里三处刻意不做的事，写在排除清单里免得下一轮又加回来：
- ① 便宜度锚**只留一个账面锚**。先说两个不能用的：现成的 `fair_liq` 分子就是「流动资产合计−负债
  合计」，与净流动资产 NCAV（`value_scores.ncav`）是同一个数，并进去等于把同一个比值数两遍；
  「净现金」本库有两个口径，参考价那个要吃附注与季报、历史面板上取不到，能取到的窄口径又与格雷
  厄姆派分项同源。再把这两个锚真的摆进去跑一遍，结论更直接：ln(净流动资产/市值) 打零 74%、
  ln(净现金/市值) 打零 82%——A 股把全部流动资产抵给负债后仍为正是少数派，它们量的是「有没有
  清算缓冲」这件尾部事件，是开关不是尺度（丙明令禁止）。所以 A 块只留一个全市场量得出高低的
  账面锚——有形账面价值（归母权益−商誉−无形，只要求归母权益在），尾部那件事交给格雷厄姆派
  分项与买卖点 gate。
- ② `valuation_pctile`（Wind 的 PE/PB 历史分位）不进 V：只有今天一个横截面，进不了历史面板，
  而且它刷的是过筛池（管理分<30 或造假分>50 的不刷、美股整市场不刷），拿它当分项等于让
  「有没有被刷过」混进分数。
- ③ 股债利差（E/P − 10 年国债）不进 V：`BOND_10Y = 0.017` 是写死的展示常量，不是一个时点序列。

口径（继承 fraud_validity / trap_validity / g_validity，复用同一套时点机器，不再写第四份）：
1. 分项只喂「信号日当天真的公开了」的年报行（可用日规则同前三个脚本）；锚点取可见的最近一期
   年报年，整个窗跟着锚点走。
2. 账面锚与市值同币种：市值取 `quote_daily.market_cap`（当地币种），账面量取当地币种财报，
   比值天然免汇率——所以 V 全程只做比值，不跨币种相加。取的是各家 30 天窗口内
   的最近一笔，不是全市场同一天：本库 A 股行情刷到 T-0，港美股只到 2026-09-11 那一批，绑同一天等于
   让港美股 1386 家整批丢掉 65 分价格分项，跨市场比较就先量到「谁的行情表新」上了。
   港美股因此可能拿着几天前的价格，② 把每个市场取到最新一日的占比打出来。
3. 便宜度一律取 `ln(账面锚 / 最近一笔市值)`，不夹到 0：市值高于锚是「确实不便宜」而不是「算不出」，
   夹住会把贵 1.2 倍与贵 8 倍压成同一个数（首跑那么做，87%~99% 的公司挤在零分上，一项退化成
   开关）。同一件事还有另一半纪律：**算得出而为负**（有形账面价值为负、利润为负、近五年一股
   钱都没回过）记 0 分档，**科目取不到**才算判不动、不进分母——把前者记成判不动等于「最没有
   安全边际的公司因为算不出而不被扣分」。
4. 判不动不进分母；V 的分母恒为 ΣW=100，缺项只压低分数不重新归一，可评估项数并列输出。
   与陷阱分、成长分同一纪律。块内子分（便宜块/质量块）是**回测统计量**，为了在没有价格的
   历史面板上也能比较档位，允许按块内权重归一；它不是上线的那个数。
5. 面板只测 A 股（港美股没有连续中文年报序列）；截面子跑全市场，因为要上线的人群是全市场。

已知局限（打在输出里，不藏着）：
- **幸存者内偏差**：样本只有当前挂牌的公司，退市/暴雷消失的不在里面，低分组的恶化率被系统性
  低估——本脚本能判「质量分高是否跟着更少变坏」，判不了「便宜会不会死」。
- **金融股缺的不是「流动比率那一行」而是整条归母权益链**：实测 65 家 A 股金融类里只有 23.1%
  取到 `edge_tbv` 与 `cash_yld`（其余 5488 家 99.1% / 98.3%），而 ROE、负债率、回钱年数三项
  100% 齐全、净现比 92.3%（还高于其余的 77.3%）——缺的正是 V 的 40 分。固定分母下它们那一列
  只剩 60 分可用，② 里金融中位（42.2）比全体（29.1）高就是这个造成的。这是取数层的缺口，
  上线（阶段 2）前得单独查银行/券商的权益行为什么进不来，不是评分口径的问题。
- A/B 块在面板上是**今天市值 × 历史年报**，只用于甲-3；它的覆盖率、打满/打零等分布结论一律
  以当期截面（今天市值 × 今天能看到的年报）为准。
- FY2016 之前不可见（40 期钳制），面板只能开 3 个信号年。

用法（只读库、零积分、不需要服务在跑）:
    cd backend; python -X utf8 -m scripts.v_validity
    python -X utf8 -m scripts.v_validity --limit 300 --xlimit 200    # 冒烟
    python -X utf8 -m scripts.v_validity --dump _tmp/v_obs.json      # 两遍全市场要十几分钟，存一份
    python -X utf8 -m scripts.v_validity --load _tmp/v_obs.json      # 只改报告口径或指表时用
    python -X utf8 -m scripts.v_validity --load _tmp/v_obs.json --anchor edge_tbv=-1.5:0.5
    python -X utf8 -m scripts.v_validity --load _tmp/v_obs.json --drop ocfnp
                                                                     # 变体跑：摘项/改锚点在同一份
                                                                     # 缓存上秒级重打分（权重自动抬回 ΣW=100）
    ⚠ 缓存里存的是分项原始值与结局，改权重与锚点可以直接复用；改 `v_raw`（分项定义、结局口径）
      就别复用这份缓存，要重跑。
"""
import argparse
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector" / "scripts"))

from sqlalchemy import func, select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import (Dividend, QuoteDaily, ScoreDaily, Security,  # noqa: E402
                        ShareAction)
from scripts.fraud_validity import (Avail, ashare_sids, load_announce,  # noqa: E402
                                    load_batch, _d, _num)
from scripts.g_validity import _mono, _pp, _rp, _spear, quintiles_of  # noqa: E402
from scripts.trap_validity import (TABLES, _med, _pad, _pear, fy_facts,  # noqa: E402
                                   outcomes_at, quantiles)

BATCH = 300
PANEL_YEARS = (2021, 2022, 2023)      # 有未来结局的信号年：载体年报最远到 FY2025
TODAY = date(2026, 9, 17)             # 截面日：只用它筛「今天看得见哪些年报」，不进任何比值
MCAP_WINDOW = 30                      # 取市值的回看窗（自然日）：本库港美股行情比 A 股旧几天
# 甲-2 的两条腿（减值线取 ≥5% 那档，与陷阱分同口径）与各自的预登记门槛
DETER_OUTS = ("loss", "imp5")
REQ = {"loss": 0.05, "imp5": 0.02}
OUT_LABEL = {"loss": "转亏", "imp5": "减值≥5%净资产", "imp3": "减值≥3%",
             "divcut": "分红中断", "bvpsdn": "每股净资产降≥10%"}
TOL_PP = 0.02                         # 相邻档容 2pp：分位边界的一次抖动不算形状
# 账面锚算得出来却是负的（有形账面价值为负：累计亏损吃穿了权益，或商誉+无形大于权益）＝这家
# 公司没有这项缓冲，记 0 分而不是判不动：记判不动等于「最没有安全边际的公司因为算不出而不被
# 扣分」。取一个落在所有 lo 锚之下的值，`_clip` 之后就是 0 分。
EDGE_FLOOR = -9.0


def _tangible(d):
    """有形账面价值 = 归母权益 − 商誉 − 无形；只要求归母权益在，两个扣项缺则按 0。

    实测 A 股年报行里「商誉」只有 55.3% 取得到（无形资产 98.7%），而短期借款 84.5%、应付债券
    25.2%、租赁负债 46.5%——这条链上的可选科目是「没有该项就不存这一行」，线上算有息负债的
    `ssum` 用的就是同一套约定。要求两个扣项齐全等于把「没有商誉」读成「查不到」，实测把
    edge_tbv 的覆盖压到 50.7%（一半公司连账面锚都没有，正好是最干净的那一半）。
    """
    eq = d.get("eq")
    return None if eq is None else eq - (d.get("gw") or 0.0) - (d.get("intang") or 0.0)


# (key, 中文标签, 块, 权重, 0 分锚点, 100 分锚点)——锚点是绝对阈值，不做同市场横截面重排。
# 块 A/B 要市值才能算（见文件头：面板上它们是前视对照，截面上它们才是上线人群的样子）。
# A 块量的是 `ln(账面锚 / 市值)`：0 分锚在「市值是锚的若干倍」那一侧，负值不是「算不出」而是
# 「确实不便宜」——夹到 0 会把贵 1.2 倍和贵 8 倍压成同一个数（首跑就是这么把 87%~99% 的公司
# 挤在零分上的）。第二版曾并排放三个账面锚，其中净流动资产与净现金两项打零 74%/82%（尾部事件
# 不是尺度），摘掉后只剩这一个账面锚，理由写在排除清单 ①。
# 定稿按 ⑥ 的合并纪律再摘两项：流动比率与资产负债率 r=−0.67（同一件杠杆，后者覆盖 100% 且金融
# 股结构性没有前者），商誉+无形/总资产与账面锚 r=−0.60（商誉吃权益，那个锚本来就压下去了）。
# 摘掉的 12 分还给同块兄弟，块间 65/32/3 这个先验声明不动。
ITEMS = (
    ("edge_tbv",    "ln(有形账面价值/市值)",         "A", 30, -1.5, 0.5),
    ("ep",          "盈利收益率（锚点年报/市值）",    "B", 25, 0.0,  0.10),
    ("cash_yld",    "现金股息率（近 3 年均值）",      "B", 10, 0.0,  0.05),
    ("roe_med5",    "ROE 近 5 年中位",               "C", 14, 0.0,  0.20),
    ("debt_rev",    "资产负债率（反向）",             "C", 10, 0.90, 0.30),
    ("ocfnp",       "净现比（近 5 年配对求和）",      "C", 8,  0.8,  2.5),
    ("return_cash", "近 5 年回钱年数占比",           "D", 3,  0.0,  1.0),
)
BLOCK_NAME = {"A": "账面安全边际", "B": "收益便宜度", "C": "质量与资本结构", "D": "股东回报"}
WEIGHT = {k: w for k, _, _, w, _, _ in ITEMS}
LABEL = {k: lab for k, lab, _, _, _, _ in ITEMS}
BLOCK = {k: b for k, _, b, _, _, _ in ITEMS}
SUM_W = sum(WEIGHT.values())
PRICE_KEYS = tuple(k for k, _, b, _, _, _ in ITEMS if b in ("A", "B"))
BOOK_KEYS = tuple(k for k, _, b, _, _, _ in ITEMS if b in ("C", "D"))
# 乙的对照：四派总分是 V 最强的「已经在做同一件事」质疑对象；陷阱分与造假分方向相反（越低越好），
# 照样并排打印，因为「和它反向」也是重叠度的一种。管理分单列，它是退轴条件的那一半。
EXISTING = (("score_graham_agg", "格雷厄姆(进取)"), ("score_graham_def", "格雷厄姆(防守)"),
            ("score_schloss", "施洛斯"), ("score_buffett", "巴菲特"),
            ("fraud", "造假红旗"), ("mgmt", "管理层分"), ("cycle", "周期分"),
            ("trap", "陷阱分"), ("growth", "成长分"))
SCHOOL = ("score_graham_agg", "score_graham_def", "score_schloss", "score_buffett")


# ---------- 事实 ----------

def v_facts(g, av, sid):
    """A/港/美四表 → {财年: 事实}，在 trap_validity.fy_facts 之上补 V 要的三个字段。

    不直接改 `fy_facts`：那函数是陷阱分回测的口径载体，为 V 加键会让它的输出静默变形。
    """
    f = fy_facts(g, av, {}, sid)
    for ex, _p, _t, rd in g["ba"].get(sid, []):
        if (rd.month, rd.day) != (12, 31):
            continue
        d = f.get(rd.year)
        if d is None:
            continue
        for k, col in (("tl", "负债合计"), ("intang", "无形资产")):
            v = _num(ex.get(col))
            if v is not None:
                d[k] = v
    for ex, _net, rd in g["ind"].get(sid, []):
        if (rd.month, rd.day) != (12, 31):
            continue
        d = f.get(rd.year)
        if d is None:
            continue
        v = _num(ex.get("资产负债率"))
        if v is not None:
            d["debt_r"] = v
    return f


def v_events(db, sids):
    """{sid: {归属年: 每 10 股现金红利合计}} + {sid: [注销回购年]} + 有任一行历史的 sid。

    比 trap_validity.load_events 多要「红利金额」而不是只要「有没有」；定增这里用不上，
    只留分红与注销回购。用哪三年由 `v_raw` 决定。
    """
    div, cxl, known = defaultdict(dict), defaultdict(list), set()
    for sid, dy, bonus in db.execute(select(Dividend.sid, Dividend.div_year, Dividend.bonus_per_10)
                                     .where(Dividend.sid.in_(tuple(sids)))):
        m = str(dy or "")[:4]
        b = _num(bonus)
        if m.isdigit() and int(m) >= 1990 and (b or 0) > 0:
            div[sid][int(m)] = div[sid].get(int(m), 0.0) + b
            known.add(sid)
    for sid, fin, nd, ct in db.execute(select(ShareAction.sid, ShareAction.finish_date,
                                              ShareAction.notice_date, ShareAction.cancel_type)
                                       .where(ShareAction.sid.in_(tuple(sids)))):
        if ct != "注销":
            continue
        dt = _d(fin) or _d(nd)
        if dt is not None:
            cxl[sid].append(dt.year)
            known.add(sid)
    return div, cxl, known


def load_mcap(db, sids):
    """各家自己的最近一笔市值：{sid: 市值} + 全市场最近日 + {sid: 那笔的行日}。

    不能按「全市场最大交易日」取一天：本库 A 股行情刷到 T-0，港美股只到 2026-09-11 那一批，
    绑同一天会让港美股 1386 家整批拿不到价格分项——V 的 65 分凭空判不动，跨市场比较量的是
    「谁的行情表新」而不是贵贱。改成各取各家最近一笔（窗口内），谁的价格旧就由 ② 打印出来。
    """
    day = db.execute(select(func.max(QuoteDaily.trade_date))).scalar()
    out, dates = {}, {}
    if day is None:
        return out, None, dates
    since = day - timedelta(days=MCAP_WINDOW)
    rows = db.execute(select(QuoteDaily.sid, QuoteDaily.trade_date, QuoteDaily.market_cap)
                      .where(QuoteDaily.sid.in_(tuple(sids)),
                             QuoteDaily.trade_date >= since)).all()
    best = {}
    for sid, td, mc in rows:
        if sid in best and best[sid] >= td:
            continue
        v = _num(mc)
        if v and v > 0:
            best[sid], out[sid], dates[sid] = td, v, td
    return out, day, dates


# ---------- 分项 ----------

def v_raw(f, yrs, mcap, div_amt, cxl_years, known=True):
    """信息集 → ({分项: 原始值|None}, 锚点年)；公开年报不足 3 期则整体 None。

    A/B 三项要当天市值才能算，没有市值就整体不进（None，不是 0）；C/D 六项与价格无关，
    在 2021~2023 的历史面板上照样算得出来——甲-2 靠的就是这半边。
    """
    if len(yrs) < 3:
        return None, None
    ay = yrs[-1]
    win = yrs[-5:]
    cur = f[ay]
    out = dict.fromkeys(WEIGHT)

    # 隐含股本：归母权益 / 每股净资产——与陷阱分同一招，避开送转股对「股本」列的污染。
    # 美股的每股净资产本库取不到，所以它的股息率算不出（记 None），账面折扣照算。
    sh = cur["eq"] / cur["bps"] if (cur.get("eq") and (cur.get("bps") or 0) > 0) else None

    if mcap:
        anchor = _tangible(cur)
        if anchor is not None and anchor <= 0:
            out["edge_tbv"] = EDGE_FLOOR       # 查得出来、且这家公司确实没有这项缓冲
        elif anchor:
            out["edge_tbv"] = math.log(anchor / mcap)
        # 盈利收益率用锚点年报而不是 1/PE_TTM：面板与截面必须是同一个量，而中间期的滚动 TTM
        # 在历史时点上取不到（40 期钳制 + 可用日）。扣非优先，缺则报告净利。
        # 亏损记 0 分而不是判不动：一家没有利润的公司确实没有盈利收益率，那是「贵」的一档，
        # 不是「查不到」——记 None 会让它的缺项反过来不拉低总分。
        earn = cur.get("ded") if cur.get("ded") is not None else cur.get("net")
        if earn is not None:
            out["ep"] = max(0.0, earn) / mcap
        if sh:
            paid = sum(v for y, v in div_amt.items() if ay - 3 <= y <= ay - 1)
            # 同一条纪律：三年一股没分是公开事实，记 0.0；只有股本反推不出来才算判不动。
            out["cash_yld"] = (paid / 10.0) * sh / 3.0 / mcap

    out["roe_med5"] = _med([f[y].get("roe") for y in win])
    dr = cur.get("debt_r")
    if dr is None and cur.get("ta") and cur.get("tl") is not None:
        dr = cur["tl"] / cur["ta"]
    out["debt_rev"] = dr
    pairs = [(f[y].get("net"), f[y].get("ocf")) for y in win]
    pairs = [(n, o) for n, o in pairs if n is not None and o is not None]
    sn, so = sum(n for n, _ in pairs), sum(o for _, o in pairs)
    if len(pairs) >= 3 and sn > 0:
        out["ocfnp"] = so / sn
    if known or div_amt or cxl_years:
        # 「任一年分过」首跑有 68.8% 打满，收紧成「连续三年都给」第二次跑仍有 52.2% 打满——
        # 一项 0/1 在过半公司身上都是 1 就不叫分项了。改成数「近 5 年里有几年真给了钱」，
        # 档位从 2 个变 6 个，缺的那几年照样掉分而不是开关。
        gave = sum(1 for y in range(ay - 5, ay) if y in div_amt or y in cxl_years)
        out["return_cash"] = min(1.0, gave / 5.0)
    return out, ay


def _clip(v, lo, hi):
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def v_score(raw):
    """→ (总分 0~100, 可评估项数, {分项: 0~1 得分|None})；分母恒为 SUM_W，缺项不进分子。"""
    sc = {}
    for k, _, _, _, lo, hi in ITEMS:
        v = raw.get(k)
        sc[k] = None if v is None else _clip(v, lo, hi)
    ev = sum(1 for v in sc.values() if v is not None)
    return round(sum(WEIGHT[k] * v for k, v in sc.items() if v is not None), 1), ev, sc


def blk_score(raw, sc, keys):
    """块内子分（0~100，按块内权重归一）——只作回测统计量：历史面板没有市值，不归一的话便宜块
    永远 0 分、质量块永远满分，两块的档位就没法并排看。上线的 V 用的是固定分母那个数。"""
    got = [(WEIGHT[k], sc[k]) for k in keys if sc.get(k) is not None]
    if not got:
        return None
    tw = sum(WEIGHT[k] for k in keys)
    return round(sum(w * s for w, s in got) / tw * 100.0, 1)


def variant(drop, anchors):
    """就地换指表：摘项 / 改锚点，好让同一份缓存并排出变体（两遍全市场要十几分钟）。"""
    global ITEMS, WEIGHT, LABEL, BLOCK, SUM_W, PRICE_KEYS, BOOK_KEYS
    drop = {x.strip() for x in (drop or "").split(",") if x.strip()}
    unknown = drop - set(WEIGHT)
    if unknown:
        raise SystemExit(f"--drop 不认识这些分项：{'、'.join(sorted(unknown))}")
    new_anch = {}
    for s in anchors or []:
        k, _, rng = s.partition("=")
        lo, _, hi = rng.partition(":")
        if k not in WEIGHT or not lo or not hi:
            raise SystemExit(f"--anchor 写法不对：{s!r}，应为 key=lo:hi")
        new_anch[k] = (float(lo), float(hi))
    ITEMS = tuple(it for it in ITEMS if it[0] not in drop)
    ITEMS = tuple((k, lab, b, w) + new_anch.get(k, (lo, hi)) for k, lab, b, w, lo, hi in ITEMS)
    # 摘项后 ΣW<100，而 V 的分母就是 ΣW（缺项不重新归一是设计，整表缩了不补是 bug）：
    # 剩余权重按比例抬回 100，变体与默认跑才在同一把尺上
    tot = sum(it[3] for it in ITEMS)
    if tot and tot != SUM_W:
        ITEMS = tuple((k, lab, b, round(w * 100.0 / tot, 2), lo, hi)
                      for k, lab, b, w, lo, hi in ITEMS)
    WEIGHT = {k: w for k, _, _, w, _, _ in ITEMS}
    LABEL = {k: lab for k, lab, _, _, _, _ in ITEMS}
    BLOCK = {k: b for k, _, b, _, _, _ in ITEMS}
    SUM_W = sum(WEIGHT.values())
    PRICE_KEYS = tuple(k for k in sorted(WEIGHT) if BLOCK[k] in ("A", "B"))
    BOOK_KEYS = tuple(k for k in sorted(WEIGHT) if BLOCK[k] in ("C", "D"))
    if drop or new_anch:
        parts = []
        if drop:
            parts.append("摘掉 " + "、".join(sorted(drop)))
        if new_anch:
            parts.append("锚点 " + "、".join("{} {:+g}→{:+g}".format(k, lo, hi)
                                            for k, (lo, hi) in sorted(new_anch.items())))
        print("⚠ 本次为变体跑：" + "；".join(parts))
        print("  当前指表：" + " · ".join("{} {}".format(LABEL[k], w) for k, w in WEIGHT.items()))


# ---------- 两遍跑 ----------

def _chunk_load(db, chunk):
    """两遍跑共用的四件切片：财务四表、公告日、股东回报事件、各家最近一笔市值。"""
    g = load_batch(db, chunk)
    ann = {tag: load_announce(db, M, chunk) for tag, M in TABLES}
    dv, cx, known = v_events(db, chunk)
    mc, mday, mdates = load_mcap(db, chunk)
    return g, ann, dv, cx, known, mc, mday, mdates


def run_panel(years, limit=0):
    """A 股历史面板：信号年 2021~2023，C+D 块干净、A+B 块是今天市值（前视，只喂甲-3）。"""
    db = SessionLocal()
    sids = ashare_sids(db)
    if limit:
        sids = sids[:limit]
    obs, counts, mcap_day = [], Counter(), None
    for i in range(0, len(sids), BATCH):
        chunk = sids[i:i + BATCH]
        g, ann, dv, cx, known, mc, mcap_day, mdates = _chunk_load(db, chunk)
        for sid in chunk:
            if not g["ind"].get(sid):
                counts["无指标行"] += 1
                continue
            av = Avail((ann["ba"].get(sid, {}), ann["cf"].get(sid, {}), ann["inc"].get(sid, {})))
            f = v_facts(g, av, sid)
            d_y, cx_y = dv.get(sid, {}), cx.get(sid, [])
            for t in years:
                sig = av.of(date(t, 12, 31))
                yrs = [y for y in sorted(f) if y <= t and f[y]["avail"] <= sig]
                raw, ay = v_raw(f, yrs, mc.get(sid), d_y, cx_y, True)
                if raw is None:
                    counts["公开年报不足3期"] += 1
                    continue
                if ay < t:
                    counts["锚点早于信号年（拖延披露）"] += 1
                val, ev, sc = v_score(raw)
                outs, _oy = outcomes_at(f, ay, t, sig, set(d_y), [])
                rec = {"sid": sid, "t": t, "ay": ay, "n_fy": len(yrs), "raw": raw, "sc": sc,
                       "g": val, "ev": ev, "book": blk_score(raw, sc, BOOK_KEYS),
                       "cheap": blk_score(raw, sc, PRICE_KEYS), "o": outs}
                if all(outs.get(k) is None for k in DETER_OUTS):
                    counts["结局两条腿全判不动"] += 1
                obs.append(rec)
                counts["观测"] += 1
    db.close()
    return obs, counts, mcap_day


def run_xsect(limit=0):
    """当期截面：全市场（A+港+美）七项全可测，并取最新一批 score_daily 的现成分做乙的对照。"""
    db = SessionLocal()
    universe = {(r[0]): (r[1], r[2] or "") for r in db.execute(
        select(Security.sid, Security.market, Security.industry)
        .where(Security.status == "active")).all()}
    sids = sorted(universe)
    if limit:
        sids = sids[:limit]
    day = db.execute(select(func.max(ScoreDaily.trade_date))).scalar()
    cols = [getattr(ScoreDaily, k) for k, _ in EXISTING]
    ex_rows = {r[0]: {k: r[i + 1] for i, (k, _) in enumerate(EXISTING)}
               for r in db.execute(select(ScoreDaily.sid, *cols)
                                   .where(ScoreDaily.trade_date == day)).all()}
    rows, counts, mcap_day = [], Counter(), None
    for i in range(0, len(sids), BATCH):
        chunk = sids[i:i + BATCH]
        g, ann, dv, cx, known, mc, mcap_day, mdates = _chunk_load(db, chunk)
        for sid in chunk:
            if not g["ind"].get(sid):
                counts["无指标行"] += 1
                continue
            av = Avail((ann["ba"].get(sid, {}), ann["cf"].get(sid, {}), ann["inc"].get(sid, {})))
            f = v_facts(g, av, sid)
            mkt, ind_text = universe[sid]
            yrs = [y for y in sorted(f) if f[y]["avail"] <= TODAY]
            raw, ay = v_raw(f, yrs, mc.get(sid), dv.get(sid, {}), cx.get(sid, []),
                            mkt == "A" or sid in known)
            if raw is None:
                counts["公开年报不足3期"] += 1
                continue
            val, ev, sc = v_score(raw)
            md = mdates.get(sid)
            rec = {"sid": sid, "market": mkt, "fin": _is_fin(ind_text), "ay": ay,
                   "fresh": None if md is None else (md == mcap_day),
                   "raw": raw, "sc": sc, "g": val, "ev": ev,
                   "book": blk_score(raw, sc, BOOK_KEYS),
                   "cheap": blk_score(raw, sc, PRICE_KEYS)}
            rec.update(ex_rows.get(sid) or {})
            rows.append(rec)
            counts["截面"] += 1
    db.close()
    return rows, counts, (mcap_day, day)


def _is_fin(industry):
    return any(t in (industry or "") for t in ("银行", "证券", "保险", "金融"))


# ---------- 结局统计 ----------

def _rate(rs, key):
    got = [r["o"].get(key) for r in rs if r["o"].get(key) is not None]
    return (sum(1 for x in got if x) / len(got), len(got)) if got else (None, 0)


def _shapes(rs, getter, head, vget):
    """五分位 → 两条腿的发生率表；返回 {key: (单调?, 首末差, 逐档序列)}。"""
    cells, _qs = quintiles_of(rs, getter)
    print(f"\n  {head}")
    if not cells:
        print("    可评估观测不足 25 条，无法分档")
        return {}
    print("  " + _pad("档", 6) + _pad("观测数", 8) + _pad("子分区间", 14)
          + "".join(_pad(OUT_LABEL[k] + "率", 16) for k in DETER_OUTS) + "任一坏结局率")
    drop = {}
    for k in DETER_OUTS:
        ns = [_rate(c, k)[0] for c in cells]
        # 发生率要的是「随档位不升」，而借来的 `_mono` 断言「不降」并返回末减首：先取负再交给
        # 它，差值就还原成 Q1−Q5（首跑把这张 29.3%→5.3% 的单调表印成了「有回升」）。
        m, d = _mono([-x if x is not None else None for x in ns], TOL_PP)
        drop[k] = (m, d, ns)
    for i, c in enumerate(cells):
        vs = [vget(r) for r in c]
        got = [r["o"] for r in c if any(r["o"].get(k) is not None for k in DETER_OUTS)]
        anyb = (sum(1 for o in got if any(o.get(k) for k in DETER_OUTS)) / len(got)) if got else None
        print("  " + _pad(f"Q{i + 1}", 6) + _pad(str(len(c)), 8)
              + (_pad(f"{min(vs):.1f}~{max(vs):.1f}", 14) if vs else _pad("空档", 14))
              + "".join(_pad(_pp(_rate(c, k)[0], _rate(c, k)[1]), 16) for k in DETER_OUTS)
              + _pp(anyb, len(got)))
    for k in DETER_OUTS:
        m, sp, _ns = drop[k]
        hit = sp is not None and sp >= REQ[k]
        print(f"    {OUT_LABEL[k]}率：随档位{'不升 ✓' if m else '有回升 ✗'}"
              + (f"；Q1−Q5 = {sp * 100:+.1f}pp（要求 ≥{REQ[k] * 100:.0f}pp）{'✓' if hit else '✗'}"
                 if sp is not None else "；有档位判不动 ✗"))
    return drop


def _years_mono(rs, getter, key):
    """逐年重算「Q1 转亏率 > Q5」，返回 (同向年数, 总年数)：低档更坏才算同向。"""
    by = defaultdict(list)
    for r in rs:
        by[r["t"]].append(r)
    pos = tot = 0
    for t, sub in sorted(by.items()):
        cells, _ = quintiles_of(sub, getter)
        if not cells:
            continue
        a, _na = _rate(cells[0], key)
        b, _nb = _rate(cells[-1], key)
        if a is None or b is None:
            continue
        tot += 1
        pos += 1 if a > b else 0
    return pos, tot


# ---------- 七节输出 ----------

def s_items(x):
    """丙的前半：当期截面的覆盖率与打满/打零。"""
    print("\n" + "=" * 126)
    print(f"① {len(ITEMS)} 个分项在当期截面（n={len(x)} 家，全市场）上的覆盖与退化（判定线丙）")
    print("=" * 126)
    print("  " + _pad("分项", 26) + _pad("块", 4) + _pad("权重", 6) + _pad("锚点(0→100)", 14)
          + _pad("覆盖", 8) + _pad("打满", 8) + _pad("打零", 8) + _pad("原始中位", 11)
          + _pad("p10→p90", 15) + _pad("A股中位", 11) + _pad("港股中位", 11) + "美股中位")
    res, bad = {}, []
    for k, lab, b, w, lo, hi in ITEMS:
        got = [(r["raw"][k], r) for r in x if r["raw"].get(k) is not None]
        cov = len(got) / len(x) if x else 0
        sat = sum(1 for v, r in got if r["sc"][k] >= 0.999) / len(got) if got else 0
        zer = sum(1 for v, r in got if r["sc"][k] <= 0.001) / len(got) if got else 0
        med = statistics.median([v for v, _ in got]) if got else None
        # 重锚只准看这一列（输入端分布），不准看任何结局列——G 那轮的 accel 就是这么定的
        pr = quantiles([v for v, _ in got], [0.1, 0.9]) if len(got) >= 20 else None
        per = {}
        for m in ("A", "HK", "US"):
            vm = [v for v, r in got if r["market"] == m]
            per[m] = statistics.median(vm) if vm else None
        if cov < 0.6 or sat > 0.5 or zer > 0.5:
            bad.append(f"{lab}（覆盖 {cov * 100:.0f}%／打满 {sat * 100:.0f}%／打零 {zer * 100:.0f}%）")
        res[k] = {"cov": cov, "sat": sat, "zero": zer}
        print("  " + _pad(lab, 26) + _pad(b, 4) + _pad(f"{w:g}", 6)
              + _pad(f"{lo:g}→{hi:g}", 14) + _pad(f"{cov * 100:.1f}%", 8)
              + _pad(f"{sat * 100:.1f}%", 8) + _pad(f"{zer * 100:.1f}%", 8)
              + _pad(f"{med:+.3f}" if med is not None else "—", 11)
              + _pad(f"{pr[0]:+.2f}→{pr[1]:+.2f}" if pr else "—", 15)
              + "".join(_pad(f"{per[m]:+.3f}" if per[m] is not None else "—", 11)
                        for m in ("A", "HK"))
              + (f"{per['US']:+.3f}" if per["US"] is not None else "—"))
    print(f"\n  判定线丙·覆盖（每项 ≥60% 且无一项打满/打零 >50%）："
          + ("✓ 全过" if not bad else "✗ 不达标：" + "、".join(bad)))
    return not bad, res


def s_market(x):
    """丙的后半：三个市场的 V 中位数差 ≤20 分，否则这一列在跨市场列表页上是市场标签。"""
    print("\n" + "=" * 126)
    print("② 三市场可用性：V 的分布与可评估项数（跨市场中位差 ≤20 分才算同一把尺）")
    print("=" * 126)
    meds = {}
    for m, lab in (("A", "A 股"), ("HK", "港股"), ("US", "美股")):
        sub = [r for r in x if r["market"] == m]
        if not sub:
            continue
        qs = quantiles(sorted(r["g"] for r in sub), [0.1, 0.5, 0.9])
        meds[m] = qs[1]
        fr = [r["fresh"] for r in sub if r.get("fresh") is not None]
        nq = sum(1 for r in sub if r.get("cheap") is not None)
        print(f"  {lab} {len(sub)} 家：V 中位 {qs[1]:.1f}（p10 {qs[0]:.1f} / p90 {qs[2]:.1f}）· "
              f"可评估项数平均 {statistics.mean([r['ev'] for r in sub]):.2f}/{len(ITEMS)} · "
              f"V>0 占 {sum(1 for r in sub if r['g'] > 0) * 100 / len(sub):.1f}% · "
              f"有市值可算 {nq} 家"
              + (f"（其中取最新一日的 {sum(1 for v in fr if v) * 100 / len(fr):.0f}%）" if fr else ""))
    ok = True
    if len(meds) > 1:
        sp = max(meds.values()) - min(meds.values())
        ok = sp <= 20
        print(f"\n  跨市场中位差 {sp:.1f} 分（阈值 20）：{'✓ 同一把尺' if ok else '✗ 这一列在替市场贴标签'}")
    for m in ("A",):
        fin = [r for r in x if r["market"] == m and r["fin"]]
        rest = [r for r in x if r["market"] == m and not r["fin"]]
        if fin and rest:
            print(f"  {m} 股金融行业（银行/券商/保险 {len(fin)} 家）V 中位 "
                  f"{statistics.median([r['g'] for r in fin]):.1f} vs 其余 {len(rest)} 家 "
                  f"{statistics.median([r['g'] for r in rest]):.1f}；可评估项数 "
                  f"{statistics.mean([r['ev'] for r in fin]):.2f} vs "
                  f"{statistics.mean([r['ev'] for r in rest]):.2f}"
                  "（金融类缺的正是账面锚与股息率那 40 分，方向见文件头已知局限）")
    return ok


def s_book(obs):
    """甲-2：无价格的 C+D 块五分位 → 其后转亏率与减值率。判决书只看这一节。"""
    print("\n" + "=" * 126)
    print(f"③ 甲-2 判别效度（判决书）：质量+回报块（不含任何价格）五分位 → 其后的坏结局"
          f"　n={len(obs)}")
    print("=" * 126)
    drop = _shapes(obs, lambda r: r["book"], "按 C+D 块子分五分位（Q1＝质量最差 → Q5＝最好）",
                   lambda r: r["book"])
    if not drop:
        return False
    ok = True
    for k in DETER_OUTS:
        m, sp, _ns = drop[k]
        ok = ok and m and sp is not None and sp >= REQ[k]
    pos, tot = _years_mono(obs, lambda r: r["book"], "loss")
    good = tot >= 2 and pos == tot
    print(f"  逐年（Q1 转亏率 > Q5）：{pos}/{tot} {'✓' if good else '✗'}")
    ok = ok and good
    rho = _spear([(r["book"], 0.0 if r["o"].get("loss") else 1.0) for r in obs
                  if r["o"].get("loss") is not None])
    print(f"  秩相关 ρ(C+D 块, 其后没转亏) = {_rp(rho)}（期望为正）")
    print("  判读：这一节没有价格参与，所以它是 V 唯一能拿真实未来结局验收的部分。不过 ⇒ 质量那")
    print("  半边是空的，V 只剩便宜度一根柱子，而便宜度在本库量不了 ⇒ 整轴不上。")
    return ok


def s_cheap(obs, x):
    """甲-3：便宜块（今天市值 × 历史年报）对同一批结局——前视，不参与判决，只定合成形状。"""
    print("\n" + "=" * 126)
    print("④ 甲-3 前视对照（不进判决书）：便宜块五分位 → 同一批坏结局")
    print("=" * 126)
    drop = _shapes(obs, lambda r: r["cheap"],
                   "按 A+B 块子分五分位（Q1＝最贵 → Q5＝最便宜；市值是今天的）",
                   lambda r: r["cheap"])
    if not drop:
        print("  便宜块全判不动")
        return False
    sp = {k: (drop[k][1] or 0.0) for k in DETER_OUTS}
    fight = any(v < 0 for v in sp.values())
    print("  最贵档−最便宜档的坏结局率差（正＝越贵越容易变坏，便宜是好事；负＝便宜档后来更坏）："
          f"转亏 {sp['loss'] * 100:+.1f}pp · 减值≥5% {sp['imp5'] * 100:+.1f}pp")
    print("  ⇒ 合成形状：" + ("有档位上便宜与坏结局同向 ⇒ V 取小或相乘（min/√(块×块)）而不是相加"
                             if fight else
                             "未见同向 ⇒ V 可以加权相加；上线后仍要盯住便宜档的恶化率"))
    a = [r for r in x if r["cheap"] is not None and r["book"] is not None]
    if len(a) >= 25:
        print("  三种合成在当期截面上的分布（同一批人，看哪种把中间挤成一团）：")
        for name, fn in (("相加（现行）", lambda r: r["g"]),
                         ("取小", lambda r: min(r["cheap"], r["book"])),
                         ("几何平均", lambda r: (r["cheap"] * r["book"]) ** 0.5)):
            qs = quantiles(sorted(fn(r) for r in a), [0.1, 0.5, 0.9])
            print(f"    {name:<12}：p10 {qs[0]:5.1f} 中位 {qs[1]:5.1f} p90 {qs[2]:5.1f} "
                  f"跨度 {qs[2] - qs[0]:5.1f}")
    print("  ⚠ 这一节的市值是今天的，里面装着信号日之后所有消息：它只能回答「便宜和坏结局同不")
    print("    同向」，不能回答「当时买便宜的赚不赚钱」。任何把它当预测效度的说法都是错的。")
    return fight


def s_struct(x):
    """甲-1：V 同时带着两块信息，且分布不塌。"""
    print("\n" + "=" * 126)
    print("⑤ 甲-1 结构核验：两块是否各自活着（V 不能只是其中一块的别名，也不能互相抵消）")
    print("=" * 126)
    a = [r for r in x if r["market"] == "A" and r["cheap"] is not None and r["book"] is not None]
    print(f"  两块都可评估的 A 股：{len(a)} 家")
    ok = len(a) >= 25
    r_cb = _spear([(r["cheap"], r["book"]) for r in a])
    r_vc = _spear([(r["g"], r["cheap"]) for r in a])
    r_vb = _spear([(r["g"], r["book"]) for r in a])
    print(f"  ρ(便宜块, 质量块) = {_rp(r_cb)}"
          + ("　← 接近 ±1 说明两块其实是一件事" if r_cb is not None and abs(r_cb) >= 0.6 else ""))
    print(f"  ρ(V, 便宜块) = {_rp(r_vc)}（要求 ≥0.30）　ρ(V, 质量块) = {_rp(r_vb)}（要求 ≥0.30）")
    ok = ok and abs(r_cb or 0) < 0.6 and (r_vc or 0) >= 0.30 and (r_vb or 0) >= 0.30
    qs = quantiles(sorted(r["g"] for r in a), [0.1, 0.9])
    print(f"  V 分布：p10 {qs[0]:.1f} → p90 {qs[1]:.1f}，跨度 {qs[1] - qs[0]:.1f} 分（要求 ≥15）")
    ok = ok and (qs[1] - qs[0]) >= 15
    print(f"\n  判定线甲-1：{'✓ 全过' if ok else '✗ 有项不达标'}")
    return ok


def s_rho_x(x):
    keys = [k for k, _, _, _, _, _ in ITEMS]
    print("\n" + "=" * 126)
    print(f"⑥ 分项之间的连续值相关（|r|>0.5 说明两项在量同一件事，定稿时须合并其一；n={len(x)}）")
    print("=" * 126)
    print("  " + _pad("", 26) + "".join(_pad(k[:8], 9) for k in keys))
    pairs = []
    for a in keys:
        vals = []
        for j in keys:
            r = 1.0 if a == j else _pear([(z["raw"][a], z["raw"][j]) for z in x
                                          if z["raw"].get(a) is not None
                                          and z["raw"].get(j) is not None])
            if a < j and r is not None:
                pairs.append((abs(r), a, j, r))
            vals.append(_pad(f"{r:+.2f}" if r is not None else "—", 9))
        print("  " + _pad(LABEL[a], 26) + "".join(vals))
    dup = sorted([(v, a, j, r) for v, a, j, r in pairs if v > 0.5], reverse=True)
    print("  同源对：" + (" · ".join(f"{LABEL[a]}×{LABEL[j]} r={r:+.2f}" for v, a, j, r in dup)
                        if dup else "无 |r|>0.5 的对，各项各自独立"))
    ev = [r["ev"] for r in x]
    print("  可评估项数分布：平均 {:.2f}/{} · ".format(statistics.mean(ev), len(ITEMS))
          + " · ".join(f"{n} 项 {sum(1 for v in ev if v == n) * 100 / len(ev):.1f}%"
                       for n in range(len(ITEMS) + 1) if any(v == n for v in ev)))
    return dup


def s_overlap(x):
    """乙：V 与最新一批现成分的重叠度（A 股当期截面）。"""
    print("\n" + "=" * 126)
    print("⑦ 乙 重叠度：V vs 线上现成分（A 股当期截面，取 score_daily 最新一批）")
    print("=" * 126)
    a = [r for r in x if r["market"] == "A"]
    print("  " + _pad("现成分", 22) + _pad("Spearman ρ(V)", 15) + _pad("n", 8) + "备注")
    mx, worst, mg = 0.0, None, None
    for k, lab in EXISTING:
        pr = [(r[k], r["g"]) for r in a if r.get(k) is not None]
        rho = _spear(pr)
        if rho is None:
            continue
        tag = ("← 四派总分，判乙看这一组" if k in SCHOOL
               else ("← 退轴条件的一半" if k == "mgmt" else ""))
        print("  " + _pad(lab, 22) + _pad(f"{rho:+.3f}", 15) + _pad(str(len(pr)), 8) + tag)
        if k == "mgmt":
            mg = rho
        if k != "growth" and (k in SCHOOL or k in ("trap", "fraud", "cycle")) and abs(rho) > mx:
            mx, worst = abs(rho), lab
    ok = mx <= 0.85
    print(f"\n  max|ρ(V, 现成分)| = {mx:.3f}" + (f"（{worst}）" if worst else "")
          + f"，阈值 0.85：{'✓ 未越过，V 携带现成分没有的信息' if ok else '✗ 越过'}")
    print("  （成长分只打印不判乙：V 与 G 是刻意互补的两轴，越线才要解释，没越是本该如此）")
    if not ok:
        print(f"  退轴条件：V 与 ⑦ 管理分同时越线才算重复信息 —— 当前 ρ(V, 管理分)={_rp(mg)}，"
              + ("也越过 ⇒ 整轴不上" if mg is not None and abs(mg) > 0.85 else "未越过 ⇒ 还有话说"))
        cells, _ = quintiles_of(a, lambda r: r["g"])
        if cells:
            print("  组内区分度（V 五分位下「四派里最高的那个分」的中位，越平说明 V 只是在重排同一件事）：")
            for i, c in enumerate(cells):
                v = [max(r[k] for k in SCHOOL if r.get(k) is not None) for r in c
                     if any(r.get(k) is not None for k in SCHOOL)]
                print(f"    Q{i + 1} n={len(c)}："
                      + (f"中位 {statistics.median(v):.1f}" if v else "判不动"))
    return ok


# ---------- 汇总 ----------

def report(obs, x, counts, note):
    print(f"样本：面板 A 股 {len({r['sid'] for r in obs})} 家 · 观测 {counts['观测']} 条 · "
          f"信号年 {sorted({r['t'] for r in obs})}；截面 {counts['截面']} 家（全市场）")
    for k in ("无指标行", "公开年报不足3期", "锚点早于信号年（拖延披露）", "结局两条腿全判不动"):
        if counts[k]:
            print(f"  {k}: {counts[k]}")
    print(f"  市值取各家 30 天窗口内最近一笔（全市场最近日 {note[0]}），现成分取自 score_daily 的 "
          f"{note[1]}（面板里 A+B 两块用的也是这批市值 ⇒ 前视）")
    nf = Counter(min(r["n_fy"], 9) for r in obs)
    print("  信号日可见年报期数：" + " · ".join(
        f"{k}{'+' if k == 9 else ''} 期 {v / max(1, len(obs)) * 100:.1f}%" for k, v in sorted(nf.items())))
    print("  ⚠ 幸存者内偏差：样本只有当前挂牌的公司，退市/暴雷消失的不在里面，低分组恶化率被低估。")
    print("  ⚠ 本库没有历史市值 ⇒ 便宜块不可能有预测效度，判决书里它只出现在甲-3 对照。")

    bing, _res = s_items(x)
    bing = s_market(x) and bing
    jia2 = s_book(obs)
    s_cheap(obs, x)
    jia1 = s_struct(x)
    s_rho_x(x)
    yi = s_overlap(x)
    verdict(jia1, jia2, yi, bing)


def verdict(jia1, jia2, yi, bing):
    print("\n" + "=" * 126)
    print("判决书（四条线全部在文件头预登记；甲-3 只定合成形状，不计过与否）")
    print("=" * 126)
    print("  甲-1 结构  ：ρ(V, 便宜块) 与 ρ(V, 质量块) 均 ≥0.30、块间 |ρ|<0.6、V 跨度 ≥15 分 → "
          f"{'PASS' if jia1 else 'FAIL'}")
    print("  甲-2 判别效度：C+D 块五分位 → 转亏率单调且降 ≥5pp、减值率单调且降 ≥2pp、逐年全同向 → "
          f"{'PASS' if jia2 else 'FAIL'}")
    print("  乙 重叠度  ：max|ρ(V, 四派/陷阱/造假/周期)| ≤0.85 → "
          f"{'PASS' if yi else 'FAIL（再判管理分那条退轴线）'}")
    print(f"  丙 可用性  ：每项覆盖 ≥60% 且无一项打满/打零 >50%、跨市场中位差 ≤20 分 → "
          f"{'PASS' if bing else 'FAIL'}")
    if jia1 and jia2 and yi and bing:
        print("\n  ⇒ 四条全过：V 值得进阶段 1（JS 权威实现 → Python 镜像 → parity）。指表按上面")
        print("     锚点原样落地，不做拟合；合成形状取 ④ 的结论。")
    elif not jia2:
        print("\n  ⇒ 甲-2 不过：V 的质量那半边在真实结局上切不开，整轴不进阶段 1。")
    elif not jia1:
        print("\n  ⇒ 甲-1 不过：V 退化成单块的别名或两块互相抵消。先按 ⑥ 的同源对合并再重跑；")
        print("     若是互相抵消，按 ④ 改合成形状而不是硬加权。")
    elif not bing:
        print("\n  ⇒ 丙不过：不达标那项换掉或摘掉再重跑，不要带着退化项定稿。")
    else:
        print("\n  ⇒ 只有乙不过：V 与某一现成分共线。按文件头预登记的退路判——与 ⑦ 管理分同时")
        print("     越线 ⇒ 重复信息，终止 V；否则看组内区分度，切得开就保留。")


def main():
    ap = argparse.ArgumentParser(description="价值分项判别效度回测（只读库、零积分、不改任何数据）")
    ap.add_argument("--years", nargs="*", type=int, default=list(PANEL_YEARS),
                    help="面板信号年；结局要其后的年报，只有 2021~2023 有")
    ap.add_argument("--limit", type=int, default=0, help="面板只跑前 N 家（冒烟测用）")
    ap.add_argument("--xlimit", type=int, default=0, help="截面只跑前 N 家（冒烟测用）")
    ap.add_argument("--drop", default="", help="逗号分隔的分项 key，本次先摘掉再出表（试变体用）")
    ap.add_argument("--anchor", action="append", default=[],
                    help="改某项锚点，形如 edge_tbv=-1.5:0.5，可重复；只看输入端分布，不碰结局")
    ap.add_argument("--dump", default="", help="把面板与截面观测写成 JSON，供 --load 复用")
    ap.add_argument("--load", default="", help="从 JSON 读回观测，不碰库")
    a = ap.parse_args()
    variant(a.drop, a.anchor)
    counts = Counter()
    if a.load:
        blob = json.loads(Path(a.load).read_text(encoding="utf-8"))
        obs, x, note = blob["panel"], blob["x"], tuple(blob["note"])
        counts.update({"观测": len(obs), "截面": len(x)})
        print(f"（读缓存 {a.load}：面板 {len(obs)} 条 · 截面 {len(x)} 家，未碰库）")
    else:
        obs, c1, mday = run_panel(a.years, a.limit)
        counts.update(c1)
        x, c2, note = run_xsect(a.xlimit)
        counts.update(c2)
        if a.dump:
            Path(a.dump).write_text(json.dumps({"panel": obs, "x": x,
                                                "note": [str(v) for v in note]},
                                               ensure_ascii=False), encoding="utf-8")
            print(f"（观测已写 {a.dump}）")
        print(f"  面板市值日：{mday}")
    if not obs or not x:
        print("没有可评估观测")
        return 1
    # 缓存里存的是 raw 与结局，都不随锚点变；总分、可评估项数与两个块子分是建缓存那份表的产品，
    # 按当前表重算一遍，否则 --drop / --anchor 改了指表却没改分
    for rec in list(obs) + list(x):
        rec["g"], rec["ev"], rec["sc"] = v_score(rec["raw"])
        rec["book"] = blk_score(rec["raw"], rec["sc"], BOOK_KEYS)
        rec["cheap"] = blk_score(rec["raw"], rec["sc"], PRICE_KEYS)
    report(obs, x, counts, note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
