# 一键关闭:采集调度器 与 FastAPI 服务(连带残留采集子进程)
# 用法:
#   .\ops\stop.ps1               关闭全部(默认)
#   .\ops\stop.ps1 -Api          只关服务
#   .\ops\stop.ps1 -Scheduler    只关调度器
#   .\ops\stop.ps1 -KeepJobs     关闭服务/调度器但保留正在跑的采集任务
param(
    [switch]$Api,
    [switch]$Scheduler,
    [switch]$KeepJobs
)

. (Join-Path $PSScriptRoot '_common.ps1')

Write-Host "=== stock-value-analysis 关闭 ===" -ForegroundColor Cyan

# 先写停机标记再杀进程:计划任务 va-guard 每 10 分钟跑一次 guard.ps1,
# 不立这个牌子就会在下一轮把服务重新拉起(等于关不掉)
if (-not (Test-Path $RunDir)) { New-Item -ItemType Directory -Path $RunDir | Out-Null }
Set-Content -Path $PausedFile -Encoding UTF8 -Value (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')

$onlyOne = $Api.IsPresent -or $Scheduler.IsPresent
$doApi       = -not $onlyOne -or $Api.IsPresent
$doScheduler = -not $onlyOne -or $Scheduler.IsPresent   # 不带参数 = 全关

# 先停调度器,避免其在服务关闭间隙再拉起新任务
if ($doScheduler) {
    Write-Host "`n-- 定时采集 --"
    if (-not (Stop-Svc 'scheduler')) {
        Write-Host "  [警告] 调度器进程未能全部结束,请检查任务管理器" -ForegroundColor Yellow
    }
}

if ($doApi) {
    Write-Host "`n-- 服务 --"
    if (-not (Stop-Svc 'api')) {
        Write-Host "  [警告] 服务进程未能全部结束,请检查任务管理器" -ForegroundColor Yellow
    }
}

# 采集脚本可能被独立启动过(手动 python -m collector.run stock),随整体/调度器一并清理;
# 只停服务(-Api)时不动正在跑的采集任务
if (-not $KeepJobs -and -not $Api.IsPresent) {
    Write-Host "`n-- 采集子进程 --"
    $k = Stop-Jobs
    if ($k -eq 0) { Write-Host "  [清理] 无残留采集进程" }
}

Write-Host "`n=== 已关闭 ===" -ForegroundColor Green
