# -*- ops 公共服务函数 -*-
# 被 start.ps1 / stop.ps1 / status.ps1 dot-source 引入,勿直接执行。
# 进程识别一律按命令行特征匹配(而非 pid 文件),避免 PID 复用误杀、
# 也避免脚本换机器后状态错乱。

$ErrorActionPreference = 'Stop'

$Root     = Split-Path $PSScriptRoot -Parent
$Backend  = Join-Path $Root 'backend'
$Frontend = Join-Path $Root 'frontend'
$RunDir   = Join-Path $Root 'run'
# 停机标记:stop.ps1 写入、start.ps1 删除;guard.ps1 见到它就静默让路,
# 否则手动关服务会被计划任务在下一个 tick 复活(等于关不掉)
$PausedFile = Join-Path $RunDir '.paused'

if ($env:VA_PORT)   { $Port   = [int]$env:VA_PORT } else { $Port = 8000 }
if ($env:VA_PYTHON) { $Python = $env:VA_PYTHON }        else { $Python = 'python' }

$HealthUrl = "http://127.0.0.1:$Port/api/health"
$AppUrl    = "http://127.0.0.1:$Port/"

# 服务表:Match = 命令行特征;Log = run/<name>.log 前缀
function Get-SvcTable {
    @{
        api       = @{ Match = 'uvicorn\s+app\.main:app'; Log = 'api';       Desc = 'FastAPI 服务(:' + $Port + ')' }
        scheduler = @{ Match = 'collector\.scheduler';    Log = 'scheduler'; Desc = '采集调度器(APScheduler)' }
    }
}

function Find-SvcProc([string]$Name) {
    $match = (Get-SvcTable)[$Name].Match
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match $match }
}

# 采集 job 子进程(stock/agro/deep/events/import):调度器被杀后可能残留
# 含 multiprocessing spawn 工人（--workers>1 的子进程）：只杀父进程会留一堆
# 孤儿 worker 继续打数据源（上一轮全量抓取就是这么断的）
function Find-JobProc {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'collector\.run\s|collector\\scripts|agro-price\\scripts|multiprocessing\.spawn|fetch_data\.py' }
}

# 服务日志轮转:启动前把上一轮现场改名留存。
# 为什么不是“追加”:Start-Process -RedirectStandardOutput 只接受路径,PowerShell 以
# 覆盖方式打开文件(无 -Append 语义),想追加只能改成 cmd /c ... >> log 2>&1,而那样
# -PassThru 拿到的 PID 会是 cmd 的。轮转反而更干净:每轮现场单独一个文件,不交叉。
# 没这一轮的代价已经付过:2026-09-03 想回查 09:05 agro 为何报 fetch_edb 失败时,发现
# 唯一输出现场早已在上一次重启时被截掉(run/scheduler.out.log 里本该有 scheduler.py
# 的“触发/退出码”两行,子进程不另定向,traceback 也跟着落在这里)。每流只留最近 10 份。
function Rotate-SvcLog([string]$Path) {
    try {
        if (-not (Test-Path $Path)) { return }
        if ((Get-Item $Path).Length -eq 0) { return }   # 空日志不值得留
        $dir  = Split-Path $Path -Parent
        $base = (Split-Path $Path -Leaf) -replace '\.log$', ''
        # 时间戳只到秒,同一秒内转两次（单测里撞到的）会撞上自己刚生成的副本;
        # 目标已存在时退到 -2/-3 序号,不能静默走进 catch 去覆盖——那就丢了现场
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $new = $base + '.' + $stamp + '.log'
        $i = 2
        while (Test-Path (Join-Path $dir $new)) {
            $new = $base + '.' + $stamp + '-' + $i + '.log'; $i++
        }
        Rename-Item -Path $Path -NewName $new
        # 只按“<base>.<时间戳>.log”回收,不用 -Filter（它的通配语义有 8.3 名陷阱,
        # 万一把正在写的 <base>.out.log 列进去删了就是丢现场）
        $pat = '^' + [regex]::Escape($base) + '\.\d{8}-\d{6}(-\d+)?\.log$'
        $stale = @(Get-ChildItem -Path $dir -File -ErrorAction SilentlyContinue |
                   Where-Object { $_.Name -match $pat } |
                   Sort-Object LastWriteTime -Descending | Select-Object -Skip 10)
        foreach ($f in $stale) { Remove-Item $f.FullName -Force }
    } catch {
        # 上一轮进程还扣着这个文件时不能阻断启动,退回覆盖旧行为(带上原因便于定位)
        Write-Host ("  [警告] 日志轮转失败,本次直接覆盖: {0} — {1}" -f (Split-Path $Path -Leaf), $_.Exception.Message)
    }
}

function Start-Svc([string]$Name, [string[]]$ArgList) {
    $tbl = (Get-SvcTable)[$Name]
    $existing = @(Find-SvcProc $Name)
    if ($existing.Count -gt 0) {
        Write-Host ("  [跳过] {0} 已在运行 (PID {1})" -f $tbl.Desc, ($existing.ProcessId -join ', '))
        return $null
    }
    if (-not (Test-Path $RunDir)) { New-Item -ItemType Directory -Path $RunDir | Out-Null }
    $out = Join-Path $RunDir ($tbl.Log + '.out.log')
    $err = Join-Path $RunDir ($tbl.Log + '.err.log')
    Rotate-SvcLog $out
    Rotate-SvcLog $err
    $p = Start-Process -FilePath $Python -ArgumentList $ArgList -WorkingDirectory $Backend `
            -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $out -RedirectStandardError $err
    Write-Host ("  [启动] {0}  PID={1}  日志 run/{2}.*.log" -f $tbl.Desc, $p.Id, $tbl.Log)
    return $p.Id
}

function Stop-Svc([string]$Name) {
    $tbl = (Get-SvcTable)[$Name]
    $procs = @(Find-SvcProc $Name)
    if ($procs.Count -eq 0) {
        Write-Host ("  [停止] {0} 未在运行" -f $tbl.Desc)
        return $true
    }
    foreach ($p in $procs) {
        # /T 连带子进程一起结束(调度器可能正拉起采集脚本)
        & taskkill /T /F /PID $p.ProcessId 2>$null | Out-Null
        Write-Host ("  [停止] {0}  PID={1}" -f $tbl.Desc, $p.ProcessId)
    }
    Start-Sleep -Milliseconds 800
    return (@(Find-SvcProc $Name).Count -eq 0)
}

function Stop-Jobs {
    $procs = @(Find-JobProc)
    foreach ($p in $procs) {
        & taskkill /T /F /PID $p.ProcessId 2>$null | Out-Null
        Write-Host ("  [清理] 采集子进程 PID={0}" -f $p.ProcessId)
    }
    return $procs.Count
}

function Wait-Health([int]$TimeoutSec = 45) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest $HealthUrl -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -eq 200) { return $r.Content }
        } catch { Start-Sleep -Milliseconds 700 }
    }
    return $null
}

function Show-Tail([string]$Path, [int]$Lines = 20) {
    if (Test-Path $Path) {
        Write-Host ("  ---- {0} 末尾 {1} 行 ----" -f (Split-Path $Path -Leaf), $Lines)
        Get-Content $Path -Tail $Lines | ForEach-Object { Write-Host "  $_" }
    }
}

function Test-MySql {
    try {
        $c = New-Object Net.Sockets.TcpClient
        $c.Connect('127.0.0.1', 3306); $c.Close(); return $true
    } catch { return $false }
}
