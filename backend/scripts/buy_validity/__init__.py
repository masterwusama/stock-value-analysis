# -*- coding: utf-8 -*-
"""四派买点信号回测(收益侧效度)——包形态,五个子模块各管一段:

    prices      价格文件/交易日历/月末网格/索引与前瞻收益(纯函数,零 DB)
    synthesis   DB 装载(extras 无损原件、三表并集可用日)+ 点时切片 + 公司 dict
                合成 + refs_at(喂 scoring 原函数,单一事实源)
    panel       面板构建主循环(事件判定/180 天去重/收益挂接)
    verdict     双对照聚合(市场中位/邻域)与判决书渲染
    cli         命令行入口与 --check-live 同构对拍

设计与方法论:docs/设计-四派买点信号回测.md(含 2026-10-04 首跑判决书)。与
rr_validity 等效度脚本同族:离线研究,只读库与本地 data/prices/,零积分零调度零 DDL。
用法保持 `python -X utf8 -m scripts.buy_validity`(--check-live/--dump/--school/--discount)。
"""

SCHOOLS = ("graham_agg", "graham_def", "schloss", "buffett")
HORIZONS = ((6, "6m"), (12, "1y"), (24, "2y"))
GRID_FROM, GRID_TO = 2020, 2025   # 信号年(2y 腿截尾:信号>2024-09 的观测自然缺失)
TOL_PP = 0.015                    # 乙判邻域差容差(与 rr_validity 的 TOL_PP 同级)
CAL = "000001"                    # 交易日历锚(平安银行,长期无长停牌)
