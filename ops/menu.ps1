# 跑数菜单 + 实时进度表：选一个数据维度就开跑，当前窗口只当"看表"。
# 用法（仓库根目录）：
#   .\ops\menu.ps1                        进菜单
#   .\ops\menu.ps1 -Job deep              跳过菜单直接跑（ops\jobs\run-deep.bat 走这条）
#   .\ops\menu.ps1 -Watch                 不启动新任务，附著到现在正在跑的那一轮
#   .\ops\menu.ps1 -Job import -NoWatch   拉起来就退出，不看表
#
# 为什么是"后台跑 + 前台看"而不是直接前台跑：run.py 只在收尾写 etl_job_log，前台
# Ctrl+C 会留下一条永不结束的 running 行（2026-09-03 的 id=14 就是这么来的）。这里
# 关窗口或按 Ctrl+C 只是结束看表，采集进程照跑，回头 `menu.ps1 -Watch` 再接上。
# 起跑仍然走 collect.ps1 -Background，前置检查（.env / MySQL / 已有采集进程）与日志
# 轮转都只有一份口径，菜单不另起一套。
param(
    [Parameter(Position = 0)]
    [ValidateSet('','daily','stock','deep','agro','edb','valuation','events','import')]
    [string]$Job,
    [switch]$Watch,
    [switch]$NoWatch,
    [int]$TickSec = 4
)

. (Join-Path $PSScriptRoot '_common.ps1')

# 顺序即"越靠前越该常有新数据"。Wind = 依赖本机 Wind 客户端登录且按调用烧积分，
# 选它要先二次确认（积分是日预算制，见 docs\使用说明书.md §8.1）。
$Meta = [ordered]@{
    stock     = @{ Desc = '全市场行情快照（腾讯批量估值）';      Est = '≈2 分钟';     Wind = $false }
    agro      = @{ Desc = '生意社/中农立华 6 个产品价格';         Est = '20~40 分钟';  Wind = $false }
    deep      = @{ Desc = '全市场财务深抓（四表/分红/报告/评分）'; Est = '3~5 小时';    Wind = $false }
    import    = @{ Desc = '只回灌：JSON 工作目录 → MySQL';        Est = '≈3 分钟';     Wind = $false }
    valuation = @{ Desc = 'PE/PB/PS 十年分位（按游标续跑一批）';  Est = '6~12 分钟';   Wind = $true  }
    edb       = @{ Desc = '行业量价 EDB 指标（增量窗口）';        Est = '3~5 分钟';    Wind = $true  }
    events    = @{ Desc = 'Wind 事件 + 股东结构（默认全市场）';   Est = '数小时';      Wind = $true  }
    daily     = @{ Desc = '每日节奏 = stock → agro';              Est = '≈40 分钟';    Wind = $false }
}
# daily 的展开目标（与 collect.ps1 的 $DailyJobs 同口径）
$DailyJobs = @('stock','agro')

# 各 job 日志里的进度口径见下方 Get-Progress2（按行号自动选当前阶段，不给 job 写死）

# 取整一律用 Floor，绝不用 [int]：PowerShell 的 [int] 是**四舍五入**，151 秒会被
# 算成 `[int](151/60)=3` 分钟、显示成 3:31（单测复现：151→3:31、179→3:59）。
# 看表里“已跑多久”往大里跳一分钟，正好处在“这轮是不是卡了”的判断上，不能错。
function Format-Dur([int]$Sec) {
    if ($Sec -lt 60)    { return "$Sec`秒" }
    if ($Sec -lt 3600)  { return ('{0:d1}:{1:d2}' -f [int][Math]::Floor($Sec / 60), ($Sec % 60)) }
    return ('{0}h{1:d2}m{2:d2}' -f [int][Math]::Floor($Sec / 3600),
                                            [int][Math]::Floor(($Sec % 3600) / 60), ($Sec % 60))
}

# ISO "2026-09-13T05:10:01" → "09-13 05:10"（菜单里只展示到分）
function Format-When([string]$Iso) {
    if ($Iso -and $Iso.Length -ge 16) { return ($Iso.Substring(5, 5) + ' ' + $Iso.Substring(11, 5)) }
    return '-'
}

# 中文在控制台占两格，而 .Length 按字符数算：只按 $w-1 截断的话，一行实际渲染宽度
# 会超窗自动换行，下一轮原地重画就残留半截旧字（实测：l1 的“已跑 2:31”被糊成
# 各种错位样子）。所以截断与补空格都必须按“格”算，不是按字符数算。
function Get-Cells([string]$s) {
    $w = 0
    foreach ($ch in $s.ToCharArray()) {
        $c = [int]$ch
        $wide = ($c -ge 0x1100 -and $c -le 0x115F) -or ($c -ge 0x2E80 -and $c -le 0xA4CF) -or
                ($c -ge 0xAC00 -and $c -le 0xD7A3) -or ($c -ge 0xF900 -and $c -le 0xFAFF) -or
                ($c -ge 0xFE30 -and $c -le 0xFE6F) -or ($c -ge 0xFF00 -and $c -le 0xFF60) -or
                ($c -ge 0xFFE0 -and $c -le 0xFFE6) -or $c -eq 0x2591 -or $c -eq 0x2588   # ░ █
        if ($wide) { $w += 2 } else { $w += 1 }
    }
    return $w
}

function Cut-Cells([string]$s, [int]$Max) {
    if ((Get-Cells $s) -le $Max) { return $s }
    $w = 0; $i = 0
    foreach ($ch in $s.ToCharArray()) {
        $cw = Get-Cells ([string]$ch)
        if ($w + $cw -gt $Max - 1) { break }      # 留最后一格，避免触发自动换行
        $w += $cw; $i++
    }
    return $s.Substring(0, $i)
}

function Format-Bar([int]$Done, [int]$Total, [int]$Width) {
    if ($Total -le 0) { return '' }
    $fill = [int][Math]::Floor([Math]::Min(1, $Done / $Total) * $Width)
    return ('█' * $fill) + ('░' * ($Width - $fill))
}

# etl_job_log 最近若干条（--json 是纯 ASCII 转义，绕开 PS 按 GBK 解码中文的坑）
function Get-JobHistory([int]$N) {
    $tmp  = Join-Path $env:TEMP ('va-menu-out-'  + [Guid]::NewGuid().ToString('N') + '.txt')
    $tmpE = Join-Path $env:TEMP ('va-menu-err-'  + [Guid]::NewGuid().ToString('N') + '.txt')
    try {
        # 用 Start-Process 而不是 `&`：_common.ps1 设了 ErrorActionPreference=Stop，
        # 而 python 只要往 stderr 吐一行告警就会被 PS 判成终止性错误抛断脚本。
        Start-Process -FilePath $Python -ArgumentList @('-X','utf8','-m','scripts.job_status','-n',$N,'--json') `
                -WorkingDirectory $Backend -WindowStyle Hidden -Wait `
                -RedirectStandardOutput $tmp -RedirectStandardError $tmpE | Out-Null
        if (-not (Test-Path $tmp)) { return @() }
        $raw = (Get-Content $tmp -Raw -ErrorAction SilentlyContinue)
        if (-not $raw) { return @() }
        # 必须先接回变量再 return：`return @($raw | ConvertFrom-Json)` 在 PS5 里会把
        # 整个数组当成一个对象吐给调用方（实测：调用侧 foreach 只跑 1 次、$r.job 是
        # Object[]），菜单拿到的就是“一条记录”而且 [int]$r.secs 直接抛类型转换异常
        $objs = $raw | ConvertFrom-Json
        return @($objs)
    } catch {
        return @()
    } finally {
        Remove-Item $tmp,$tmpE -Force -ErrorAction SilentlyContinue
    }
}

# job 名 → 最后一次运行记录（菜单的"最近一次"列与跑完的收尾都读它）
function Get-LastRunsMap([int]$N = 120) {
    $map = @{}
    foreach ($r in @(Get-JobHistory $N)) { if (-not $map.ContainsKey($r.job)) { $map[$r.job] = $r } }
    return $map
}

function Find-RunProc([string]$Name) {
    # 只认 `python -m collector.run <name>` 那个父进程；回灌子进程 import_legacy 由它 wait
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match ('collector\.run\s+' + $Name + '(\s|$)') }
}

# 此刻在跑的 job 名（可能多个：调度器与手动并行时）
function Get-RunningJobs {
    # 用 [regex]::Match 而不是 -match 后的 $_.Matches：后者根本不存在（-match 的分组
    # 在自动变量 $matches 里，而 Where-Object 之后那个上下文己失），拿它会得到 null
    $names = @()
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        ForEach-Object {
            if (-not $_.CommandLine) { return }
            $m = [regex]::Match($_.CommandLine, 'collector\.run\s+(\w+)')
            if ($m.Success) { $names += $m.Groups[1].Value }
        }
    return @($names | Select-Object -Unique)
}

# 一个 job 的日志是分阶段的（抓取 → 回灌），口径得跟着阶段走：抓取看 `[1200/6939]`、
# 回灌看 `[import] 1500/6939 家`、估值分位看 `批 3/56`。所以不按 job 写死映射，而是每种
# 口径都找它最后一次出现的行号，取行号最大的那个当当前进度——谁在跑就用谁的口径。
$ProgressPat = @(
    '^\s*\[(\d+)/(\d+)\]'      # fetch_data：每 200 家一行（stock/deep 的抓取段）
    '\[import\] (\d+)/(\d+)'   # import_legacy：每 500 家一行（所有 job 的回灌段）
    '批 (\d+)/(\d+)'           # fetch_valuation：每批一行
)

function Get-Progress2([string]$Name, [string[]]$Lines) {
    $p = @{ Done = 0; Total = 0; Tag = ''; Last = ''; Kind = ''; Phase = 0; Unit = '家/分' }
    $best = -1
    foreach ($pat in $ProgressPat) {
        for ($i = $Lines.Count - 1; $i -ge 0; $i--) {
            if ($Lines[$i] -match $pat) {
                if ($i -gt $best) {
                    $best = $i; $p.Done = [int]$matches[1]; $p.Total = [int]$matches[2]; $p.Kind = $pat
                }
                break
            }
        }
    }
    if ($best -ge 0) {
        $line = $Lines[$best]
        $bits = @()
        if ($line -match 'ok=(\d+) part=(\d+) fail=(\d+)') {
            $bits += ('ok={0} part={1} fail={2}' -f $matches[1], $matches[2], $matches[3])
        }
        if ($line -match '(\d+) 行待刷') { $bits += ('待刷 {0} 行' -f $matches[1]) }
        if ($line -match 'ETA=([\d.]+)h') {
            $min = [double]$matches[1] * 60
            $bits += ('剩 {0:d1}:{1:d2}' -f [int][Math]::Floor($min), [int][Math]::Round(($min % 1) * 60))
        }
        if ($line -match '· 注意: ') { $bits += '注意:批内有取数失败' }
        if ($p.Kind -eq $ProgressPat[1]) { $bits += '回灌中' }
        # 速率必须按“当前阶段”算。用进程已跑时长作分母会把上一阶段算进来：实测 stock
        # 那轮回灌到 6500/6939 时显示 4193 家/分，因为 93s 里前 58s 在抓行情。import_legacy
        # 每行自带 `· 44s ·`（就是它那一段的计时），拿它当分母才是真速率。
        if ($line -match '·\s*(\d+)s') { $p.Phase = [int]$matches[1] }
        $p.Unit = if ($p.Kind -eq $ProgressPat[2]) { '批/分' } else { '家/分' }
        $p.Tag = ($bits -join '  ')
    } elseif ($Name -eq 'edb') {
        # fetch_edb 只打每指标一行 [ok]，分母在配置文件里，不值得为它读一份 JSON
        $p.Done = @($Lines | Select-String -Pattern '^\s*\[ok\]').Count
        $p.Tag  = '个指标已取回（EDB 一轮 44 个）'
    } elseif ($Name -eq 'agro') {
        # 农价的 [ok]/[stale] 行集中在收尾的断档报告里，抓取途中一条都没有；
        # 拿它当分母会一上来就显示 100%，不如只报已跑时长与最后一行
        $p.Tag = '6 个品种逐个抓，无中间百分比'
    }
    for ($i = $Lines.Count - 1; $i -ge 0; $i--) {
        if ($Lines[$i].Trim() -ne '') { $p.Last = $Lines[$i].Trim(); break }
    }
    return $p
}

$script:CanCursor = $true
try { [Console]::CursorTop | Out-Null } catch { $script:CanCursor = $false }

function Draw-Block([string[]]$Lines, [int]$Prev) {
    if (-not $script:CanCursor) { $Lines | ForEach-Object { Write-Host "  $_" }; return }
    try {
        $w = [Console]::WindowSize.Width
        if ($Prev -gt 0) { [Console]::SetCursorPosition(0, [Console]::CursorTop - $Prev) }
        foreach ($l in $Lines) {
            $l = Cut-Cells ('  ' + $l) ($w - 1)
            # 补到整行宽：否则上一轮更长的内容会在尾部留残字
            Write-Host ($l + (' ' * [Math]::Max(0, $w - 1 - (Get-Cells $l))))
        }
    } catch {
        # 控制台被接管（尺寸变化、输出被重定向）时不抛断，退回逐行追加
        $script:CanCursor = $false
        $Lines | ForEach-Object { Write-Host "  $_" }
    }
}

# 看表该读哪份日志。两条路径写两个文件：手动 collect.ps1 -Background 写
# run/collect.<job>.out.log，调度器触发的那轮只写 run/scheduler.out.log。旧写法是
# “collect 日志存在且不新于进程 → 换调度器”，但调度器那一轮里 collect 日志常常压根不
# 存在（这个 job 从没手动跑过）或还是上一轮的旧文件，条件就永远不成立，整轮显示“无
# 日志”。改成“谁的 mtime 不早于进程创建时间就用谁”，都不新时信调度器（拿旧 collect 日志
# 当现场就是假进度）。
function Select-RunLog([string]$Name, [datetime]$Since) {
    $c = Join-Path $RunDir ('collect.' + $Name + '.out.log')
    $s = Join-Path $RunDir 'scheduler.out.log'
    if ((Test-Path $c) -and (Get-Item $c).LastWriteTime -ge $Since) { return $c }
    if (Test-Path $s) { return $s }
    return $c
}

# 读调度器日志时只取本轮触发之后的行（scheduler.py 每个 job 开头打
# `[scheduler] <时间> 触发 <name>`）。不切的话，上一轮和并行另一个 job 的进度行会被
# 当成当前进度（实测：deep 那轮的 [1200/6939] 会让 stock 的看表显示成同一个进度）。
function Slice-RunLog([string]$Name, [string[]]$Lines, [string]$Path) {
    if ((Split-Path $Path -Leaf) -ne 'scheduler.out.log') { return $Lines }
    for ($i = $Lines.Count - 1; $i -ge 0; $i--) {
        if ($Lines[$i] -match ('触发\s+' + [regex]::Escape($Name) + '\s*$')) {
            return @($Lines[$i..($Lines.Count - 1)])
        }
    }
    return $Lines
}

# 静默多久算“疑似卡住”。不能全用同一个阈值：fetch_data / import_legacy / fetch_valuation
# 都有几十秒一行的心跳，而 fetch_prices.py 整轮只在中途不吐字（品种报告集中在收尾），
# 实测 agro 一轮 7 分多钟日志一个字没有——给它 3 分钟阈值就是每轮都误报。取 40 分钟：
# 比 agro 迄今最长那轮（2026-09-03 实测 41 分钟）略长，报了就值得去看而不是忽略。
$QuietStall = @{ agro = 2400; edb = 900; valuation = 900; events = 900; deep = 600; default = 300 }

function Watch-Job([string]$Name) {
    $log = Join-Path $RunDir ('collect.' + $Name + '.out.log')
    $procs = @()
    for ($i = 0; $i -lt 40; $i++) {          # 等 collect.ps1 后台拉起的进程出现（最多 20 秒）
        $procs = @(Find-RunProc $Name)
        if ($procs.Count -gt 0) { break }
        Start-Sleep -Milliseconds 500
    }
    if ($procs.Count -eq 0) {
        Write-Host ("  [看表] 没等到 collector.run {0}；可能已经秒完，核对 .\ops\status.ps1 -Jobs" -f $Name) -ForegroundColor Yellow
        return
    }
    $target = $procs[0]
    $t0     = $target.CreationDate
    $desc    = if ($Meta[$Name]) { $Meta[$Name].Desc } else { $Name }
    $prev    = 0
    $logShown = ''
    Write-Host ''
    while ($true) {
        if (-not (Get-Process -Id $target.ProcessId -ErrorAction SilentlyContinue)) { break }
        $log = Select-RunLog $Name $t0
        if ($log -ne $logShown) {
            $logShown = $log
            Write-Host ('  [看表] 现场 = run\' + (Split-Path $log -Leaf)) -ForegroundColor DarkGray
        }
        $lines = @()
        # 采集日志是 python 写的 UTF-8（公司名都是中文），PS5 默认按 ANSI 读会乱码
        if (Test-Path $log) { $lines = @(Get-Content $log -Tail 500 -Encoding UTF8 -ErrorAction SilentlyContinue) }
        $lines = Slice-RunLog $Name $lines $log
        $p     = Get-Progress2 $Name $lines
        $el    = [int]((Get-Date) - $t0).TotalSeconds
        $quietS = -1
        $quiet = '无日志'
        if (Test-Path $log) {
            $quietS = [int][Math]::Floor(((Get-Date) - (Get-Item $log -ErrorAction SilentlyContinue).LastWriteTime).TotalSeconds)
            if ($quietS -lt 0) { $quietS = 0 }
            $quiet = ('{0:d2}s' -f $quietS)
        }
        # 静默阈值按 job 走（见 $QuietStall）：低于它只显秒数，不下“卡住”结论
        $stall = if ($QuietStall.ContainsKey($Name)) { $QuietStall[$Name] } else { $QuietStall.default }
        if ($quietS -ge $stall) { $quiet = ('{0}s 超 {1:d0} 未更新' -f $quietS, $stall) }
        $l1 = ('{0}  {1}  ·  PID {2}  ·  已跑 {3}  ·  日志静默 {4}  ·  {5}' -f `
                   $Name, $desc, $target.ProcessId, (Format-Dur $el), $quiet, $p.Tag)
        $bar = Format-Bar $p.Done $p.Total 26
        if ($p.Total -gt 0) {
            # 分母优先用阶段计时（$p.Phase，见 Get-Progress2），否则用进程已跑时长
            $mins = if ($p.Phase -gt 5) { $p.Phase / 60 } else { $el / 60 }
            $rate = if ($mins -gt 0.3) { ('  {0:d} {1}' -f [int][Math]::Floor($p.Done / $mins), $p.Unit) } else { '' }
            $l2 = ('[{0}] {1}/{2} {3:F1}%{4}' -f $bar, $p.Done, $p.Total, (100 * $p.Done / $p.Total), $rate)
        } elseif ($p.Done -gt 0) {
            $l2 = ('已完成 {0} 项' -f $p.Done)
        } else {
            $l2 = '（这一类没有可数分母，看下面最后一行输出）'
        }
        $l3 = if ($p.Last) { '↳ ' + $p.Last } else { '' }
        $block = @($l1, $l2) + $(if ($l3) { @($l3) } else { @() })
        Draw-Block $block $prev
        $prev = $block.Count
        Start-Sleep -Seconds $TickSec
    }
    Write-Host ''
    $last = (Get-LastRunsMap 40)[$Name]
    if ($last) {
        $col = if ($last.status -eq 'success') { 'Green' } elseif ($last.status -eq 'failed') { 'Red' } else { 'Yellow' }
        Write-Host ("  [收口] etl_job_log id={0}  {1}  用时 {2}" -f $last.id, $last.status.ToUpper(), (Format-Dur ([int]$last.secs))) -ForegroundColor $col
        if ($last.message) { Write-Host ("         {0}" -f (($last.message -replace "`n", ' ')[0..89] -join '')) }
    } else {
        Write-Host '  [收口] etl_job_log 里没找到这一轮，直接看下面的日志末尾' -ForegroundColor Yellow
    }
    if (Test-Path $log) {
        Write-Host ('  ---- ' + (Split-Path $log -Leaf) + ' 末尾 8 行 ----')
        @(Get-Content $log -Tail 8 -Encoding UTF8 -ErrorAction SilentlyContinue) |
            ForEach-Object { Write-Host ('  ' + $_) }
    }
    Write-Host '  下一步：.\ops\status.ps1 -Jobs 看全景，或回菜单再跑一个维度' -ForegroundColor DarkGray
}

function Show-Menu {
    $map = Get-LastRunsMap 160
    $run = @(Get-RunningJobs)
    Write-Host ''
    Write-Host '=== 跑数菜单（选一个维度，回车即开跑）===' -ForegroundColor Cyan
    $svc = @(Find-SvcProc 'api'); $sch = @(Find-SvcProc 'scheduler')
    Write-Host ("  MySQL {0}  ·  服务 {1}  ·  调度器 {2}  ·  现在 {3}" -f `
        $(if (Test-MySql) { '在' } else { '不在' }),
        $(if ($svc.Count) { "PID $($svc[0].ProcessId)" } else { '未起' }),
        $(if ($sch.Count) { "PID $($sch[0].ProcessId)" } else { '未起' }),
        (Get-Date -Format 'MM-dd HH:mm')) -ForegroundColor DarkGray
    if ($run.Count -gt 0) {
        Write-Host ("  ⚠ 此刻在跑：{0} —— 先等它跑完，或用 -Watch 附著过去看（再点新的会被抓取锁挡下）" -f ($run -join ', ')) -ForegroundColor Yellow
    }
    Write-Host ''
    Write-Host ('  #   job          内容                                      量级        最近一次') -ForegroundColor DarkGray
    $i = 0
    foreach ($k in $Meta.Keys) {
        $i++
        $m = $Meta[$k]
        $r = $map[$k]
        $last = '—'
        if ($r) {
            $mark = @{ success = 'OK'; failed = 'FAIL'; running = 'RUN' }[$r.status]
            if (-not $mark) { $mark = $r.status }
            $last = '{0} {1} {2}' -f (Format-When $r.started), $mark, $(if ($r.secs) { Format-Dur ([int]$r.secs) } else { '-' })
            if ($r.status -eq 'failed') { $last = $last + ' !' }
        }
        $tag = if ($m.Wind) { '[Wind] ' } else { '         ' }
        $color = if ($m.Wind) { 'Magenta' } else { 'Gray' }
        Write-Host ('  [{0:d}]  {1,-11} {2} {3,-34} {4,-11}  {5}' -f $i, $k, $tag, $m.Desc, $m.Est, $last) -ForegroundColor $color
    }
    Write-Host ''
    Write-Host '  [w] 附著当前正在跑的一轮      [j] 只看最近运行记录      [q] 退出' -ForegroundColor DarkGray
    Write-Host '  [Wind] 那三个要本机 Wind 客户端已登录，且按调用烧当日积分' -ForegroundColor DarkGray
}

# ── 入口分发 ────────────────────────────────────────────────────────────────

if ($Watch) {
    $run = @(Get-RunningJobs)
    if ($run.Count -eq 0) {
        Write-Host '  此刻没有采集在跑。要开一轮：.\ops\menu.ps1' -ForegroundColor Yellow
        $map = Get-LastRunsMap 6
        foreach ($r in $map.Values) { Write-Host ("    {0} {1} {2}  {3}" -f (Format-When $r.started), $r.job.PadRight(14), $r.status.ToUpper().PadRight(7), $(if ($r.secs) { Format-Dur ([int]$r.secs) } else { '-' })) }
        return
    }
    foreach ($n in $run) { Watch-Job $n }
    return
}

if (-not $Job) {
    while ($true) {
        Show-Menu
        $ans = Read-Host '  输入序号或字母'
        if ($ans -match '^\s*$') { continue }
        if ($ans -match '^[qQ]$') { return }
        if ($ans -match '^[wW]$') { foreach ($n in @(Get-RunningJobs)) { Watch-Job $n }; continue }
        if ($ans -match '^[jJ]$') { & (Join-Path $PSScriptRoot 'collect.ps1') -List; continue }
        if ($ans -notmatch '^\d+$' -or [int]$ans -lt 1 -or [int]$ans -gt $Meta.Count) {
            Write-Host '  没这个选项' -ForegroundColor Yellow; Start-Sleep -Milliseconds 700; continue
        }
        $Job = @($Meta.Keys)[[int]$ans - 1]
        break
    }
}

# Wind 类：日预算制，起跑前挡一道（2026-09 的积分约束，见 docs\使用说明书.md §8.1）
if ($Meta[$Job] -and $Meta[$Job].Wind -and -not $NoWatch) {
    Write-Host ("  [Wind] {0} 要本机 Wind 客户端已登录，且按调用烧当日积分。" -f $Job) -ForegroundColor Magenta
    $ok = Read-Host '  确认开跑？回车继续，输入 n 取消'
    if ($ok -match '^\s*[nN]') { Write-Host '  已取消。'; return }
}

$targets = if ($Job -eq 'daily') { $DailyJobs } else { @($Job) }
foreach ($name in $targets) {
    if ($targets.Count -gt 1) { Write-Host ("`n=== daily 第 {0} 步：{1} ===" -f ($targets.IndexOf($name) + 1), $name) -ForegroundColor Cyan }
    # 交给 collect.ps1 起跑：它带 .env / MySQL / 已有采集进程三道前置检查，并且跑的是
    # 调度器同款命令（自动回灌 + 写 etl_job_log），口径不会和调度器分叉。
    & (Join-Path $PSScriptRoot 'collect.ps1') $name -Background
    if ($LASTEXITCODE -ne 0) {
        Write-Host ("  [没起跑] {0} 被 collect.ps1 前置检查挡下（退出码 {1}）；上面有原因" -f $name, $LASTEXITCODE) -ForegroundColor Red
        continue
    }
    if ($NoWatch) { continue }
    Watch-Job $name
}
