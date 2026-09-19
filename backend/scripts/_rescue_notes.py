# -*- coding: utf-8 -*-
"""一次性救援：fin_note(DB) → companies/*.json 的 notes 字段。

背景（2026-09-19 发现）：9-16 的「三大表清空」事故修复脚本重写了全部 A 股公司
JSON，重写时**丢失了 notes（现金类附注）**——而 fin_note 表里数据完好（更早轮次
导入）。后果：全市场 A 股的净现金退回 ×0.3 粗口径（广信股份 37 亿定期存款按
3 折计），净现金/市值系统性低估。

用法：cd backend; python -X utf8 -m scripts._rescue_notes
（下周六深抓会从 PDF 自然重收附注，本脚本主要用于等待期内的即时恢复。）
"""
import json
import os
from pathlib import Path

from sqlalchemy import text

from app.db import engine

CD = Path(__file__).resolve().parents[1] / "collector" / "data" / "companies"


def main():
    with engine.connect() as c:
        rows = c.execute(text('''
            SELECT s.code, f.report_date, f.term_deposit, f.restricted_cash
            FROM fin_note f JOIN security s ON s.sid = f.sid
            WHERE f.term_deposit IS NOT NULL OR f.restricted_cash IS NOT NULL''')).all()

    by_code = {}
    for code, rd, td, rc in rows:
        d = by_code.setdefault(code, {})
        entry = {}
        if td is not None:
            entry['termDeposit'] = float(td)
        if rc is not None:
            entry['restrictedCash'] = float(rc)
        if entry:
            d[rd.isoformat()] = entry

    rescued = skipped_has = skipped_nofile = 0
    for code, note_map in by_code.items():
        p = CD / f"{code}.json"
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        if d.get("notes"):
            skipped_has += 1  # 本轮深抓已重收附注的文件不动
            continue
        d["notes"] = note_map
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, separators=(",", ":")),
                       encoding="utf-8")
        os.replace(tmp, p)
        rescued += 1
    print(f"救援完成：回填 {rescued} 家（已有附注跳过 {skipped_has}，无文件 {skipped_nofile}）")


if __name__ == "__main__":
    main()
