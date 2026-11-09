# 停止 scripts/start_watch.ps1 启动的订阅刷新进程（只认 PID 文件）。

$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
$pidPath = Join-Path $root "local\subscriptions\watch.pid"

if (-not (Test-Path $pidPath)) {
    Write-Output "未找到 PID 文件: $pidPath"
    exit 0
}

$watchPid = (Get-Content $pidPath | Select-Object -First 1)
if (-not $watchPid) {
    Remove-Item $pidPath -Force
    Write-Output "PID 文件为空，已清理"
    exit 0
}

$proc = Get-Process -Id $watchPid -ErrorAction SilentlyContinue
if ($proc -and $proc.ProcessName -match "python|py") {
    Stop-Process -Id $watchPid -Force
    Write-Output "已停止订阅刷新: PID=$watchPid"
} else {
    Write-Output "PID=$watchPid 不存在或不是 Python，跳过"
}

Remove-Item $pidPath -Force
