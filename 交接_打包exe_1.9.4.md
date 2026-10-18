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
