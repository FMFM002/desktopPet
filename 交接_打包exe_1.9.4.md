# 交接：把 1.9.4 打包成 exe（让没有 Python 的用户也能用）

> 写于 2026-10-18。上一阶段（1.9.4 便携 zip）已完成并推上 GitHub。
> 本文只讲**下一件事：B 形态（PyInstaller exe）**。
> 依据全部来自仓库里的实际文件，不是回忆。

---

## 一、现在的状态（别重复做）

| 项 | 状态 |
|---|---|
| 版本 | **1.9.4**（唯一来源 `src/version.py`，有守卫核对） |
| 提交 | `174966c`，分支 `main`，已推到 <https://github.com/FMFM002/desktopPet>（公开） |
| 回归 | **86 套件 = 85 真跑 + 1 既定 SKIP**，PASS **2559**，FAIL **0** |
| A 形态（便携 zip） | ✅ 完成：`dist/PRTS-DesktopPet-1.9.4-portable.zip`，**77.0 MB / 357 文件** |
| B 形态（exe） | ⚠️ **曾产出过 403.2 MB，但从未被真正运行过** ← **本阶段要解决的就是这个** |
| C 形态（安装包） | ❌ 只有 `installer/PRTS.iss`，**从未编译**（本机无 Inno Setup） |

**唯一那个 SKIP**：`_test_ltm_171_live`（长期记忆真机端到端，要真调 LLM 花 token）。
**已于 2026-10-18 手动跑过：3 条用例全对（含"问天气不该记忆"负例），ALL PASS。**

---

## 二、本阶段目标

**让"机器上没装 Python"的用户，双击一个文件就能用。**
也就是 PyInstaller 打成 **onedir**（不是 onefile），把 Python 解释器 + 依赖一起带进去。

---

## 三、★★ 我审出来的三个缺口 —— **开工第一件事就是补它们**

### 缺口 1：`.env.preset` **没被打进 exe**（会导致出厂预设密钥失效）

`PRTS.spec` 的 `DATAS` 清单是（L61-70）：

```
data/桌宠形象 · data/memes · data/assets · data/China-City-List-latest.csv
config.default.json · .env.example · requirements.txt · README.md
```

**没有 `.env.preset`**。

而这个文件是 1.9.4 新增的**出厂预设密钥**（天气 / Tavily / 终末地），
`main.py` 首启会把它复制成 `.env` —— **exe 里没有它，就复制不出来**，
于是用户拿到 exe 会发现**天气/搜索/终末地 全都没配**。

⇒ **必须加进 `DATAS`**，并顺手把 4 份文档也带上（发布说明 / 凭据指南 /
施工说明书 / 评估），与 zip 保持一致。

### 缺口 2：`src.core.account_login` **不在 `HIDDEN` 里**（账号开通页会崩）

`PRTS.spec` 的 `HIDDEN` 只登记了：

```
src.core.models · src.core.first_run · src.ui.first_run_wizard …
```

**没有 `src.core.account_login`**。它是**运行时才 import** 的（设置页里 `from src.core import account_login`），
PyInstaller 静态分析**很可能扫不到** → 冻结后一进「账号开通」页就 `ModuleNotFoundError`。

⇒ 加进 `HIDDEN`。

### 缺口 3：`playwright` / `gppt` 在**冻结环境里是另一回事**（要么明确不支持，要么专门处理）

这两个是"一键开通 B站/P站"用的。在 exe 里：

- **`playwright` 需要浏览器驱动**（它自己的一套 Chromium / 或 `channel="msedge"` 用系统 Edge）。
  冻结后 `playwright` 找不到它的驱动目录，通常会报"没装浏览器"。
  想让它可用，要同时处理 `PLAYWRIGHT_BROWSERS_PATH` 并把驱动目录打进 `DATAS` —— **体积 +150MB 起**。
  ★ 我们代码里用的是 `channel="msedge"`（**系统自带的 Edge**），这条**可能不需要**打驱动；
    但**必须在真机上验证**，不能假设。
- **`gppt`** 会起一个**本地回调服务**并开浏览器；冻结环境下端口/浏览器行为需要实测。

⇒ **建议的取舍（我倾向这个）**：
  1. 先把这两个包**排除**在 exe 之外（`EXCLUDES` 加 `playwright`、`gppt`）；
  2. 让「账号开通」页在 exe 里**明确显示"这个功能需要 Python 环境，请用便携 zip 版"**
     （而不是崩、也不是假报"没装"）；
  3. 等有人真的需要，再单独做一版带驱动的。

  ⚠️ 但这是**产品决定**，要你拍板。**我把它列在这里，不擅自做。**

---

## 四、已经踩过、别再踩的坑（都是真金白银试出来的）

### 坑 1：GENIE 会**交互式问要不要下模型**，把打包刷屏卡死

`src/genie_tts/Resources.py` 发现 `GENIE_DATA_DIR` 没设时，会弹
`Would you like to download it automatically from HuggingFace? (y/N):`
—— 打包时**刷几十行、还会顺手去下 nltk 数据**。

**已在两处修好，别动**：
- `main.py` 模块级（`BASE_DIR` 之后、任何 import 之前）`os.environ.setdefault("GENIE_DATA_DIR", …)`
- `PRTS.spec` **在 `Analysis` 之前**设 `GENIE_DATA_DIR` + `HF_HUB_OFFLINE=1`
  （L50-54；注释写明"分析期绝不许联网拉模型"）

### 坑 2：体积 3776 MB → 820 MB → **403.2 MB**

撑大的是这些（`EXCLUDES` 里已排）：
`easyocr/torch/scipy/sklearn/spacy/transformers/cv2` … 以及
**`sudachidict_core` 207 MB（日语词典）+ `paddle` 195 MB（PaddleOCR）**。

★ 我们只做中/英 TTS（feibi），日韩 G2P 用不上。**别把它们加回来。**

### 坑 3：`console=False` 的单文件 exe **杀软误报是常态**

这是 PyInstaller 的固有现象，不是我们的 bug。
所以 spec 用 **onedir（不是 onefile）**：误报更少、启动更快（L33-34 有注释）。

★ **排查手段**：双击没反应时，把 `console=False` 改成 `True` 重打包，
就能看到早期报错（这条也写进 spec 注释了）。

### 坑 4：**开发沙箱里启动 GUI 进程会挂住** → 所以我从没真正验证过 exe

我试过两次，进程挂住且无产物残留。**所以"exe 坏了"这个结论我从来没下过，
也不该被当成已验证。** 本阶段的核心工作之一就是**补上这个验证**。

---

## 五、施工步骤（建议顺序）

### 第 0 步：先跑一遍现状，别在坏的基础上改

```powershell
python -m compileall -q src tools
& tools\dev_tests\_sweep190.ps1        # 必须 FAIL=0 再往下
python tools\preflight.py              # 看"离发布还差什么"
```

### 第 1 步：补 `PRTS.spec` 的三个缺口

```python
# ① DATAS 里加（与 zip 保持一致）
".env.preset",                       # ★★ 出厂预设密钥（漏了天气/搜索/终末地全废）
"发布说明_1.9.4.txt",
"B站与P站凭据获取指南_1.9.4.md",
"施工说明书_ComfyUI集成_1.9.4.txt",
"评估_画图工具自部署_1.9.4.txt",

# ② HIDDEN 里加
"src.core.account_login",            # 设置页运行时 import，扫不到就会 ModuleNotFoundError

# ③ EXCLUDES 里加（若决定 exe 不支持一键开通）
"playwright", "gppt", "pixivpy3",
```

### 第 2 步：打包

```powershell
pyinstaller PRTS.spec --noconfirm --clean
# 产物在 dist\PRTS-DesktopPet-1.9.4\
```

### 第 3 步：★★★ **在一台"没装 Python"的环境里验证**（本阶段的真正验收）

这一步**沙箱做不了**，必须真机。建议顺序：

1. 把 `dist\PRTS-DesktopPet-1.9.4\` 整个目录拷到一个**干净位置**
   （**不是**仓库目录 —— 否则它会读到仓库里的 `config.json` / `.env`，验证就没意义了）
2. **双击 `PRTS-DesktopPet-1.9.4.exe`**
3. 期望：
   - 弹出**首次配置引导**
   - 填 Key → 「测试一下」能通 → 完成
   - **形象出现、能聊天**
   - **天气能用** ← 这条专门验 `.env.preset`
   - 设置 → **账号开通**页**不崩**（按第三节的取舍，它应显示"需要 zip 版"或可用）

### 第 4 步：回归 + 更新文档

```powershell
python -m compileall -q src tools
& tools\dev_tests\_sweep190.ps1
```
然后更新 `发布说明_1.9.4.txt` 的「已知问题」：**把"exe 从未被真正运行过"
改成实测结论**（成功的样子 / 或具体报错）。

---

## 六、验收清单（做到才算完）

- [ ] `.env.preset` 在 exe 里 → 干净环境下**天气/搜索/终末地免填 KEY 就能用**
- [ ] `src.core.account_login` 能被找到 → **账号开通页不崩**
- [ ] 干净位置双击 exe → **弹出引导 → 填 Key → 能聊天 → 形象出现**
- [ ] `config.json` / `.env` **自动生成在 exe 旁边**（不是仓库里）
- [ ] 缺模型时**优雅降级**（TTS 退回 edge-tts、whisper 不可用但不崩）
- [ ] 杀软误报有说明（README / 发布说明里写清"这是 PyInstaller 固有现象"）
- [ ] 全量回归 **FAIL=0**
- [ ] `发布说明_1.9.4.txt` 的「已知问题」**按实测结果更新**（不许沿用旧话）

---

## 七、明确不要做的事

1. **不要把模型打进 exe**（GENIE 1855MB / whisper 465MB / easyocr 100MB）
   —— 模型走 `tools/model_manager.py` 按需获取，exe 只保证"缺了不崩"。
2. **不要把 `data/cookies/`、`data/account_login/`、`.env`、`logs/`、`data/DBs/` 打进去**
   —— 那是真实凭据与个人数据。发布装配器 `tools/build_release.py` 已有排除表，
     **exe 也要遵守同一套**（别只改 spec 忘了这条）。
3. **不要删 `tools/_vendor/**`**（第三方代码，包括 `bili_pixiv_toolkit.py`）。
4. **不要在没验证的情况下说"exe 能用了"** —— 这是上一阶段唯一没敢下的结论。

---

## 八、参考：这次 1.9.4 学到的教训（对打包同样适用）

1. **"设置里写的"和"实际跑的"不一致，是最坑的一类 bug**
   （TTS 预设写 genie 实际跑 edge、B站 cookie 格式不匹配导致画质只有 480P）。
   ⇒ 打包后**每一条"应该能用"，都要真的点一遍**。
2. **"能力做了"不等于"用户/模型会用"**
   （一键开通做了却没写进工具描述，模型回"做不到"）。
   ⇒ exe 里新增/缺失的能力，要**在界面上说清楚**。
3. **日志标签必须唯一对应一种事件**，否则排查时自己骗自己。
4. **新建的数据目录，`.gitignore` 和发布排除表两处都要加**
   （`data/account_login/` 我漏了 `.gitignore`，差点把浏览器 profile 提交进去）。
5. **批量改动必须逐处看语义**，不能只看"形状对得上"
   （`app_launcher` 那次差点把用户的应用窗口也藏掉）。

---

## 九、一句话总结

> 上一阶段把 **A（便携 zip）** 做成了可以发布、可以公开的状态。
> 这一阶段要做的是 **B（exe）**：先补 `PRTS.spec` 的三个缺口
> （`.env.preset`、`account_login`、`playwright/gppt` 的取舍），
> 然后**在一台没装 Python 的机器上真的双击运行一次** ——
> 那是上一阶段唯一没做到、也唯一不能靠推理补上的事。

---

# 十、★ 本阶段结果（2026-10-18 晚，已执行）

> 结论：**B 形态真的跑起来了**，而且是**可复现地**跑起来（不是"我看了一眼"）。

## 10.1 做了什么

| # | 事情 | 结果 |
|---|---|---|
| 1 | 补 `DATAS`：`.env.preset` + 4 份文档 + `data/tools` | ✅ |
| 2 | 补 `HIDDEN`：`src.core.account_login` | ✅ |
| 3 | `playwright` / `gppt` / `pixivpy3` **排除**，界面**明说 exe 不支持**、按钮置灰 | ✅ |
| 4 | ★ **工具/依赖**：`tools/*.py` 是**按文件路径**加载的 → 必须当**数据**复制进包，且必须进 HIDDEN（否则工具自己的依赖不会被分析） | ✅ 18 个工具全部可用 |
| 5 | ★★ `contents_directory="."` → `_MEIPASS` **就是 exe 目录** → 数据写 exe 旁边（**一行配置**省掉 40 处路径改造） | ✅ |
| 6 | ★★ 输出目录改名 `-exe`：`--noconfirm` **会先删同名目录**，同名会顺手删掉 A 的 77MB 装配 | ✅ |
| 7 | `main.py --selftest` 自检（写 `logs/selftest_*.txt`，退出码 0/3） | ✅ |
| 8 | 首次向导的模型下载在 exe 里改成**进程内**跑（冻结后 `sys.executable` 是 exe 自己） | ✅ |
| 9 | `setuptools` 不许排除（排了 → `pyi_rth_pkgres` 缺 `jaraco` → 窗口版弹模态框卡死） | ✅ 真机踩到并修好 |
| 10 | 窗口版 `sys.stdout/stderr` 是 `None` → 启动最早期兜成 devnull | ✅ |

## 10.2 实测证据（可复现）

```powershell
# 1) 打包
python -m PyInstaller PRTS.spec --noconfirm
#   → dist/PRTS-DesktopPet-1.9.4-exe/  （2238 文件 / 447.5 MB，**没有 _internal/**）

# 2) 拷到干净位置（不是仓库目录！）
robocopy dist\PRTS-DesktopPet-1.9.4-exe 临时文件\_exe_smoke /E

# 3) 自检
临时文件\_exe_smoke\PRTS-DesktopPet-1.9.4.exe --selftest
#   → 退出码 0；logs/selftest_*.txt: ok=true，27 项全过
```

两个场景都跑了：

| 场景 | 结果 |
|---|---|
| **全新目录**（没有任何配置） | ✅ ok=true / 退出码 0；`.env` 从 `.env.preset` 自动生成（**天气/搜索/终末地 KEY 都在**，LLM Key 按设计留空）；`config.json` / `logs/` / `data/` 全在 **exe 旁边** |
| **已配 Key**（把仓库 `.env` 拷进去） | ✅ ok=true / 退出码 0；`session_ready` = true（会话真的建起来了）；日志无 `[MainWindow] ERROR` |

自检看的是这些（都过了）：`base_dir_is_exe_dir` / 15 项随包文件（含 `.env.preset`、
`tools/_vendor/bili_pixiv_toolkit.py`）/ `env_preset_merged` / `logs_dir` /
`characters`（2 个）/ `window_created` / `qt_platform`(windows) / `pet_ready`（形象）/
`tray_ready`（托盘）/ `tools_registered`（**18 个**）/ `session_ready`（有 Key 时）/
`chat_ready`。

## 10.3 ★ 仍然没验证的（不许当成"已完成"）

1. **图形桌面上双击**：沙箱里 `Start-Process` / `tasklist` 会被拒（"拒绝访问"），
   所以只能用命令行等价方式验证（同样的 exe、同样的参数、`DEVNULL` 的标准输入输出）。
   没验过的是"在资源管理器里双击"这一下本身。
2. **真的填 Key 聊一轮**：那要真人交互 + 花 token。
3. **桌宠子进程模式**：沙箱拒绝创建子进程（`WinError 5`）→ 每次都回退到
   **主进程直接渲染**（已验证可用）。正常桌面上应当走子进程，但没在干净机器上验过。
4. **C 安装程序**：本机仍无 Inno Setup，`.iss` 只做了静态校验。

## 10.4 下次要改的话，从这三个地方下手

* 想支持「一键开通」：删 `PRTS.spec` 里 `playwright/gppt/pixivpy3` 那行 exclude，
  把 playwright 的驱动目录加进 `DATAS`（+107MB 起），并把 `account_login`
  的 `is_frozen()` 分支去掉。
* 想再瘦身：`yt_dlp` 的 hook 还拉进了 `google*` / `grpc` / `opentelemetry` / `flask`
  / `pikepdf` / `cryptography`（约几十 MB）—— 这些是**可选能力**的传递依赖，
  要逐个确认没有工具在启动路径上 import 才能排。
* 想改"数据放哪"：只动 `PRTS.spec` 的 `contents_directory` 与 `src/utils/runtime.py`
  两个地方，别去改那 40 处 `Path(__file__).parents[...]`。

---

# 十一、★ 真机反馈后的第二轮（2026-10-19，已执行）

> 用户真机跑第一版 exe，报了两件事：**"用户预设头像没了"**、**"为什么不支持开通功能？"**
> 两条都确认是**打包漏的**，已修好并重新打包。

## 11.1 头像：`PRTS.spec` 的 DATAS 漏了 `data/avatars`

`config.default.json` 里写着 `ui.user_avatar_path = "data/avatars/user_avatar.jpg"`，
而 exe 的 DATAS 只带了 `桌宠形象 / memes / assets / tools` —— **没有 avatars**
→ 文件不存在 → 聊天里显示"文档"占位图。

* **为什么 A 形态没这个问题**：A 是整棵 `data/` 一起装（只按排除表剔掉 DB/凭据等），
  avatars 自然在里面。**只有手写 DATAS 的 exe 会漏**。
* 修：DATAS 加 `data/avatars`、`data/自建工具模板`；`--selftest` 加两项检查；
  `_test_exe_pack_194.py` 加反向守卫（仓库里得有这张图 + 出厂配置得指向它）。

## 11.2 一键开通：把 1.9.4 的"建议方案"反转

第一版按交接文档第三节的**建议**把 `playwright/gppt/pixivpy3` 排除了（省 107MB）。
用户真机一问，说明这个取舍不对 —— 改成**支持**：

| 改动 | 说明 |
|---|---|
| `EXCLUDES` | 三个包不再排除（那行改成注释，留作"精简版"开关） |
| `HIDDEN` | 加 `playwright / playwright.sync_api / playwright.async_api / gppt / pixivpy3`（都是**运行时才 import**） |
| `DATAS` | 加 `playwright/driver`（**node.exe 91MB + cli.js**）—— PyInstaller **没有** playwright 的 hook，驱动是数据，必须显式带 |
| `account_login` | 判据从"**是不是 exe**"改成"**依赖在不在**"：完整版 exe 可用；精简版才降级并说清补救 |
| `settings_window` | 按钮置灰条件改成 `feature_available()`，并写明原因 |
| `--selftest` | 加 `playwright_driver` 检查（用 `compute_driver_executable()` 校验两个文件真在） |

★ 用的是 `channel="msedge"`（**系统 Edge**），所以**不需要** Chromium（再 +150MB）。

## 11.3 体积（用户问"为什么 244MB"）

| | 压缩包 | 解压后 | 里面有什么 |
|---|---|---|---|
| A 便携 zip | **77.0 MB** | 89.5 MB | 只有源码 + `data/`（GIF 66MB）；**Python 与依赖用你机器上已装的** |
| B exe（第一版） | 249.1 MB | 447.5 MB | 自带运行时 + 依赖（不含 playwright） |
| **B exe（现在）** | **285.7 MB** | **551.8 MB** | 再加 playwright 驱动 107MB |

解压后的大头（实测）：PySide6 92 · `data/` 83 · playwright 107 · pymupdf 36 ·
exe 本体 35 · jieba 30 · numpy 26 · PIL 13 · grpc 11.5 · cryptography 9.4 · hf_xet 9 …
**这是"不用装 Python"的必然代价**，不是配置写错。
要更小就砍能力（`PRTS.spec` 里注释 playwright → −107MB；pymupdf → −36MB），
但**别开 UPX**（杀软误报）、**别排 setuptools**（起不来，真踩过）。

## 11.4 这一轮的实测

```
exit=0  ok=true  31 项全过  frozen=true  base_dir == exe 目录
avatars(预设头像) = true
playwright_driver = true  →  <exe目录>\playwright\driver\node.exe + package\cli.js
```

产物：`dist/PRTS-DesktopPet-1.9.4-exe/`（2360 文件 / 551.8 MB）
      + `dist/PRTS-DesktopPet-1.9.4-exe.zip`（285.7 MB）

**仍没验的**（只有人能做的）：图形桌面真双击、真填 Key 聊一轮、
真点一次「一键开通」走完登录（沙箱里起不了浏览器）。

---

# 十二、★ 第三轮：P站一键开通失败（2026-10-19 晚，已修）

## 12.1 真机现象

* **B站成功**：26 个 cookie（含 SESSDATA）→ 说明 playwright + 驱动 + 系统 Edge 在 exe 里**都正常**；
* **P站失败**，并且**闪了一个终端黑窗**、**浏览器根本没出现**（开 VPN 也一样）：

```
Pixiv 登录失败（gppt5:login）: Error: BrowserType.launch: Executable doesn't exist at
D:\Download\...\playwright\driver\package\.local-browsers\chromium-1223\chrome-win64\chrome.exe
Looks like Playwright was just installed or updated.
Please run the following command to download new browsers: playwright install
```

## 12.2 根因（gppt 5.x 的默认行为）

`gppt/browser.py` 的 `fetch_authorization()` 里写死了两行：

```python
install([pw.chromium], with_deps=True)                       # ← 去下它自带的 Chromium
browser = pw.chromium.launch(headless=headless, args=BROWSER_ARGS)
```

也就是说 **gppt 默认用 playwright 自带的 Chromium**（不是系统浏览器）：

1. `install(...)` 会跑 `node cli.js install chromium` 去下 ~150MB ——
   在 exe 里**下不成**（而且 `install_playwright` 用的是裸 `subprocess.run`，
   **没有隐藏窗口** → 这就是那个"闪一下的黑窗"）；
2. 下不来 → `launch()` 找不到 `chrome.exe` → 报上面那条。

**与网络/VPN 无关**（用户开 VPN 也一样），所以之前那句"常见原因：网络需要代理"是**误导**。

## 12.3 修法（不改第三方代码，只接管它的启动参数）

`src/core/account_login.py` 新增 `_login_gppt_with_system_edge()`：

```python
_gb.install = _no_install          # ① 不下 Chromium（顺手消掉黑窗）
_BT.launch  = _launch_edge        # ② 给 launch 补 channel="msedge"（用系统 Edge）
try:    return _gppt_login(headless)
finally: 两处还原                  # ★ 只在这一个调用期间生效
```

于是 **B站与P站用的是同一个系统 Edge**（B站那条本来就是
`launch_persistent_context(channel="msedge")`，已验证）。

另外把报错也**翻成了人话**（`_humanize_pixiv_error`）：
"找不到浏览器 → 确认装了 Edge / 或先用 zip 版"、
"登录没完成 → 代理/验证码/窗口被关"、"超时"。

## 12.4 验证到哪一步（如实）

* **能自动验的**：守卫 `_test_account_login_194.py` 加了 12 条（含**行为自证**：
  调用期间 `install` 确实是 no-op、`launch` 确实是 Edge 版、调用完**两处都还原**）；
* **不能验的**：沙箱**不允许创建进程管道**（`WinError 5`），
  连 `sync_playwright()` 都起不来 → **真弹 Edge 这一步只能你点**。

★ 如果这次点 P站**仍然失败**，请把界面上那三行错误原文发我（现在应该是中文提示了），
  我按新的那三行定位；不要自己反复重装。
