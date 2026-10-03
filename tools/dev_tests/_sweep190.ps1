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
# ★ 1.9.3（第 3 轮）：**禁止回归启动用户的 ComfyUI** —— 真凶是
#   _test_comfyui_191 第 491 行的 execute(action="draw") 会让
#   comfyui_jobs 的 worker 去 ensure_running() 真的把服务拉起来。
$env:PRTS_NO_COMFY_START = "1"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root
$outDir = Join-Path $root "tools\dev_tests\_sweep_out"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

# ★ 1.9.2: config.json / .env 保镖（开跑前快照）
#   为什么：config.json 是**入库的用户真实配置**，而有些套件会实例化主窗口并触发
#   "保存配置"，把用户配置写成测试形象（实测 character: 铃兰→deepseek）。
#   用户不在线时这等于悄悄改了人家的桌宠。所以跑之前快照、跑完还原，
#   并用 sha256 证明字节一致；单个测试进程的退出还原见 _quiet/sitecustomize.py。
$guardDir  = Join-Path $root "tools\dev_tests\_cfg_guard"
$guardName = @("config.json", ".env")
$env:PRTS_CFG_GUARD = "1"
$guardHash = @{}
if (Test-Path $guardDir) {
    # 上一轮没正常收尾（Ctrl+C / 硬崩）留下的快照 → 先还原，别把污染过的当基线
    Write-Output "★ 发现上一轮遗留的配置快照 → 先还原（上次回归没正常收尾）"
    foreach ($n in $guardName) {
        $g = Join-Path $guardDir $n
        if (Test-Path $g) { Copy-Item $g (Join-Path $root $n) -Force }
    }
    Remove-Item $guardDir -Recurse -Force -ErrorAction SilentlyContinue
}
New-Item -ItemType Directory -Force -Path $guardDir | Out-Null
foreach ($n in $guardName) {
    $src = Join-Path $root $n
    if (Test-Path $src) {
        Copy-Item $src (Join-Path $guardDir $n) -Force
        $guardHash[$n] = (Get-FileHash $src -Algorithm SHA256).Hash
    }
}
Write-Output ("配置保镖已就位: " + ((@($guardName) | Where-Object { $guardHash.ContainsKey($_) } |
    ForEach-Object { "$_=" + $guardHash[$_].Substring(0, 12) }) -join "  "))


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

# ★ 1.9.2: 还原 config.json / .env（并证明跟开跑前字节一致）
foreach ($n in $guardName) {
    $g = Join-Path $guardDir $n
    if (-not (Test-Path $g)) { continue }
    $dst = Join-Path $root $n
    $nowHash = if (Test-Path $dst) { (Get-FileHash $dst -Algorithm SHA256).Hash } else { "" }
    Copy-Item $g $dst -Force
    $backHash = (Get-FileHash $dst -Algorithm SHA256).Hash
    if ($backHash -eq $guardHash[$n]) {
        $note = if ($nowHash -eq $guardHash[$n]) { "跑动期间没被动过" } else { "跑动期间被改过 → 已还原" }
        $summary += "配置保镖: $n OK ($note)"
    } else {
        $summary += "★★ 配置保镖: $n 还原后哈希不一致，请手工检查！"
    }
}
Remove-Item $guardDir -Recurse -Force -ErrorAction SilentlyContinue

$summary | ForEach-Object { Write-Output $_ }
$summary | Out-File (Join-Path $outDir "_summary.txt") -Encoding utf8
