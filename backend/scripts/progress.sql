-- 采集 / 回灌进度自查（MySQL 8，库 db_va）。逐条执行，全部只读。
--
-- 重要前提：抓取进程写的是 JSON 文件（backend/collector/data/），只有跑
--   python -m scripts.import_legacy 之后才会进库。所以 SQL 看到的是「已回灌进度」，
--   不是「抓取实时进度」；抓取实时进度看 collector/data/index.json 的 mtime 与 .fetch.lock。
-- 另：etl_job_log 只有经 collector.run 启动的任务才写；直接 python fetch_data.py 不记。

-- 0) 最省事的一条：回灌完成率（分母用深抓目标 4652 只；改了目标就换这里的数字）
SELECT COUNT(DISTINCT f.sid)                        AS imported,
       ROUND(100 * COUNT(DISTINCT f.sid) / 4652, 1)  AS pct,
       MAX(f.updated_at)                            AS last_write
FROM fin_indicator f JOIN security s ON s.sid = f.sid AND s.market = 'A';

-- 1) 各明细表覆盖只数（分母 = 已入库的 A 股名单），一眼看出深抓回灌到哪一步
SELECT 'A_stock_in_db' AS src, COUNT(*) AS sids, NULL AS last_write
FROM security WHERE market = 'A'
UNION ALL
SELECT CONCAT('quote_daily@', (SELECT MAX(trade_date) FROM quote_daily)),
       COUNT(DISTINCT sid), MAX(fetched_at) FROM quote_daily
UNION ALL
SELECT CONCAT('score_daily@', (SELECT MAX(trade_date) FROM score_daily)),
       COUNT(DISTINCT sid), MAX(updated_at) FROM score_daily
UNION ALL
SELECT 'fin_indicator', COUNT(DISTINCT sid), MAX(updated_at) FROM fin_indicator
UNION ALL
SELECT 'fin_income', COUNT(DISTINCT sid), MAX(updated_at) FROM fin_income
UNION ALL
SELECT 'fin_balance', COUNT(DISTINCT sid), MAX(updated_at) FROM fin_balance
UNION ALL
SELECT 'fin_cashflow', COUNT(DISTINCT sid), MAX(updated_at) FROM fin_cashflow
UNION ALL
SELECT 'dividend', COUNT(DISTINCT sid), NULL FROM dividend
UNION ALL
SELECT 'periodic_report', COUNT(DISTINCT sid), NULL FROM periodic_report
UNION ALL
SELECT 'wind_event', COUNT(DISTINCT sid), MAX(fetched_at) FROM wind_event;

-- 2) 缺口：有名单但没有任何财务指标的票（= 深抓/回灌还没覆盖到）
SELECT COUNT(*) AS missing_fin
FROM security s
WHERE s.market = 'A'
  AND NOT EXISTS (SELECT 1 FROM fin_indicator f WHERE f.sid = s.sid);

-- 3) 回灌是否还在推进：看各表「最近一次写库距今」，谁小就是谁在动
--    注意 score_daily 只在重算评分时才变，单独看它会误判成卡住，所以要并排比
SELECT src, MAX(ts) AS last_write, TIMESTAMPDIFF(SECOND, MAX(ts), NOW()) AS secs_ago
FROM (
  SELECT 'fin_indicator' AS src, MAX(updated_at) AS ts FROM fin_indicator
  UNION ALL SELECT 'fin_balance',  MAX(updated_at) FROM fin_balance
  UNION ALL SELECT 'quote_daily',  MAX(fetched_at) FROM quote_daily
  UNION ALL SELECT 'score_daily',  MAX(updated_at) FROM score_daily
) t
GROUP BY src
ORDER BY secs_ago;

-- 4) 期数覆盖：财报按报告期铺开，深抓会在老票上补历史期，看最新几期就知道补到哪
SELECT report_date, COUNT(DISTINCT sid) AS sids, COUNT(*) AS rows_
FROM fin_indicator
GROUP BY report_date
ORDER BY report_date DESC
LIMIT 8;

-- 5) 采集任务运行记录（scheduler / collector.run 才有；含 running 未收口的行）
SELECT job_name, status, started_at, IFNULL(finished_at, '-') AS finished_at,
       TIMESTAMPDIFF(MINUTE, started_at, IFNULL(finished_at, NOW())) AS minutes,
       LEFT(IFNULL(message, ''), 90) AS msg
FROM etl_job_log
ORDER BY started_at DESC
LIMIT 12;

-- 6) 此刻 MySQL 里在跑什么（import_legacy 的大事务 / 别人的长查询都会现形）
SELECT id, user, db, command, time AS secs, state, LEFT(IFNULL(info, ''), 110) AS sql_head
FROM information_schema.processlist
WHERE command <> 'Sleep'
ORDER BY time DESC;

-- 7) 全库行数与体积粗览（information_schema 估算值，秒出，不必 COUNT(*) 扫全表）
--    注意：est_rows 是 InnoDB 采样估算，大批量写入/TRUNCATE 后可能偏一个量级（实测
--    fin_balance 真实 3 万+ 而估算显示 850），只用于看“哪张表占空间”，不要当进度读数。
SELECT table_name, table_rows AS est_rows, ROUND(data_length / 1048576, 1) AS data_mb
FROM information_schema.tables
WHERE table_schema = 'db_va'
ORDER BY data_length DESC;
