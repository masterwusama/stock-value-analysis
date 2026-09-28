# -*- coding: utf-8 -*-
"""ST/退市风险预警的横截面自检：scoring._st_risk 的规则在 2025 年报截面上的命中面。

定位（先读这个再跑）：这不是 g_validity/v_validity 那种「先验声明→回测验收→出厂」的拟合
分数——旗的要件直接取自交易所《股票上市规则》退市风险警示的原文（利润总额/净利润/扣非
孰低为负 且 营业收入低于线；净资产为负；审计非标……），规则是监管定的，本脚本只回答两件
实现层的事：
  ① 命中面大小：每面旗、每个等级在当前截面命中多少家——预警徽标若是满屏红就是噪声，
     要么沉默要么狼来了，两头都不可用；
  ② 对「已成事实」的覆盖：loss_rev（*ST 财务类要件）命中的家数里，多少家简称已经带上
     ST（规则正常运转的旁证）、多少家还没带——后者是两种人：豁免窗内的双创未盈利 biotech
     （上市后第 4 个完整会计年度起才适用，代码里已降级为关注）和真·在途候选。

已知边界（如实记录，不当成回测）：
- 横截面不构成预测效度证据——「现在的旗能否预测将来变 ST」需要带历史简称的多期面板，
  本库没有（简称只有当前快照）。真正的预测验证只能等这批旗在后续年份自然暴露。
- 「连续两年扣非亏损」在 2025 年报截面命中 1,270 家（23%）：现行规则下它本身不触发任何
  警示，设旗就是狼来了，故不设（scoring._st_risk 文件头同款说明）。
- 现价旗（price_low ≤1.2 元）的截面数取自运行日行情；交易日之间会浮动。

用法（在 backend 目录下）:
    python -X utf8 -m scripts.st_validity          # 读 collector/data 的 JSON 工作目录
"""
import io
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "collector" / "scripts"))
from scoring import _st_board_line, _st_risk, annual_rows  # noqa: E402

DATA = HERE.parent / "collector" / "data"


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    idx = json.loads((DATA / "index.json").read_text(encoding="utf-8"))
    st_codes = {c["code"] for c in idx.get("companies", [])
                if (c.get("market") or "A") == "A" and "ST" in str(c.get("name") or "").upper()}

    flag_n: Counter = Counter()
    level_n: Counter = Counter()
    l2_named, l2_unnamed = [], []
    n_a = n_st = st_flagged = 0
    for c in idx.get("companies") or []:
        if (c.get("market") or "A") != "A":
            continue
        code = c["code"]
        n_a += 1
        f = DATA / "companies" / f"{code}.json"
        if not f.exists():
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        risk, flags = _st_risk(d)
        if risk is None:
            level_n["判不动"] += 1
            continue
        level_n[risk] += 1
        for k in filter(None, flags.split(",")):
            flag_n[k] += 1
        if flags:
            level_n["ANY"] += 1
        if code in st_codes:
            n_st += 1
            if risk:
                st_flagged += 1
        if risk == 2:
            (l2_named if code in st_codes else l2_unnamed).append(code)

    print(f"A 股 {n_a} 家（其中简称含 ST {n_st} 家）")
    print(f"等级分布: 高危(2) {level_n[2]} · 关注(1) {level_n[1]} · 无旗(0) {level_n[0]} · 判不动 {level_n['判不动']}"
          f" · 有旗合计 {level_n['ANY']}")
    print(f"各旗命中: " + " · ".join(f"{k} {v}" for k, v in sorted(flag_n.items())))
    print(f"loss_rev 命中 {len(l2_named) + len(l2_unnamed)} 家 = 已是ST简称 {len(l2_named)}"
          f" + 尚未ST {len(l2_unnamed)}（豁免降级后留在关注的另计）")
    if l2_unnamed:
        print("  尚未ST样例: " + "、".join(l2_unnamed[:8]))
    if n_st:
        print(f"当前 ST 简称家里，本预警亮旗的 {st_flagged}/{n_st} 家——其余多经审计非标/立案/"
              f"资金占用等 gate 侧要件进入警示（gate_flags 已单独覆盖），不表")

    # 规则形状自检（2026-09-28 起不做具体家期望——规则改动时硬编码期望极易失效成假绿，
    # 改验值域与不变量：risk∈{None,0,1,2}；risk=2 必含 loss_rev/case 类旗；
    # flags 非空必有 risk≥1；非 A 股恒 None；有旗必有对应旗名）
    VALID_FLAGS = {"loss_rev", "loss_rev_ex", "loss_rev_near", "debt90", "price_low", "case"}
    bad = 0
    n_checked = 0
    for c in idx.get("companies") or []:
        if (c.get("market") or "A") != "A":
            continue
        f = DATA / "companies" / f"{c['code']}.json"
        if not f.exists():
            continue
        got, flags = _st_risk(json.loads(f.read_text(encoding="utf-8")))
        n_checked += 1
        fl = [x for x in flags.split(",") if x]
        errs = []
        if got not in (None, 0, 1, 2):
            errs.append(f"risk 值域外 {got}")
        if fl and got == 0:
            errs.append("有旗却 risk=0")
        if not fl and got == 1:
            errs.append("risk=1 却无旗")
        if got == 2 and not any(k in fl for k in ("loss_rev", "case")):
            errs.append(f"高危却无 loss_rev/case 旗: {flags}")
        for k in fl:
            if k not in VALID_FLAGS:
                errs.append(f"未知旗名 {k}")
        if errs:
            bad += 1
            if bad <= 5:
                print(f"  ✗ {c['code']}: {'; '.join(errs)}")
    print(f"形状自检: {n_checked} 家, 违反不变量 {bad} 家")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
