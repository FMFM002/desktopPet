# 全量回归：逐个套件独立进程 + 输出重定向（避免退出阶段挂住时污染控制台）
# ⚠ 本文件必须存成 UTF-8 **带 BOM**：PowerShell 5.1 按 GBK 解析无 BOM 的中文注释时
#   会吃掉换行，`param()` 被并进注释 → 参数（如 -Only）静默失效（踩过一次）。
#   用别的编辑器改过它之后，记得补回 BOM。
# 用法（注意：内层 shell 是 Windows PowerShell 5.1，`pwsh` 不在 PATH，
#       所以要用 `&` 调用本脚本，别写 `pwsh -File ...`）：
#   & tools\dev_tests\_sweep190.ps1                 # 全部 30 个
#   & tools\dev_tests\_sweep190.ps1 -Only _test_a190   # 只跑一个
param([string]$Only = "")
$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"
# ★ 1.9.0: 给所有子进程注入"崩溃不弹模态框"的 sitecustomize。
#   不注入的话，某个套件在 Qt 退出阶段踩访问违例会弹出
#   "python.exe - 应用程序错误"，既把回归挂住、又看起来像桌宠崩了。
$env:PYTHONPATH = (Join-Path $PSScriptRoot "_quiet") + ";" + $env:PYTHONPATH
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root
$outDir = Join-Path $root "tools\dev_tests\_sweep_out"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

$summary = @()
foreach ($f in (Get-ChildItem (Join-Path $root "tools\dev_tests") -Filter "_test_*.py" | Sort-Object Name)) {
    $name = $f.BaseName
    if ($name -eq "_test_ltm_171_live") { $summary += "$name : SKIP(真机 API)"; continue }
    if ($Only -and $name -ne $Only) { continue }
    $out = Join-Path $outDir "$name.out.txt"
    $err = Join-Path $outDir "$name.err.txt"
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $p = Start-Process -FilePath "python" -ArgumentList "-u", $f.FullName `
         -WorkingDirectory $root -NoNewWindow -PassThru `
         -RedirectStandardOutput $out -RedirectStandardError $err
    $ok = $p.WaitForExit(180000)
    $sw.Stop()
    if (-not $ok) {
        try { $p.Kill() } catch {}
        $summary += "$name : TIMEOUT(180s)"
        continue
    }
    # Windows PowerShell 5.1 下 WaitForExit(ms) 不回填 ExitCode，补一次无参等待
    try { $p.WaitForExit(); $p.Refresh() } catch {}
    $code = $p.ExitCode
    $tail = ""
    $flag = ""
    if (Test-Path $out) {
        $lines = Get-Content $out -Encoding utf8 -ErrorAction SilentlyContinue
        # ★ 接管修正（2026-09-17 21:45）：上一位把计数改成 `^\s*PASS\s`，但**多数
        #   套件打的是 `  [PASS] 名称`（带方括号）** → 那些套件全被数成 PASS=0，
        #   而它只看 FAIL=0 就以为没事（它的 sweep 摘要里一大片 PASS=0 就是证据）。
        #   现在改成分层取数，谁能给出准确数字就用谁：
        #     ① `N PASS / M FAIL`      —— _test_a190 和多数套件
        #     ② `ALL PASS (n/m)` / `n/m PASS`
        #     ③ 数断言行（`  PASS x` / `  [PASS] x` / `  [T1] PASS [OK]`），
        #        并排除聚合行（含 `PASS /`、`/N PASS`、`PASS=`）
        $joined = ($lines -join "`n")
        $pz = -1; $fz = -1
        # ① `N PASS / M FAIL` —— 但**不能取第一处匹配**：主套件自己就有断言在打印
        #    这类字样，第一处常常是测试正文（我第一版就栽在这，报出 386/1）。
        #    做法：从候选里挑"最像汇总"的那行（含 结果/回归/RESULT/汇总），
        #    没有就取最后一个。
        $cand = @($lines | Where-Object { $_ -match '(\d+)\s*PASS\s*/\s*(\d+)\s*FAIL' })
        if ($cand.Count -gt 0) {
            $kw = @($cand | Where-Object { $_ -match '结果|回归|RESULT|汇总' })
            $best = if ($kw.Count -gt 0) { $kw[-1] } else { $cand[-1] }
            $m = [regex]::Match($best, '(\d+)\s*PASS\s*/\s*(\d+)\s*FAIL')
            $pz = [int]$m.Groups[1].Value; $fz = [int]$m.Groups[2].Value
        }
        if ($pz -lt 0) {
            $m = [regex]::Match($joined, 'ALL PASS\s*\(\s*(\d+)\s*/\s*(\d+)\s*\)')
            if (-not $m.Success) { $m = [regex]::Match($joined, '(\d+)\s*/\s*(\d+)\s*PASS') }
            if ($m.Success) { $pz = [int]$m.Groups[1].Value
                              $fz = [int]$m.Groups[2].Value - [int]$m.Groups[1].Value }
        }
        if ($pz -lt 0) {
            # ② 数断言行：**方括号可选**（`  PASS x` / `  [PASS] x` / `  [T1] PASS [OK]`），
            #    并排除聚合行，免得汇总行被两侧各算一次（原来那个 FAIL=1 假阳性）
            $skip = 'ALL PASS|PASS\s*/|/\s*\d+\s*PASS|PASS='
            $pz = @($lines | Where-Object {
                $_ -match '(^|\s|\[|\])PASS(\s|\[|\]|:|$)' -and $_ -notmatch $skip }).Count
            $fz = @($lines | Where-Object {
                $_ -match '(^|\s|\[|\])FAIL(\s|\[|\]|:|$)' -and $_ -notmatch 'FAIL\s*/|/\s*\d+\s*FAIL|FAIL=' }).Count
            # 只有一句 "ALL PASS"、没有任何明细的套件（_test_ltm_171）：
            # 数字拿不到，但别报成 PASS=0 让人以为没测
            if ($pz -eq 0 -and $joined -match 'ALL PASS') { $pz = "ALL" }
        }
        $tail = "PASS=$pz FAIL=$fz"
        # ★ 1.9.0: 连汇总行都没有 = 套件中途死了（访问违例/被杀）。
        #   sitecustomize 已经让 Windows 不再弹模态框，崩掉的进程会直接消失，
        #   靠这条就能认出是哪个套件（_test_pet_proxy 本来就不打印 PASS/FAIL，白名单放行）。
        $hasSummary = ($lines | Where-Object { $_ -match "PASS /|结果:|ALL PASS|失败:|failed|\d+\s*/\s*\d+\s*PASS" }).Count -gt 0
        if (-not $hasSummary -and $name -ne "_test_pet_proxy") {
            $flag = "   ★没有汇总行（疑似中途崩溃/被杀）"
        }
    }
    $summary += "$name : $tail ($([int]$sw.Elapsed.TotalSeconds)s)$flag"
}

$summary | ForEach-Object { Write-Output $_ }
$summary | Out-File (Join-Path $outDir "_summary.txt") -Encoding utf8
