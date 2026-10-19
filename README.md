# 铃兰桌宠（PRTS Desktop Pet）v1.9.4

> **版本号唯一来源是 [`src/version.py`](src/version.py)** —— 本文标题里的数字必须与它一致，
> `tools/dev_tests/_test_version_193.py` 会**自动核对**（不一致就报红），避免再出现
> "README 写 1.8.9、启动日志写 1.5.0"那种互相矛盾的情况。

基于 PySide6 + SQLite + LLM API 的桌面宠物，具备多源数据监控、数据清洗流水线、K-V 长期记忆与用户画像系统。

> 本文档着重说明**数据流细节、数据监控、长期记忆**三大核心机制。
> 逐次改动与踩坑记录见 `1.9.3wsl开发日志.txt`。

---

## 快速开始（新用户看这里）

> ★ 1.9.4 新增。本节目的是**让你从零到能说话**，不必先读完全文。

### 1. 准备环境

| 项 | 要求 |
|---|---|
| 系统 | Windows 10 / 11（前台窗口识别、剪贴板监控、SAPI 都依赖 Windows API） |
| Python | **3.10 ~ 3.12**（开发机实测 3.12.10） |
| 磁盘 | 程序本体约 **90 MB**；模型按需下载（见第 4 节） |

```powershell
pip install -r requirements.txt
```

> ★ `requirements.txt` 在 1.9.4 **重写过**。旧版把 PDF/Word/PPT/剪贴板等依赖
> **只写在注释里**，而 `pip install -r` **不执行注释** —— 结果装完少功能却不报错。
> 现在这些都已真正声明。

### 2. 直接运行

```powershell
python main.py
```

**首次运行会弹出一个「配置引导」**，它会：

1. **自己检查**一遍你的电脑（Python 依赖、模型、目录…），把"已自动发现 N 项"列给你看；
2. **只问你 1 件事：LLM API Key** —— 去 <https://platform.deepseek.com> 申请，粘进去，
   可以点「测试一下」当场验证；测不通也能先存着；
3. 让你**勾选用得上的能力**（ComfyUI / 天气 / 联网搜索 / 代理 / P站 / 终末地 / Ollama）
   —— **没勾的不会写入任何配置**；
4. 告诉你"写了什么、写到哪、在哪能改"。

> · 引导**可以整个跳过**（关掉窗口即可），以后随时在**右键桌宠 → 设置**里配。
> · **已经配好 Key 的老用户不会被弹窗打扰**（判据是"只差 LLM Key 才算首次"）。

### 3. 你的配置写在两个地方

| 文件 | 放什么 | 进版本库吗 |
|---|---|---|
| `.env` | **密钥**（`DEEPSEEK_API_KEY` 等） | ❌ **绝不**（`.gitignore` 已排除） |
| `config.json` | 界面、工具开关、角色等运行配置 | ❌ **绝不**（1.9.4 起已从版本库移除） |

`config.json` **原本不在你的目录里也没关系** —— 首次运行会从随包的
**出厂配置 `config.default.json`** 自动生成一份（它由
`python tools/make_default_config.py` 从代码默认值生成，保证"程序没读文件时用什么"
和"出厂文件写什么"是同一个真理）。

### 4. 模型是**按需获取**的，不是随包发的

发布包**故意不带模型**（否则包会从 ~90 MB 变成 ~2 GB）。以下能力需要额外准备：

| 能力 | 需要什么 | 大小 | 没有它会怎样 |
|---|---|---|---|
| 语音合成（TTS） | `src/genie_tts/assets/genie/` | ~1855 MB | 该功能不可用，其余照常 |
| 语音识别（STT） | `data/whisper/`（whisper.cpp + 模型） | ~465 MB | 同上 |
| 本地 OCR | `data/models/easyocr/` | ~100 MB | 同上 |
| 本地画图 | 外部 ComfyUI 服务 | — | 同上 |

★ **这些都是可选能力：没有它们程序照常跑，只是对应工具不可用** ——
1.9.4 专门为此加了守卫（`_test_standalone_194.py`），确保"缺依赖只是少功能，不是崩"。

### 5. ⚠️ 两个**环境级地雷**（与代码无关，但会让你以为程序坏了）

1. **`site-packages` 里如果有 GBK 编码的 `.pth`，Python 会全线启动失败。**
   Python 的 `site.py` 用 **UTF-8** 读 `.pth`；某些中文项目（如 GPT-SoVITS）
   写入的 `.pth` 是 **GBK** 的 → 报 `UnicodeDecodeError` → **所有 Python 进程都起不来**
   （现象：跑测试时一堆套件"没有输出/没有汇总行"）。
   排查：`python -c "print(1)"` 若直接报错，就去看 `Lib/site-packages/*.pth`。
2. **可编辑安装（editable）的 `.pth` 别误删。**
   例如 `__editable__.jmcomic-*.pth` / `__editable__.genie_tts-*.pth` ——
   它们是"包在哪"的指路文件，删/改名会让对应工具**导不进包**。
   本项目**不依赖**外部安装这两个包（`jmcomic` 缺失时工具会明确提示
   `pip install jmcomic`；TTS 用的是项目内的 `src/genie_tts`），
   但**如果你装过它们**，别把 `.pth` 弄丢。

### 6. 遇到问题先看这两个地方

- `logs/prts_*.log` —— 运行日志（最新那个）
- **右键桌宠 → 设置** —— 绝大多数开关都在这里，改了立即生效、不用重启

---

## 目录

0. [快速开始（新用户看这里）](#快速开始新用户看这里)
1. [总体架构](#总体架构)
2. [数据流详解](#数据流详解)
   - [监控数据流](#监控数据流raw_events)
   - [清洗流水线](#清洗流水线pipeline)
   - [聊天与文件链路](#聊天与文件链路)
   - [长期记忆流](#长期记忆流long_term_memories)
3. [数据监控](#数据监控)
4. [窗口与设置持久化](#窗口与设置持久化)
5. [长期记忆](#长期记忆)
6. [数据库设计](#数据库设计)
7. [配置参考](#配置参考)
8. [发布与打包](#发布与打包)
9. [开发与测试](#开发与测试)

---

## 总体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                          main.py（入口）                          │
│   日志 → 依赖检查 → 预初始化（DB/LTM/Pipeline）→ Qt 主循环          │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│                     MainWindow（src/ui）                          │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────┐      │
│  │ UnifiedMonitor│  │ DataPipeline │  │  ChatSession(LLM)   │      │
│  │ 屏幕键盘音频系统│  │ 定时+定量+积压 │  │  话题管理+工具调用   │      │
│  └──────┬───────┘  └──────┬───────┘  └─────────┬──────────┘      │
│         │                 │                     │                 │
│  ┌──────▼─────────────────▼─────────────────────▼──────────┐     │
│  │              SQLite（data/DBs/，WAL 模式）                │     │
│  │  raw_monitor_data.db │ chat_records.db │ long_term_memories.db│
│  │        （内建 FTS5 全文索引 + 触发器同步）                  │
│  └─────────────────────────────────────────────────────────┘     │
│                                                                  │
│  长期记忆 Tool（K-V + LLM 判断/提取/合并） ── 画像系统 ── 上下文注入 │
└──────────────────────────────────────────────────────────────────┘
```

桌宠形象跑在**独立子进程**里（`src/ui/pet_process.py`，由 `pet_proxy.py` 驱动），
所以主进程做重活时 GIF 不会卡。

关键源码位置：

| 模块 | 文件 |
|---|---|
| 监控采集 | `src/core/monitor.py`、`src/core/input_monitor.py` |
| 清洗流水线 | `src/core/data_pipeline.py` |
| 数据库 | `src/core/database.py` |
| 会话循环 | `src/core/session.py` |
| 聊天窗口 / 文件卡片 | `src/ui/chat_widget.py` |
| 主窗口 / 启动编排 | `src/ui/main_window.py` |
| 设置窗口 | `src/ui/settings_window.py` |
| 长期记忆工具 | `tools/long_term_memory_tool.py` |
| LTM 定期总结 | `tools/ltm_summarizer.py` |
| 用户画像 | `src/core/user_profile.py` |
| 配置 | `config.json`（运行时可经设置窗口热改） |

---

## 数据流详解

整个系统的数据可归纳为**三条主链路**，最终汇入长期记忆并回流到 LLM 上下文：

```
监控采集 ──► raw_events ──► Pipeline清洗 ──► monitoring_events ─┐
                                                                ├─► 长期记忆(K-V) ──► 上下文注入
聊天对话 ──► chat_records ──────────────────────────────────────┘
```

### 监控数据流（raw_events）

1. **采集**：各监控线程（`BaseMonitor` 子类，守护线程）通过 `_emit()` 回调 `UnifiedMonitor._on_monitor_data`。
2. **结构化**：按 source 归一化为 `content` 文本（键盘取文本、截图取哈希、系统取窗口切换/进程摘要），原始 dict 存入 `metadata` JSON 字段。
3. **落库**：`db.raw.add_raw_event()` 写入 `raw_events` 表（内容截断 ≤2000 字符），`write_counter` +1。
4. **容量控制**：每写入 50 条执行一次 `enforce_cap(1000)`，超出上限即删除最旧记录；另有 `cleanup_old_memories()` 清理 30 天前的已清洗记录。
5. **旁路**：数据同时进入内存队列 `deque(maxlen=200)` 供 UI 实时展示；截图额外触发视觉回调（LLM 视觉分析）。

### 清洗流水线（Pipeline）

`DataPipeline`（守护线程，`src/core/data_pipeline.py`）把原始噪声数据压缩为结构化事件。

**触发条件（主循环每 60 秒醒来检查一次）**：

| 触发方式 | 条件 | 默认值 |
|---|---|---|
| 定时 | 距上次清洗 ≥ `summarize_interval_minutes` | 10 分钟 |
| 定量 | 内存计数器 `write_counter` ≥ `summarize_trigger_count` | 100 条（触发后归零） |
| 积压 | 库里 `cleaned=0` 的真实总数 ≥ `summarize_trigger_count` | 100 条 |
| **大力清洗** | 积压 > `bulk_threshold` → 单批改取 `bulk_batch_size` | > 500 → 单批 500 |
| 外部 | `trigger_now()` 手动触发 | — |

> 为什么「积压」和「大力清洗」要单独算：`write_counter` 是**内存计数器、每次重启归零**，
> 而库里可能躺着大量历史 `cleaned=0`。只认"新增 N 条"的话，历史积压只能干等定时触发；
> 只按 100 条一批啃的话，1000 条要跑十几分钟且期间每分钟都在调 LLM。

**单批次流程（顺序固定）**：

```
raw_events(cleaned=0, 默认最多100条 / 大力清洗时500条)
   │
   ▼ ① 取数 _fetch_uncleaned(db, limit)
   │     取最旧的 limit 条；窗口随 limit 缩放
   │     (id_window = max(100, limit)，time_window = 60s × limit/100)
   │     ★ 保底推进：万一仍有行落在窗口外，也【不许丢】——
   │       丢掉的行走不到 mark_cleaned，会一直排在最前面被剔 → 永久卡死。
   │       宁可多送几条给 LLM，也只记一条 warning 后全部保留。
   │
   ▼ ② 机械清洗 _mechanical_clean（纯规则，不调 LLM、不花钱）
   │     · 规则1 浏览器/Electron 子进程噪声
   │       source 前缀匹配 process / system / window（覆盖 process_metadata、
   │       window_metadata）；噪声词用【词边界】正则，避免窗口标题里出现
   │       "Storage 管理" / "audio track" 就被误杀
   │     · 规则2 空内容（<3 字符）
   │     · 规则3 纯文件路径（<200 字符）
   │     · 规则4 剪贴板内容截断至 2000 字符
   │
   ▼ ③ 重复合并 _dedup_events（★ 省 token 的大头）
   │     同 source + 同【归一化内容】合并成一条。
   │     归一化 = 去掉数字与 "持续 Ns"，所以
   │       "[进程] idea64.exe(1进程, 2749MB)…" 与 "(2097MB)…" 视为同一种状态。
   │     合并时保留首次出现的位置与时间戳，把末次时间与次数写进正文：
   │       "活动应用类别: browser, ide（同批 ×44，至 2026-09-15 21:10:00）"
   │     —— 比直接丢弃更有用：既知道"这段时间一直是这个状态"，
   │        又保住了时间跨度，数据也不丢。
   │     实测重复率极高（100 条里 65 条重复），合并后送 LLM 的数据量减少约 65%。
   │
   ▼ ④ 文件关联标注 _enrich_file_paths
   │     截图 → 追加 "[待视觉分析]"，音频 → "[待STT转写]"
   │
   ▼ ⑤ LLM 总结 _llm_summarize_with_retry（temperature=0.3，最多重试3次）
   │     打包事件 → LLM 输出 JSON 结构化事件数组
   │     · max_tokens 跟着输入条数放大
   │       (max(2000, min(bulk_max_tokens, 1200 + 输入条数×12)))
   │       —— 固定 2000 在大力清洗时会被截断成非法 JSON
   │     · JSON 解析失败 → 追加格式纠正指令重试
   │     · 3次全失败 → 标记 cleaned=-1（数据回退，不丢失）
   │
   ▼ ⑥ 校验通过的事件写入 monitoring_events（event/detail/confidence/time_start/end）
   │
   ▼ ⑦ 批量标记 raw_events.cleaned=1
   │
   ▼ ⑧ 喂给长期记忆（monitoring_events.memory_ref=0 的，异步不阻塞）
```

设计原则：**不降级、不纯机械模式**——LLM 不可用时宁可回退数据（`cleaned=-1`）也不写入不可靠的总结。

**退出顺序**：Pipeline 是唯一会在后台调 LLM + 写库的长任务，因此 `_quit` 里
它被**第一个**停且要等它收尾；监控次之；`db.close()` 放在最后。
另有 `_shutting_down` 旗子，退出期间所有落库回调直接 return。
（这些守卫的由来见 `1.9.3wsl开发日志.txt` 的 1.8.6 / 1.8.9。）

### 聊天与文件链路

**话题系统（1.6.0+）**：一个角色一个连续上下文窗口。话题 token 超过 `max_tokens`
（默认 168k）即视为满，归档时由 LLM 生成 `summary`，新话题开启时注入上一话题总结实现"记忆续接"。
`chat_records.memory_processed` 标记该条消息是否已被长期记忆流程处理过。

**用户上传文件（1.8.0+，图片与其它文件同级体验）**：

| 类型 | 处理 | 落库方式 |
|---|---|---|
| 图片 | 视觉识别（`vision.mode`）+ 多模态直传 `image_url` | `![文件名](file:///…)` |
| 纯文本 | 卡片 + 2000 字预览随消息直发（带绝对路径） | `[[FILE:…]]` + 预览 |
| 音频 | 后台转写（STT）→ 结果作为用户消息 | — |
| 视频 | 后台 ffprobe 分析 → 结果作为用户消息 | — |
| PDF / Office | 后台抽取全文（PyMuPDF / python-docx / python-pptx / openpyxl） | `[[FILE:…]]` + 全文 |
| 其它二进制 | 元信息 + 绝对路径发给 LLM，需要时由 `file_ops read` 分段读取 | `[[FILE:…]]` |

**附件持久化标记**：图片与文件的预览/卡片不是"只画在界面上"，而是以两种内联标记
**写进消息正文一起落库**，重启后 `_reload_chat_view` → `_render_message_html`
会按标记重建，所以历史里仍能看到缩略图与文件卡片：

- 图片：markdown `![alt](file:///…)`（1.5.0 起支持渲染）
- 文件：`[[FILE:路径|大小|标签]]`（1.8.4 新增；Windows 路径不允许含 `|`，分隔安全）
- 文件已被删除时渲染成 `[文件已不在: xxx]`，不会露出原始标记

**文件卡片样式（1.8.2 / 1.8.4）**：与图片预览同款行布局 —— 靠发送方、带发送方头像，
一条消息一个头像（QQ 式）。用户上传靠右+用户头像，桌宠发来的靠左+桌宠头像。
尺寸用厘米定义、按屏幕 DPI 换算像素：

```python
ChatWidget.CARD_WIDTH_CM  = 4.5   # 想调大小只动这两行
ChatWidget.CARD_HEIGHT_CM = 1.5
```

> Qt 富文本的坑：`div` 的 `width/height/max-width` **全部被忽略**（卡片会铺满整行），
> 只有 `<table width height>` 真正按像素生效；而且底色必须写在 `<table bgcolor>`，
> 写到 `<td bgcolor>` 只覆盖文字那么高、撑不满固定高度。

### 长期记忆流（long_term_memories）

三条入口汇入同一个 K-V 库：

1. **监控链路**：`monitoring_events.memory_ref=0` 的事件由长期记忆流程取出分析，处理后标记 `memory_ref=1/-1`。
2. **聊天链路**：`_feed_ltm_from_chat()` 把未处理的 `chat_records` 异步喂给 LTM 工具（role=tool 的记录直接标记跳过）。
3. **画像链路**：`ProfileExtractor` 后台线程用 LLM 从对话/监控文本中提取用户特征（最多 5 条/次，置信度 >0.7 的同时写入长期记忆）。

---

## 数据监控

### 监控源与开关

由 `config.json` 的 `monitor` 段控制，设置窗口修改后监控自动重启（`restart_monitor` 信号）：

| 监控源 | 开关 | 触发逻辑 | 说明 |
|---|---|---|---|
| 屏幕 `screen` | `screen_enabled`（默认关） | 定时截图（默认 60s），MD5 哈希去重，仅画面变化时记录 | 最多保留 `screen_max_photos` 张 JPEG，存于 `data/DBs/screenphotos/` |
| 键盘 `keyboard` | `keyboard_enabled`（默认开） | Enter 按下或 30 秒无输入时，把缓冲区文本作为一条事件 | pynput 实现 |
| 剪贴板 `clipboard` | `clipboard_enabled`（默认开） | 剪贴板内容变化 | 内容截断 2000 字符 |
| 窗口 `window_metadata` | `system_enabled` 内含 | 轮询前台窗口切换（含持续时间） | `window_monitor_mode: polling` |
| 进程 `process_metadata` | `system_enabled` 内含 | 快照 diff 进程列表（top5 含内存占用） | `process_monitor_mode: snapshot_diff` |
| 系统 `system` | `system_enabled`（默认开） | 定时（默认 30s）窗口+进程采样 | psutil / win32gui |
| 音频 `audio` | `audio_enabled`（默认关） | 音量超过阈值时记录 | |

鼠标监控已删除（配置项仅为兼容保留）。

### 数据治理（防腐化三道闸）

1. **采集端**：内容一律截断 ≤2000 字符；截图哈希去重；截图文件数量硬上限。
2. **存储端**：`raw_events` 总量上限 `raw_data_cap`（默认 1000 条，每 50 次写入检查一次删除最旧）；30 天以上已清洗数据可被清理。
3. **清洗端**：机械规则过滤噪声 + 重复合并 + 窗口过滤，避免把时间跨度大、内容重复的记录打包给 LLM。

### 全文检索

三张业务表均内建 FTS5 索引（`unicode61 remove_diacritics 2` 分词器）：

- `fts_raw_events` / `fts_monitoring_events` / `fts_chat_records`：触发器自动同步（仅 INSERT/DELETE）。
- `fts_long_term_memories`：应用层手动同步（upsert/delete 时显式维护）。

---

## 窗口与设置持久化

### 启动编排与 Phase5

主窗口分阶段初始化，每步之间让事件循环渲染一帧 GIF：

| 阶段 | 内容 |
|---|---|
| Phase1 | 配置 + 形象子进程 + 基础 UI + 托盘（最先可用） |
| Phase2 | 微步队列：chat 信号接线 → 视觉/总结客户端 → 会话 → 载入历史 → 重画聊天区 |
| Phase3 | TTS + 设置窗口 |
| Phase4 | 后台线程加载监控 / 输入监控 / STT / 画像 / 待机说话 |
| **Phase5** | `_init_finalize()`：应用启动 UI 配置、重定位窗口、启动 Pipeline |

> ⚠️ **Phase5 必须通过信号回到主线程**：`_load_heavy` 跑在 `threading.Thread` 里，
> 在那里调 `QTimer.singleShot(0, …)` 是**永远不会触发**的（子线程没有 Qt 事件循环）。
> 历史上 Phase5 因此从未执行过，导致"改了颜色/头像/窗口边距，重启后全变回默认"。
> 现在用 `_TaskSignalRelay.heavy_ready` 信号回主线程。启动日志里能看到
> `Phase5: 应用启动 UI 配置…` 和 `主窗口初始化完成` 两行 —— 它们就是 Phase5 跑没跑的凭据。

### 线程 → UI 的唯一正确姿势

`_TaskSignalRelay(QObject)` 提供 `system_msg` / `user_msg` / `heavy_ready` 三个信号
（跨线程 Signal 是 queued connection，必然回到主线程）。
音频转写、视频分析、文档抽取、STT 回调、重组件收尾全部走它。
**不要**在 `threading.Thread` 里用 `QTimer.singleShot` 回主线程。

### 会被持久化的东西

| 项 | 写入时机 | 启动恢复 |
|---|---|---|
| `ui.chat_window_width/height` | 鼠标拖拽缩放结束后（防抖 800ms） | Phase5 `_apply_config_to_ui` |
| `ui.sidebar_visible` | 点聊天窗口标题栏的 ◀/▶ 时 | 同上 |
| `ui.sidebar_width` / `opacity` / 6 种颜色 / 聊天头像 | 设置窗口「保存设置」 | 同上 |
| 角色性格（persona/scale/style/emoji/GIF） | 「保存设置」或**退出时** | 载入对应角色 `settings.json` |
| 侧栏宽度、下载路径、工具策略等 | 「保存设置」 | 启动读取 |

**两处容易踩的点**：

- 侧栏是聊天窗口**外面**的独立窗口，聊天窗口宽度**不包含**它。
  所以 `_on_chat_resized` 不许减掉侧栏宽度（UI 空间关系的约定见开发日志里「UI 几何模型」那一节）。
- 退出时 `MainWindow._quit` 会调 `SettingsWindow.flush_all_on_exit()`，
  把"没点过保存"的改动也写回本地；切换角色卡片前也会先 flush 旧角色。

`config.save()` 用进程内锁串行化（设置窗口后台线程与主线程定时器都会写 config.json）。
视觉专用 API Key 走 `.env` 的 `DEEPSEEK_VISION_API_KEY`（与 LLM Key 相同时自动删除该变量）。

---

## 长期记忆

### 存储模型：K-V

`long_term_memories.db` 的 `long_term_memories` 表，**一个 `variable_name` 全库唯一**（如 `user_name`、`preferred_language`）：

| 字段 | 含义 |
|---|---|
| `variable_name` | 记忆键（UNIQUE），同一键更新不新增行 |
| `value` | 记忆值 |
| `category` | 分类：identity / preferences / habits / skills / interests / projects / relationships / goals / environment / other |
| `confidence` | 置信度 0.0–1.0 |
| `source` | 来源：chat / monitoring / manual / llm_extraction |
| `update_count` | 被更新次数（同一键每次 upsert +1） |
| `access_count` | 被注入上下文的次数（衰减反哺依据） |
| `last_accessed` | 最近一次注入上下文时间（用于判定是否过期） |
| `decay_score` | 动态衰减分（summarize 时重算） |

### 写入流水线（LongTermMemoryTool v2.0）

`execute(text)` 的五步流程，**每一步失败都不污染数据库**：

```
文本（≥10字符才处理）
   │
   ▼ Step 1  LLM YES/NO 二分类：是否值得长期记忆？（max_tokens=10, temp=0.1, 重试2次）
   │           LLM 不可用 → 直接放弃，零写入
   ▼ Step 2  LLM 结构化提取：输出 [{variable_name, value, category, confidence}] JSON
   │           解析/校验失败 → 放弃，数据回退
   ▼ Step 3  去重检查（读库，可并行）：
   │           · 精确匹配 variable_name 已存在 → 进入冲突包
   │           · difflib 弱匹配（相似度>0.7）   → 进入冲突包（带上 _new_vn）
   │           · 均无 → 无冲突直写列表
   ▼ Step 4  无冲突 → 持锁串行 upsert 入库
   ▼ Step 5  有冲突 → 打包新旧值交给 LLM 合并决策（keep_old/keep_new/replace/merge）
               合并结果按决策写回；LLM 不可用 → 放弃整批冲突，保留旧值
```

并发与生命周期控制：

- **线程池**：最多 2 个 worker（`ltm-worker`），`execute_async()` 供后台场景非阻塞调用。
- **写锁**：`_db_mutex` 保证 SQLite 写入串行化，读操作不加锁。
- **空闲卸载**：作为 `resident` 策略工具常驻，但闲置后由 ToolManager 回收线程池与 LLM 客户端。
- **禁用静默**：禁用时直接返回提示，不报错、不阻塞聊天主流程。

### 记忆回流（注入上下文）

每次对话构建 prompt 时：

1. `user_profile.get_context_prompt()` → 取置信度最高、最近更新的 15 条记忆，按分类渲染成 `## 用户画像（长期记忆）` 片段注入。
2. `long_term_memory.summarize_recent(24)` → 最近 24 小时更新的记忆摘要注入。
3. 话题归档总结 → 续接上一话题上下文。
4. `ltm_db.get_summary()` 在注入时累计 `access_count` + 更新 `last_accessed`（为衰减提供依据）。

即：**监控 → 事件 → 记忆 → 画像 → 更懂你的对话**，形成完整闭环。

### 定期总结与衰减

`LTMSummarizer`（`tools/ltm_summarizer.py`，守护线程）按**文件大小 / 条目数定量**触发三类操作：

| 任务 | 行为 | 默认阈值 |
|---|---|---|
| 触发检查 | DB 文件 ≥ 1MB **或** 条目 ≥ 200，任一达到即触发 | `file_size_threshold_kb=1024`、`count_threshold=200` |
| 冷却期 | 两次 summarise 最小间隔，避免频繁跑 LLM | `cooldown_minutes=5` |
| 衰减清理 | `last_accessed` ≥ 7 天的条目，每周期扣 0.1 confidence；扣到 ≤0.15 删除 | `decay_idle_days=7`、`decay_rate_per_period=0.1`、`delete_threshold=0.15` |
| 聚类合并 | `difflib` 按 `variable_name` 相似度 ≥0.75 聚类，size≥2 的簇调 LLM 合并为 1 条 | `cluster_similarity=0.75` |

**触发点（双路冗余）**：写后 hook（`execute()` 末尾 `maybe_trigger()`）+ 守护线程每 60 秒兜底巡检。

**手动触发**：

```bash
python tools/run_ltm_summarize.py --dry-run        # 现状报告 + 是否达阈值
python tools/run_ltm_summarize.py --decay-only     # 只跑衰减（不调 LLM）
python tools/run_ltm_summarize.py --cluster-only    # 只跑聚类（不衰减）
python tools/run_ltm_summarize.py                  # 跑完整链路
```

设计原则：**回退而不污染**——任何一步异常都打日志退出，不丢数据、不删条目。

---

## 数据库设计

三个独立 DB 文件位于 `data/DBs/`，均为 WAL 模式 + 独立连接 + 线程锁：

```
data/DBs/
├── raw_monitor_data.db      # raw_events + monitoring_events + FTS×2
├── chat_records.db          # topics + chat_records + FTS
├── long_term_memories.db    # long_term_memories + FTS
├── screenphotos/            # 截图文件（数量上限控制）
└── audio/                   # 音频文件
```

状态标记字段约定（贯穿三条链路的"处理位"）：

| 表.字段 | 0 | 1 | -1 |
|---|---|---|---|
| `raw_events.cleaned` | 未清洗 | 已清洗 | 清洗失败（回退） |
| `monitoring_events.memory_ref` | 未入记忆 | 已处理 | 失败 |
| `chat_records.memory_processed` | 未判断 | 已处理 | 失败 |

排查用只读诊断脚本：

```bash
python tools/dev_tests/_db_probe.py             # 各表行数 / cleaned / memory_ref 分布
python tools/dev_tests/_diag_pipeline_layers.py # 逐层看取数/机械清洗/重复合并各过滤多少
python tools/dev_tests/_diag_integrity.py       # config/.env/角色资源/数据库 完整性自检
```

---

## 配置参考

`config.json` 中与本文相关的关键项：

```jsonc
{
  "monitor": {
    "screen_enabled": false,          // 屏幕截图监控
    "keyboard_enabled": true,         // 键盘监控
    "clipboard_enabled": true,        // 剪贴板监控
    "system_enabled": true,           // 窗口/进程监控
    "summarize_interval_minutes": 10, // Pipeline 定时触发
    "summarize_trigger_count": 100,   // Pipeline 定量触发 / 积压阈值
    "raw_data_cap": 1000              // raw_events 硬上限
  },
  "database": { "enabled": true },
  "pipeline": {
    "enabled": true,                  // 清洗流水线总开关
    "vision_queue_enabled": false,    // 视觉分析队列（预留）
    "stt_queue_enabled": false,       // STT 转写队列（预留）
    "bulk_threshold": 500,            // 积压超过这个数 → 大力清洗
    "bulk_batch_size": 500,           // 大力清洗时单批取多少条原始数据
    "bulk_max_tokens": 4000           // 大力清洗时 LLM 输出 token 上限
  },
  "ltm": {                            // 长期记忆定期总结
    "enabled": true,
    "file_size_threshold_kb": 1024,   // DB ≥1MB 触发
    "count_threshold": 200,           // 条目 ≥200 触发
    "cooldown_minutes": 5,            // 两次 summarize 最小间隔
    "decay_idle_days": 7,             // 多久未访问开始衰减
    "decay_rate_per_period": 0.1,     // 每周期扣多少 confidence
    "delete_threshold": 0.15,         // confidence ≤ 此值删除
    "cluster_similarity": 0.75,       // 聚类相似度阈值
    "run_on_start": false             // 启动时立刻跑一次
  },
  "vision": {
    "mode": "api",                    // api | ollama | ocr
    "multimodal": true                // 图片作为 image_url 直传给对话模型
  },
  "ui": {
    "chat_window_width": 420,         // 鼠标拖拽后会写回
    "chat_window_height": 640,
    "sidebar_width": 120,             // 侧栏是独立窗口，不占聊天窗口宽度
    "sidebar_visible": true,          // 点 ◀/▶ 会写回
    "opacity": 1.0
  },
  "profile": {
    "enabled": true,                  // 用户画像系统
    "auto_extract": true,             // 自动提取画像
    "inject_context": true            // 注入对话上下文
  },
  "enabled_tools": [ "long_term_memory", "...", ],   // LTM 工具需在此启用
  "tool_policies": { "long_term_memory": "resident" } // resident / lazy / disabled
}
```

调优提示：

- **清洗太慢**：调大 `pipeline.bulk_batch_size`（如 1000）或调小 `bulk_threshold`。
- **一次太猛 / LLM 输出被 JSON 截断**：调小 `bulk_batch_size`（如 300）或调大 `bulk_max_tokens`。
- **不想让它在启动时啃历史积压**：把 `pipeline.enabled` 设为 `false`。

---

## 发布与打包

> ★ 1.9.4 新增。三种发布形态**都已内置脚本**（用户拍板的顺序：**先 A，再 B，最后 C**）。

### 发布前自检（**先跑这个**）

```powershell
python tools/preflight.py            # 人类可读
python tools/preflight.py --json     # 给脚本/CI
```

它会**只读地**把发布前该确认的事一次列清：版本一致性、个人信息是否会随包、
独立运行、模型状态、三种形态的产物是否已生成 —— 以及**三项"只有人能做"的确认**
（A 的真机启动演练 / B 的 exe 双击 / C 的安装程序编译），并给出下一步的确切命令。

> ★ 它**刻意把"未验证"和"已验证"分开写**，不把没验过的说成验过了 ——
> `_test_preflight_194.py` 专门盯着这一点（含"每个待办必须带可执行动作"）。

### 三种形态怎么选

| 形态 | 体积（实测） | 用户需要什么 | 说明 |
|---|---|---|---|
| **A 便携 zip** | **76.9 MB** | 自己有 Python 3.10~3.12 | 最省事；解压双击 `启动.bat`，启动器会自己装依赖 |
| **B exe** | **551.8 MB** / 2360 文件（zip **285.7 MB**） | 什么都不用装 | 双击即用（自带 Python 运行时）。**代价**：体积大 + **杀软误报是常态**（PyInstaller 的固有现象，非代码问题） |
| **C 安装程序** | — | 双击安装 | 体验最好（开始菜单/卸载/可选组件）。需要 Inno Setup 编译，公开发布通常还要**代码签名**才不被拦 |

**建议**：目标用户是自己或熟人 → **A 就够**。要发给不懂电脑的人 → B 或 C。

### B（exe）的几个特别之处（1.9.4 打包时才定下来的）

0. **体积 551.8 MB 是"把运行环境一起装进去"的代价**（zip 285.7 MB）。拆开看：
   PySide6 92 MB · `data/` 83 MB（角色 GIF 66 MB）· pymupdf 36 MB · playwright 驱动 107 MB ·
   exe 本体 35 MB · numpy/PIL/jieba/grpc 等约 100 MB。
   **80 MB 的便携版不含这些** —— 它用的是你机器上已经装好的 Python 与依赖。
   想瘦：`PRTS.spec` 里排掉 playwright（−107MB）/ pymupdf（−36MB，少"看 PDF"），
   但**别再动 UPX 与 setuptools**（前者杀软误报、后者直接起不来，都踩过）。

1. **数据就落在 exe 旁边**。`PRTS.spec` 用了 PyInstaller 6 的
   `contents_directory="."`（legacy onedir 布局）→ `sys._MEIPASS` **就是** exe 所在目录。
   于是 `config.json` / `.env` / `logs/` / `data/` 全部生成在 exe 旁边，
   与便携 zip 版行为一致；**不会**写进 `_internal/`（默认布局会，升级时整目录替换
   = 用户聊天记录没了）。代价是 exe 目录里有 2000 多个文件，看着乱。
2. **`--selftest`：双击没反应时先跑它**。exe 是 `console=False`，看不到任何报错；
   这个模式会把"窗口/形象/托盘/工具/`.env.preset` 合并"逐项检查写进
   `logs/selftest_*.txt`，然后自己退出（**退出码 0 = 全过，3 = 有必检项没过**）：
   ```powershell
   .\PRTS-DesktopPet-1.9.4.exe --selftest
   # 报告：logs\selftest_YYYYmmdd_HHMMSS.txt
   ```
   它**跳过**首次配置引导（否则模态框会挡住自检），所以"还没填 LLM Key"不算失败。

> ★ **B 形态支持「一键开通 B站/P站」**（1.9.4 第一版曾为了省 107MB 排除过它）。
> 现在 playwright 的浏览器驱动（`playwright/driver/node.exe` + `cli.js`）**作为数据随包**，
> 用的是**系统自带的 Edge**（`channel="msedge"`），所以**不需要**再下 Chromium。
> ★ P站那条特别处理过：gppt 5.x 默认会**自己下载 Chromium**（npm 版）——exe 里既下不了、
> 还会闪一个终端黑窗。`account_login._login_gppt_with_system_edge()` 把它的
> `install()` 换成 no-op、并给 `BrowserType.launch` 补上 `channel="msedge"`，
> 于是 P站和 B站用的是**同一个系统 Edge**。
> ★ 如果要发"精简版 exe"：把 `PRTS.spec` 里 `# "playwright", "gppt", "pixivpy3",`
> 那行取消注释重打包即可（**约 −107MB**，界面会自动降级成"不可用，用 zip 版"，
> 不会崩）。

### 怎么构建

```powershell
# A：便携 zip（含启动器与「首次使用必读」，顶层带一层目录名，解压不撒桌面）
python tools/build_release.py --zip
#   → dist/PRTS-DesktopPet-1.9.4-portable.zip

# B：PyInstaller exe（onedir，配置见根目录 PRTS.spec）
python -m PyInstaller PRTS.spec --noconfirm
#   → dist/PRTS-DesktopPet-1.9.4-exe/PRTS-DesktopPet-1.9.4.exe
#   ★ 目录名带 -exe：绝不能与 A 的装配目录同名 ——
#     `--noconfirm` 会**先删掉同名输出目录**，同名就等于打一次 exe 顺手删掉便携装配。
#   分发给别人时把这一层目录压成 zip（顶层带目录名，解压不散）：
#     → dist/PRTS-DesktopPet-1.9.4-exe.zip

# C：安装程序（需先装 Inno Setup 6）
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\PRTS.iss
```

> ⚠️ **C 的 `.iss` 在本机没有编译验证过**（开发机没装 Inno Setup），只做了静态校验 ——
> 这点写在脚本注释与 `_test_installer_194.py` 里，别误以为已经验证。

### ★★ 两条打包铁律

1. **绝不把模型打进包**。模型合计约 **2.4 GB**（TTS 1855MB + STT 465MB + OCR 100MB）；
   打进去包会从 77 MB 变成 2 GB 级。它们**按需获取**，见下节。
2. **绝不把用户数据打进包**。`.env`（密钥）、`config.json`、`data/DBs`（聊天记录与长期记忆）、
   `data/cache`、`logs/`、`data/桌宠私有data` 一律排除 —— **发布出去就收不回来**。
   这条由 `_test_release_pack_194.py` 盯着（含反向守卫）。

### 模型集中管理

模型**统一放在 `data/models/<名字>/`**（1.9.4 起；由 `src/core/models.py` 的注册表当唯一真理）：

```
data/models/genie/      TTS（GENIE / GPT-SoVITS）
data/models/whisper/    STT（whisper.cpp）
data/models/easyocr/    OCR
```

```powershell
python tools/model_manager.py status             # 看每个模型在不在、在哪、多大
python tools/model_manager.py status --json      # 给安装程序用
python tools/model_manager.py migrate --dry-run  # 把旧位置的模型搬进 data/models/
python tools/model_manager.py migrate            # 真搬（先看 --dry-run 更稳）
python tools/model_manager.py install --help     # 获取缺失的模型
```

> · **没搬也能跑**：解析器优先规范位置、**找不到就回落旧位置**，所以迁移是渐进的。
> · **缺失不影响启动**：这些是**可选能力**，没有只是对应功能不可用
>   （`_test_standalone_194.py` 专门盯这条）。
> · `data/tools/aria2c.exe` 是**二进制工具**不是模型，由 `tools/fetch_aria2.py` 管，不搬。

---

## 开发与测试

```bash
pip install -r requirements.txt
python main.py
```

LLM API 密钥通过环境变量配置（`.env`）。运行日志与崩溃转储见 `logs/` 目录。
启动时会自动清理所有 `__pycache__`，确保改完代码立即生效。

### 回归测试

`tools/dev_tests/` 下均为**离屏可跑**的回归测试（`QT_QPA_PLATFORM=offscreen`），
直接 `python tools/dev_tests/<name>.py`，退出码 0 表示通过。

| 测试 | 覆盖 |
|---|---|
| `_test_audit_fixes_1810.py` | 全仓 bug 审计修出来的问题（画像提取、跨线程 UI、Phase 顺序、工具回调等） |
| `_test_bg_thread_ui_182.py` | 后台线程回主线程的信号机制（含"QTimer 在后台线程不触发"的根因复现）+ 真实 pptx 端到端 |
| `_test_pipeline_shutdown_186.py` | 退出时 Pipeline 不再开新批次 |
| `_test_pipeline_trigger_187.py` | 定时/定量/积压三种触发 |
| `_test_pipeline_clean_188.py` | 机械清洗白名单 + 重复合并 |
| `_test_bulk_clean_189.py` | 大力清洗阈值/单批大小/不丢行 |
| `_test_phase5_startup_185.py` | Phase5 启动应用 UI 配置 |
| `_test_window_size_184.py` | 窗口宽度不被侧栏减法破坏 |
| `_test_settings_persist_182.py` | 设置 load↔save 往返 + config 并发保存 |
| `_test_attach_persist_184.py` | 附件标记、文件卡片、退出落盘（**只在临时副本上跑，不碰真实数据**） |
| `_test_reasoning_content_183.py` | 思考模式 reasoning_content 补齐 |
| `_test_file_card_182.py` | 卡片尺寸（厘米→像素）与省略 |
| `_test_file_flow_180.py` / `_test_plugins.py` | 文件读写链路 / 工具插件 |
| `_test_ui_160.py` / `_test_session_windows.py` / `_test_topic_bookmarks.py` | UI / 话题窗口 / 侧栏书签 |
| `_test_multimodal.py` / `_test_message_sanitize.py` | 多模态消息组装 / message 清洗 |
| `_test_dataflow_170.py` / `_test_ltm_171.py` / `_test_ltm_summarizer_173.py` | 数据流 / 长期记忆 / 定期总结 |
| `_test_startup_chain.py` / `_test_tools_layout.py` / `_test_pet_proxy.py` / `_test_tts_memory.py` / `_test_endfield.py` | 启动链路 / 工具目录 / 形象代理 / TTS 内存 / 终末地 |

> 写测试时的两条约定：
> 1. **绝不删真实的 `data/桌宠形象`**——历史上有一个测试为了隔离去 `rmtree` 它，
>    而运行中的程序正锁着 GIF，结果删掉了 11 个文件。现在一律在工作区内的临时副本上跑，
>    并把 `CHARACTERS_DIR` 临时指过去。
> 2. 涉及 `config.json` / `.env` 的测试必须自带备份与还原，并在结尾断言"真实数据没被写脏"。
