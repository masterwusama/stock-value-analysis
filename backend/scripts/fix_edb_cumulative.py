# -*- coding: utf-8 -*-
"""修 fetch_edb 早期版本留下的「累计值冒充当月值」污染点（不花 Wind 积分）。

来历：fetch_edb 对统计局累计口径序列（CUM_TO_MONTHLY_CODES）做 `当月 = 本月累计 - 上月累计`
差分，但抓取窗口只有一个 365 天，窗口内第一个月尾点拿不到上月累计值，旧版本就把它
`m = v` 原样输出了 —— 也就是「1~本月累计」被当成「本月单月」。2026-09-03 盘库实测：
地产链 5 条序列在 2025-09-30（= 窗口 2025-09-02 之后的首个月尾）都存着这样一个点，
值是同序列中位数的 8.8~9.3 倍，并已随回灌进了 edb_value。窗口只会往后滑，这个点再也不会
被后续抓取回头修正，所以必须手工修一次（fetch_edb 侧的同类问题已用「往前多拉 buffer 天
算前值、算完再裁」堵住）。

修法只用文件自身的数据：edb.json 里存的是逐月单月值，从年初累加即可还原累计值
（每年首点是 1-2 月合并发布的前两月合计），于是
    正确单月值 = 可疑点的值(累计) - 该点之前本年各月单月值之和
累加器必须按自然年归零：统计局的“累计”是年初至今，拿去年的累加基数去减今年某个点
会把重算值压成负数（不写盘、静默漏修）。只接受"重算结果落在合理区间"
（>0 且不超过中位数 2 倍）的点，避免把真实的季节性高点
（例如 2025-12-31 房屋竣工面积的年末交付潮）误判成污染点。

用法（在 backend 目录下）：
    python -X utf8 -m scripts.fix_edb_cumulative --dry-run   # 只看要改什么
    python -X utf8 -m scripts.fix_edb_cumulative             # 写回 edb.json
改完回灌：python -X utf8 -m collector.run import
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import LEGACY_DATA_DIR  # noqa: E402

EDB = LEGACY_DATA_DIR / "agro-price" / "data" / "edb.json"
RATIO_SUSPECT = 3.0   # 超过同序列中位数这么多 → 疑似累计值冒充
RATIO_PLAUSIBLE = 2.0  # 重算值必须回到中位数量级，否则不认是污染


def repair_series(points):
    """返回 (修正后的 points, 改动列表 [(日期, 原值, 新值)])。points 形如 [["YYYY-MM-DD", v], ...]。"""
    vals = sorted(v for _t, v in points if isinstance(v, (int, float)))
    if len(vals) < 4:
        return points, []
    med = vals[len(vals) // 2]
    if med <= 0:
        return points, []
    out, fixes = [], []
    cum = 0.0
    prev_year = None
    for k, (d, v) in enumerate(points):
        year = str(d)[:4]
        if year != prev_year:   # 跨年归零：统计局累计是“年初至今”，不是全期累加
            cum = 0.0
            prev_year = year
        if k and isinstance(v, (int, float)) and v > RATIO_SUSPECT * med:
            fixed = round(v - cum, 2)
            if 0 < fixed <= RATIO_PLAUSIBLE * med:
                out.append([d, fixed])
                fixes.append((d, v, fixed))
                cum += fixed
                continue
        out.append([d, v])
        if isinstance(v, (int, float)):
            cum += v
    return out, fixes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印将要修正的点，不写文件")
    args = ap.parse_args()

    src = json.loads(EDB.read_text(encoding="utf-8"))
    total = 0
    for cat in src.get("categories", []):
        for ind in cat.get("indicators", []):
            new_pts, fixes = repair_series(ind.get("points") or [])
            if not fixes:
                continue
            total += len(fixes)
            if not args.dry_run:
                ind["points"] = new_pts
            for d, old, new in fixes:
                print("%-11s %-10s %-22s %s  %s → %s" % (
                    cat["id"], ind["code"], ind.get("label") or ind.get("name"), d, old, new))
    if not total:
        print("未发现可修的污染点（序列已经干净，或本文件的点都不满足判据）")
        return 0
    if args.dry_run:
        print(f"[dry-run] 共 {total} 个点待修，未写文件")
        return 0
    EDB.write_text(json.dumps(src, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"已修 {total} 个点并写回 {EDB}（下一步：python -X utf8 -m collector.run import）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
