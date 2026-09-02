# 状态一览:服务/调度器进程、健康检查、前端产物、日志、最近采集任务
# 用法:
#   .\ops\status.ps1             常规状态
#   .\ops\status.ps1 -Jobs       附带最近采集任务记录(需数据库可连)
param([switch]$Jobs)

. (Join-Path $PSScriptRoot '_common.ps1')

Write-Host "=== stock-value-analysis 状态 ===" -ForegroundColor Cyan

Write-Host ("`nMySQL 3306        : {0}" -f $(if (Test-MySql) { '在线' } else { '离线(先启动数据库服务)' }))

foreach ($name in 'api', 'scheduler') {
    $tbl = (Get-SvcTable)[$name]
    $procs = @(Find-SvcProc $name)
    if ($procs.Count -gt 0) {
        Write-Host ("{0,-17}: 运行中 PID {1}" -f $tbl.Desc, ($procs.ProcessId -join ', '))
    } else {
        Write-Host ("{0,-17}: 未运行" -f $tbl.Desc)
    }
}

$listen = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
Write-Host ("端口 {0}          : {1}" -f $Port, $(if ($listen) { "监听中(PID $(($listen.OwningProcess | Sort-Object -Unique) -join ', '))" } else { '未监听' }))

if (@(Find-SvcProc 'api').Count -gt 0) {
    try {
        $h = (Invoke-WebRequest $HealthUrl -UseBasicParsing -TimeoutSec 8).Content
        Write-Host ("健康检查          : $h")
    } catch {
        Write-Host ("健康检查          : 失败 $($_.Exception.Message)")
    }
}

$Dist = Join-Path $Frontend 'dist'
if (Test-Path (Join-Path $Dist 'index.html')) {
    $js = @(Get-ChildItem (Join-Path $Dist 'assets') -Filter '*.js' -ErrorAction SilentlyContinue)
    Write-Host ("前端产物          : 已构建 ({0:yyyy-MM-dd HH:mm}, {1} 个 chunk)" -f `
        (Get-Item (Join-Path $Dist 'index.html')).LastWriteTime, $js.Count)
} else {
    Write-Host "前端产物          : 未构建(执行 .\ops\start.ps1 -Rebuild)"
}

$jobs_ = @(Find-JobProc)
if ($jobs_.Count -gt 0) {
    Write-Host ("采集任务          : 正在运行 PID $($jobs_.ProcessId -join ', ')")
}

Write-Host ("访问地址          : $AppUrl")

if ($Jobs) {
    Write-Host "`n-- 最近采集任务 --"
    Push-Location $Backend
    try { & $Python -X utf8 -m scripts.job_status } finally { Pop-Location }
}
