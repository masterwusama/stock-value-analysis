# stock-value-analysis

A股 / 港股 / 美股的价值分析数据服务 —— 原 `masterwusama.github.io/stock-data/` 纯前端静态站的本地化重构：
FastAPI + MySQL 提供结构化查询接口，Vue 3 前端，采集层沿用经年验证的 Python 抓取脚本。

## 打开

```
本机          http://127.0.0.1:8000/
手机 / 外网   https://<设备名>.<tailnet>.ts.net/
```

远程访问走 Tailscale Serve，**必须用 `https://`**（这个域名的 80 端口由本机 IIS 应答），
且设备要先加入同一个 tailnet。服务默认只监听 `127.0.0.1:8000`，`ts.net` 是远程访问的唯一入口。
`<设备名>.<tailnet>.ts.net` 换成自己的：Tailscale 管理台的设备列表，或本机 `tailscale status --self`。

三个页面：`/#/` 全市场列表 · `/#/stock/<代码>` 单家详情 · `/#/agro` 农价与行业量价。
窄屏（≤600px）自动切换为移动版：列表改卡片、筛选与排序折叠、图表自适应。

> **部署与运维请看 [docs/使用说明书.md](docs/使用说明书.md)**——三个页面怎么用、数据什么时候更新、
> 首次部署七步、采集任务与时刻表、16 张表逐列口径、故障处置表、已知边界。
> 本文只留架构速览与常用命令。

## 架构

```
┌─ 采集层 collector/ ──────────────┐   ┌─ 服务层 ───────────────────────────┐
│ fetch_data.py   AKShare/腾讯/巨潮 │   │ FastAPI :8000                      │
│ scoring.py      四流派评分物化    │→JSON→│  /api/*      REST 接口           │
│ fetch_prices/fetch_edb 农价/EDB   │ 工作 │  /            托管 frontend/dist │
│ fetch_events.py Wind 事件(手动)   │ 目录 │ MySQL db_va (16 表, localhost)   │
│                                  │      │ collector/scheduler.py 定时进程  │
└──────────────────────────────────┘   └────────────────────────────────────┘
                                        ┌─ 前端 frontend/ (Vue3+Vite) ──────┐
                                        │ 列表/筛选 → #/ · 详情九模块 #/stock/:code
                                        │ 农价·EDB → #/agro                │
                                        └───────────────────────────────────┘
```

- 采集脚本以 **JSON 工作目录**（`collector/{data,agro-price}`）为产物，与 GitHub Pages 原结构逐字段一致，作为唯一中间层；`import_legacy` 增量 upsert 回灌 MySQL。
- 数据库是查询层：列表的筛选、排序、分页全部由 SQL 完成（`score_daily` + `quote_daily`），支撑近 7000 只标的。
- GitHub Pages 静态版保留在博客仓库作为回退；以本仓库与 `db_va` 为准。

## 快速开始

日常只用一条命令：`.\ops\start.ps1`（前端有改动加 `-Rebuild`）。首次部署见使用说明书 §7.1。

```powershell
# 依赖
cd backend
pip install -r requirements.txt -r collector/requirements.txt

# 数据库（首次）：建表 + 全量导入工作目录 JSON
python -m scripts.create_tables
python -m scripts.import_legacy --no-clean

# 前端（改过源码才需要）
cd ..\frontend ; npm install ; npm run build    # 产物 dist/ 由 FastAPI 托管
cd ..\backend

# 服务 + 定时采集
cd ..
.\ops\start.ps1                # 或手工：cd backend; python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 前端开发（热更新，/api 代理到 8000）
cd frontend ; npm run dev      # → http://localhost:5173
```

配置在 `backend/.env`（参考 `backend/.env.example`）：`DATABASE_URL` / `BOND_10Y` / `LEGACY_DATA_DIR`，
外加只有 Wind 两个 job 需要的 `WIND_SKILL_DIR`。

## 启停脚本

双击 `ops\start.bat` / `ops\stop.bat` 即可，或命令行：

```powershell
.\ops\start.ps1                  # 服务 + 定时采集（已在跑则跳过，可重复执行）
.\ops\start.ps1 -Rebuild         # 前端有改动：先 npm run build 再启动
.\ops\start.ps1 -NoScheduler     # 只启服务        -Lan: 监听 0.0.0.0
.\ops\status.ps1                 # 进程/端口/健康检查/前端产物/采集任务/抓取锁/index 规模
.\ops\status.ps1 -Jobs           # 附带 etl_job_log 最近运行记录
.\ops\stop.ps1                   # 关调度器 + 服务 + 残留采集子进程
.\ops\stop.ps1 -Api              # 只关服务         -KeepJobs: 不动正在跑的采集
.\ops\collect.ps1                # 手动补跑每日采集：stock → agro（调度器同款命令）
.\ops\collect.ps1 edb -Background # 只跑一个 job；长任务转后台
.\ops\collect.ps1 -List          # job 表 + 调度时刻 + 最近记录
.\ops\guard.ps1                  # 幂等拉起（崩溃自愈，由计划任务每 10 分钟调用）
```

进程按命令行特征识别（不依赖 pid 文件），日志落在 `run/*.log`（已 gitignore）。
环境变量 `VA_PORT` / `VA_PYTHON` 可覆盖端口与 Python 解释器。

## 采集任务

标的池 = 全 A 股（沪深京 ≈5553 只）+ 港股通（≈621 只）+ 美股五大指数成分（≈765 只）。
财务按季更新，所以拆两层节奏，不每天重抓 1.9GB 明细：

| job | 调度 | 内容 |
|---|---|---|
| `stock` | 周一~六 16:05 / 22:05 | 全市场估值快照 → 回灌（≈1~2 分钟） |
| `deep` | 周六 09:05 | 全市场财务/报告/审计重抓 + **评分重算**（≈5 小时，可断点续） |
| `agro` | 每天 09:05 / 21:05 | 农化价格（生意社）→ 回灌 |
| `edb` | 周日 20:00 | 行业量价 44 指标（Wind EDB，唯一自动用 Wind 的任务） |
| `events` | 手动 | Wind 事件与股东结构 |
| `import` | 手动 | 只把 JSON 工作目录回灌 MySQL |

命令统一是 `python -m collector.run <job>`（在 `backend/` 下执行），每次运行写 `etl_job_log`。
全量抓取持 `collector/data/.fetch.lock` 互斥，所以深抓期间到点的 `stock` 会静默跳过——这是设计。
`companies/` 明细与 `index.json` 已 gitignore，换机器靠重新抓取而非 git。

首次上手：先跑一遍 `python -m collector.run deep`（中断后重跑同一命令即从断点续抓），跑完自动回灌，之后交给调度器。

## 验证

```powershell
cd backend
python -X utf8 -m scripts.verify_import     # 16 表行数 + 逐字段断言
python -X utf8 -m scripts.verify_api        # API 响应 vs 源 JSON
python -X utf8 -m scripts.verify_filters    # 列表筛选与排序 35 组用例，源↔库集合级比对
python -X utf8 scripts\verify_writer.py     # 离线：写入层行为
python -X utf8 scripts\verify_edb_merge.py  # 离线：EDB 合并防线与增量窗口
```

评分是 Python 与 JS 两份实现，改任一边都要跑一致性比对：

```powershell
node backend\collector\scripts\_score_check_node.js       # 数秒预检
cd backend\collector; python -X utf8 scripts\_score_check.py   # 全量 ≈7 分钟，逐家比对
```

## 目录

```
backend/
  app/            FastAPI（接口/模型/DB）
  scripts/        建表 · 导入 · 验证
  collector/      采集脚本 + JSON 工作目录 + 调度（run.py / scheduler.py）
                  agro-price/ 农价与 EDB 抓取
frontend/         Vue3 + Vite（hash 路由，echarts）
docs/             使用说明书.md · schema.sql（16 表 DDL 存档）
ops/              start / stop / status / guard / collect（.ps1 + .bat 包装）
run/              运行期日志与停机标记（gitignore）
```
