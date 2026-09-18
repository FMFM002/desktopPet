# 清理两处「AI 的沙箱动不了」的东西 —— ★ 1.9.0
#
# 用法（在 PowerShell 里粘这一行就行）：
#   & "D:\Dtopdesk\普瑞赛斯在哪\倒腾\桌宠\PTRTS1.9.0\_cleanup_190.ps1"
#
# 干两件事（动手前会先问一次）：
#   ① 删两个空目录：tools\dev_tests\_llm_tmp、tools\dev_tests\_scratch_a
#   ② 清 D:\AppData\Ollama\models\blobs 里的半截下载残片（*-partial*，约 4.36GB）
#      —— 只认 *-partial*，正式的模型文件一个都不碰。
#
# 可传参（默认就是上面那两处，所以直接跑即可）：
#   -Root <项目根>      默认 = 本脚本所在目录
#   -Blobs <blobs 目录>  默认 = D:\AppData\Ollama\models\blobs
#   -Yes                不问，直接干
#   -NoRecycle          不进回收站，直接删

param(
    [string]$Root = "",
    [string]$Blobs = "D:\AppData\Ollama\models\blobs",
    [switch]$Yes,
    [switch]$NoRecycle
)

$ErrorActionPreference = 'Continue'

if (-not $Root) { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }

$dirs = @(
    (Join-Path $Root 'tools\dev_tests\_llm_tmp'),
    (Join-Path $Root 'tools\dev_tests\_scratch_a')
)

function Remove-One([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return $true }
    # 先试普通删除（最省事）；失败再上 takeown + icacls 抢权限
    try {
        Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction Stop
        if (-not (Test-Path -LiteralPath $path)) { return $true }
    } catch {
        Write-Host ("      普通删除失败：{0}" -f $_.Exception.Message) -ForegroundColor DarkYellow
    }
    try {
        Write-Host "      尝试 takeown + icacls 抢权限…" -ForegroundColor DarkGray
        & takeown /f "$path" /r /d y 2>&1 | Out-Null
        & icacls "$path" /grant "$($env:USERNAME):(F)" /t /c 2>&1 | Out-Null
        Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction Stop
        if (-not (Test-Path -LiteralPath $path)) { return $true }
    } catch {
        Write-Host ("      抢权限后仍失败：{0}" -f $_.Exception.Message) -ForegroundColor DarkYellow
    }
    return (-not (Test-Path -LiteralPath $path))
}

function Send-ToBin([System.IO.FileInfo]$file) {
    if ($NoRecycle) { return $false }        # 让调用方走普通删除
    try {
        Add-Type -AssemblyName Microsoft.VisualBasic -ErrorAction Stop
        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
            $file.FullName, 'OnlyErrorDialogs', 'SendToRecycleBin')
        return $true
    } catch {
        return $false
    }
}

Write-Host ''
Write-Host '================ PTRTS1.9.0 清理工具 ================' -ForegroundColor Cyan
Write-Host ("项目根：{0}" -f $Root) -ForegroundColor DarkGray
Write-Host ''

# ── ① 待删目录 ──
Write-Host '① 空目录：' -ForegroundColor Yellow
$dirPlan = @()
foreach ($d in $dirs) {
    if (Test-Path -LiteralPath $d) {
        $n = -1
        try { $n = @(Get-ChildItem -LiteralPath $d -Recurse -Force -ErrorAction SilentlyContinue).Count } catch { }
        Write-Host ("   [存在] {0}   （{1} 项）" -f $d, $n)
        $dirPlan += $d
    } else {
        Write-Host ("   [已没有] {0}" -f $d) -ForegroundColor DarkGray
    }
}

# ── ② 待删残片 ──
$partials = @()
if (Test-Path -LiteralPath $Blobs) {
    $partials = @(Get-ChildItem -LiteralPath $Blobs -File -Force -ErrorAction SilentlyContinue |
                  Where-Object { $_.Name -like '*-partial*' })
}
$pSize = 0.0
if ($partials.Count -gt 0) { $pSize = ($partials | Measure-Object Length -Sum).Sum / 1GB }
Write-Host ''
Write-Host '② Ollama 半截下载残片：' -ForegroundColor Yellow
if ($partials.Count -gt 0) {
    Write-Host ("   {0} 个文件，合计 {1:N2} GB" -f $partials.Count, $pSize)
    Write-Host ("   位置：{0}" -f $Blobs) -ForegroundColor DarkGray
} else {
    Write-Host '   没有找到（可能已经清过了）' -ForegroundColor DarkGray
}
$solid = @()
if (Test-Path -LiteralPath $Blobs) {
    $solid = @(Get-ChildItem -LiteralPath $Blobs -File -Force -ErrorAction SilentlyContinue |
               Where-Object { $_.Name -notlike '*-partial*' })
}
Write-Host ("   （正式模型文件 {0} 个 —— 绝对不动）" -f $solid.Count) -ForegroundColor DarkGray

if ($dirPlan.Count -eq 0 -and $partials.Count -eq 0) {
    Write-Host ''
    Write-Host '没有需要清理的东西，收工。' -ForegroundColor Green
    return
}

if (-not $Yes) {
    Write-Host ''
    $ans = Read-Host '开始清理吗？输入 y 回车继续，其它键取消'
    if ($ans -ne 'y' -and $ans -ne 'Y') {
        Write-Host '已取消，什么都没动。' -ForegroundColor Green
        return
    }
}

Write-Host ''
Write-Host '---- 开始 ----' -ForegroundColor Cyan

foreach ($d in $dirPlan) {
    if (Remove-One $d) {
        Write-Host ("   [已删] {0}" -f $d) -ForegroundColor Green
    } else {
        Write-Host ("   [删不掉] {0}" -f $d) -ForegroundColor Red
        Write-Host '           在资源管理器里右键删除即可（它就是个空壳）' -ForegroundColor DarkGray
    }
}

$okN = 0; $badN = 0
foreach ($f in $partials) {
    $done = $false
    if (Send-ToBin $f) { $done = $true }
    if (-not $done) { $done = Remove-One $f.FullName }
    if ($done) { $okN++ } else {
        $badN++
        Write-Host ("   [删不掉] {0}" -f $f.Name) -ForegroundColor Red
    }
}
if ($partials.Count -gt 0) {
    Write-Host ("   [残片] 清掉 {0} 个，失败 {1} 个" -f $okN, $badN) -ForegroundColor Green
}

Write-Host ''
Write-Host '---- 结果复核 ----' -ForegroundColor Cyan
foreach ($d in $dirs) {
    $still = Test-Path -LiteralPath $d
    $col = if ($still) { 'Yellow' } else { 'Green' }
    Write-Host ("   {0}`n      还存在? {1}" -f $d, $still) -ForegroundColor $col
}
if (Test-Path -LiteralPath $Blobs) {
    $left = @(Get-ChildItem -LiteralPath $Blobs -File -Force -ErrorAction SilentlyContinue |
              Where-Object { $_.Name -like '*-partial*' })
    $ls = 0.0
    if ($left.Count -gt 0) { $ls = ($left | Measure-Object Length -Sum).Sum / 1GB }
    $nSolid = @(Get-ChildItem -LiteralPath $Blobs -File -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -notlike '*-partial*' }).Count
    Write-Host ("   残片剩下 {0} 个 / {1:N2} GB；正式模型文件 {2} 个" -f $left.Count, $ls, $nSolid)
}
Write-Host ''
Write-Host '完成。' -ForegroundColor Green
