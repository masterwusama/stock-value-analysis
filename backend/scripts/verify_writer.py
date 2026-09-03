# -*- coding: utf-8 -*-
"""回归:fin_*/dividend/periodic_report 的 upsert 语句与写入预处理(不碰数据库)。

要点：1) 主键列不出现在 SET 子句；2) 各表核心科目列仍在 INSERT 列里（改之前
误删过一次）；3) 缺字段时用 COALESCE 保住库里旧值，不被 NULL 抹掉；4) 超列宽的长文截断、
超量程的定点值置 NULL、崩批退回逐行只丢真装不下的那一行。

用法(零积分、不需要 API 服务、不需要建表): python -X utf8 scripts/verify_writer.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.import_legacy import (  # noqa: E402
    DIV_KEYS, FIN_KEYS, RPT_KEYS, FIN_SPECS, _Writer, _Stats, coalesce_upd,
)


def mk(model, keys):
    return _Writer(None, _Stats(), model, keys, mode="upsert",
                   upd_skip=("sid", "report_date"), upd_coalesce=True)


fails = []


def check(tag, cond, got=""):
    print(("  ok   " if cond else "  FAIL ") + tag + ("  " + got if got else ""))
    if not cond:
        fails.append(tag)


for json_key, model, core_map, _ in FIN_SPECS:
    w = mk(model, FIN_KEYS + tuple(core_map))
    sql = str(w.stmt)
    tbl = model.__tablename__
    set_part = sql.split("ON DUPLICATE KEY UPDATE")[1]
    check("%s 核心列在插入列里" % tbl,
          all(("`%s`" % c) in sql for c in core_map), str(list(core_map)[:2]))
    check("%s extras 在插入列里" % tbl, "`extras`" in sql)
    check("%s SET 不含主键" % tbl,
          "`sid`=" not in set_part and "`report_date`=" not in set_part)
    check("%s SET 走 COALESCE" % tbl, "COALESCE(VALUES(`extras`)" in set_part)
    check("%s SET 覆盖全部非主键列" % tbl,
          set_part.count("COALESCE") == len([c for c in w.cols if c not in ("sid", "report_date")]))

wd = _Writer(None, _Stats(), __import__("app.models", fromlist=["x"]).Dividend,
             DIV_KEYS, mode="upsert", upd_skip=("sid",), upd_coalesce=True)
check("dividend SET 不含 sid", "`sid`=" not in str(wd.stmt).split("UPDATE")[1])
check("dividend SET 含 ex_date", "COALESCE(VALUES(`ex_date`)" in str(wd.stmt))
wr = _Writer(None, _Stats(), __import__("app.models", fromlist=["x"]).PeriodicReport,
             RPT_KEYS, mode="upsert", upd_skip=("sid", "report_date"), upd_coalesce=True)
check("periodic_report SET 含 audit_opinion",
      "COALESCE(VALUES(`audit_opinion`)" in str(wr.stmt))
check("coalesce_upd 空表返回空", coalesce_upd([]) == "")

# ---- 列宽/量程预处理（以前被 INSERT IGNORE + MySQL 默默钳掉的那些值）----
from app.models import Dividend, FinIndicator  # noqa: E402

wd0 = _Writer(None, _Stats(), Dividend, DIV_KEYS)
wd0.add({"sid": 1, "div_year": "2025年报", "div_type": "年度分红",
         "description": "10派" + "x" * 400 + "元", "bonus_per_10": 1.0})
r0 = wd0.rows[-1]
check("长文本截到列宽", len(r0["description"]) == 128 and wd0.stats["dividend.clipped"] == 1,
      "len=%d" % len(r0["description"]))

wfi = _Writer(None, _Stats(), FinIndicator, FIN_KEYS + tuple(FIN_SPECS[0][2]))
wfi.add({"sid": 1, "report_date": None, "updated_at": None, "extras": None,
         "eps": 1234567.8, "bps": 3.5, "net_profit": -697837000.0})
r1 = wfi.rows[-1]
check("越界定点值置 NULL", r1["eps"] is None and wfi.stats["fin_indicator.out_of_range"] == 1)
check("量程内值不受影响", r1["bps"] == 3.5 and r1["net_profit"] == -697837000.0)
wfi.add({"sid": 1, "report_date": None, "updated_at": None, "extras": None, "eps": 0})
check("0 不是越界", wfi.rows[-1]["eps"] == 0)


class _FakeTx:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, t, v, tb):
        return False


class _FakeDb:
    """带 poison 行时整批报错、逐行时只炸那一行——复现 1406/1264 的崩批场景。"""

    def __init__(self, poison):
        self.poison, self.calls, self.nested = poison, [], 0

    def begin_nested(self):
        self.nested += 1
        return _FakeTx(self)

    def execute(self, stmt, rows):
        rows = rows if isinstance(rows, list) else [rows]
        self.calls.append(len(rows))
        for r in rows:
            if r.get("description") == self.poison:
                raise RuntimeError("1406 Data too long")


poison = "BAD"
fd = _FakeDb(poison)
wf = _Writer(fd, _Stats(), Dividend, DIV_KEYS)
for i in range(5):
    wf.add({"sid": i, "div_year": "y", "div_type": "t",
            "description": poison if i == 2 else "ok"})
wf.flush()
check("崩批退回逐行（1 次批量 + 5 次单行）", fd.calls == [5, 1, 1, 1, 1, 1], str(fd.calls))
check("只丢真装不下的那行", wf.stats["dividend.bad_rows"] == 1 and wf.stats["dividend"] == 4,
      str(dict(wf.stats)))
check("逐行重试仍走 SAVEPOINT", fd.nested == 6, "nested=%d" % fd.nested)

print("\n%s" % ("全部通过" if not fails else "失败 %d 项: %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
