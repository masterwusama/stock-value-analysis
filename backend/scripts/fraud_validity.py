# -*- coding: utf-8 -*-
"""红旗分与商誉暴露的判别效度回测：T 年的信号 → T+1 年的三类结局。

为什么要跑它：`fraud` 已经拿它做两件事（列表页门槛筛选、估值分位刷池 >50 不刷），巴菲特
那 3 分也已经拿「无形+商誉/总资产 ≥ 10%」当护城河证据，可它们到底有没有预测力一直没验够。
本脚本只读库、零积分、不改任何数据。

两节共用同一套机器：
- A 节：红旗分按 20/40/60 分四档 → 三类结局；
- B 节：商誉+无形暴露的分档 → 同样三类结局。合计口径两套（占归母权益 0.3/0.6、占总资产
  10%），两个分量各自再单独分档（商誉/总资产、无形/总资产），最后把过了 10% 线的那批按
  「商誉扛的还是无形扛的」拆开对照——护城河那 3 分奖励的是合计值，而 A 股实体 5432 家里有
  799 家拿满，其中 407 家的分主要来自无形资产（科目里以土地使用权/矿业权为主）而非商誉。
  拆开才知道该留哪一半。

口径（四条都不妥协，否则结论没意义）：
1. **T 年的分和它的结局都要用「当时真的公开了」的时间轴**。输入端：一版按「报告期 ≤
   T-12-31」截断（等于假设年报在年末那天就到手），二版按披露日截断（信号日 = FY-T 年报自己的
   披露日，只喂披露日不晚于它的行）。结局端：一版死盯 FY-(T+1) 那份年报，二版取信号日之后公开
   的第一份年报（上限 18 个月，只挡停牌级的披露空洞）。两两配成两版完整做法并跑，差异直接打出
   来——实测输入端近乎恒等（年报天然按序披露），真正的时点偏差在结局窗口端：晚披露的公司（恰是
   最可能出事的那批）的信号被上一版提前了三四个月。
2. **结局只算判得动的**：缺审计意见不进非标分母，缺减值科目或缺净资产不进减值分母。缺数
   据当 0 会把发生率整体压低、把 lift 做没。
3. **T 年年报当时还没披露的公司留在样本里**，用「只能看到 FY-(T-1) 」的信息集打分——那批
   拖延披露的恰恰是高危样本，剔掉它等于把结论最需要的地方删了。
4. **只测 A 股**：结局三件套（审计意见、利润表「资产减值损失/信用减值损失」科目名）与红旗
   口径同源，港股没有审计意见、美股财年归一且科目名不同一套，硬并进来只会污染分母。

结局定义（Y = 事件时窗内公开的那份年报，通常就是 FY-(T+1)）：
- `非标`   —— FY Y 年报审计意见不在 `标准无保留意见`/`无保留意见` 之内（`periodic_report`
              的期次从 `title` 解析——那表的 `report_date` 是公告日，不是报告期）
- `大额减值` —— Y 年「资产减值损失＋信用减值损失」的净损失 ≥ 当年期末归母净资产的 5%
              （次要口径 3% 一并给出）。利润表这两项按准则以负数列示，故取负号还原成损失；
              银人口径的「减值及拨备」不计入——拨备是常态科目，不是爆雷信号
- `转亏`   —— Y 年净利润 < 0 且 T 年 ≥ 0（盈利公司次年转负）

已知局限（打在输出里，不藏着）：披露日取自财报行 `extras` 里的 `公告日期` 与 `更新日期`
两列，而**这两列各自都会错**——中期行的 `公告日期` 就是真实披露日（实测 = 更新日期次日），
年报行却有约 2.9 万条把次年的公告日挂在了这行上（另有约 1200 条反过来早两年），故取两列
里更早、且落在报告期之后 10~400 天那个合理带内的值；两列都不合格才退回法定期限（年报次年
4-30、半年报 8-31、季报各自截止日），兜底只会把可得时间说晚、不会说早。`periodic_report`
只留了 FY2023 起的年报公告，故非标结局天然只覆盖 T ∈ 2022~2024。

用法(只读库，不需要服务在跑):
    cd backend; python -X utf8 -m scripts.fraud_validity
    python -X utf8 -m scripts.fraud_validity --years 2022 2023 2024 --imp-share 0.05 0.03
    python -X utf8 -m scripts.fraud_validity --no-goodwill
"""
import argparse
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector" / "scripts"))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import (FinBalance, FinCashflow, FinIncome, FinIndicator,  # noqa: E402
                        PeriodicReport, Security)
from scoring import fraud_analysis  # noqa: E402  线上同一个实现，不在脚本里复算

AUDIT_CLEAN = {"标准无保留意见", "无保留意见"}   # 与 import_legacy.AUDIT_CLEAN 同集
IMP_KEYS = ("资产减值损失", "信用减值损失")
RPT_YEAR = re.compile(r"(20\d\d)\s*年")
GW_KEYS = ("商誉", "无形资产")
# 分档按整十切，与列表页门槛（造假 ≤）和刷池口径（>50 不刷）大致对齐，
# 但不照抄 50 那条线——否则被人问「50 与 51 凭什么不同档」
BUCKETS = ((0, 20, "低 <20"), (20, 40, "中 20~40"), (40, 60, "高 40~60"), (60, 101, "极高 ≥60"))
LABS = [b[2] for b in BUCKETS]
OUTCOMES = ("aud", "imp", "imp3", "loss")
KSLABS = (("aud", "非标意见"), ("imp", "大额减值"), ("imp3", "减值≥3%"), ("loss", "次年净利转负"))
BATCH = 300   # 一批 300 家的四表行：全 A 股一次读会到 GB 量级，分批就只在内存里留一批
CALIBERS = ("公告日", "报告期")   # 前者是真·时点信息集，后者是旧口径（当作对照）
ALIGNS = ("事件时", "财年")       # 结局窗口怎么对齐：前者与信号日同步，后者死盯 FY-(T+1)
# 两两配成「一版完整做法」：第二行是上一版脚本，第一行是本脚本的主结论
COMBOS = (("公告日", "事件时", "本脚本主结论：信号日 = FY-T 年报披露日，结局看其后公开的第一份年报"),
          ("报告期", "财年", "上一版做法：假设 FY-T 年报在 T-12-31 就在手，结局死盯 FY-(T+1)"))
GW_EQ_LABS = ("≤ 0.3 权益", "0.3~0.6 权益", "> 0.6 权益", "权益≤0/缺", "未知")
GW_ASSET_LABS = ("< 10% 总资产", "≥ 10% 总资产", "未知")
# D1 的判决书要看是哪一半把比率推过线的：护城河那 3 分奖励的是「商誉+无形」的和，
# 而实测拿满的 799 家里一半靠无形资产（土地使用权/矿业权）、一半靠商誉
GW_SHARE_LABS = ("未列示/0", "0~5% 总资产", "5~10% 总资产", "≥ 10% 总资产", "未知")
GW_WHO_LABS = ("< 10% 总资产（基准）", "≥10% 且商誉为主", "≥10% 且无形为主", "未知")


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _pct(r):
    return "      —" if r is None else f"{r * 100:6.2f}%"


def _d(v):
    """'2024-04-26' / datetime.date / None → date | None"""
    if v is None:
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def deadline(rd):
    """法定最晚披露日：公告日缺失时的兜底。宁可把「什么时候能看到」说晚，不能说早。"""
    y, md = rd.year, (rd.month, rd.day)
    if md == (12, 31):
        return date(y + 1, 4, 30)
    if md == (3, 31):
        return date(y, 4, 30)
    if md == (6, 30):
        return date(y, 8, 31)
    if md == (9, 30):
        return date(y, 10, 31)
    return date(y, 12, 31)


def ashare_sids(db):
    """A 股 sid 升序清单——分批的边界，也是把港美股挡在外面的那一步。"""
    return [sid for (sid,) in db.execute(
        select(Security.sid).where(Security.market == "A").order_by(Security.sid))]


def load_audit(db):
    """(sid, 财报年份) → 该年年报是否非标；没有意见的年份压根不进这个 dict（= 不进分母）。"""
    out = {}
    for sid, title, opinion in db.execute(
            select(PeriodicReport.sid, PeriodicReport.title, PeriodicReport.audit_opinion)
            .where(PeriodicReport.category == "年报", PeriodicReport.audit_opinion.isnot(None))):
        m = RPT_YEAR.search(str(title or ""))
        if not m:
            continue
        key = (sid, int(m.group(1)))
        bad = str(opinion).strip() not in AUDIT_CLEAN
        out[key] = out.get(key, False) or bad
    return out


AVAIL_MIN_LAG = 10      # 报告期结束后 10 天内就「公告」的，是数据源的脏值不是原件
AVAIL_MAX_LAG = 400     # 晚于报告期 400 天：那是次年的公告日被挂到了这行上（实测 2.9 万行如此）


def avail_pair(ex, rd):
    """一行的可用日期 = 「公告日期」与「更新日期+1 天」里更早、且落在合理区间的那个 → (date, 来源)。

    数据源这两列各错各的：中期行的 `公告日期` 就是真实披露日（实测 = 更新日期 + 1 天），
    年报行却有 2.9 万条把**次年**那次公告日挂了过来（平安银行 FY2023 年报写成 2025-03-15，
    真实披露是更新日期的次日 2024-03-15）；反过来也有约 1200 行 `公告日期` 比更新日期早
    两年（原件日期真、更新日期只是数据源刷新）。单信任任何一列都会把信号推错方向，
    故两列互为校验、取更早的那个，并用区间把明显的错值筛掉。
    """
    cands = []
    a = _d((ex or {}).get("公告日期")) if isinstance(ex, dict) else None
    u = _d((ex or {}).get("更新日期")) if isinstance(ex, dict) else None
    if a is not None:
        cands.append((a, "公告日期"))
    if u is not None:
        cands.append((date.fromordinal(u.toordinal() + 1), "更新日期次日"))
    ok = [c for c in cands if AVAIL_MIN_LAG <= (c[0] - rd).days <= AVAIL_MAX_LAG]
    return min(ok) if ok else None


def load_announce(db, table, sids):
    """{sid: {报告期: (可用日, 来源)}} —— 三张财报同属一份披露文件，任一表取到即算。"""
    out = defaultdict(dict)
    ins = tuple(sids)
    for sid, rd, ex in db.execute(
            select(table.sid, table.report_date, table.extras).where(table.sid.in_(ins))):
        got = avail_pair(ex, rd)
        if got is not None:
            cur = out[sid].get(rd)
            if cur is None or got[0] < cur[0]:
                out[sid][rd] = got
    return out


def load_batch(db, sids):
    """一批公司的四张表 → sid: 行列表。每行都带原始中文科目行（`extras` 即无损原件）与报告日，
    指标表另带 `net_profit`，资产负债表另带两列权益；结局口径直接用这些实体列，不从中文科目猜。"""
    ins = tuple(sids)
    out = {k: defaultdict(list) for k in ("ind", "ba", "cf", "inc")}
    for sid, ex, net, rd in db.execute(
            select(FinIndicator.sid, FinIndicator.extras, FinIndicator.net_profit,
                   FinIndicator.report_date).where(FinIndicator.sid.in_(ins))):
        out["ind"][sid].append((ex or {}, net, rd))
    for sid, ex, parent, total, rd in db.execute(
            select(FinBalance.sid, FinBalance.extras, FinBalance.equity_parent,
                   FinBalance.equity_total, FinBalance.report_date).where(FinBalance.sid.in_(ins))):
        out["ba"][sid].append((ex or {}, parent, total, rd))
    for sid, ex, rd in db.execute(
            select(FinCashflow.sid, FinCashflow.extras,
                   FinCashflow.report_date).where(FinCashflow.sid.in_(ins))):
        out["cf"][sid].append((ex or {}, rd))
    for sid, ex, rd in db.execute(
            select(FinIncome.sid, FinIncome.extras,
                   FinIncome.report_date).where(FinIncome.sid.in_(ins))):
        out["inc"][sid].append((ex or {}, rd))
    return out


def annual_row(rows, date_key, year):
    """取该年 12-31 那条（利润表/指标的年报行本身就是全年累计值）。"""
    want = f"{year}-12-31"
    for row in rows:
        if str(row.get(date_key) or "")[:10] == want:
            return row
    return None


def imp_loss(inc_row):
    """净减值损失（正＝计提、负＝转回）；两个科目都缺给 None（不进分母）。"""
    if not inc_row:
        return None
    tot, hit = 0.0, False
    for k in IMP_KEYS:
        v = _num(inc_row.get(k))
        if v is not None:
            tot -= v      # 准则以负数列示损失，取负还原成「损失为正」
            hit = True
    return tot if hit else None


def gw_tiers(ba_row, eqv, assets):
    """B 节的分档：合计占归母权益、合计占总资产，外加两个分量各自占总资产、以及
    「≥10% 那条护城河线是被哪一半推上去的」。

    商誉与无形都缺 → 「未知」，不能当 0：那是「不知道有没有」，不是「确认没有」。
    """
    gwv = _num(ba_row.get(GW_KEYS[0]))
    itv = _num(ba_row.get(GW_KEYS[1]))
    if gwv is None and itv is None:
        return {"eq": "未知", "asset": "未知", "gw_asset": "未知",
                "it_asset": "未知", "who": "未知"}

    def share(v):
        if not assets or assets <= 0:
            return "未知"
        if not v:
            return "未列示/0"
        r = v / assets
        return ("≥ 10% 总资产" if r >= 0.1 else
                ("5~10% 总资产" if r >= 0.05 else "0~5% 总资产"))

    s = (gwv or 0.0) + (itv or 0.0)
    if eqv is None or eqv <= 0:
        out_eq = "权益≤0/缺"        # 负权益单独一档：比值算不出来，但风险恰恰最高
    else:
        r = s / eqv
        out_eq = "> 0.6 权益" if r > 0.6 else ("0.3~0.6 权益" if r > 0.3 else "≤ 0.3 权益")
    a = s / assets if (assets and assets > 0) else None
    return {"eq": out_eq,
            "asset": "未知" if a is None else ("≥ 10% 总资产" if a >= 0.1 else "< 10% 总资产"),
            "gw_asset": share(gwv), "it_asset": share(itv),
            "who": "未知" if a is None else (
                "< 10% 总资产（基准）" if a < 0.1 else
                ("≥10% 且商誉为主" if (gwv or 0) >= (itv or 0) else "≥10% 且无形为主"))}


class Avail:
    """某家公司的「报告期 → 什么时候能看到」映射。

    指标表（同花顺摘要）压根不带披露日期，而它是红旗分的主输入。三张财报同属一份披露文件，
    故用同一报告期在任一表里取到的最早可用日代理；一个都没有时退回法定期限。兜底方向是
    「说晚不说早」，最坏是把信号推迟，不会凭空造出提前量。
    """

    def __init__(self, maps):
        self.m = tuple(maps)       # 每张表的 {报告期: (可用日, 来源)}

    def _hit(self, rd):
        return [m[rd] for m in self.m if rd in m]

    def of(self, rd):
        got = self._hit(rd)
        return min(got)[0] if got else deadline(rd)

    def src(self, rd):
        """可用日出自哪一列，供报告把来源构成数出来。"""
        got = self._hit(rd)
        return min(got)[1] if got else "法定期限兜底"


def score_at(ind, ba, cf, av, t_year, caliber):
    """T 年红旗分（`fraud_analysis` 只看年报序列，故信息集就是「年报一列」的可得集合）。

    caliber='公告日' 用真·时点信息集：报告期 ≤ T-12-31 且公告日 ≤ 信号日；
    caliber='报告期' 是旧口径，只按报告期截断，当作对照。
    """
    end = date(t_year, 12, 31)
    sig = av.of(end)

    def keep(rd):
        return rd <= end and (caliber == "报告期" or av.of(rd) <= sig)

    d = {"indicators": [r for r, _, rd in ind if keep(rd)],
         "balance": [r for r, _, _, rd in ba if keep(rd)],
         "cashflow": [r for r, rd in cf if keep(rd)]}
    return (fraud_analysis(d) if d["indicators"] else None), sig


def bucket_of(score):
    for lo, hi, lab in BUCKETS:
        if lo <= score < hi:
            return lab
    return LABS[-1]


class Cell:
    """一个「档 × 结局」的格子：命中数 / 判得动数，外加该档观测数（重算出分的次数）。"""

    def __init__(self):
        self.n = dict.fromkeys(OUTCOMES, 0)
        self.hit = dict.fromkeys(OUTCOMES, 0)
        self.observations = 0

    def add(self, key, outcome):
        if outcome is None:
            return
        self.n[key] += 1
        self.hit[key] += 1 if outcome else 0

    def rate(self, key):
        return self.hit[key] / self.n[key] if self.n[key] else None


def corr(pairs):
    """分与 0/1 结局的相关系数（点二列）：一个数概括「分高是否更可能出事」。"""
    n = len(pairs)
    if n < 3:
        return None
    sx = sum(s for s, _ in pairs)
    sy = sum(o for _, o in pairs)
    sxx = sum(s * s for s, _ in pairs)
    syy = sum(o * o for _, o in pairs)
    sxy = sum(s * o for s, o in pairs)
    den = math.sqrt((n * sxx - sx * sx) * (n * syy - sy * sy))
    return None if den <= 0 else (n * sxy - sx * sy) / den


def ztest(h1, n1, h2, n2):
    """两比例 z 检验：样本几千时 0.5 个百分点的差能不能分开，靠它而不是靠眼看。"""
    if min(n1, n2) < 5:
        return None
    p = (h1 + h2) / float(n1 + n2)
    se = math.sqrt(p * (1 - p) * (1.0 / n1 + 1.0 / n2))
    return None if se <= 0 or p in (0.0, 1.0) else ((h1 / float(n1) - h2 / float(n2)) / se)


WINDOW_MAX = 548   # 18 个月：只切「连着两年没出年报」这种停牌/退市形状的空洞
# 上限不能取 12 个月——相邻两份年报的披露日本身就相隔约 365 天，取 365 会按
# 「今年比去年早几天披露」这种无关因素随机砍掉四成观测（实测 2204/5553）。


def annual_facts(sid, ind, ba, inc, av, audit, want_gw):
    """{财年: 那年年报的事实}。两套对齐共用同一份原件，结局与特征都只从这张表取。"""
    f = {}
    for row, col, rd in ind:
        if (rd.month, rd.day) == (12, 31):
            v = _num(row.get("净利润"))
            if v is None and col is not None:
                v = float(col)
            if v is not None:
                f.setdefault(rd.year, {})["net"] = v
    for row, p, t, rd in ba:
        if (rd.month, rd.day) == (12, 31):
            d = f.setdefault(rd.year, {})
            if p is not None or t is not None:
                d["eq"] = float(p if p is not None else t)   # 与 gate 的 neg_equity 同列同口径
            if want_gw:
                d["gw"] = gw_tiers(row, d.get("eq"), _num(row.get("资产总计")))
    for row, rd in inc:
        if (rd.month, rd.day) == (12, 31):
            f.setdefault(rd.year, {})["loss"] = imp_loss(row)
    for y, d in f.items():
        end = date(y, 12, 31)
        d["avail"] = av.of(end)
        d["aud"] = audit.get((sid, y))
    return f


def outcomes(f, t, sig, imp_share, align):
    """T 年信号之后的三类结局 → (dict, 用于判定结局的财年)。

    `财年` 是旧对齐：死盯 FY-(T+1) 那份年报，等于默认信号在 T-12-31 就在手——可 FY-T 年报
    本身要到次年四月才公开，前四个月的行情根本还没被这个分照到过。
    `事件时` 是主对齐：拿信号日之后公开的第一份年报当结局（18 个月内还出不来就算判不动，
    那只剩停牌/退市形状的空洞），窗口与可得性对齐。
    判不动的（缺意见/缺科目/缺权益）逐项给 None，不进分母。
    """
    if align == "财年":
        y = t + 1
    else:
        cands = [yy for yy in sorted(f)
                 if yy > t and sig < f[yy]["avail"] <= _plus(sig, WINDOW_MAX)]
        y = cands[0] if cands else None
    d = f.get(y) if y else None
    if d is None:
        return {"aud": None, "imp": None, "imp3": None, "loss": None}, y
    imp = imp3 = None
    loss, eqy = d.get("loss"), d.get("eq")
    if loss is not None and eqy and eqy > 0:
        imp = loss >= imp_share[0] * eqy
        imp3 = loss >= imp_share[1] * eqy
    n_t = (f.get(t) or {}).get("net")
    n_y = d.get("net")
    flip = None if (n_t is None or n_y is None or n_t < 0) else n_y < 0
    return {"aud": d.get("aud"), "imp": imp, "imp3": imp3, "loss": flip}, y


def _plus(d0, days):
    return date.fromordinal(d0.toordinal() + days)


def run(years, imp_share, goodwill):
    """全市场 → 观测清单。每条观测带：两套口径的分、两套对齐的结局、B 节的暴露档位。"""
    db = SessionLocal()
    sids = ashare_sids(db)
    audit = load_audit(db)
    obs = []
    counts = Counter()
    for i in range(0, len(sids), BATCH):
        chunk = sids[i:i + BATCH]
        g = load_batch(db, chunk)
        ann = {t: load_announce(db, M, chunk) for t, M in
               (("ba", FinBalance), ("cf", FinCashflow), ("inc", FinIncome))}
        for sid in chunk:
            ind, ba, cf, inc = (g[k].get(sid, []) for k in ("ind", "ba", "cf", "inc"))
            if not ind:
                continue
            av = Avail((ann["ba"].get(sid, {}), ann["cf"].get(sid, {}), ann["inc"].get(sid, {})))
            f = annual_facts(sid, ind, ba, inc, av, audit, goodwill)
            for t in years:
                end = date(t, 12, 31)
                sig = av.of(end)
                counts["src:" + av.src(end)] += 1
                if t not in f:
                    counts["FY-T 年报当时未披露"] += 1
                rec = {"sid": sid, "t": t, "sig": sig, "gw": (f.get(t) or {}).get("gw")}
                for cal in CALIBERS:
                    rec[cal], _ = score_at(ind, ba, cf, av, t, cal)
                for align in ALIGNS:
                    outs, y = outcomes(f, t, sig, imp_share, align)
                    rec["out:" + align], rec["y:" + align] = outs, y
                    if align == "事件时" and y is None:
                        counts["事件窗内没出年报"] += 1
                obs.append(rec)
    db.close()
    return obs, counts


def tables(obs, key, years, labs, align):
    """按 key 取档名（None = 这条不进该表），聚成 {(T,档): Cell} 与 {档: Cell}。"""
    per_t = {(t, lab): Cell() for t in years for lab in labs}
    pooled = {lab: Cell() for lab in labs}
    for rec in obs:
        lab = key(rec)
        if lab is None:
            continue
        for c in (per_t[(rec["t"], lab)], pooled[lab]):
            c.observations += 1
            for k, out in rec["out:" + align].items():
                c.add(k, out)
    return per_t, pooled


def grid(pooled, labs, imp_share):
    ks = (("aud", "非标意见"), ("imp", f"减值≥{imp_share[0]:.0%}净资产"),
          ("imp3", f"减值≥{imp_share[1]:.0%}净资产"), ("loss", "次年净利转负"))
    print("  档位            观测数   " + "".join(f"{lab:>18}" for _, lab in ks))
    for lab in labs:
        c = pooled[lab]
        print(f"  {lab:<12} {c.observations:>8}   "
              + "".join(f"{_pct(c.rate(k)):>18}" for k, _ in ks))


def lift_table(pooled, labs, imp_share, tops, title):
    """`tops` 给一个档名或一串档名——B 节要把「商誉为主」和「无形为主」并排比。"""
    if isinstance(tops, str):
        tops = [tops]
    ks = (("aud", "非标意见"), ("imp", f"减值≥{imp_share[0]:.0%}净资产"),
          ("imp3", f"减值≥{imp_share[1]:.0%}净资产"), ("loss", "次年净利转负"))
    for top in tops:
        print(f"\n  {title}（{top}）vs 其余档：发生率 / lift / 两比例 z（|z|≥1.96 才算真分得开）")
        hi = pooled[top]
        for k, lab in ks:
            h1, n1 = hi.hit[k], hi.n[k]
            h2 = sum(pooled[l].hit[k] for l in labs if l != top)
            n2 = sum(pooled[l].n[k] for l in labs if l != top)
            r1 = h1 / n1 if n1 else None
            r2 = h2 / n2 if n2 else None
            z = ztest(h1, n1, h2, n2)
            lift = (r1 / r2) if (r1 and r2) else None
            print(f"   {lab:<18} {h1:>5}/{n1:<6}={_pct(r1)}   其余 {h2:>5}/{n2:<7}={_pct(r2)}   "
                  + (f"lift={lift:.2f}×  z={z:+.2f}" if (lift and z is not None) else "分不开"))


def direction(per_t, years, top, low):
    """上面的 z 把「家 × 年」当独立观测，同一家在多个 T 上各计一次，因而偏乐观；
    这一条不依赖独立性假设：逐个 T 年看方向，再看合并分档是否单调。"""
    seq_note = []
    for k, lab in KSLABS:
        both = [t for t in years
                if per_t[(t, top)].rate(k) is not None and per_t[(t, low)].rate(k) is not None]
        wins = sum(1 for t in both if per_t[(t, top)].rate(k) > per_t[(t, low)].rate(k))
        seq_note.append(f"   {lab:<18} {wins}/{len(both)} 个 T 年 {top} 高于 {low}")
    return seq_note


def report(obs, counts, years, imp_share, goodwill):
    n_src = sum(v for k, v in counts.items() if k.startswith("src:"))
    print(f"样本：A 股 {len({r['sid'] for r in obs})} 家 · T ∈ {list(years)} · 观测 {len(obs)} 条")
    mix = " · ".join(f"{k[4:]} {v} 条（{v / max(1, n_src) * 100:.1f}%）"
                     for k, v in sorted(counts.items(), key=lambda x: -x[1])
                     if k.startswith("src:"))
    print(f"信号日来源：{mix}")
    print(f"FY-T 年报当时尚未披露的 {counts['FY-T 年报当时未披露']} 条留在样本里，"
          f"用「只能看到 FY-(T-1) 」的信息集打分——拖延披露的恰是高危样本，剔掉等于把最需要"
          f"判断的地方删了")
    print(f"事件时窗内没出年报的 {counts['事件窗内没出年报']} 条：四类结局一律不进分母"
          f"——那是判不动，不是判成无事")

    print("\n==== A 节 · 红旗分四档 → 其后结局 ====")
    for cal, align, blurb in COMBOS:
        def key(rec, cal=cal):
            sc = rec[cal]
            return None if sc is None else bucket_of(sc)
        per_t, pooled = tables(obs, key, years, LABS, align)
        print(f"\n—— 信号 = 截至「{cal}」可得的财报 · 结局窗口 = 「{align}」——\n"
              f"   {blurb}")
        grid(pooled, LABS, imp_share)
        lift_table(pooled, LABS, imp_share, LABS[-1], "红旗极高档")
        print("\n  分与结局的相关系数（点二列，全部观测）：")
        for k, lab in KSLABS:
            pairs = [(r[cal], 1.0 if r["out:" + align][k] else 0.0) for r in obs
                     if r[cal] is not None and r["out:" + align][k] is not None]
            r_ = corr(pairs)
            print(f"   {lab:<18} " + (f"r={r_:+.4f}  n={len(pairs)}" if r_ is not None else "—"))
        print("\n  方向一致性（不拿独立性假设充数）：")
        for line in direction(per_t, years, LABS[-1], LABS[0]):
            print(line)
        for k, lab in KSLABS:
            seq = [pooled[l].rate(k) for l in LABS]
            got = [v for v in seq if v is not None]
            mono = len(got) == len(seq) and all(x <= y for x, y in zip(got, got[1:]))
            print(f"   {lab:<18} 合并四档单调上升={mono}")

    print("\n—— 口径差：信息集按公告日截断 相对 只按报告期截断（同一信号日）——")
    both = [r for r in obs if r["公告日"] is not None and r["报告期"] is not None]
    shift = Counter()
    for rec in both:
        la, lb = bucket_of(rec["公告日"]), bucket_of(rec["报告期"])
        shift["同档" if la == lb else f"{lb} → {la}"] += 1
    print(f"  两版都有分的 {len(both)} 条：同档 {shift['同档']} 条"
          f"（{shift['同档'] / max(1, len(both)) * 100:.2f}%）、挪档 {len(both) - shift['同档']} 条")
    for k, v in shift.most_common():
        if k != "同档":
            print(f"    {k}: {v}")
    if both:
        deltas = [r["公告日"] - r["报告期"] for r in both]
        print(f"  分差：均值 {statistics.mean(deltas):+.3f} · 区间 {min(deltas):+.1f} ~ {max(deltas):+.1f}")
    print("  近乎恒等是结构性的：红旗分只吃年报序列（最近两期年报 + 近 5 年年报），而信号日就是")
    print("  FY-T 年报自己的披露日，那一行按定义在信号日当天已在手——换成按公告日截断，喂进去的还是")
    print("  同一列年报。所以时点偏差不在输入端，在结局窗口端，见下一块。")

    print("\n—— 窗口差：事件时窗 相对 死盯 FY-(T+1)（同一套分）——")
    lags = [(r["sig"] - date(r["t"], 12, 31)).days for r in obs]
    print(f"  信号日距 T 年末：中位 {statistics.median(lags):.0f} 天 · 均值 "
          f"{statistics.mean(lags):.0f} 天 · 最长 {max(lags)} 天")
    late = sum(1 for r in obs if r["sig"] > date(r["t"] + 1, 4, 30))
    print(f"  晚于法定期限（次年 4-30）才披露 FY-T 年报的 {late} 条——上一版把这些公司的信号提前了")
    moved = Counter()
    for rec in obs:
        y1, y2 = rec["y:财年"], rec["y:事件时"]
        moved["同年" if y1 == y2 else ("事件窗判不动" if y2 is None else "换了财年")] += 1
    print("  结局载体：" + " · ".join(f"{k} {v} 条" for k, v in moved.most_common()))
    for k, lab in KSLABS:
        j = Counter()
        disagree = 0
        for rec in obs:
            a, b = rec["out:财年"][k], rec["out:事件时"][k]
            if a is None and b is None:
                j["两边判不动"] += 1
            elif a is None or b is None:
                j["只一边判得动"] += 1
            else:
                j["两边判得动"] += 1
                disagree += a != b
        n_ = j["两边判得动"]
        print(f"   {lab:<18} {j['两边判得动']} 条两边判得动（其中结论相反 {disagree} 条 = "
              f"{disagree / max(1, n_) * 100:.2f}%）· 只一边判得动 {j['只一边判得动']} · "
              f"两边判不动 {j['两边判不动']}")

    if goodwill:
        print("\n==== B 节 · 商誉+无形暴露 → 其后结局（给护城河那 3 分定性；结局窗口 = 事件时）====")
        for keyf, labs, top, title in (
                (lambda r: (r["gw"] or {}).get("eq"), GW_EQ_LABS, "> 0.6 权益",
                 "按归母权益分档（施洛斯扣分线 0.3/0.6）"),
                (lambda r: (r["gw"] or {}).get("asset"), GW_ASSET_LABS, "≥ 10% 总资产",
                 "按总资产分档（巴菲特护城河满分线 10%）"),
                (lambda r: (r["gw"] or {}).get("gw_asset"), GW_SHARE_LABS, "≥ 10% 总资产",
                 "只看商誉/总资产（护城河项的两个分量之一）"),
                (lambda r: (r["gw"] or {}).get("it_asset"), GW_SHARE_LABS, "≥ 10% 总资产",
                 "只看无形资产/总资产（另一个分量）"),
                (lambda r: (r["gw"] or {}).get("who"), GW_WHO_LABS,
                 ["≥10% 且商誉为主", "≥10% 且无形为主"],
                 "过了 10% 线的那批里，是商誉扛的还是无形扛的（< 10% 当基准）")):
            _, pooled = tables(obs, keyf, years, labs, "事件时")
            used = [l for l in labs if pooled[l].observations]
            print(f"\n—— {title} ——")
            grid(pooled, used, imp_share)
            if any(t in used for t in ([top] if isinstance(top, str) else top)):
                lift_table(pooled, used, imp_share, top, title)


def main():
    ap = argparse.ArgumentParser(description="红旗分与商誉暴露的判别效度回测（只读库、零积分）")
    ap.add_argument("--years", nargs="*", type=int, default=[2019, 2020, 2021, 2022, 2023, 2024],
                    help="作为「T」的报告年份，结局取各年信号日之后公开的第一份年报")
    ap.add_argument("--imp-share", nargs=2, type=float, default=[0.05, 0.03],
                    help="大额减值占当年期末归母净资产的比例：主口径 次要口径")
    ap.add_argument("--no-goodwill", action="store_true", help="跳过 B 节（商誉暴露分档）")
    a = ap.parse_args()
    obs, counts = run(a.years, a.imp_share, not a.no_goodwill)
    report(obs, counts, a.years, a.imp_share, not a.no_goodwill)


if __name__ == "__main__":
    main()
