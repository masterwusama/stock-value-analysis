# -*- coding: utf-8 -*-
"""P1:存量 JSON 数据导入 MySQL db_va。

数据源(原 GitHub Pages 静态服务,LEGACY_DATA_DIR 默认指向博客仓库):
    data/index.json            → security 主数据 + score_daily(评分快照)
    data/companies/*.json      → quote_daily / fin_indicator / fin_income /
                                 fin_balance / fin_cashflow / dividend / periodic_report
    data/events/*.json         → wind_event / wind_holder
    data/events/index.json     → score_daily.wind_*(事件增量覆盖层)
    data/valuation/latest.json → valuation_pctile(Wind 口径 PE/PB/PS 十年分位截面)
    data/actions/latest.json   → share_action(定增 / 回购全市场明细)
    agro-price/data/products.json → agro_product / agro_price
    agro-price/data/edb.json   → edb_indicator / edb_value

幂等策略:默认先清空业务表再导入(源 JSON 为唯一权威数据)。
用法(在 backend 目录下):
    python -m scripts.import_legacy             # 清空后全量导入
    python -m scripts.import_legacy --no-clean  # 不清空,按唯一键 upsert;
        wind_event/wind_holder 无自然唯一键,按 sid 先删后插(整文件权威)

2026-09-03:fin_*/dividend/periodic_report 从 INSERT IGNORE 改成按唯一键 upsert。
    旧写法下“源 JSON 是唯一权威”只对第一次入库成立:东财修订过的数字、以及后来新增的
    字段（如 财年截止）永远进不了库,深抓 job 重抓全市场实际只写进了新报告期那几行。
    改后 MySQL 不再默默钳掉超列宽/越界的值,故 _Writer 里显式截长文、越界置 NULL。
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import JSON, Numeric, String, select, text
from sqlalchemy.dialects.mysql import insert as mysql_insert

from app.config import LEGACY_DATA_DIR, MARKET_CURRENCY
from app.db import SessionLocal
from app.fin_columns import (
    BALANCE_CORE,
    CASHFLOW_CORE,
    INCOME_CORE,
    INDICATOR_CORE,
    split_core,
)
from app.models import (
    AgroPrice,
    AgroProduct,
    Dividend,
    EdbIndicator,
    EdbValue,
    EtlJobLog,
    FinBalance,
    FinCashflow,
    FinIncome,
    FinIndicator,
    FinNote,
    PeriodicReport,
    QuoteDaily,
    ScoreDaily,
    Security,
    ShareAction,
    ValuationPctile,
    WindEvent,
    WindHolder,
)

DATA_DIR = LEGACY_DATA_DIR / "data"
AGRO_DIR = LEGACY_DATA_DIR / "agro-price" / "data"

# Wind 事件类型 → etype
EVENT_TYPE_MAP = {
    "increase_hold": "增减持",
    "ma": "并购",
    "penalty": "违规",
    "lawsuit": "诉讼",
    "st_change": "ST",
}

# EDB 频率中文 → 枚举
EDB_FREQ_MAP = {"日": "day", "周": "week", "月": "month"}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def parse_date(s):
    """'2026-09-01T16:14:47+08:00' / '2026-09-01' → date;无效返回 None。"""
    if not s:
        return None
    return datetime.fromisoformat(str(s)).date()


def parse_dt(s):
    if not s:
        return None
    return datetime.fromisoformat(str(s))


def event_pick(row: dict):
    """从 Wind 事件行提取 (event_date, title);字段名随类型变化,按规则扫描。"""
    ed, title = None, None
    for k, v in row.items():
        if isinstance(v, str) and DATE_RE.match(v):
            if ed is None or any(t in k for t in ("日期", "披露日", "时间")):
                ed = parse_date(v)
        if title is None and isinstance(v, str) and len(v) > 6 and not DATE_RE.match(v):
            if any(t in k for t in ("标题", "名称", "内容")):
                title = v
    return ed, title


def holder_pick(row: dict, htype: str):
    """Wind 股东行 → wind_holder 列;字段名在 top10/institution 间不同。"""
    if htype in ("top10", "top10_float"):
        name_k, pct_k = "最新一期前十大股东名称", "最新一期前十大股东持股比例"
        shares_k = "最新一期前十大股东持股数量"
        chg_k, type_k = "股东持股数量变动", "最新一期前十大股东持股股本性质"
    elif htype in ("controller", "unlock"):
        # 实际控制人/限售解禁行字段结构与持股行不同,不做列映射,仅存 detail 原样行
        return {"rank_no": None, "holder_name": None, "pct": None,
                "shares": None, "shares_change": None, "shares_type": None,
                "detail": row}
    else:
        name_k, pct_k = "最新一期机构股东名称", "最新一期机构持股比例"
        shares_k = "最新一期机构持股数量"
        chg_k, type_k = "最新一期机构持股数量变动", None
    return {
        "rank_no": row.get("名次"),
        "holder_name": row.get(name_k),
        "pct": row.get(pct_k),
        "shares": row.get(shares_k),
        "shares_change": row.get(chg_k),
        "shares_type": row.get(type_k) if type_k else None,
        "detail": row,
    }


def school_cols(school: str):
    """index.json 流派键 → score_daily 列名前缀。"""
    return {"grahamAgg": "graham_agg", "grahamDef": "graham_def"}.get(school, school)


# ==================== 全市场规模批量写入 [扩量改造] ====================
# 5500 只 × 30+ 报告期 ≈ 百万行；逐行 db.execute 在本机要跑几十分钟，故按表攒批走
# executemany（pymysql 拼多值 INSERT）+ 分批 commit，语句模板按（表, 列集, 冲突策略）缓存。
BULK_ROWS = 2000          # 单次 executemany 行数（受 max_allowed_packet 限制前停）
BULK_COMPANIES = 25       # 每导入 N 家冲刷 + commit 一次（中断不丢、内存有界）

# (公司 JSON 字段, 模型, 核心列映射, 报告期键)
FIN_SPECS = (
    ("indicators", FinIndicator, INDICATOR_CORE, "报告期"),
    ("income", FinIncome, INCOME_CORE, "报告日"),
    ("balance", FinBalance, BALANCE_CORE, "报告日"),
    ("cashflow", FinCashflow, CASHFLOW_CORE, "报告日"),
)
SEC_KEYS = ("code", "market", "name", "industry", "currency", "status",
            "list_date", "updated_at")
QUOTE_KEYS = ("sid", "trade_date", "price", "change_pct", "pe_ttm", "pb", "market_cap",
              "float_market_cap", "turnover_rate", "fetched_at")
DIV_KEYS = ("sid", "div_year", "div_type", "description", "bonus_per_10",
            "transfer_per_10", "announce_date", "record_date", "ex_date", "pay_date")
RPT_KEYS = ("sid", "report_date", "category", "title", "pdf_url", "detail_url",
            "audit_firm", "audit_opinion")
# 财务四表公共列（各表再拼自己的核心科目列，见 import_companies）
FIN_KEYS = ("sid", "report_date", "updated_at", "extras")
# 附注表：定期存款 / 受限货币资金 + 出自哪份披露
NOTE_KEYS = ("sid", "report_date", "term_deposit", "restricted_cash",
             "source", "updated_at")
# 估值分位表（外源截面，每标的一行）。与其它表不同：源 latest.json 是累计快照，
# 本轮判定不可信（亏损 / 序列停更）就是要抹空，不能 COALESCE 留住上一轮的假数
PCT_KEYS = ("sid", "trade_date", "pe_pctile", "pb_pctile", "ps_pctile",
            "pe_days", "pb_days", "ps_days", "source", "updated_at")
# 股本事件表（定增 + 回购一表两族）。列名与 collector/fetch_actions.py 产出的字段一一对应，
# 只有两处例外：日期列要过 parse（源是文本），长尾字段收进 detail JSON。
ACTION_KEYS = ("sid", "kind", "src_id", "name", "issue_date", "listing_date",
               "plan_notice_date", "price", "num", "raise_funds", "notice_date",
               "finish_date", "progress", "progress_label", "finished",
               "plan_price_cap", "plan_num_lower", "plan_num_cap",
               "plan_amount_lower", "plan_amount_cap", "done_num", "done_amount",
               "done_price", "purpose", "cancel_type", "evidence", "updated_at",
               "detail")
_ACT_DATES = ("issue_date", "listing_date", "plan_notice_date", "notice_date",
              "finish_date")
# 列表页用不上的字段：存进行级 JSON，既保住核对所需的原文，又不占列宽
_ACT_JSON = ("apply_price", "price_before", "raise_ratio", "share_before",
             "share_after", "way", "lockin", "target", "market", "objective",
             "dim_date", "start_date", "end_date", "done_price_high",
             "done_price_low")
# 证券主数据：名称/行业/上市日缺失时不用 NULL 覆盖已有值
SEC_UPD = ("name=VALUES(name), "
           "industry=COALESCE(VALUES(industry), industry), "
           "list_date=COALESCE(VALUES(list_date), list_date), "
           "updated_at=VALUES(updated_at)")


def coalesce_upd(cols):
    """ON DUPLICATE KEY UPDATE 子句：本轮没抓到的字段（NULL）保留库里旧值。

    源 JSON 是权威，撞唯一键必须更新，不能靠 INSERT IGNORE 默默丢掉：那会让东财
    修订过的数字、以及后加的字段（如 2026-09-03 的 财年截止）永远进不了库，
    深抓 job 每天重抓全市场财务实际只写进了新报告期那几行。
    抓到 0 仍会覆盖（0 不是 NULL），只有真缺值才留旧。
    """
    return ", ".join(f"`{c}`=COALESCE(VALUES(`{c}`), `{c}`)" for c in cols)


class _Writer:
    """一张表一个写入器：攒够 BULK_ROWS 冲刷一次，commit 时机由调用方掌握。"""

    def __init__(self, db, stats, model, keys=None, mode="ignore",
                 upd_skip=(), upd_expr=None, upd_coalesce=False):
        tbl = model.__table__
        self.cols = [k for k in (keys or [c.name for c in tbl.columns]) if k in tbl.c]
        self.json_cols = {c.name for c in tbl.columns
                          if c.name in self.cols and isinstance(c.type, JSON)}
        self.str_len = {c.name: c.type.length for c in tbl.columns
                        if c.name in self.cols and isinstance(c.type, String) and c.type.length}
        # 定点列可表达的最大绝对值：Numeric(p,s) 的整数位只有 p-s 位
        self.num_max = {c.name: 10 ** (c.type.precision - c.type.scale) - 1
                        for c in tbl.columns
                        if c.name in self.cols and isinstance(c.type, Numeric)
                        and c.type.precision and c.type.scale is not None}
        names = ", ".join(f"`{c}`" for c in self.cols)
        holders = ", ".join(f":{c}" for c in self.cols)
        if mode == "ignore":
            sql = f"INSERT IGNORE INTO {tbl.name} ({names}) VALUES ({holders})"
        else:
            upd = [c for c in self.cols if c not in upd_skip]
            if upd_expr:
                expr = upd_expr
            elif upd_coalesce:
                expr = coalesce_upd(upd)
            else:
                expr = ", ".join(f"`{c}`=VALUES(`{c}`)" for c in upd)
            sql = f"INSERT INTO {tbl.name} ({names}) VALUES ({holders}) ON DUPLICATE KEY UPDATE {expr}"
        self.stmt, self.db, self.stats = text(sql), db, stats
        self.table, self.rows = tbl.name, []

    def add(self, row):
        """一行入集：按列型预处理（截长文 / 越界置 NULL）后再交给 executemany。

        以前走 INSERT IGNORE， MySQL 会默默把这些值按列宽/量程钳掉（1406/1264 降为
        warning）；改 upsert 后同样的数据会直接抛错、把整轮回灌（含全市场行情快照）
        一起回滚。钳位与置 NULL 的口径收回到这里，统一计数：核心列只是查询索引，
        真值原样存在 extras 里，存一个“假的最大值”只会污染排序/筛选。
        """
        vals = {}
        for c in self.cols:
            v = row.get(c)
            if c in self.json_cols and v is not None:
                v = json.dumps(v, ensure_ascii=False)
            elif isinstance(v, str):
                lim = self.str_len.get(c)
                if lim and len(v) > lim:
                    # 截头：分红方案/公告标题的开头就是人要看的部分，比丢整行好
                    v = v[:lim]
                    self.stats[f"{self.table}.clipped"] += 1
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                lim = self.num_max.get(c)
                if lim is not None and abs(v) >= lim:
                    v = None
                    self.stats[f"{self.table}.out_of_range"] += 1
            vals[c] = v
        self.rows.append(vals)
        if len(self.rows) >= BULK_ROWS:
            self.flush()

    def flush(self):
        rows, self.rows = self.rows, []
        if not rows:
            return
        bad_total = 0
        for i in range(0, len(rows), BULK_ROWS):
            batch = rows[i:i + BULK_ROWS]
            try:
                # SAVEPOINT 包住每批：一批出错只退这一批，不把已扫进事务的
                # 前几千行连带丢给外层 rollback
                with self.db.begin_nested():
                    self.db.execute(self.stmt, batch)
            except Exception as e:
                # 整批被一行坏数据带崩（见过 1406/1264）：逐行重试，只丢真装不下的行
                bad, first = 0, None
                for r in batch:
                    try:
                        with self.db.begin_nested():
                            self.db.execute(self.stmt, r)
                    except Exception as e2:
                        bad += 1
                        first = first or f"{type(e2).__name__}: {e2}"[:200]
                bad_total += bad
                self.stats[f"{self.table}.bad_rows"] += bad
                print(f"[warn] {self.table} 批量写入报错退回逐行，跳过 {bad}/{len(batch)} 行"
                      f"；首个错误 {first or repr(e)[:200]}", flush=True)
        self.stats[self.table] += len(rows) - bad_total


def load_sid_map(db):
    """全量 (code, market) → sid，避免逐只 SELECT（5500 次往返）。"""
    rows = db.execute(select(Security.code, Security.market, Security.sid)).all()
    return {(r.code, r.market): r.sid for r in rows}


def build_score_rows(index, trade_date, db):
    """index.json companies[] → score_daily 行（含 Wind 事件覆盖层与硬门槛标记）。"""
    score_rows = {}
    for c in index.get("companies", []):
        sc = c.get("scores") or {}
        refs = sc.get("priceRefs") or {}
        calc = refs.get("netCashCalc") or {}
        row = {
            "trade_date": trade_date,
            # 评分基准报告期：优先用评分引擎给的最新年报期（compute_scores.reportDate，与
            # annual_rows 只取 12-31 的口径同源），再退净现金代入明细里的期次，最后才用跑数日。
            # 旧写法只认 calc.report，算不出净现金的公司就整片回落成跑数日（实测 2026-09-12
            # 那轮 5095/6939 行如此），报告龄与「这批分用的哪一期财报」双双无法回答。
            "report_date": (parse_date(sc.get("reportDate")) or parse_date(calc.get("report"))
                            or trade_date),
            "score_graham_agg": sc.get("grahamAgg"),
            "score_graham_def": sc.get("grahamDef"),
            "score_schloss": sc.get("schloss"),
            "score_buffett": sc.get("buffett"),
            "fraud": sc.get("fraud"),
            "mgmt": sc.get("mgmt"),
            "cycle": sc.get("cycle"),
            "cyclical": sc.get("cyclical"),
            "cycle_trend": sc.get("cycleTrend"),
            "trap": sc.get("trap"),
            "trap_c": sc.get("trapC"),
            "trap_eval": sc.get("trapEval"),
            "fair_liq": refs.get("fairLiq"),
            "net_cash_ratio": refs.get("netCashRatio"),
            "net_cash_calc": calc or None,
            "updated_at": parse_dt(index["updated_at"]),
        }
        for school in ("grahamAgg", "grahamDef", "schloss", "buffett"):
            col = school_cols(school)
            r = refs.get(school) or {}
            row[f"buy_{col}"] = r.get("buy")
            row[f"sell_cons_{col}"] = r.get("sellCons")
            row[f"sell_fair_{col}"] = r.get("sellFair")
        score_rows[c["code"]] = row

    # Wind 事件覆盖层并入评分行（三字段拆列供列表计算 + 原始条目全量透传供详情⑨总览）
    ov_path = DATA_DIR / "events" / "index.json"
    if ov_path.exists():
        overlay = json.loads(ov_path.read_text(encoding="utf-8")).get("byCode", {})
        for code, ov in overlay.items():
            if code in score_rows:
                score_rows[code]["wind_fraud_delta"] = ov.get("fraudDelta")
                score_rows[code]["wind_mgmt_delta"] = ov.get("mgmtDelta")
                score_rows[code]["wind_flags"] = ov.get("flags")
                score_rows[code]["wind_overlay"] = ov

    # 硬门槛：拿库里已导入的定性事实贴标，不改任何分数（见 GATE_FLAGS）
    hits, judgeable = load_gate_facts(db)
    for code, row in score_rows.items():
        flags = hits.get(code)
        row["gate_flags"] = flags or None
        # 命中=True；可判但未命中=False；一个信号都判不了（未抓财务、无审计无权益）=NULL，
        # NULL 不等于通过，所以列表页默认不拿它排除任何标的
        row["gate"] = True if flags else (False if code in judgeable else None)
    return guard_score_batch(db, score_rows, trade_date)


# ---------- 评分批次护栏 ----------
# 评分是财务的函数，而 fetch_data 每个数据块都是 `except → result[key] = []` 后整档盖回，
# 所以上游一次限流就能把已经抓好的明细覆写成空（2026-09-12 实测：4832 家的三大表被清空，
# 随后重算把 fraud=0 的家数从 647 抬到 2956）。这种「分数集体变干净」比分数算错更坏——
# 筛选会放过一批本该拦住的标的，而链路上每一步都写着 success。
# 所以在唯一的写入口比一次批次分布：与库里上一批比，某个「越低越好」列的零值或 NULL 家数
# 同时超过 2 倍且多出 500 家（两个条件都要满足才判，避免把小盘日的正常抖动当成洗盘），
# 或者行数掉了一成，就整批不写。扣住这批的后果是列表页停在旧分那一天的截面（表头快照日
# 取行情与评分两张表最新日的交集），行情照写——宁可用昨天的分，也不用一片假的 0 分。
SCORE_HOLD_COLS = ("fraud", "trap")     # 0 是「无红旗 / 无证据」那一端的两个分，最容易被洗成 0
SCORE_HOLD_RATIO = 2.0
SCORE_HOLD_FLOOR = 500
SCORE_HOLD_ROW_DROP = 0.9
SCORE_PREV_SQL = """
SELECT t.trade_date, COUNT(*) n,
       SUM(fraud = 0), SUM(fraud IS NULL), SUM(trap = 0), SUM(trap IS NULL)
FROM score_daily t
WHERE t.trade_date = (SELECT MAX(trade_date) FROM score_daily WHERE trade_date < :d)
GROUP BY t.trade_date
"""
# 本轮扣住了哪些批（main 据此把这次回灌记成 failed 并非零退出，否则红不起来）
hold_notes: list[str] = []


def guard_score_batch(db, rows, trade_date):
    """比对上一批分布，异常就返回 {}（本轮不写 score_daily）并把原因记进 `hold_notes`。"""
    if not rows:
        return rows
    prev = db.execute(text(SCORE_PREV_SQL), {"d": trade_date}).first()
    if not prev or not prev[1]:
        return rows           # 库里没有上一批可比（首轮导入），不设卡
    prev_date, prev_n, *prev_counts = prev
    prev_n = int(prev_n)
    prev_counts = [int(x or 0) for x in prev_counts]
    new_n = len(rows)
    counters = {c: [0, 0] for c in SCORE_HOLD_COLS}
    for r in rows.values():
        for i, c in enumerate(SCORE_HOLD_COLS):
            v = r.get(c)
            if v is None:
                counters[c][1] += 1
            elif v == 0:
                counters[c][0] += 1
    bad = []
    if new_n < prev_n * SCORE_HOLD_ROW_DROP:
        bad.append(f"行数 {prev_n} → {new_n}")
    for idx, c in enumerate(SCORE_HOLD_COLS):
        was_zero, was_null = prev_counts[idx * 2], prev_counts[idx * 2 + 1]
        if was_null >= prev_n:
            continue     # 上一批整列没值 = 这一列刚上线，没有基线可比，别把首批判成洗盘
        for label, now, was in (("零值", counters[c][0], was_zero),
                                ("NULL", counters[c][1], was_null)):
            if now > max(was * SCORE_HOLD_RATIO, was + SCORE_HOLD_FLOOR):
                bad.append(f"{c} {label}家数 {was} → {now}")
    if not bad:
        return rows
    note = (f"评分批次护栏触发（对比 {prev_date} 那批）：{'；'.join(bad)} —— "
            f"本轮 {new_n} 行评分不写 score_daily，列表页停在上一批分数")
    hold_notes.append(note)
    print(f"⚠ {note}", flush=True)
    return {}


# ---------- 硬门槛（score_daily.gate / gate_flags）----------
# 四派分是排序工具，只回答「相对本派锚位便不便宜」；但有些定性事实一旦成立，分数再高
# 也不该进买入区——审计非标、被交易所风险警示、有立案/处罚记录、已经资不抵债。
# 这类信号刻意不进任何加权项：一个二元否决被稀释成「扣几分」后既拦不住东西也解释不了。
# 口径全取库里既成事实（不额外花抓取额度）；Wind 事件只覆盖 16 家、靠不住，所以风险警示
# 按证券简称判，与列表页原有的「剔除 ST」同一口径。
GATE_FLAGS = {
    "audit_qualify": "最近一份年报的审计意见非标准无保留意见",
    "risk_warning": "证券简称含 ST / *ST（交易所风险警示）",
    "case_filed": "Wind 事件里有违规/立案/处罚记录",
    "neg_equity": "最新年报归母股东权益为负（资不抵债）",
}
# 视同「标准无保留」的意见文本：A 股披露写「标准无保留意见」，英/港文本是「无保留意见」，
# 两者都不是红旗；带强调事项段/保留/否定/无法表示意见才算。
AUDIT_CLEAN = {"标准无保留意见", "无保留意见"}

# periodic_report.report_date 存的是公告日（源 JSON 的 date），所以「MAX(report_date)
# 的那一份年报」正好就是「最近披露的那份年报」，不必再推期次
GATE_AUDIT_SQL = """
SELECT s.code, p.audit_opinion
FROM periodic_report p
JOIN (SELECT sid, MAX(report_date) md FROM periodic_report
      WHERE category='年报' AND audit_opinion IS NOT NULL GROUP BY sid) t
  ON t.sid = p.sid AND t.md = p.report_date
JOIN security s ON s.sid = p.sid
WHERE p.category='年报'
"""
# 最新年报的归母权益（港美股财年已归一到 12-31，同一句子三市场共用）
GATE_EQUITY_SQL = """
SELECT s.code, b.equity_parent, b.equity_total
FROM fin_balance b
JOIN (SELECT sid, MAX(report_date) md FROM fin_balance
      WHERE DATE_FORMAT(report_date, '%m-%d') = '12-31' GROUP BY sid) t
  ON t.sid = b.sid AND t.md = b.report_date
JOIN security s ON s.sid = b.sid
WHERE DATE_FORMAT(b.report_date, '%m-%d') = '12-31'
"""
GATE_CASE_SQL = """
SELECT DISTINCT s.code FROM wind_event w
JOIN security s ON s.sid = w.sid
WHERE w.etype IN ('违规', '立案', '处罚', 'ST')
"""
GATE_NAME_SQL = "SELECT code, market, name FROM security"


def load_gate_facts(db):
    """库里的定性事实 → (命中标记 {code: [GATE_FLAGS 键]}, 至少有一个可判信号的 code 集)。

    本函数在两条回灌路径里都在财务表回灌**之前**跑（评分行要先建好），所以用的是上一轮的
    权益与审计意见。年报口径一年只变一次，滞后一轮不影响门槛判断；真要立刻反映就再跑一次 import。
    """
    hits: dict[str, list[str]] = {}
    judgeable: set[str] = set()

    def mark(code, flag=None):
        if code is None:
            return
        judgeable.add(code)
        if flag:
            hits.setdefault(code, []).append(flag)

    for code, opinion in db.execute(text(GATE_AUDIT_SQL)):
        mark(code, 'audit_qualify' if opinion and str(opinion).strip() not in AUDIT_CLEAN else None)
    for code, parent, total in db.execute(text(GATE_EQUITY_SQL)):
        eq = parent if parent is not None else total
        mark(code, 'neg_equity' if (eq is not None and float(eq) < 0) else None)
    for (code,) in db.execute(text(GATE_CASE_SQL)):
        mark(code, 'case_filed')
    # 风险警示只对 A 股成立：美股简称里带 ST 的（STAR、Stifel…）不是风险警示，实测误命中 88 家
    for code, market, name in db.execute(text(GATE_NAME_SQL)):
        if market == 'A':
            mark(code, 'risk_warning' if 'ST' in str(name or '').upper() else None)

    order = {k: i for i, k in enumerate(GATE_FLAGS)}
    for flags in hits.values():
        flags.sort(key=lambda k: order.get(k, 99))
    return hits, judgeable


def import_scores_only(db, stats, quiet=False):
    """index.json → score_daily 单表（评分算法变更后只刷分，不碰其他业务表）。

    全量与 --snapshot-only 两条回灌路径都会顺带重写 security/quote_daily 与财务明细，
    而改一次评分公式往往只需要把 6939 行分数换掉：本函数只做 _Writer(score_daily) 的
    upsert，既不建主数据也不写行情，永远不会清空任何表。
    sid 拿不到的标的（新上市、还没建主数据）跳过并报数，留给下一轮深抓。
    """
    index = json.loads((DATA_DIR / "index.json").read_text(encoding="utf-8"))
    trade_date = parse_date(index["updated_at"])
    score_rows = build_score_rows(index, trade_date, db)
    sids = load_sid_map(db)
    w_score = _Writer(db, stats, ScoreDaily, mode="upsert",
                      upd_skip=("sid", "trade_date"))
    written = 0
    for c in index.get("companies") or []:
        code, market = c.get("code"), c.get("market") or "A"
        sid = sids.get((code, market))
        if sid is None or code not in score_rows:
            stats["skipped_nosec"] += 1
            continue
        w_score.add(dict(sid=sid, **score_rows[code]))
        written += 1
    w_score.flush()
    db.commit()
    if not quiet:
        print(f"  [import] 仅评分 {written} 行 · 评分日 {trade_date}", flush=True)


def import_index_snapshot(db, stats, quiet=False):
    """index.json → security / quote_daily / score_daily（全市场日更的唯一写入面）。

    --snapshot-only 只刷腾讯批量行情、不重抓财务，companies/*.json mtime 不变；
    若仍走全目录导入就是白读 5500 个文件。本函数只读一个 index.json，干完行情与
    评分两张快照表，新上市标的的 security 主数据也在这里建（财务等下轮深抓）。
    """
    index = json.loads((DATA_DIR / "index.json").read_text(encoding="utf-8"))
    trade_date = parse_date(index["updated_at"])
    score_rows = build_score_rows(index, trade_date, db)

    w_sec = _Writer(db, stats, Security, SEC_KEYS, mode="upsert", upd_expr=SEC_UPD)
    w_quote = _Writer(db, stats, QuoteDaily, QUOTE_KEYS, mode="upsert",
                      upd_skip=("sid", "trade_date"))
    w_score = _Writer(db, stats, ScoreDaily, mode="upsert",
                      upd_skip=("sid", "trade_date"))
    sids = load_sid_map(db)
    updated_at = parse_dt(index["updated_at"]) or datetime.now()

    for n, c in enumerate(index.get("companies") or [], 1):
        code, market = c.get("code"), c.get("market") or "A"
        if not code:
            continue
        w_sec.add(dict(code=code, market=market, name=c.get("name") or code,
                       industry=c.get("industry"), currency=MARKET_CURRENCY[market],
                       status="active", list_date=None, updated_at=updated_at))
        if n % BULK_COMPANIES == 0:
            w_sec.flush()
            db.commit()
            sids = load_sid_map(db)

    # 主数据先落库（新上市代码要能拿到 sid 给下面两张子表）
    w_sec.flush()
    db.commit()
    sids = load_sid_map(db)

    for c in index.get("companies") or []:
        code, market = c.get("code"), c.get("market") or "A"
        sid = sids.get((code, market))
        if sid is None:
            stats["skipped_nosec"] += 1
            continue
        q = c.get("quote") or {}
        qdate = parse_date(q.get("time")) or trade_date
        if q and qdate:
            w_quote.add(dict(
                sid=sid, trade_date=qdate,
                price=q.get("price"), change_pct=q.get("change_pct"),
                pe_ttm=q.get("pe_ttm"), pb=q.get("pb"),
                market_cap=q.get("market_cap"), float_market_cap=q.get("float_market_cap"),
                turnover_rate=q.get("turnover_rate"),
                fetched_at=parse_dt(q.get("time")) or updated_at,
            ))
        if code in score_rows:
            w_score.add(dict(sid=sid, **score_rows[code]))

    for w in (w_sec, w_quote, w_score):
        w.flush()
    db.commit()
    if not quiet:
        print(f"  [import] index 快照 {len(index.get('companies') or [])} 条 · "
              f"交易日 {trade_date}", flush=True)


def import_companies(db, stats, only_fresh=None, quiet=False, head=True):
    """companies/*.json + index.json → 行情/财务/分红/报告/评分（攒批写入）。

    only_fresh：仅导入 mtime 在 N 小时内的公司文件（日更增量，旧行已在库中）。
    head=False：不写 security/quote_daily/score_daily 三张表头（由 import_index_snapshot
    统一处理，避开全市场日更重复写 5500 行）。
    报告期无法解析的行直接丢弃（旧路径依赖 INSERT IGNORE 默默掉，批量下需前置过滤）。
    """
    index = json.loads((DATA_DIR / "index.json").read_text(encoding="utf-8"))
    trade_date = parse_date(index["updated_at"])
    by_code = {c["code"]: c for c in index.get("companies", []) if c.get("code")}
    score_rows = build_score_rows(index, trade_date, db)

    files = sorted((DATA_DIR / "companies").glob("*.json"))
    if only_fresh:
        cutoff = time.time() - only_fresh * 3600
        files = [p for p in files if p.stat().st_mtime >= cutoff]
    if not files:
        print("[import] 无待导入的公司 JSON")
        return

    writers = {}

    def writer(name, model, keys=None, **kw):
        w = writers.get(name)
        if w is None:
            w = writers[name] = _Writer(db, stats, model, keys, **kw)
        return w

    w_sec = writer("security", Security, SEC_KEYS, mode="upsert", upd_expr=SEC_UPD)
    w_quote = writer("quote_daily", QuoteDaily, QUOTE_KEYS, mode="upsert",
                     upd_skip=("sid", "trade_date"))
    w_score = writer("score_daily", ScoreDaily, mode="upsert",
                     upd_skip=("sid", "trade_date"))
    # 分红/报告/财务四表都有唯一键（uk 或复合主键），故按源 JSON 更新而非忽略；
    # 主键列不参与 SET 子句（upd_skip），余下各列缺值时留库旧值（upd_coalesce）
    w_div = writer("dividend", Dividend, DIV_KEYS, mode="upsert",
                   upd_skip=("sid",), upd_coalesce=True)
    w_rpt = writer("periodic_report", PeriodicReport, RPT_KEYS, mode="upsert",
                   upd_skip=("sid", "report_date"), upd_coalesce=True)
    w_fin = {
        spec[1].__tablename__: writer(
            spec[1].__tablename__, spec[1],
            FIN_KEYS + tuple(spec[2]),
            mode="upsert", upd_skip=("sid", "report_date"), upd_coalesce=True,
        )
        for spec in FIN_SPECS
    }
    # 附注表同键；某期本轮没再解析出来（PDF 换链接、闭合不上）时留库里旧值——
    # 那一期的披露本来就不会变坏
    w_note = writer("fin_note", FinNote, NOTE_KEYS, mode="upsert",
                    upd_skip=("sid", "report_date"), upd_coalesce=True)

    sids = load_sid_map(db)
    total, t0 = len(files), time.time()
    for n, path in enumerate(files, 1):
        d = json.loads(path.read_text(encoding="utf-8"))
        code, name = d["code"], d["name"]
        info = d.get("info") or {}
        idx_c = by_code.get(code, {})
        # A 股公司 JSON 无 market 字段,优先取 index.json,回退 A
        market = idx_c.get("market") or d.get("market") or "A"
        updated_at = parse_dt(d.get("updated_at")) or parse_dt(index["updated_at"])
        list_dt = None
        if info.get("上市日期"):
            try:
                list_dt = datetime.strptime(str(info["上市日期"])[:10], "%Y-%m-%d").date()
            except ValueError:
                list_dt = None
        sec = w_sec
        if head:
            sec.add(dict(code=code, market=market, name=name,
                         industry=idx_c.get("industry") or info.get("行业"),
                         currency=MARKET_CURRENCY[market], status="active",
                         list_date=list_dt, updated_at=updated_at))
        sid = sids.get((code, market))
        if sid is None:
            if not head:
                # 表头已由 import_index_snapshot 落库：仍无此证券 = 代码不在 index.json
                # （上一轮抓到一半被中断的孤儿文件），财务行跳过不报错
                stats["skipped_nosec"] += 1
                continue
            # 新证券：先把主数据落库再取 sid（子表有外键约束）
            sec.flush()
            db.commit()
            sid = db.execute(select(Security.sid).where(
                Security.code == code, Security.market == market)).scalar_one()
            sids[(code, market)] = sid

        snap = d.get("snapshot") or {}
        # index.json 的 quote 块由 --snapshot-only 日更，比未重抓的公司快照新时优先
        idx_q = idx_c.get("quote") or {}
        if str(idx_q.get("time") or "") > str(snap.get("time") or ""):
            snap = idx_q
        if snap and head:
            qdate = parse_date(snap.get("time")) or updated_at.date()
            w_quote.add(dict(
                sid=sid, trade_date=qdate,
                price=snap.get("price"), change_pct=snap.get("change_pct"),
                pe_ttm=snap.get("pe_ttm"), pb=snap.get("pb"),
                market_cap=snap.get("market_cap"),
                float_market_cap=snap.get("float_market_cap"),
                turnover_rate=snap.get("turnover_rate"),
                fetched_at=updated_at,
            ))

        for key, model, core_map, date_col in FIN_SPECS:
            w = w_fin[model.__tablename__]
            for r in d.get(key) or []:
                rd = parse_date(r.get(date_col))
                if not rd:
                    stats["skipped_rows"] += 1
                    continue
                core, extras = split_core(r, core_map)
                w.add(dict(sid=sid, report_date=rd, updated_at=updated_at,
                           extras=extras, **core))

        for day, v in (d.get("notes") or {}).items():
            rd = parse_date(day)
            if not rd:
                stats["skipped_rows"] += 1
                continue
            w_note.add(dict(sid=sid, report_date=rd, updated_at=updated_at,
                             term_deposit=v.get("termDeposit"),
                             restricted_cash=v.get("restrictedCash"),
                             source=v.get("source")))

        for r in d.get("dividends") or []:
            w_div.add(dict(
                sid=sid, div_year=r.get("year"), div_type=r.get("type"),
                description=r.get("description"),
                bonus_per_10=r.get("bonus_per_10"), transfer_per_10=r.get("transfer_per_10"),
                announce_date=parse_date(r.get("announce_date")),
                record_date=parse_date(r.get("record_date")),
                ex_date=parse_date(r.get("ex_date")), pay_date=parse_date(r.get("pay_date")),
            ))

        for r in d.get("reports") or []:
            if not r.get("date"):
                continue
            w_rpt.add(dict(
                sid=sid, report_date=parse_date(r["date"]), category=r.get("category", ""),
                title=r.get("title"), pdf_url=r.get("pdf_url"), detail_url=r.get("detail_url"),
                audit_firm=r.get("audit_firm"), audit_opinion=r.get("audit_opinion"),
            ))

        if head and code in score_rows:
            w_score.add(dict(sid=sid, **score_rows[code]))

        if n % BULK_COMPANIES == 0:
            for w in writers.values():
                w.flush()
            db.commit()
        if not quiet and (n % 500 == 0 or n == total):
            print(f"  [import] {n}/{total} 家 · {time.time() - t0:.0f}s · "
                  f"{sum(len(x.rows) for x in writers.values())} 行待刷", flush=True)

    for w in writers.values():
        w.flush()
    db.commit()


def import_events(db, stats):
    """events/*.json → wind_event / wind_holder。"""
    stats.setdefault("skipped_events", 0)
    for path in sorted((DATA_DIR / "events").glob("*.json")):
        if path.name == "index.json":
            continue
        d = json.loads(path.read_text(encoding="utf-8"))
        row = db.execute(
            select(Security.sid).where(Security.code == d["code"], Security.market == "A")
        ).first()
        if not row:
            stats["skipped_events"] += 1
            continue
        sid = row[0]
        fetched = parse_dt(d.get("fetched_at")) or datetime.now()
        # [P5 修复] wind_event/wind_holder 无自然唯一键,增量回灌时 IGNORE 不生效会重复插行;
        # 单文件即该证券全量权威 → 先删后插,天然幂等且对内容变更免疫
        db.execute(text("DELETE FROM wind_event WHERE sid = :s"), {"s": sid})
        db.execute(text("DELETE FROM wind_holder WHERE sid = :s"), {"s": sid})
        for group, etype in EVENT_TYPE_MAP.items():
            for rec in (d.get("events") or {}).get(group) or []:
                ed, title = event_pick(rec)
                db.execute(mysql_insert(WindEvent).values(
                    sid=sid, etype=etype, event_date=ed or fetched.date(),
                    title=title, detail=rec, fetched_at=fetched,
                ))
                stats["wind_event"] += 1
        for group, htype in (("top10", "top10"), ("top10_float", "top10_float"),
                             ("institutions", "institution"),
                             ("actual_controller", "controller"), ("unlock", "unlock")):
            for rec in (d.get("holders") or {}).get(group) or []:
                vals = holder_pick(rec, htype)
                db.execute(mysql_insert(WindHolder).values(
                    sid=sid, holder_type=htype, report_date=None,
                    fetched_at=fetched, **vals,
                ))
                stats["wind_holder"] += 1


def import_agro(db, stats):
    """products.json → agro_product / agro_price（JSON 为唯一权威，源里删掉的点同步删）。"""
    src = json.loads((AGRO_DIR / "products.json").read_text(encoding="utf-8"))
    for p in src.get("products", []):
        stmt = mysql_insert(AgroProduct).values(
            product_id=p["id"], name=p["name"], category=p.get("category", ""),
            spec=p.get("spec"), unit=p.get("unit"), primary_source=None,
            config=None, active=True,
        ).on_duplicate_key_update(
            name=p["name"], category=p.get("category", ""),
            spec=p.get("spec"), unit=p.get("unit"),
        )
        db.execute(stmt)
        stats["agro_product"] += 1
        keep = set()
        for pr in p.get("prices", []):
            d = parse_date(pr["date"])
            stmt = mysql_insert(AgroPrice).values(
                product_id=p["id"], price_date=d,
                source=pr.get("source", ""), price=pr["price"], note=pr.get("note"),
            ).on_duplicate_key_update(price=pr["price"], note=pr.get("note"))
            db.execute(stmt)
            stats["agro_price"] += 1
            keep.add((d, pr.get("source", "")))
        # 只 upsert 不删会让库里残留源里已消失的行：采集端 merge_prices 明确会丢弃
        # 「本轮窗口内却没重新解析到」的日期（2026-09-03 对硝基氯化苯 08-03 那条），
        # 丢弃即 products.json 删点，DB 不跟着删就与唯一真源永久背离、verify_api 常红。
        # 按 (price_date, source) 差集删，不能只按日期——同一天可能有多个交易商来源。
        for row in db.execute(text(
                "SELECT price_date, source FROM agro_price WHERE product_id = :p"),
                {"p": p["id"]}).fetchall():
            if (row[0], row[1]) not in keep:
                db.execute(text("DELETE FROM agro_price WHERE product_id = :p"
                                " AND price_date = :d AND source = :s"),
                           {"p": p["id"], "d": row[0], "s": row[1]})
                stats["agro_price_stale_deleted"] += 1

    live = {p["id"] for p in src.get("products", [])}
    for row in db.execute(select(AgroProduct.product_id)).fetchall():
        # 产品整体从源里撤掉时同理：两表间无外键约束，不跟着删就留下孤儿明细行，
        # 从此再没有一轮回灌能命中它（差集只按源里现存产品算）
        if row[0] not in live:
            db.execute(text("DELETE FROM agro_price WHERE product_id = :p"), {"p": row[0]})
            db.execute(text("DELETE FROM agro_product WHERE product_id = :p"), {"p": row[0]})
            stats["agro_product_stale_deleted"] += 1


def import_edb(db, stats):
    """edb.json → edb_indicator / edb_value。"""
    src = json.loads((AGRO_DIR / "edb.json").read_text(encoding="utf-8"))
    for ci, cat in enumerate(src.get("categories", [])):
        for ii, ind in enumerate(cat.get("indicators", [])):
            freq = EDB_FREQ_MAP.get(str(ind.get("freq", "")).strip(), "month")
            extra = {
                "source": ind.get("source"), "name": ind.get("name"),
                "cat_name": cat.get("name"), "cat_idx": ci, "ind_idx": ii,
            }
            ival = dict(
                edb_code=ind["code"], name=ind.get("label") or ind.get("name", ""),
                category=cat["id"], freq=freq, unit=ind.get("unit"),
                display_group=ind.get("group"), extra=extra,
            )
            iupd = {k: v for k, v in ival.items() if k != "edb_code"}
            db.execute(mysql_insert(EdbIndicator)
                       .values(**ival).on_duplicate_key_update(**iupd))
            stats["edb_indicator"] += 1
            keep = set()
            for pt in ind.get("points", []):
                if len(pt) != 2 or not pt[0]:
                    continue
                d = parse_date(pt[0])
                stmt = mysql_insert(EdbValue).values(
                    edb_code=ind["code"], data_date=d, val=pt[1],
                ).on_duplicate_key_update(val=pt[1])
                db.execute(stmt)
                stats["edb_value"] += 1
                keep.add(d)
            # 与 import_agro 同理：--no-clean 下只 upsert 不删，指标撤点会永久残留。
            # 但删除要限定在源自身覆盖的区间内：Wind 积分有限（2026-09-03 约剩 1000），
            # 而 fetch_edb 默认只抓近 365 天，一旦 edb.json 被整份覆盖成一年数据，
            # 无条件差集会把库里回溯到前几年的点一起抹掉，而且补不回来。区间外的历史点
            # 保留（真要清库用不带 --no-clean 的全量重灌）。
            src_dates = sorted(x for x in keep if x is not None)
            if not src_dates:
                stats["edb_value_delete_skipped"] += 1
                continue  # 源里该指标一个可用点都没有：一个都不删
            lo, hi = src_dates[0], src_dates[-1]
            for row in db.execute(text(
                    "SELECT data_date FROM edb_value WHERE edb_code = :c"),
                    {"c": ind["code"]}).fetchall():
                if row[0] in keep:
                    continue
                if lo <= row[0] <= hi:
                    db.execute(text("DELETE FROM edb_value WHERE edb_code = :c"
                                    " AND data_date = :d"), {"c": ind["code"], "d": row[0]})
                    stats["edb_value_stale_deleted"] += 1
                else:
                    # 窗口外的历史点：源里没给，但不能当成“被撤点”删掉
                    stats["edb_value_delete_skipped"] += 1


def _as_date(v):
    """源侧日期是文本（'2026-05-20' / '2026-05-20 00:00:00'），认不出的当缺失而不是抛错。"""
    return parse_date(v) if isinstance(v, str) and DATE_RE.match(v) else None


def import_actions(db, stats):
    """actions/latest.json → share_action（定增 + 回购全市场明细）。

    一个文件两类行（kind 分），(sid, kind, src_id) 自然主键，故按主键 upsert。
    源是全市场权威快照，所以本轮没再出现的 (kind, src_id) 要跟着删：东财撤一行
    （撤公告、改 REPURCODE）时不删就永久多出一笔再没有任何来源能命中它的旧事件。
    删的口子只会放过整文件级的变化——采集侧的行数守卫已经把大面积掉行挡在写盘之前。
    """
    path = DATA_DIR / "actions" / "latest.json"
    if not path.exists():
        return  # 首轮采集还没跑过：无源可读不是回灌失败
    src = json.loads(path.read_text(encoding="utf-8"))
    sids = load_sid_map(db)
    now = parse_dt(src.get("fetched_at"))
    w = _Writer(db, stats, ShareAction, ACTION_KEYS, mode="upsert",
                upd_skip=("sid", "kind", "src_id"), upd_coalesce=True)
    live = {}
    for kind in ("seo", "buyback"):
        for r in src.get(kind) or []:
            sid = sids.get((r.get("code"), "A"))
            if sid is None:
                # 新上市代码还没进 index.json 的表头，下一轮深抓补上主数据即自动回填
                stats["actions.skipped_nosec"] += 1
                continue
            src_id = r.get("src_id")
            if not src_id:
                stats["actions.skipped_noid"] += 1
                continue
            live.setdefault(kind, set()).add(src_id)
            row = {k: v for k, v in r.items() if k in ACTION_KEYS}
            for k in _ACT_DATES:
                row[k] = _as_date(row.get(k))
            row["updated_at"] = datetime.combine(
                _as_date(r.get("updated_at")) or (now or datetime.now()).date(),
                datetime.min.time())
            row["detail"] = {k: v for k, v in r.items()
                             if k in _ACT_JSON and v not in (None, "")}
            row["sid"], row["kind"], row["src_id"] = sid, kind, src_id
            w.add(row)
    w.flush()

    for kind, ids in live.items():
        stale = [r[0] for r in db.execute(
            select(ShareAction.src_id).where(ShareAction.kind == kind)).all()
            if r[0] not in ids]
        if stale:
            for i in range(0, len(stale), BULK_ROWS):
                db.execute(text("DELETE FROM share_action"
                                " WHERE kind = :k AND src_id = :i"),
                           [{"k": kind, "i": x} for x in stale[i:i + BULK_ROWS]])
            stats["actions.stale_deleted"] += len(stale)
    db.commit()


def import_valuation(db, stats):
    """valuation/latest.json → valuation_pctile（每个标的一行最新观测）。

    分位值本身已在采集侧判过可信度（亏损、序列停更、值域越界都置空），这里只按
    (code, market) 找 sid；三项分位全空的标的整行不落——列表页那一格本来就该是“-”，
    留一行全 NULL 只会让“铺到但没有效值”和“根本没铺到”两种状态混在一起没法对账。
    """
    path = DATA_DIR / "valuation" / "latest.json"
    if not path.exists():
        return  # 首轮采集还没跑过：无源可读不是回灌失败
    src = json.loads(path.read_text(encoding="utf-8"))
    sids = load_sid_map(db)
    w = _Writer(db, stats, ValuationPctile, PCT_KEYS, mode="upsert", upd_skip=("sid",))
    for key, v in (src.get("items") or {}).items():
        market, _, code = key.partition(":")
        sid = sids.get((code, market))
        if sid is None:
            stats["valuation.skipped_nosec"] += 1
            continue
        tdate = parse_date(v.get("trade_date"))
        pcts = {m: (v.get(m) or {}).get("pct") for m in ("pe", "pb", "ps")}
        if not tdate or all(pcts[m] is None for m in pcts):
            stats["valuation.skipped_empty"] += 1
            continue
        days = {m: (v.get(m) or {}).get("days") for m in ("pe", "pb", "ps")}
        w.add(dict(
            sid=sid, trade_date=tdate, updated_at=parse_dt(v.get("fetched_at")) or datetime.now(),
            pe_pctile=pcts["pe"], pb_pctile=pcts["pb"], ps_pctile=pcts["ps"],
            pe_days=days["pe"], pb_days=days["pb"], ps_days=days["ps"],
            source={m: {"windCode": (v.get(m) or {}).get("wind_code"),
                        "date": (v.get(m) or {}).get("date"), "round": v.get("round")}
                    for m in ("pe", "pb", "ps")},
        ))
    w.flush()


TABLES = [
    "wind_holder", "wind_event", "score_daily", "periodic_report", "dividend",
    "fin_cashflow", "fin_balance", "fin_income", "fin_indicator", "fin_note",
    "quote_daily", "valuation_pctile", "share_action",
    "security", "agro_price", "agro_product", "edb_value", "edb_indicator", "etl_job_log",
]


def clean_tables(db):
    """按依赖倒序清空业务表(先全局关闭外键检查加速)。"""
    db.execute(text("SET FOREIGN_KEY_CHECKS=0"))
    for t in TABLES:
        db.execute(text(f"TRUNCATE TABLE {t}"))
    db.execute(text("SET FOREIGN_KEY_CHECKS=1"))


class _Stats(dict):
    """计数器:首次访问自动补 0。"""

    def __missing__(self, key):
        self[key] = 0
        return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-clean", action="store_true", help="不清空,直接导入（upsert）")
    ap.add_argument("--only-fresh", type=float, metavar="HOURS",
                    help="仅导入 N 小时内更新过的公司 JSON（全市场日更增量）")
    ap.add_argument("--only-scores", action="store_true",
                    help="只重算 score_daily（评分算法变更后单刷分），不碰其他业务表")
    ap.add_argument("--quiet", action="store_true", help="不打导入进度")
    args = ap.parse_args()
    if args.only_fresh and not args.no_clean:
        # 兜住一个能把库打空的组合：clean 会 TRUNCATE 全部业务表，而增量窗口只重灌
        # 最近 N 小时改过的公司文件，窗口外的历史行永久丢失（实跑踩过：
        # periodic_report 13.7 万 → 1.1 万）。
        ap.error("--only-fresh 必须配 --no-clean：增量模式下先清空会丢掉窗口外的全部明细")
    if args.only_scores:
        # 同上，但这里直接改掉而不是报错：只刷分却先 TRUNCATE 18 张表、事后只回填一张，
        # 是一个不该靠调用方记得敲 --no-clean 才能避免的坑
        args.no_clean = True

    started = datetime.now()
    db = SessionLocal()
    stats = _Stats()
    t0 = time.time()
    mode = "only-scores" if args.only_scores else ("only-fresh" if args.only_fresh else "full")
    try:
        if not args.no_clean:
            clean_tables(db)
            print("已清空业务表")
        if args.only_scores:
            import_scores_only(db, stats, quiet=args.quiet)
        elif args.only_fresh:
            # 日更：表头三表走 index.json 全量（快），财务明细只读刚重抓的公司文件
            import_index_snapshot(db, stats, quiet=args.quiet)
            import_companies(db, stats, only_fresh=args.only_fresh,
                             quiet=args.quiet, head=False)
            import_events(db, stats)
            import_agro(db, stats)
            import_edb(db, stats)
            import_valuation(db, stats)
            import_actions(db, stats)
        else:
            import_companies(db, stats, quiet=args.quiet)
            import_events(db, stats)
            import_agro(db, stats)
            import_edb(db, stats)
            import_valuation(db, stats)
            import_actions(db, stats)
        held = list(hold_notes)
        db.add(EtlJobLog(
            job_name="import_legacy", started_at=started, finished_at=datetime.now(),
            status="failed" if held else "success",
            message=("；".join(held) + f" | 导入完成[{mode}]: {dict(stats)}") if held
            else f"导入完成[{mode}]: {dict(stats)}",
            stats=dict(stats),
        ))
        db.commit()
        print(f"导入完成[{mode}]（{time.time() - t0:.0f}s）:")
        for k in sorted(stats):
            print(f"  {k}: {stats[k]}")
        for note in held:
            print(f"⚠ {note}", flush=True)
        return 1 if held else 0
    except Exception as e:
        db.rollback()
        db.add(EtlJobLog(
            job_name="import_legacy", started_at=started, finished_at=datetime.now(),
            status="failed", message=f"[{mode}] {str(e)[:1900]}", stats=dict(stats),
        ))
        db.commit()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
