# 启动专用 Mihomo 实例（不干扰日常 Clash 客户端）
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File scripts\start_mihomo.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\start_mihomo.ps1 -Bin "C:\path\mihomo.exe"
#
# 配置默认: local/mihomo/config.yaml
# PID 文件: local/mihomo/mihomo.pid

param(
    [string]$Bin = $env:MIHOMO_BIN,
    [string]$DataDir = "",
    [string]$Config = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

if (-not $DataDir) { $DataDir = Join-Path $root "local\mihomo" }
if (-not $Config)  { $Config  = Join-Path $DataDir "config.yaml" }

if (-not $Bin) {
    $candidates = @(
        "C:\Program Files\Clash Verge\verge-mihomo.exe",
        "C:\Program Files\Clash Verge\mihomo.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $Bin = $c; break }
    }
    if (-not $Bin) { $Bin = "mihomo.exe" }
}

if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Force -Path $DataDir | Out-Null }
if (-not (Test-Path $Config)) {
    Write-Error "找不到配置: $Config（先运行 python clash_cli.py sub sync）"
    exit 1
}

$pidPath = Join-Path $DataDir "mihomo.pid"
if (Test-Path $pidPath) {
    $old = (Get-Content $pidPath -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) {
        Write-Output "已在运行: PID=$old"
        exit 0
    }
    Remove-Item $pidPath -Force -ErrorAction SilentlyContinue
}

# ⚠️ Windows 路径可能带空格，-ArgumentList 必须整体加引号，否则 mihomo 会
# 把路径截断成 C:\Users\...\Python，然后静默使用默认端口/配置。
$argLine = '-d "{0}" -f "{1}"' -f $DataDir, $Config
$p = Start-Process -FilePath $Bin -ArgumentList $argLine `
    -WorkingDirectory $DataDir `
    -RedirectStandardOutput (Join-Path $DataDir "stdout.log") `
    -RedirectStandardError  (Join-Path $DataDir "stderr.log") `
    -WindowStyle Hidden -PassThru

Set-Content -Path $pidPath -Value $p.Id -Encoding ascii
Write-Output "已启动: PID=$($p.Id)"
Write-Output "数据目录: $DataDir"
Write-Output "配置: $Config"
Write-Output "日志: $DataDir\stdout.log"
