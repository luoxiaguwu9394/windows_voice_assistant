# Windows Voice Assistant — 依赖与模型清单

**生成时间**: 2026-09-19
**项目版本**: 0.1.0-dev

---

## 1. 必须自行安装的外部框架/运行时

| 组件 | 版本要求 | 安装方式 | 说明 |
|------|----------|----------|------|
| **Python** | 3.10+ | 官网 / winget / scoop | 推荐 3.11 或 3.12 |
| **Ollama** | 最新 | https://ollama.com/download | 本地 LLM 服务（必选） |
| **llama.cpp** | b4600+ (支持 `-mgf`) | 自行编译或下载 release | 用于 **Constrained Decoding (GBNF)**，**必须**支持 `-mgf` 参数 |
| **Git** | 任意 | winget/scoop/官网 | 克隆仓库、版本控制 |
| **Visual Studio Build Tools** | 2022 | https://visualstudio.microsoft.com/downloads/ | 编译 llama.cpp、安装部分 Python wheel (如 `sounddevice`) 需要 C++ 编译器 |

> ⚠️ **关键**：普通 `ollama serve` **不支持** GBNF 语法约束。必须单独运行 `llama-server`：
> ```bash
> llama-server -m qwen2.5-7b-instruct.gguf -mgf grammar.gbnf --port 8080
> ```
> 配置文件中 `llm.local.base_url` 应指向 `http://localhost:8080/v1`（而非 11434）。

---

## 2. Python 依赖（已在 pyproject.toml / requirements.txt 声明）

### 核心依赖（`pip install -e .` 自动安装）

| 包 | 版本 | 用途 |
|----|------|------|
| `sherpa-onnx` | ≥1.12.0 | KWS/VAD/ASR/SV/TTS 推理引擎 |
| `numpy` | ≥2.0.0 | 数值计算 |
| `pydantic` | ≥2.10.0 | 数据模型、Schema 验证 |
| `pyyaml` | ≥6.0.1 | 配置文件解析 |
| `watchdog` | ≥5.0.0 | 配置文件热重载监听 |
| `structlog` | ≥25.0.0 | 结构化日志 |
| `orjson` | ≥3.10.0 | 高性能 JSON 序列化 |
| `httpx` | ≥0.28.0 | HTTP 客户端（LLM 调用） |
| `tqdm` | ≥4.67.0 | 下载进度条 |
| `requests` | ≥2.32.0 | 模型下载备选 |
| `sounddevice` | ≥0.5.0 | 音频输入/输出流 |
| `scipy` | ≥1.13.0 | 音频处理辅助 |
| `pywin32` | ≥310 | Windows API 调用（音量、媒体键、进程管理） |
| `pyautogui` | ≥1.0.0 | 桌面自动化辅助 |

### 可选依赖（按需安装）

```bash
# UI 界面 (PySide6)
pip install -e .[ui]

# Prometheus 指标推送
pip install -e .[metrics]

# 开发工具 (pytest, mypy, ruff 等)
pip install -e .[dev]
```

---

## 3. 必须下载的模型文件

所有模型由 `scripts/download_models.py` 自动下载到 `models/` 目录（可通过 `--models-dir` 修改）。

> **大小均为实测值**（2026-09 通过 HTTP HEAD 校验）。脚本**不再强制比对文件大小**——上游偶尔
> 重新打包 release 会导致大小变化，硬比对会误报失败。仅在 `sha256` 字段填入真实 64 位哈希时
> 才做完整性校验。

| 类别 | 模型 | 下载后路径 | 实测大小 | 状态 |
|------|------|-----------|----------|------|
| **Wake Word (KWS)** | Zipformer zh-en 3M (2025-12-20) | `models/kws/zipformer-zh-en/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20/` | 32.9 MB | ✅ 已校验 SHA256 |
| **VAD** | Silero VAD v5 | `models/vad/silero/silero_vad_v5.onnx` | 2.3 MB | ✅ |
| **ASR (主)** | SenseVoice int8（中英日韩粤 + ITN） | `models/asr/sense-voice/sherpa-onnx-sense-voice-...-int8-2024-07-17/` | **163 MB** | ✅ |
| **ASR (备选)** | Streaming Zipformer **small** bilingual zh-en | `models/asr/zipformer/sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16/` | 458 MB | ✅ |
| **TTS** | VITS 中文（icefall aishell3） | `models/tts/vits-zh/vits-icefall-zh-aishell3/` | 31.6 MB | ✅ |
| **Speaker Verification** | 3D-Speaker CAM++ zh-cn 16k | `models/sv/campplus/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx` | 28.3 MB | ✅ |
| **LLM (本地)** | Qwen2.5-3B-Instruct Q4_K_M | `models/llm/qwen2.5-3b-instruct/qwen2.5-3b-instruct-q4_k_m.gguf` | ~2.2 GB | ✅ |

### 已修正的失效链接（重要）

之前版本脚本中的以下链接**已失效或被误改**，现均已修正：

| 项目 | 旧（错误） | 新（已实测 200 OK） |
|------|-----------|---------------------|
| KWS | `...zipformer-zh-en-2024-01-01.tar.bz2` (404) | `...zipformer-zh-en-3M-2025-12-20.tar.bz2` |
| VAD | `vad-models/silero_vad.onnx` (404) | `asr-models/silero_vad_v5.onnx` |
| ASR Zipformer | `...bilingual-zh-en-2023-02-16.tar.bz2` (404，缺 `small`) | `...streaming-zipformer-**small**-bilingual-zh-en-2023-02-16.tar.bz2` |
| ASR SenseVoice | 期望 494 MB（脚本猜错） | 实际 **163 MB**——下载本身没问题，是校验值错了 |
| TTS | `rhasspy/piper/.../zh_CN-huayan-medium.onnx` (404) | `tts-models/vits-icefall-zh-aishell3.tar.bz2` |
| Speaker Verification | `modelscope/3D-Speaker/releases/.../campplus.onnx` (404) | `speaker-recongition-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx` |

> 注：`speaker-recongition-models` 是上游 release **标签本身的拼写错误**，属实需照抄。

### 下载命令

```powershell
# 查看清单（含每项说明与是否有 SHA256）
python scripts/download_models.py --list

# 下载全部核心模型（不含 LLM）
python scripts/download_models.py --all

# 下载核心模型 + 推荐 3B LLM
python scripts/download_models.py --all-with-llm

# 按需下载
python scripts/download_models.py --kws        # 唤醒词
python scripts/download_models.py --vad        # VAD
python scripts/download_models.py --asr        # SenseVoice + 流式 Zipformer
python scripts/download_models.py --tts        # 中文 VITS
python scripts/download_models.py --sv         # CAM++ 声纹
python scripts/download_models.py --llm        # Qwen2.5-3B
python scripts/download_models.py --llm-1.5b   # Qwen2.5-1.5B（更快）
```

**脚本行为**：
- 断点续传（`.part` 文件保留，重跑即续传）
- 下载完成后打印 SHA256 → 复制回 `MANIFEST` 的 `sha256` 字段即可启用后续校验
- `.tar.bz2` 自动解压到同级目录
- 重复运行会跳过已存在的文件（`--force` 强制重下）

### 本地 LLM 模型获取 (推荐小模型，适配灵耀14 Air 等轻薄本)

```bash
# 方案 A: 从 Hugging Face 下载量化版 GGUF (推荐)
# ──────────────────────────────────────────────
# Qwen2.5 系列 (中文最强小模型，推荐首选)
huggingface-cli download Qwen/Qwen2.5-1.5B-Instruct-GGUF qwen2.5-1.5b-instruct-q4_k_m.gguf --local-dir ./models/llm  # ~1.2GB，极速
huggingface-cli download Qwen/Qwen2.5-3B-Instruct-GGUF qwen2.5-3b-instruct-q4_k_m.gguf --local-dir ./models/llm    # ~2.2GB，推荐平衡
huggingface-cli download Qwen/Qwen2.5-0.5B-Instruct-GGUF qwen2.5-0.5b-instruct-q4_k_m.gguf --local-dir ./models/llm # ~0.5GB，极小

# Phi-3 系列 (微软，指令遵循强)
huggingface-cli download microsoft/Phi-3-mini-4k-instruct-gguf phi-3-mini-4k-instruct-q4_k_m.gguf --local-dir ./models/llm  # ~2.4GB

# Gemma-2 系列 (Google，多语言好)
huggingface-cli download google/gemma-2-2b-it-gguf gemma-2-2b-it-q4_k_m.gguf --local-dir ./models/llm  # ~1.6GB

# Llama-3.2 系列 (Meta 新架构)
huggingface-cli download meta-llama/Llama-3.2-1B-Instruct-GGUF llama-3.2-1b-instruct-q4_k_m.gguf --local-dir ./models/llm  # ~1.0GB
huggingface-cli download meta-llama/Llama-3.2-3B-Instruct-GGUF llama-3.2-3b-instruct-q4_k_m.gguf --local-dir ./models/llm  # ~2.0GB

# 方案 B: 使用 Ollama 拉取再转换 (不推荐，Ollama 格式不直接兼容 llama.cpp)
# ollama pull qwen2.5:3b-instruct
# 然后需用 llama.cpp 转换工具转 GGUF
```

> **灵耀14 Air 推荐配置**：
> - **首选**：`qwen2.5-3b-instruct-q4_k_m.gguf` (2.2GB) — 中文理解最好，GBNF 约束解码下意图分类准确率高
> - **极速备选**：`qwen2.5-1.5b-instruct-q4_k_m.gguf` (1.2GB) — 推理 <100ms，纯 CPU 跑满 NPU 加速更快
> - **最小**：`qwen2.5-0.5b-instruct` (0.5GB) — 仅做简单意图分类勉强够用
>
> **配置提示**：在 `config/config.yaml` 中设置：
> ```yaml
> llm:
>   local:
>     base_url: "http://localhost:8080/v1"   # llama-server 端口
>     model: "qwen2.5-3b-instruct"           # llama-server 加载时的模型名（不含 -q4_k_m.gguf 后缀）
>     confidence_threshold: 0.65             # 小模型阈值略低，依赖 GBNF 保证格式
> ```

---

## 4. 目录结构约定

```
windows_voice_assistant/
├── models/                                       # 所有模型文件 (gitignore)
│   ├── kws/zipformer-zh-en/
│   │   └── sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20/
│   ├── vad/silero/silero_vad_v5.onnx
│   ├── asr/sense-voice/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/
│   ├── asr/zipformer/sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16/
│   ├── tts/vits-zh/vits-icefall-zh-aishell3/
│   ├── sv/campplus/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx
│   └── llm/qwen2.5-3b-instruct/qwen2.5-3b-instruct-q4_k_m.gguf
├── snapshots/                 # 破坏性操作快照 (自动创建)
├── logs/                      # 结构化日志 (每日轮转、gz 压缩)
├── config/
│   └── config.yaml            # 主配置文件
├── grammar.gbnf               # llama.cpp GBNF 语法文件 (项目根目录)
└── winvoice/                  # 源码包
```

> 该结构已与 `config/config.yaml` 中的 `kws.model` / `vad.model` / `asr.model` /
> `tts.model` / `sv.model` 路径一一对应。

---

## 5. 环境变量

| 变量名 | 必选 | 说明 | 示例 |
|--------|------|------|------|
| `REMOTE_API_KEY` | 否 (仅云端启用时) | OpenAI 兼容 API Key | `sk-xxx...` |
| `WINVOICE_STUB_AUDIO` | 否 | `1`=使用存根音频引擎(无模型开发) | `1` |
| `WINVOICE_CONSOLE_LOG` | 否 | `1`=控制台也输出日志 | `1` |

---

## 6. 首次运行完整步骤

```powershell
# 1. 克隆与环境
git clone https://github.com/luoxiaguwu9394/windows_voice_assistant.git
cd windows_voice_assistant
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]

# 2. 安装外部运行时
#   - 安装 Ollama (https://ollama.com)
#   - 编译/下载 llama.cpp (需支持 -mgf)
#   - 安装 Visual Studio Build Tools (如未安装)

# 3. 下载全部模型（含推荐 3B LLM）
python scripts/download_models.py --all-with-llm

# 4. 启动 llama.cpp server (GBNF 约束解码)
llama-server `
  -m models/llm/qwen2.5-3b-instruct-q4_k_m.gguf `
  -mgf grammar.gbnf --port 8080

# 5. 启动 Ollama (可选，云端/其他模型)
ollama serve

# 6. 自检：加载全部真实模型，确认无误后退出
python -m winvoice --check

# 7. 声纹注册 (首次必须)
python -m winvoice.enroll --speaker me --samples 8

# 8. 运行主程序
python -m winvoice

# 9. 开发/测试模式 (无模型，仅验证流程)
python -m winvoice --stub-audio
```

### 验证命令速查

| 目的 | 命令 | 期望结果 |
|------|------|----------|
| 全量编译检查 | `python -m py_compile (Get-ChildItem -Recurse -Filter *.py \| % FullName)` | 无报错 |
| 引擎冒烟测试（真实模型） | `python scripts/smoke_test_models.py` | `18/18 passed` |
| 测试套件 | `python -m pytest tests/ -q` | `47 passed` |
| 启动自检（真实模型） | `python -m winvoice --check` | `Startup check PASSED` |
| 存根模式启动 | `python -m winvoice --stub-audio` | `engines_ready stub=True` |

---

## 7. 常见问题排查

| 问题 | 可能原因 | 解决 |
|------|----------|------|
| `ModuleNotFoundError: sherpa_onnx` | 未安装或版本不匹配 | `pip install sherpa-onnx==1.13.8` |
| `llama-server: unrecognized option '-mgf'` | llama.cpp 版本太旧 | 重新编译最新 master (b6000+，2026年) |
| `sounddevice.PortAudioError` | 无音频设备/驱动问题 | 检查麦克风/扬声器，或用 `--stub-audio` |
| `Config value unresolved: ${REMOTE_API_KEY}` | 云端启用但未设置环境变量 | `setx REMOTE_API_KEY "sk-..."` 或关闭 `llm.remote.enabled` |
| `min_intra < 0.4` 注册失败 | 录音质量差/环境嘈杂 | 换安静环境、贴近麦克风、重新录制 8 遍 |
| `write_file` 报 "Path not allowed" | 路径不在用户目录下 | 只允许 `C:\Users\<你>\` 下的路径 |
| `numpy` 版本冲突 / `TypeError` | numpy 2.x 不兼容旧代码 | 确保依赖均支持 numpy 2.x，或锁定 `numpy<2` |
| 模型下载 **404** | 上游改了 asset 名或换了 release 标签 | `python scripts/download_models.py --list` 核对；脚本内 URL 已按 2026-09 实测更新 |
| 模型下载后报 **Size mismatch** | **旧版脚本**硬比对文件大小，而上游重打包过 | 已移除大小强校验；只需 SHA256 通过即可 |
| `Missing dependency: requests` | 未装下载依赖 | `pip install requests tqdm`（tqdm 缺失时脚本会自动降级，不报错） |

---

## 8. 版本锁定建议 (生产/复现用)

```txt
# requirements-lock.txt (示例，2026-09 可用版本)
sherpa-onnx==1.13.8
numpy==2.0.1
pydantic==2.10.3
pyyaml==6.0.2
watchdog==5.0.2
structlog==25.1.0
orjson==3.11.0
httpx==0.28.1
tqdm==4.67.1
requests==2.32.4
sounddevice==0.5.0
scipy==1.14.0
pywin32==310
pyautogui==1.0.0
```

生成命令：
```bash
pip freeze > requirements-lock.txt
```

---

*文档结束。请按上述清单准备环境，随后运行 `python -m py_compile` 全量检查或 `pytest tests/unit -v` 验证。*