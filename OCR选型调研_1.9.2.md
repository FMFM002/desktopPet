# OCR 选型调研（1.9.2）

> 起因：用户批注 ——
> "我的结论是，本地ocr低资源占用模型就是一坨。只能识别标准字体。你看看有没有低资源占用+效果客观（可以识别漫画字体）。**没有就算了**。"
>
> 调研方式：**纯网络调研**（未安装/未运行任何东西、未改动任何文件）。
> 除 easyocr 776MB 是**用户实测**外，本文件其余内存数字**均非实测**，
> 已按【官方】/【三方】/【推断】/【未验证】分级标注。

---

## 一、结论

**「常驻 <300MB + 能稳定识别中日文漫画手写/美术/描边字」的通用本地方案 ——
不存在。用户的结论方向正确。**

但精确化两点：

1. **失败点在"识别"，不在"检测"**：漫画文字"找框"低资源完全够用
   （comic-text-detector / PP-OCRv5_mobile_det 仅 4.7MB）；失败的是"读字"。
2. **有一个重要例外**：日文有 `manga-ocr` 这个专用小模型（ONNX 版 ~400MB），
   是"低资源单模型"里唯一真对漫画字体有效的 —— 但**超出 300MB**，
   且**只支持日文**。**中文没有对应物。**

### 决定性证据（三条独立来源）

1. **PP-OCRv5 官方自证精度**（[model_list](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/model_list.md)
   / [PP-OCRv5.en.md](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/algorithm/PP-OCRv5/PP-OCRv5.en.md)）：

   | 评测集 | v5_mobile_rec | v5_server_rec |
   |---|---|---|
   | 印刷中文 | 0.8605 | 0.9013 |
   | 日文 | 0.7577 | 0.7372 |
   | 竖排 | 0.8089 | 0.9314 |
   | **手写中文** | **0.4166** | **0.5807** |
   | **美术字** | **0.5398** | **0.6397** |

   → **连 server 大模型认美术字都只有 6 成**。这是"低资源认不了漫画字"最权威的证据。

2. **三方日文横评**（[zenn 2026-04](https://zenn.dev/piment/articles/53710c5f3ed756)，附可复现 Colab）：

   | 引擎 | 印刷日文招牌 | CPU 耗时 | 草书/艺术化字形 |
   |---|---|---|---|
   | ChromeScreenAI | 1.00 | 0.727s | 0.42 |
   | **RapidOCR** | **0.94** | 1.209s | **0.16** |
   | NDLOCR-Lite | 0.90 | 2.701s | **0.93** |
   | PaddleOCR | 0.89 | 20.098s | 0.15 |
   | **EasyOCR（我们现在用的）** | **0.80** | 3.017s | 0.13 |
   | Manga OCR | 0.48 | 9.570s | 0.30 |
   | **Tesseract** | **0.24** | 0.803s | 0.07 |

3. **中文社区共识**：汉化工作流（BallonsTranslator / manga-image-translator）
   **无一**用低资源小模型；微调版 `PaddleOCR-VL-For-Manga` 的讨论区标题就是
   "这个模型对于手写体汉字的识别很差"
   （[HF](https://huggingface.co/jzhang533/PaddleOCR-VL-For-Manga/discussions/2)）。

---

## 二、低资源能到什么程度（可量化边界）

| 内存档 | 能做什么 | 配置 |
|---|---|---|
| **~200MB**【推断】 | **只认印刷体/规整字**（中日文印刷、竖排、干净对话框）；美术/手写/描边**不行** | RapidOCR + PP-OCRv5_mobile det 4.7MB + rec 16MB + onnxruntime 5.6MB |
| **~零额外内存**（借系统） | 印刷体（中文不错；日文需装 `ja-JP` 语言包）。**无置信度、不能自知出错** | `winocr` / WinRT OCR；Win11 的 OneOCR（CJK 13MB，但许可与系统限制） |
| **~400–700MB**【推断】 | **日文漫画字可用**（真漫画字形/描边/多字体/竖排/振假名）；**只日文，且会幻觉编造** | `manga-ocr-torchless`（ONNX ~400MB） |
| **~1GB+ / GPU / 联网** | **中日文美术字、手写、描边** —— 唯一能打的档位 | VLM：PaddleOCR-VL / Qwen-VL / **项目已有的 vision API** |

**一句话：低资源只能认印刷体；漫画字必须上大模型或云端 VLM。**

---

## 三、★ 最有价值的一条：easyocr → RapidOCR（纯赚）

我们现在用 easyocr，**实测 776MB 常驻**。而第三方横评里：

- **RapidOCR 印刷日文 0.94 > EasyOCR 0.80**（而且 CPU 更快：1.2s vs 3.0s）
- 权重只要 **21MB**（det 4.7MB + rec 16MB），onnxruntime wheel 仅 **5.6MB**
- RapidOCR 官方明确推荐 onnxruntime 后端为"占用内存更少"的引擎，
  且**只有 ONNX Runtime 没有大图内存泄漏**（OpenVINO / PaddlePaddle 都有）
  （[来源](https://raw.githubusercontent.com/RapidAI/RapidOCRDocs/main/docs/blog/posts/inference_engine/which_inference.md)）
- ⚠️ Paddle 原生运行时被官方自己评价为"wheel 90.1MB、臃肿"，**不要走那条路**

→ 也就是：**现在花 776MB 买到的日文印刷质量，还不如一个 21MB 权重 + 5.6MB 运行时
的方案**。这是本次调研里唯一"又省又更准"的改动。

⚠️ **诚实标注**：RapidOCR 的实际 RSS **未实测**，约 200MB 是从包体积推断的。
落地前必须自己 import 一次打 RSS 验证（这一步我来做，测完再决定）。

---

## 四、日文漫画字：manga-ocr（若你愿意接受 400–700MB）

- [kha-white/manga-ocr](https://github.com/kha-white/manga-ocr)：
  官方支持竖排/横排、振假名、文字压在图上、"wide variety of fonts and font styles"、
  **一次 forward 读完整段多行**（整个气泡一次读完）。
  **局限（官方原话）**：`It probably won't be able to handle handwritten text though.`
  而且会 "dream up"（**幻觉编造合理句子**）。**日文专用，读不了中文。**
- [liksunrice/manga-ocr-torchless](https://github.com/liksunrice/manga-ocr-torchless)：
  ONNX 后端，**模型 ~400MB**（首次运行从 HF 下载），
  原话 `eliminating ~2GB PyTorch`，100% 字符级 parity。
  **自带剪贴板监控 `-b` 与目录监控 `-d`** —— 形态跟桌宠几乎一致。
  内存约 500–700MB【推断】。Apache-2.0。

> 注意：横评里 Manga OCR 在**印刷**招牌上只有 0.48 —— 证明它是**窄域专用**，
> 不能当通用 OCR 用。

---

## 五、已明确"不要碰"的

| 候选 | 原因 |
|---|---|
| **Tesseract 5 中日** | 日文 0.24（全场最差之一）；本机也没装，不必装 |
| **Surya 2** | 650M 参数 VLM，必须挂 vllm/llama.cpp server；官方说定位文档、非漫画 |
| **DocTR** | CRNN 印刷体路线 + 重依赖 |
| **PaddlePaddle 原生运行时** | wheel 90MB、官方称臃肿、有大图内存泄漏 |
| **comic-text-detector 单独用** | **它只检测不识别**，别以为装上就能读字 |
| **comet-ocr** | 没检索到可靠仓库/文档，**不评价**（不编造） |

---

## 六、给本项目的建议（含取舍）

1. **【最优先，纯赚】easyocr → RapidOCR**
   更省（776MB → 推断 ~200MB）**且更准**（日文印刷 0.80 → 0.94）。
   代价：要新增 `onnxruntime` + `rapidocr` 依赖 → **按规矩得先问用户**；
   PP-OCRv5 字典更大，**CPU 单图比 v4 慢**（官方原话）。
2. **补 `winocr` 作为"零额外内存"的印刷体档**（本机没 tesseract，它更值）。
   注意：① 启动时要检测 `AvailableRecognizerLanguages`，缺 `ja-JP`/`zh-Hans`
   要提示装包；② **Windows OCR 不返回置信度**，不能参与"难例分流"判断；
   ③ 中文结果自动插空格需要清洗。
3. **日文漫画场景再考虑 manga-ocr**（400MB ONNX 是唯一低体量下真能读漫画字的选择）。
   取舍：只日文、会幻觉、不能与 RapidOCR 同时常驻 → 建议做成
   **可插拔后端 + 空闲卸载**（复用我们已有的 30 秒空闲卸载那套）。
4. **真·漫画字：本地检测 + 上云识别**
   检测用 comic-text-detector（专为漫画训练）或 PP-OCRv5_mobile_det（4.7MB），
   只把裁出的**气泡**送已有的 vision API（不发整页）→ 费用/延迟降一个量级，
   本地内存保持 200MB 档。⚠️ 缺一个"难例判别器"（判断哪些框要上云），
   **没找到现成开源项目**，阈值需自行调优。
5. **最优架构**（若都做）：
   `RapidOCR/winocr（印刷体） + comic-text-detector（找气泡） + vision API（难例）` 三级分流。

### 一表总结

| 目标 | 现实答案 |
|---|---|
| <300MB 读漫画美术字 | ❌ **不行，别找了**（用户结论成立） |
| <300MB 读印刷体中日文 | ✅ 可行（RapidOCR / winocr），**且比现在更省更准** |
| 读日文漫画字、可接受 400–700MB | ✅ 可行（manga-ocr-torchless），唯一低体量正解 |
| 读中文漫画美术字 | ❌ 本地无解，必须 VLM / 云端 |
| 最优架构 | RapidOCR/winocr + 找气泡 + vision API |

---

## 附：下一步实测清单（三个数一测，选型即定）

1. RapidOCR 冷启动后 RSS
2. manga-ocr-torchless 加载后 RSS
3. 用真实漫画页跑 RapidOCR / winocr / manga-ocr，人工对比**美术字那一档**

> 数据可信度：**唯一实测数字**是用户提供的 easyocr 776MB。
> 其余标注为【官方】/【三方】/【推断】/【未验证】。
> **【未验证】的项**：RapidOCR 实际 RSS、Tesseract 语言包体积、WinRT OCR 常驻内存、
> Chrome Screen AI 体积/内存、comic-text-detector 体积、comet-ocr 是否存在。
