# -*- coding: utf-8 -*-
"""只重算 index.json 里各家的 scores，不重抓任何数据。

现在是调度链的一环：`stock`（每日）与 `deep`（周六）都在采集结尾跑这一步，所以评分算法
改一行不必等周六——`--only-fresh` 那条日更回灌本来就会把 index.json 的 scores 写进
score_daily（`import_index_snapshot`），跑完列表页就是新分。手动跑同样有效，配
`python -m scripts.import_legacy --only-scores` 可以只刷 score_daily 落库。

定增史在这里按当日的整表快照（`data/actions/latest.json`）覆盖，不用 companies/*.json 里
那份深抓时切的旧片段——否则今天新公告的摊薄要等到下次深抓才进陷阱分，而详情页读库里的
`share_action` 当天就能看到，同一个 T 两边不同日。

价值综合分同理，但只给它一个数：市值。`companies/<代码>.json` 的 `snapshot` 是深抓那一刻
切的（实测 A 股停在上一轮周六：5,551 家两侧都有可用市值，与本页行情中位差 2.18%、p90
7.98%；港美股那笔与快照同批、漂移为 0），而 V 是唯一把市值当输入的入库分数，跟着旧快照走
会让列表页那一分滞后整整一周、与详情页现算的对不上。所以这里把本条
`quote`（日更刚刷的那一笔）作为 `batch_quote` 注进评分输入，只喂 V 的市值那一项；四派分、
陷阱分、成长分与 12 个买卖参考价照旧读 `snapshot`，一个数都不动。
`batch_quote` 只活在这次传入的内存字典里，不写回 companies/*.json。

⚠ 改算法要先改完 scoring.py 并与 stockLegacy.js 同步（_score_check.py 全量比对），
否则刷出来的分与详情页现算的对不上。

index.json 既是列表页的数据源也快充当行情快照载体，所以这里只替换每条的 scores 块，
quote 等其余字段原样保留，updated_at 也不动（它是这批分的评分日，动一下就把列表页的
「快照日」推到行情还不存在的日期）。

用法（在 backend/collector 目录下）：
    python -X utf8 scripts/_refresh_scores.py --dry-run   # 只看会改多少家
    python -X utf8 scripts/_refresh_scores.py             # 原子写回 index.json
"""
import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from scoring import compute_scores  # noqa: E402
from actions_lookup import seo_actions  # noqa: E402  查本地定增快照，不发请求

DATA = HERE.parent / "data"
INDEX_PATH = DATA / "index.json"
LOCK_PATH = DATA / ".fetch.lock"
KEYS = ("grahamAgg", "grahamDef", "schloss", "buffett")


def main():
    ap = argparse.ArgumentParser(description="仅重算 index.json 的 scores 块")
    ap.add_argument("--dry-run", action="store_true", help="不写文件，只报会改多少家")
    ap.add_argument("--force", action="store_true", help="采集中也强行跑（默认拒绝）")
    args = ap.parse_args()

    if LOCK_PATH.exists() and not args.force:
        print(f"采集中（{LOCK_PATH.name} 存在）：现在写 index 会和抓取抢文件，等这轮跑完再加 --force")
        return 2

    idx = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    companies = idx.get("companies") or []
    changed, failed, noscore, seo_hit = [], 0, 0, 0
    for c in companies:
        f = DATA / "companies" / f"{c['code']}.json"
        if not f.exists():
            noscore += 1
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        # 定增史以当日整表快照为准，不以文件里那份旧切片为准：companies/*.json 的
        # `seo_actions` 只在深抓时切一次，直接用会让列表分滞后到下次深抓，而详情页读的
        # 是库里每日更新的 share_action —— 同一个 T 两边用了不同日的定增史。
        # None 是「快照没落地」，此时保留文件里那份，不能替全市场担保没摊薄过。
        if (c.get("market") or "A") == "A":
            fresh = seo_actions(c["code"])
            if fresh is not None:
                d["seo_actions"] = fresh
                seo_hit += 1
        # V 的市值只认这一笔（其余评分与参考价仍读 d["snapshot"]）：见文件头
        d["batch_quote"] = c.get("quote") or {}
        try:
            new = compute_scores(d)
        except Exception as e:
            failed += 1
            print(f"  保留 {c['code']} 旧分：compute_scores 抛 {type(e).__name__}: {e}")
            continue
        if new is None:
            failed += 1
            print(f"  保留 {c['code']} 旧分：compute_scores 返回空")
            continue
        if new != c.get("scores"):
            changed.append((c["code"], new, c.get("scores")))
            c["scores"] = new

    print(f"{len(changed)}/{len(companies)} 家分数有变（无明细 {noscore} 家、算失败 {failed} 家、"
          f"定增快照覆盖 {seo_hit} 家）")
    for code, new, old in changed[:10]:
        print(f"  {code} " + " | ".join(
            f"{k} {'' if old is None else old.get(k)} → {new.get(k)}" for k in KEYS))
    if len(changed) > 10:
        print(f"  …另有 {len(changed) - 10} 家")
    if args.dry_run:
        print("试运行：未写任何文件")
        return 0
    if not changed and not failed:
        print("分数无变化，index.json 保持原样")
        return 0

    # 与 fetch_data.save_json 同样的原子落盘：半截 index 会让列表页整片读空
    tmp = INDEX_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(idx, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, INDEX_PATH)
    print(f"已写回 {INDEX_PATH}（{len(changed)} 家分数更新）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
