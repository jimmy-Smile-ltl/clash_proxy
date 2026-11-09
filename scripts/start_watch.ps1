# 启动「专用 Mihomo + 定期刷新」后台进程（Windows）。
#
# 用法:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_watch.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_watch.ps1 -Interval 3600
#
# 行为:
#   1. 确保 local/mihomo 实例在运行（scripts/start_mihomo.ps1）
#   2. 后台运行: python clash_cli.py sub watch --reload --test
#   3. PID: local/subscriptions/watch.pid
#   4. 日志: local/subscriptions/watch.log / watch.err.log

param(
    [int]$Interval = 0
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$localDir = Join-Path $root "local\subscriptions"
if (-not (Test-Path $localDir)) { New-Item -ItemType Directory -Force -Path $localDir | Out-Null }

$pidPath = Join-Path $localDir "watch.pid"
$logPath = Join-Path $localDir "watch.log"
$errPath = Join-Path $localDir "watch.err.log"

if (Test-Path $pidPath) {
    $old = (Get-Content $pidPath -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) {
        Write-Output "订阅刷新已在运行: PID=$old"
        exit 0
    }
    Remove-Item $pidPath -Force -ErrorAction SilentlyContinue
}

# 确保专用实例在跑
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "start_mihomo.ps1")

# 找到真正的 python.exe（py.exe 启动后会退出，PID 不对）
$pyExe = ""
$pyCmd = Get-Command py -ErrorAction SilentlyContinue
if ($pyCmd) {
    try { $pyExe = (& $pyCmd.Source -3 -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1) } catch {}
}
if (-not $pyExe) {
    $pyCmd2 = Get-Command python -ErrorAction SilentlyContinue
    if ($pyCmd2) { $pyExe = $pyCmd2.Source }
}
if (-not $pyExe -or -not (Test-Path $pyExe)) {
    Write-Error "找不到 Python，请安装 Python 3 或设置 PATH"
    exit 1
}

# watch 默认就会 reload + test；要关闭用 --no-reload / --no-test
$argLine = "clash_cli.py sub watch --interval $Interval"
$p = Start-Process -FilePath $pyExe -ArgumentList $argLine `
    -WorkingDirectory $root `
    -RedirectStandardOutput $logPath `
    -RedirectStandardError $errPath `
    -WindowStyle Hidden -PassThru

Set-Content -Path $pidPath -Value $p.Id -Encoding ascii
Write-Output "订阅刷新已启动: PID=$($p.Id)"
Write-Output "间隔: $(if ($Interval -gt 0) { $Interval } else { '使用 .env 的 CLASH_SUBSCRIPTION_INTERVAL' }) 秒"
Write-Output "日志: $logPath"
