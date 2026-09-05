# 手动补跑采集任务:跑的正是调度器到点执行的那一条(collector/run.py),
# 自动回灌 MySQL 并写 etl_job_log,不必先停调度器。
# 用法(仓库根目录):
#   .\ops\collect.ps1                      每日节奏:依次跑 stock → agro
#   .\ops\collect.ps1 agro                 只跑一个 job(stock/deep/agro/edb/events/import)
#   .\ops\collect.ps1 deep -Background     后台跑(数小时的任务别占着窗口)
#   .\ops\collect.ps1 -List                看 job 表、调度时刻、最近运行记录
#   .\ops\collect.ps1 stock --codes 600519 尾参透传给该 job 首个采集脚本(整体替代默认参数)
#
# 前台跑别用 Ctrl+C 中断:run.py 只在收尾时写 etl_job_log,中断会留下一条永不结束的
# running 行(2026-09-03 的 id=14 就是被裸 stop.ps1 这么弄出来的)。长任务用 -Background。
# 勿在生产工作目录透传 fetch_data 的 --limit:它会把 index.json 重建成部分公司。
# 双击入口见 ops\collect.bat。
param(
    [Parameter(Position = 0)]
    [ValidateSet('daily','stock','deep','agro','edb','events','import')]
    [string]$Job = 'daily',
    [switch]$Background,
    [switch]$List,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Extra
)

. (Join-Path $PSScriptRoot '_common.ps1')

# daily = 调度表里"每天/几乎每天都该有新数据"的那两个 job(见 scheduler.py 的 add_job)。
# deep 是周六、edb 是周日,都属于周更;edb 还依赖本机 Wind 客户端已登录,不放进默认。
$DailyJobs = @('stock','agro')
$JobWhen = [ordered]@{
    stock  = '周一~六 16:05/22:05  腾讯批量估值快照 → 回灌(≈2 分钟)'
    agro   = '每天 09:05/21:05     生意社/中农立华农价 → 回灌(2026-09-03 实测 41 分钟)'
    deep   = '周六 09:05           全市场财务深抓 --resume(≈5 小时,可断点续)'
    edb    = '周日 20:00           Wind 行业量价(唯一自动碰 Wind 的任务)'
    events = '不进调度             Wind 一次性事件/股东'
    import = '不进调度             只把 JSON 工作目录回灌 MySQL'
}

# 前台执行并拿退出码。为什么是 Start-Process 而不是 `& python`:
# `&` 给子进程的是管道,python 的 stdout 于是块缓冲——41 分钟的 agro 全程不吐字,
# 看着就是卡死;而 2>&1 一接上 stderr,ErrorActionPreference=Stop(_common.ps1 设的)
# 又会把普通告警判成终止性错误抛断脚本(实测退出码 0 照样红着中断)。
# -NoNewWindow 让子进程直接接住控制台:进度实时、stderr 只是普通输出。
function Invoke-PyFg([string[]]$ArgList) {
    $p = Start-Process -FilePath $Python -ArgumentList $ArgList -WorkingDirectory $Backend `
            -NoNewWindow -Wait -PassThru
    return $p.ExitCode
}

function Show-JobLog([int]$N) {
    # 只读回显,不关心退出码,所以不必绕 Start-Process
    & $Python -X utf8 -m scripts.job_status -n $N
}

if ($List) {
    Write-Host "=== 采集 job 与调度时刻(定义在 backend\collector\scheduler.py) ===" -ForegroundColor Cyan
    foreach ($k in $JobWhen.Keys) { Write-Host ("  {0,-7} {1}" -f $k, $JobWhen[$k]) }
    Write-Host ("  {0,-7} = {1}(本脚本默认目标)" -f 'daily', ($DailyJobs -join ' + '))
    Write-Host "`n-- 最近运行记录 --"
    Push-Location $Backend
    try { Show-JobLog 8 } finally { Pop-Location }
    return
}

$targets = if ($Job -eq 'daily') { $DailyJobs } else { @($Job) }
if ($Background -and $targets.Count -gt 1) {
    Write-Host "  [中止] -Background 只支持单个 job:分开跑 -Job stock -Background 与 -Job agro -Background" -ForegroundColor Red
    exit 1
}
# run.py 自己也会滤掉 '--',这里再兜一层,免得空元素被拼进命令行
$extra = @($Extra | Where-Object { $_ -and $_ -ne '--' })

Write-Host ("=== 手动采集:{0} ===" -f ($targets -join ' → ')) -ForegroundColor Cyan

# --- 前置检查 ---
if (-not (Test-Path (Join-Path $Backend '.env'))) {
    Write-Host "  [中止] backend\.env 不存在(复制 backend\.env.example 再填 DATABASE_URL)" -ForegroundColor Red
    exit 1
}
if (-not (Test-MySql)) {
    Write-Host "  [中止] MySQL 3306 未监听,请先启动数据库服务(服务名通常为 MySQL)" -ForegroundColor Red
    exit 1
}
# 两个 fetch_data 并行会互盖 index.json。锁本是为调度器写的,人手动补跑最容易撞上这一条,
# 所以在这里先拦住,而不是等 fetch_data 退出码 2 再猜原因。
$running = @(Find-JobProc)
if ($running.Count -gt 0) {
    Write-Host ("  [中止] 已有采集进程在跑 PID {0},先看 .\ops\status.ps1 的「抓取锁」一行" -f ($running.ProcessId -join ', ')) -ForegroundColor Red
    exit 1
}

$failed = @()
foreach ($name in $targets) {
    Write-Host ("`n-- {0} --" -f $name)
    $argList = @('-X','utf8','-m','collector.run',$name) + $extra

    if ($Background) {
        if (-not (Test-Path $RunDir)) { New-Item -ItemType Directory -Path $RunDir | Out-Null }
        $out = Join-Path $RunDir ('collect.' + $name + '.out.log')
        $errf = Join-Path $RunDir ('collect.' + $name + '.err.log')
        Rotate-SvcLog $out; Rotate-SvcLog $errf
        $p = Start-Process -FilePath $Python -ArgumentList $argList -WorkingDirectory $Backend `
                -WindowStyle Hidden -PassThru -RedirectStandardOutput $out -RedirectStandardError $errf
        Write-Host ("  [后台] {0} PID={1}  日志 run\collect.{2}.out.log  进度 .\ops\status.ps1" -f $name, $p.Id, $name)
        continue
    }

    $t0 = Get-Date
    $code = Invoke-PyFg $argList
    $secs = [int]((Get-Date) - $t0).TotalSeconds
    if ($code -eq 0) {
        # 0 也含"锁被占直接跳过"这一支(run.py 把它记成 success),别当成真抓了一遍
        Write-Host ("  [完成] {0} 用时 {1}s" -f $name, $secs)
    } else {
        Write-Host ("  [失败] {0} 退出码 {1} 用时 {2}s — 摘要看下面的运行记录" -f $name, $code, $secs) -ForegroundColor Red
        $failed += $name
    }
}

Write-Host "`n-- 最近运行记录 --"
Push-Location $Backend
try { Show-JobLog ($targets.Count + 2) } finally { Pop-Location }

if ($failed.Count -gt 0) {
    Write-Host ("`n=== 未成功:{0} ===" -f ($failed -join ', ')) -ForegroundColor Red
    exit 1
}
if ($Background) {
    Write-Host "`n=== 已转入后台,现在只等启动成功 ===" -ForegroundColor Green
} else {
    Write-Host "`n=== 完成 ===" -ForegroundColor Green
}
