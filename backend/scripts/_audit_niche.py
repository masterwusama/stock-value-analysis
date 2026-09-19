# -*- coding: utf-8 -*-
"""细分归属的人工审计清单：niche 词典 v1 的质量闭环。

import_zygc 依词典给 security.niche 归属，词典是人工的——本脚本把结果摊开：
覆盖率 / 单例细分 / 未命中榜（有构成数据却没归上细分的主营首项，按频次排）/
各细分成员样例。人工过目：成员混入异类 = 词典写错，先修 niche_dict 再重跑
import_legacy --only-zygc。阈值（预登记）：覆盖 ≥50%、单例 ≤15%、未命中 ≤10%。

用法（只读库）: cd backend; python -X utf8 -m scripts._audit_niche
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector" / "scripts"))

from sqlalchemy import func, select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import MainBusiness, Security  # noqa: E402
from scripts.niche_dict import niche_of  # noqa: E402


def main():
    db = SessionLocal()
    code_of = dict(db.execute(select(Security.sid, Security.code)).all())
    name_of = dict(db.execute(select(Security.sid, Security.name)).all())
    n_ash = db.execute(select(func.count()).select_from(Security)
                       .where(Security.market == "A")).scalar()
    n_set = db.execute(select(func.count()).select_from(Security)
                       .where(Security.market == "A", Security.niche.isnot(None))).scalar()
    per = db.execute(
        select(Security.niche, func.count().label("cnt"))
        .where(Security.market == "A", Security.niche.isnot(None))
        .group_by(Security.niche)
        .order_by(func.count().desc())).all()

    singles = [x for x in per if x.cnt == 1]
    print(f"覆盖：{n_set}/{n_ash} A 股（{n_set * 100 / max(1, n_ash):.1f}%，线 ≥50%）")
    print(f"细分种数 {len(per)} · 单例 {len(singles)} 种（线 ≤15% = {len(singles)} ≤ "
          f"{0.15 * len(per):.0f}）")

    # 未命中榜：与 import_zygc 同一选数逻辑（最近一个有产品/行业维度的报告期，
    # 剔除「其中:」子项与兜底行）——否则地区行会污染榜，量出假未命中
    rows = db.execute(
        select(MainBusiness.sid, MainBusiness.report_date, MainBusiness.mainop_type,
               MainBusiness.item_name, MainBusiness.income_ratio)
        .order_by(MainBusiness.sid, MainBusiness.report_date.desc())).all()
    per_sid = {}
    for sid, rd, tp, name, ratio in rows:
        per_sid.setdefault(sid, []).append((rd, tp, name, ratio))
    miss = Counter()
    miss_n = 0
    miss_names = {}
    for sid, lst in per_sid.items():
        items = None
        for rd in sorted({x[0] for x in lst}, reverse=True):
            items = [(nm, ra) for x_rd, x_tp, nm, ra in lst if x_rd == rd
                     and x_tp in (2, 1) and nm and "其中" not in nm and "补充" not in nm]
            if items:
                break
        if items is None:
            continue
        niche, _src = niche_of(items)
        if niche is None:
            miss_n += 1
            miss[items[0][0]] += 1
            miss_names.setdefault(items[0][0], sid)
    print(f"未命中（词典没接住的主营首项）：{miss_n} 家（线 ≤10% = {miss_n} ≤ "
          f"{0.1 * max(1, len(per_sid)):.0f}）")
    print("  未命中 TOP20（首项名 → 样例公司）：")
    for name, cnt in miss.most_common(20):
        code = code_of.get(miss_names.get(name)) or "?"
        print(f"    {name}  ×{cnt}   例: {code} {name_of.get(code, '')}")

    print("\n各细分成员样例（≥5 家的细分，人工过目：混入异类 = 词典错）：")
    for niche, cnt in per:
        if cnt < 5:
            continue
        names = [r[0] for r in db.execute(
            select(Security.name).where(Security.market == "A", Security.niche == niche)
            .order_by(Security.name).limit(8))]
        print(f"  {niche}（{cnt}）: " + "、".join(names))
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
