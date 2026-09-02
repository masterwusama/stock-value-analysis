# 一键启动:FastAPI 服务(+前端) 与 采集调度器
# 用法:
#   .\ops\start.ps1                常规启动(服务 + 调度器)
#   .\ops\start.ps1 -NoScheduler   只启服务,不启定时采集
#   .\ops\start.ps1 -Rebuild       先 npm run build 再启动(前端有改动时用)
#   .\ops\start.ps1 -Lan           监听 0.0.0.0,允许局域网访问
param(
    [switch]$NoScheduler,
    [switch]$Rebuild,
    [switch]$Lan
)

. (Join-Path $PSScriptRoot '_common.ps1')

Write-Host "=== stock-value-analysis 启动 ===" -ForegroundColor Cyan

# --- 前置检查 ---
if (-not (Test-Path (Join-Path $Backend '.env'))) {
    Write-Host "  [警告] backend/.env 不存在,数据库连接可能失败" -ForegroundColor Yellow
}
if (-not (Test-MySql)) {
    Write-Host "  [警告] MySQL 3306 未监听,请先启动数据库服务(服务名通常为 MySQL)" -ForegroundColor Yellow
} else {
    Write-Host "  [检查] MySQL 3306 在线"
}

$Dist = Join-Path $Frontend 'dist'
if ($Rebuild) {
    Write-Host "  [构建] npm run build ..."
    Push-Location $Frontend
    try { & npm run build 2>&1 | Select-Object -Last 3 | ForEach-Object { Write-Host "  $_" } }
    finally { Pop-Location }
}
if (Test-Path (Join-Path $Dist 'index.html')) {
    $built = (Get-Item (Join-Path $Dist 'index.html')).LastWriteTime
    Write-Host ("  [检查] 前端产物已就绪 (build {0:yyyy-MM-dd HH:mm})" -f $built)
} else {
    Write-Host "  [提示] 未找到 frontend/dist,本次仅提供 API;需要界面请执行 .\ops\start.ps1 -Rebuild" -ForegroundColor Yellow
}

# --- 启动服务 ---
$host_ = if ($Lan) { '0.0.0.0' } else { '127.0.0.1' }
Write-Host "`n-- 服务 --"
$null = Start-Svc 'api' @('-X', 'utf8', '-m', 'uvicorn', 'app.main:app',
                          '--host', $host_, '--port', "$Port")

$health = Wait-Health
if (-not $health) {
    Write-Host "`n  [失败] 服务未能在 45 秒内就绪,详见日志:" -ForegroundColor Red
    Show-Tail (Join-Path $RunDir 'api.err.log')
    Show-Tail (Join-Path $RunDir 'api.out.log') 10
    exit 1
}
Write-Host ("  [就绪] /api/health -> {0}" -f $health)

# --- 启动调度器 ---
if (-not $NoScheduler) {
    Write-Host "`n-- 定时采集 --"
    $null = Start-Svc 'scheduler' @('-X', 'utf8', '-m', 'collector.scheduler')
    Start-Sleep -Seconds 2   # 等首行注册日志落盘
    Show-Tail (Join-Path $RunDir 'scheduler.out.log') 3
} else {
    Write-Host "`n-- 定时采集:已按 -NoScheduler 跳过 --"
}

Write-Host "`n=== 完成 ===" -ForegroundColor Green
Write-Host ("  应用  {0}" -f $AppUrl)
Write-Host ("  接口  {0}docs" -f $AppUrl)
Write-Host "  关闭  .\ops\stop.ps1      状态  .\ops\status.ps1"
