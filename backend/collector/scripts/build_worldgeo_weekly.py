# -*- coding: utf-8 -*-
"""世界局势周报生成器(每周日 10:15,worldgeo-weekly job)。

读 cmd_value(温度计 10 code 周变化)+ events.json(活跃事件与应验度)+
scenarios.json(复核提醒)+ backtest.json(矩阵弹性) → 生成
docs/worldgeo-weekly/YYYY-Www.md。纯本地计算零网络,不回灌(NO_IMPORT)。

文档定位:数据段全自动,留「人工研判」占位区——周报是给人工复核的底稿,
不是结论(研究看板纪律)。落 docs/worldgeo-weekly/(非 docs/research/ 平级,
不被研报库 glob 扫入)。
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
GEO_DIR = BACKEND / "collector" / "commodity-worldgeo" / "data"
OUT_DIR = BACKEND / "docs" / "worldgeo-weekly"

# 与 world_geo.py _THERMO 同清单(复制而非 import——采集侧不依赖 app 包)
_THERMO_CODES = [
    ("GPR", "GPR 地缘风险"), ("VIXCLS", "VIX 恐慌"),
    ("GEO_GOLD_OIL", "金油比"), ("GOLD_SILVER", "金银比"),
    ("CU_GOLD", "铜金比"), ("GEO_OIL_CU", "油铜比"),
    ("BRENT_WTI", "布伦特-WTI"), ("DTWEXBGS", "美元宽口径"),
    ("DFII10", "10Y 实际利率"), ("USDCNY_FIX", "人民币中间价"),
]


def _iso_week(d):
    y, w, _ = d.isocalendar()
    return "%04d-W%02d" % (y, w)


def _series(db, code, days=35):
    from sqlalchemy import text
    rows = db.execute(text(
        "SELECT data_date, val FROM cmd_value WHERE cmd_code = :c "
        "ORDER BY data_date DESC LIMIT :n"), {"c": code, "n": days}).fetchall()
    return [(r[0].isoformat(), float(r[1])) for r in rows][::-1]


def main():
    sys.path.insert(0, str(BACKEND))
    from app.db import SessionLocal
    from sqlalchemy import text as _t

    today = date.today()
    week_tag = _iso_week(today)
    events = json.loads((GEO_DIR / "events.json").read_text(encoding="utf-8"))
    scenarios = json.loads((GEO_DIR / "scenarios.json").read_text(encoding="utf-8"))

    db = SessionLocal()
    try:
        lines = [
            "# 世界局势周报 · %s（%s ~ %s）" % (week_tag,
                                              (today - timedelta(days=6)).isoformat(), today.isoformat()),
            "",
            "> 自动生成底稿(数据段) + 人工研判占位。研究看板非投资建议。",
            "",
            "## 一、温度计周变化",
            "",
            "| 指标 | 最新 | 近 35 日变化 | 近期分位(35 日窗口) | 全史分位 |",
            "|---|---|---|---|---|",
        ]
        for code, label in _THERMO_CODES:
            pts = _series(db, code)
            all_vals = [v for _, v in _series(db, code, 100000)]
            if not pts:
                lines.append("| %s | — | — | — | — |" % label)
                continue
            latest = pts[-1][1]
            base = pts[0][1]
            chg_pct = (latest / base - 1) * 100 if base else None
            w_lo, w_hi = min(v for _, v in pts), max(v for _, v in pts)
            pct_35 = round((latest - w_lo) / (w_hi - w_lo) * 100) if w_hi > w_lo else 50
            a_lo, a_hi = min(all_vals), max(all_vals)
            pct_all = round((latest - a_lo) / (a_hi - a_lo) * 100) if a_hi > a_lo else 50
            lines.append("| %s | %s | %s | %s%% | %s%% |" % (
                label, round(latest, 2),
                ("%+.1f%%" % chg_pct) if chg_pct is not None else "-",
                pct_35, pct_all))
        lines += ["", "## 二、活跃事件与应验度", ""]
        for ev in events.get("events", []):
            if ev.get("status") != "active":
                continue
            done = [a.get("verify") or {} for a in ev.get("assets", [])]
            confirmed = sum(1 for v in done if v.get("verdict") == "confirmed")
            refuted = sum(1 for v in done if v.get("verdict") == "refuted")
            lines.append("- **%s**（%s,%s 起,窗口 %s 天）——可核对资产应验 %d/证伪 %d" % (
                ev.get("title"), ev.get("category"), ev.get("date"),
                (ev.get("expectation") or {}).get("window_days"), confirmed, refuted))
        lines += ["", "## 三、情景树复核提醒", ""]
        for s in scenarios.get("sets", []):
            probs = " / ".join("%s %.0f%%" % (x["name"], x["probability"] * 100)
                               for x in s.get("scenarios", []))
            lines.append("- **%s**:当前概率 %s——上次更新 %s,超过一季或局势有实质变化时人工复核" % (
                s.get("title"), probs, s.get("updated")))
        lines += [
            "", "## 四、人工研判（占位）", "",
            "- 本周局势新变量:",
            "- 温度计组合读数（对照形态手册）:",
            "- 情景概率是否需要调整:",
            "- 下周关注节点:",
        ]
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out = OUT_DIR / ("%s.md" % week_tag)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("[worldgeo-weekly] 写盘 %s" % out, flush=True)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
