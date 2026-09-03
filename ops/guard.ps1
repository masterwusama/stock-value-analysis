# 计划任务动作:幂等拉起 FastAPI 服务 + 采集调度器(已在跑则 start.ps1 内部逐个跳过)
# 用法(写进计划任务动作,也可以手动跑一次自检):
#   .\ops\guard.ps1
#   .\ops\guard.ps1 -PythonExe C:\path\to\python.exe   # SYSTEM 账户下 PATH 未必有 python
#
# 崩溃自拉起为什么是"周期触发 + 幂等启动":APScheduler 挂掉的全部表现就是进程
# 消失,没有可订阅的事件源;让计划任务每 N 分钟跑一次本脚本,语义上等价于
# "最多 N 分钟内被拉起来",同时顺带覆盖开机时 MySQL/网络尚未就绪导致的首启失败。
#
# 手动 .\ops\stop.ps1 会写 run/.paused 让守护让路(否则会被周期性复活,等于关不掉);
# 再跑一次 .\ops\start.ps1 即解除标记。
#
# 杀软注意:本脚本以隐藏窗口 + Bypass 派生 powershell,容易被判成恶意脚本;
# 2026-09-03 就出现过文件被实时防护截成 0 字节(读它报 ERROR_VIRUS_INFECTED)。
# 若 status.ps1 报守护任务在跑却无日志,先把本目录加进杀软排除项再重存文件。
param([string]$PythonExe = '')

. (Join-Path $PSScriptRoot '_common.ps1')

if ($PythonExe) { $env:VA_PYTHON = $PythonExe }
if (-not (Test-Path $RunDir)) { New-Item -ItemType Directory -Path $RunDir | Out-Null }
if (Test-Path $PausedFile) { exit 0 }      # 停机维护中,静默让路

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
$log = Join-Path $RunDir 'guard.log'

# 快路径:服务与调度器都在线就直接收工,不派生嵌套 powershell 跑整个 start.ps1
# (实测任务上下文里那一次完整启动要两分钟,稳态只查 WMI 则是亚秒级)
$apiUp = @(Find-SvcProc 'api').Count
$schUp = @(Find-SvcProc 'scheduler').Count
if ($apiUp -gt 0 -and $schUp -gt 0) {
    Add-Content -Path $log -Encoding UTF8 -Value "[$stamp] guard 服务在线,无动作"
    if (@(Get-Content $log -Encoding UTF8).Count -gt 400) {
        Get-Content $log -Tail 400 -Encoding UTF8 | Set-Content $log -Encoding UTF8
    }
    exit 0
}

# 兜底路径：确有服务缺失 → 派生一个独立进程跑 start.ps1（带前置检查与健康等待）
# 只“发火不等回收”：不能捕获子进程输出，PowerShell 为管道创建的匿名句柄会随
# Start-Process（bInheritHandles=TRUE）泄给孙子进程（python 调度器），导致本脚本永远
# 等不到管道关闭而挂死；任务设了 IgnoreNew，挂住的 guard 会吃掉后续所有 tick，
# 守护反而比被守护的对象先失聪。拉起结果由下一个 tick 的快路径复检，现场看日志。
$startOut = Join-Path $RunDir 'guard.start.out'
# 上一轮 start.ps1 到底卡在哪一步,只存在这两个文件里;它们同样会被 Start-Process
# 覆盖式打开,所以拉起前先轮转（guard 每 10 分钟一个 tick,不轮转就只能看到最后一次）
Rotate-SvcLog $startOut
Rotate-SvcLog (Join-Path $RunDir 'guard.start.err')
try {
    $null = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
            '-File', (Join-Path $PSScriptRoot 'start.ps1')) `
        -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
        -RedirectStandardOutput $startOut `
        -RedirectStandardError (Join-Path $RunDir 'guard.start.err')
} catch {
    # 上一轮子进程还扣着同一个输出文件时换一个带时间戳的名字重试,不阻塞本次拉起
    $startOut = Join-Path $RunDir ('guard.start.{0:yyyyMMdd-HHmmss}.out' -f (Get-Date))
    $null = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
            '-File', (Join-Path $PSScriptRoot 'start.ps1')) `
        -WorkingDirectory $PSScriptRoot -WindowStyle Hidden `
        -RedirectStandardOutput $startOut `
        -RedirectStandardError (Join-Path $RunDir 'guard.start.err')
}
# 上面 catch 里换名的历史副本（guard.start.<时间戳>.out）谁都不清——Rotate-SvcLog 只按
# 自己那套“<base>.<时间戳>.log”命名回收，不认这个形状。反复拉起失败就是每 10 分钟多
# 一个文件永远涨，这里按同口径回收（留最近 10 份）。
foreach ($f in @(Get-ChildItem -Path $RunDir -File -ErrorAction SilentlyContinue |
                 Where-Object { $_.Name -match '^guard\.start\.\d{8}-\d{6}\.out$' } |
                 Sort-Object LastWriteTime -Descending | Select-Object -Skip 10)) {
    Remove-Item $f.FullName -Force -ErrorAction SilentlyContinue
}
# 现场文件名必须跟着 $startOut 走：走了 catch 兜底路径时它已经不是 guard.start.out 了，
# 写死名字就是把人指向一个没被本轮写过的文件
Add-Content -Path $log -Encoding UTF8 -Value `
    "[$stamp] guard 缺失(api=$apiUp scheduler=$schUp) → 已触发 start.ps1 拉起,现场见 run/$(Split-Path $startOut -Leaf)(旧现场已轮转为 guard.start.out.<时间戳>.log)"

if (@(Get-Content $log -Encoding UTF8).Count -gt 400) {
    Get-Content $log -Tail 400 -Encoding UTF8 | Set-Content $log -Encoding UTF8
}
exit 0
