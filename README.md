# stock-value-analysis

A股/港股/美股 价值分析数据服务 —— `masterwusama.github.io/stock-data/` 纯前端静态站的本地化重构：
FastAPI + MySQL 提供结构化查询接口，Vue 3 前端，采集层沿用经年验证的 Python 抓取脚本。

> **部署与使用请看 [docs/使用说明书.md](docs/使用说明书.md)**（首次部署七步、四个页面与筛选项、采集任务与调度节奏、
> 20 张表逐列口径、运维症状表、已知边界）。本文只留架构速览。

## 架构

```
┌─ 采集层 collector/ ──────────────┐   ┌─ 服务层 ───────────────────────────┐
│ fetch_data.py   AKShare/腾讯/巨潮 │   │ FastAPI :8000                      │
│ scoring.py      四流派评分物化    │→JSON→│  /api/*      REST 接口           │
│ portfolio_engine.py 三策略调仓    │ 工作 │  /            托管 frontend/dist │
│ fetch_prices/fetch_edb 农价/EDB   │ 目录 │ MySQL db_va (20 表, localhost)   │
│ fetch_events.py Wind 事件(手动)   │   │ collector/scheduler.py 定时进程    │
└──────────────────────────────────┘   └────────────────────────────────────┘
                                        ┌─ 前端 frontend/ (Vue3+Vite) ──────┐
                                        │ 列表/筛选 → #/ · 详情九模块 #/stock/:code
                                        │ 组合 #/portfolio · 农价EDB #/agro │
                                        └───────────────────────────────────┘
```

- 采集脚本以 **JSON 工作目录**（`collector/{data,portfolio,agro-price}`）为产物，与 GitHub Pages 原结构逐字段一致，作为唯一中间层；`import_legacy` 增量 upsert 回灌 MySQL。
  有唯一键的表走 upsert；`wind_event`/`wind_holder`/`pf_trade` 无自然唯一键(INSERT IGNORE 不生效)，按 sid/strat_key 先删后插，重跑幂等。
- 数据库是查询层：列表筛选/排序/分页全部 SQL 完成（`score_daily` + `quote_daily`），10000 证券规模可支撑。
- GitHub Pages 静态版保留在博客仓库，作为回退。

## 快速开始

```powershell
# 1. 数据库(首次): 建表 + 全量导入工作目录 JSON
cd backend
python -m scripts.create_tables          # --recreate 重建
python -m scripts.import_legacy          # 默认清表导入; --no-clean 增量

# 2. API 服务(内置前端托管, 需先 build)
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
# → http://127.0.0.1:8000  即为完整应用

# 3. 前端开发(热更新, /api 代理到 8000)
cd frontend && npm install && npm run dev   # → http://localhost:5173
npm run build                               # 产物 dist/ 由 FastAPI 托管(重启生效)

# 4. 采集调度(独立进程, 替代 GitHub Actions cron)
pip install -r collector/requirements.txt
python -m collector.scheduler   # stock 周一~六 16:05/22:05 · deep 周六 09:05 · agro 每天 09:05/21:05
```

配置在 `backend/.env`（参考 `backend/.env.example`）：`DATABASE_URL` / `BOND_10Y` / `LEGACY_DATA_DIR` / `WIND_SKILL_DIR`。

## 启停脚本（日常运维）

双击 `ops\start.bat` / `ops\stop.bat` 即可，或命令行：

```powershell
.\ops\start.ps1                  # 服务 + 定时采集（已在跑则跳过，可重复执行）
.\ops\start.ps1 -Rebuild         # 前端有改动：先 npm run build 再启动
.\ops\start.ps1 -NoScheduler     # 只启服务    -Lan: 监听 0.0.0.0
.\ops\status.ps1                 # 进程/端口/健康检查/前端产物/采集任务/抓取锁/index 规模
.\ops\status.ps1 -Jobs           # 附带 etl_job_log 最近运行记录
.\ops\stop.ps1                   # 关调度器 + 服务 + 残留采集子进程
.\ops\stop.ps1 -Api              # 只关服务（不动正在跑的采集任务）
```

进程按命令行特征识别（不依赖 pid 文件），日志落在 `run/*.log`（已 gitignore）。环境变量 `VA_PORT` / `VA_PYTHON` 可覆盖端口与 python 解释器。

若本机访问 GitHub 需走代理（直连 443 超时），推送时临时指定即可，不必改动全局配置：

```powershell
git -c http.proxy=http://127.0.0.1:1080 push origin main
```

## 采集任务

标的池 = 全 A 股（沪深京 ≈ 5550 只，名单来自 `collector/scripts/universe.py`）+ 港/美精选。
财务按季更新，所以拆两层节奏，不每天重抓 1.9GB 明细：

| job | 调度 | 内容 | 用法 |
|---|---|---|---|
| stock | 周一~六 16:05/22:05 | 腾讯批量刷全市场估值快照 → 调仓 → 回灌（≈1~2 分钟） | `python -m collector.run stock` |
| deep | 周六 09:05 | 全市场财务/定期报告重抓（`--resume --max-age 6`，≈5 小时） | `python -m collector.run deep` |
| agro | 每天 09:05/21:05 | 农化价格（生意社/中农立华）+ 行业 EDB（约 20~30 分钟） | `python -m collector.run agro` |
| events | 手动 | Wind 一次性事件/股东抓取（依赖博客仓 wind-mcp-skill） | `python -m collector.run events` |
| import | 手动 | 仅把 JSON 工作目录回灌 MySQL | `python -m collector.run import` |

首次上手：先 `python -m collector.run deep` 跑一遍全市场（中断后重跑同一命令即从断点续抓，
已抓的按文件 mtime 跳过），跑完自动回灌。之后交给调度器。

全量抓取持 `collector/data/.fetch.lock`（`index.json` 为全量重写，两进并行会互盖条目），
同时段的 stock job 自动跳过；锁是否被活进程持有可看 `ops\status.ps1`，
确认无进程后删该文件即可重跑。`companies/` 明细（全市场约 2GB）与 `index.json` 已 gitignore，
MySQL 才是服务层。

每次运行写 `etl_job_log` 表；抓取失败也会回灌已落盘数据（对齐原 workflow `if: always()`）。
注意：`fetch_data --limit` 会把 `index.json` 重建为部分公司，**勿在生产工作目录使用**。
`fetch_edb.py` 已改为点级合并写盘（365 天窗口与存量按日期并集），历史点/空返回指标不丢。

## 验证脚本

```powershell
python -m scripts.verify_import   # 导入行数/聚合抽查
python -m scripts.verify_api      # API 响应 vs 原 JSON 逐字段对比
python -m scripts.verify_filters  # 列表筛选与排序 23 组用例 vs 原前端语义对答案(含板块/ST 维度)
```

## 目录

```
backend/
  app/            FastAPI(接口/模型/DB)     scripts/  建表·导入·验证
  collector/      采集脚本+JSON 工作目录+调度(run.py/scheduler.py)
  docs/schema.sql 20 表 DDL 存档
frontend/         Vue3+Vite(hash 路由, echarts)
ops/              启停脚本 start/stop/status(.ps1 + .bat 包装)
```
