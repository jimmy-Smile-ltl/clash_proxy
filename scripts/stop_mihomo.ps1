# 停止由 scripts/start_mihomo.ps1 启动的专用 Mihomo 实例。
# 只认 local/mihomo/mihomo.pid，不会误杀 Clash Verge 自己的 verge-mihomo。

param(
    [string]$DataDir = ""
)

$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
if (-not $DataDir) { $DataDir = Join-Path $root "local\mihomo" }

$pidPath = Join-Path $DataDir "mihomo.pid"
if (-not (Test-Path $pidPath)) {
    Write-Output "未找到 PID 文件: $pidPath"
    exit 0
}

$mihomoPid = (Get-Content $pidPath | Select-Object -First 1)
if (-not $mihomoPid) {
    Remove-Item $pidPath -Force
    Write-Output "PID 文件为空，已清理"
    exit 0
}

$proc = Get-Process -Id $mihomoPid -ErrorAction SilentlyContinue
if ($proc -and $proc.ProcessName -match "mihomo") {
    Stop-Process -Id $mihomoPid -Force
    Write-Output "已停止: PID=$mihomoPid"
} else {
    Write-Output "PID=$mihomoPid 不存在或不是 mihomo，跳过"
}

Remove-Item $pidPath -Force
