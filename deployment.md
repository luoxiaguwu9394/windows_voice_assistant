# Windows Voice Assistant — 部署指南

**目标环境**：Windows 10/11 x64 (Intel Core Ultra / 灵耀14 Air 推荐)
**项目版本**：0.1.0-dev
**更新时间**：2026-09-19

---

## 📋 部署清单概览

| 阶段 | 预计耗时 | 关键产出 |
|------|----------|----------|
| 1. 系统前置 | 10-20 min | Python、Git、VS Build Tools、OpenVINO 运行时 |
| 2. 外部运行时 | 15-30 min | Ollama、llama.cpp (**b7376 win-cpu-x64，实测可用**)、sherpa-onnx (pip CPU 版) |
| 3. Python 环境 | 5-10 min | venv、依赖包安装 |
| 4. 模型下载 | 10-60 min | KWS/VAD/ASR/TTS/SV + 小模型 LLM |
| 5. 配置与验证 | 5-10 min | config.yaml、声纹注册、端到端测试 |
| **总计** | **45-130 min** | **可运行的语音助手** |

---

## 1️⃣ 系统前置依赖

### 1.1 安装 Python 3.11+ (推荐 3.12/3.13)

```powershell
# 方案 A: winget (推荐)
winget install Python.Python.3.12

# 方案 B: 官网下载
# https://www.python.org/downloads/windows/

# 验证
python --version
# Python 3.12.x 或 3.13.x
```

### 1.2 安装 Git

```powershell
winget install Git.Git
git --version
```

### 1.3 安装 Visual Studio Build Tools 2022 (编译 llama.cpp / sounddevice 需要)

```powershell
# 最小安装：仅 "C++ 生成工具" + "Windows 10/11 SDK"
winget install Microsoft.VisualStudio.2022.BuildTools --override "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --quiet"

# 或手动下载：https://visualstudio.microsoft.com/downloads/#build-tools-for-visual-studio-2022
```

### 1.4 安装 OpenVINO 运行时 (Intel NPU/Arc GPU 加速必需)

```powershell
# 方案 A: pip 安装 (最简单)
pip install openvino==2024.6.0

# 方案 B: Intel 官网完整安装包 (含驱动、工具)
# https://www.intel.com/content/www/us/en/developer/tools/openvino-toolkit/download.html
# 选择 "Windows" -> "Archive" -> openvino_2024.6.0_windows.zip

# 验证
python -c "import openvino; print(openvino.__version__)"
```

### 1.5 更新显卡/NPU 驱动 (灵耀14 Air 必做)

- Intel 驱动助手：https://www.intel.com/content/www/us/en/support/detect.html
- 或 厂商官网 (ASUS) 下载最新显卡/NPU 驱动
- 重启电脑

---

## 2️⃣ 外部运行时

### 2.1 安装 Ollama (备选云端/大模型)

```powershell
winget install Ollama.Ollama

# 启动服务 (后台常驻)
ollama serve

# 验证
ollama list
```

### 2.2 获取 llama.cpp（**实测可用版本：b7376 CPU 版**）

> ⚠️ **踩坑记录（2026-09-19 实测）**
> 新版 OpenVINO 构建（b11046）**在 Windows 上有 bug**，已回退到旧版 CPU 构建。
> 当前**已验证可用**的是 **b7376 (380b4c984) win-cpu-x64**：
> ```
> version: 7376 (380b4c984)
> built with Clang 19.1.5 for Windows x86_64
> ```
> CPU 后端已启用 `AVX_VNNI` / `AVX2` / `FMA` / `REPACK`，3B Q4_K_M 推理约 1.4–4.0 s。

```powershell
# 下载 llama.cpp Release（选择 win-cpu-x64 构建）
# https://github.com/ggml-org/llama.cpp/releases

# 解压到工作区
mkdir -Force .\tools\llama-b7376-bin-win-cpu-x64
Expand-Archive <下载的zip> .\tools\llama-b7376-bin-win-cpu-x64

# 验证
cd .\tools\llama-b7376-bin-win-cpu-x64
.\llama-server.exe --version
# 期望：version: 7376 (380b4c984)
```

**启动服务（实测命令）：**

```powershell
cd C:\Users\<you>\Desktop\windows_voice_assistant\tools\llama-b7376-bin-win-cpu-x64

.\llama-server.exe `
  -m ..\..\models\llm\qwen2.5-3b-instruct-q4_k_m.gguf `
  --port 8080 `
  -ngl 0 `
  -c 4096
```

成功标志：
```
main: model loaded
main: server is listening on http://127.0.0.1:8080
main: starting the main loop...
srv  update_slots: all slots are idle
```

> **参数说明**
> - `-ngl 0`：全部在 CPU 上跑（CPU 构建无 GPU 后端，必须为 0）
> - `-c 4096`：上下文长度（Qwen2.5 训练时是 32768，4096 足够意图分类）
> - **不需要 `-mgf grammar.gbnf`**：GBNF 语法由客户端**逐请求**通过 `grammar` 字段下发，
>   服务端无需任何语法参数
>
> **验证 LLM 层**（服务启动后另开终端）：
> ```powershell
> cd C:\Users\<you>\Desktop\windows_voice_assistant
> python scripts/check_llm.py
> # 期望：9/9 intents matched
> #       grammar-constrained decoding: ACTIVE (GBNF accepted)
> ```

### 2.3 安装 sherpa-onnx (CPU 版优先，OpenVINO 为进阶选项)

**⚠️ 重要：v1.13.8 没有现成的 Windows OpenVINO 预编译包，建议先用 CPU 版跑通流程**

#### 方案 A：CPU 版（推荐先跑通，灵耀14 Air CPU 完全够用）

```powershell
# 直接 pip 安装，自动下载 win_amd64 wheel (含 CPU 版 onnxruntime)
pip install sherpa-onnx==1.13.8

# 验证
python -c "import sherpa_onnx; print(sherpa_onnx.__version__)"
# 应输出: 1.13.8
```

> **性能参考**：灵耀14 Air Core Ultra 7/9 单核 3.0+ GHz，跑 Zipformer KWS + SenseVoice 实时流式完全无压力。

#### 方案 B：OpenVINO 加速（进阶，需 onnxruntime-openvino）

```powershell
# 1. 安装 OpenVINO 版 ONNX Runtime (替换 CPU 版)
pip install onnxruntime-openvino==1.18.0

# 2. 代码中显式指定 provider (需修改 winvoice/audio/*.py)
# 将 provider="cpu" 改为 provider="OpenVINOExecutionProvider"
# 或设置环境变量: $env:ORT_OPENVINO_PROVIDER=1
```

> **何时需要**：KWS 24/7 后台监听 + 多路并发 + 极致续航时再上。

#### 方案 C：从源码编译原生 OpenVINO 版（最麻烦，不推荐新手）

```bash
# 参考: https://github.com/k2-fsa/sherpa-onnx/blob/master/OPENVINO.md
# 需: OpenVINO 2024.6+ + 自编译带 OpenVINO EP 的 ONNX Runtime + 编译 sherpa-onnx
```

---

### 当前推荐：直接用方案 A

---

## 3️⃣ 项目代码与 Python 环境

### 3.1 克隆仓库

```powershell
git clone https://github.com/luoxiaguwu9394/windows_voice_assistant.git
cd windows_voice_assistant
```

### 3.2 安装依赖

> **不需要 venv**：依赖装在用户级 site-packages，`python` 随处可用。
> 若你确实想用 venv，注意 venv 是隔离的，需要在里面重新装一遍依赖。

```powershell
# 升级 pip
python -m pip install --upgrade pip

# 安装项目依赖 (含开发工具: pytest / mypy / ruff)
pip install -e .[dev]

# 验证核心包
python -c "
import sherpa_onnx, numpy, pydantic, yaml, watchdog, structlog, orjson, httpx, sounddevice, scipy, win32api, pyautogui
print('All core packages OK')
"

# KWS 唤醒词需要把文字转成模型 token，额外需要这两个
pip install sentencepiece pypinyin
```

---

## 4️⃣ 模型下载

### 4.1 下载所有音频模型 (KWS/VAD/ASR/TTS/SV)

```powershell
# 一键下载 (约 600MB，需科学上网)
python scripts/download_models.py --all

# 或分步下载 (可断点续传)
python scripts/download_models.py --kws zipformer-zh-en
python scripts/download_models.py --vad silero
python scripts/download_models.py --asr sense-voice
python scripts/download_models.py --tts
python scripts/download_models.py --sv campplus
```

> **模型位置**：`models/` 目录 (已在 .gitignore)

### 4.2 下载小模型 LLM (推荐 qwen2.5-3b)

```powershell
# 推荐：3B 模型 (~2.2GB，平衡速度/质量)
python scripts/download_models.py --llm

# 极速备选：1.5B 模型 (~1.2GB)
python scripts/download_models.py --llm-1.5b

# 最小：0.5B 模型 (~0.5GB)
python scripts/download_models.py --llm-0.5b

# 查看已下载模型
ls models/llm/
```

> **文件名 vs 配置名**：
> - 下载文件：`qwen2.5-3b-instruct-q4_k_m.gguf`
> - 配置名：`qwen2.5-3b-instruct` (不含 `-q4_k_m.gguf` 后缀)

### 4.3 验证模型完整性

```powershell
python scripts/download_models.py --list
# 应显示所有模型及大小
```

---

## 5️⃣ 配置文件

### 5.1 检查/修改 config.yaml

```powershell
# 查看当前配置
type config\config.yaml
```

**关键检查项** (灵耀14 Air 推荐值)：

```yaml
llm:
  local:
    base_url: "http://localhost:8080/v1"    # llama-server 端口
    model: "qwen2.5-3b-instruct"            # 必须与下载的模型名一致
    confidence_threshold: 0.65              # 小模型阈值略低

audio:
  input_device: "default"                   # 或指定设备名
  sample_rate: 16000
```

### 5.2 设置环境变量 (仅云端启用时需要)

```powershell
# PowerShell 永久设置
setx REMOTE_API_KEY "sk-xxx-your-api-key"

# 当前会话临时设置
$env:REMOTE_API_KEY = "sk-xxx-your-api-key"
```

---

## 6️⃣ 启动服务 (按顺序，每个开一个终端)

### 终端 1：llama.cpp server（**必须先启动**）

```powershell
# 进入 llama.cpp 解压目录（实测可用版本）
cd C:\Users\<you>\Desktop\windows_voice_assistant\tools\llama-b7376-bin-win-cpu-x64

.\llama-server.exe `
  -m ..\..\models\llm\qwen2.5-3b-instruct-q4_k_m.gguf `
  --port 8080 `
  -ngl 0 `
  -c 4096

# 看到下面两行即成功：
#   main: server is listening on http://127.0.0.1:8080
#   main: starting the main loop...
```

> **参数说明**
> - `-ngl 0`：CPU 构建必须为 0（无 GPU 后端）
> - `-c 4096`：上下文长度
> - **不需要 `-mgf grammar.gbnf`**：GBNF 由客户端逐请求下发（`grammar` 字段），
>   服务端加不加语法参数都能约束输出
>
> **相对路径**：从 `tools\llama-b7376-bin-win-cpu-x64` 出发，`..\..\` 回到项目根目录。
> 若换过目录，直接写绝对路径即可。

### 终端 1.5：验证 LLM 层（可选但强烈建议）

```powershell
cd C:\Users\<you>\Desktop\windows_voice_assistant
python scripts/check_llm.py
# 期望：9/9 intents matched
#       grammar-constrained decoding: ACTIVE (GBNF accepted)
```

### 终端 2：Ollama (可选，云端回退/大模型用)

```powershell
ollama serve
```

### 终端 3：主程序

```powershell
# 不需要 venv：依赖装在用户级 site-packages，python 随处可用。
# 但进程工作目录必须是仓库根目录（config.yaml 与 models/ 按 CWD 解析）
cd C:\Users\<you>\Desktop\windows_voice_assistant

# 自检：加载全部真实模型后退出（不需要麦克风）
python -m winvoice --check

# 存根模式（无需模型，验证流程）
python -m winvoice --stub-audio --check

# 完整模式 (需模型已下载、llama-server 已启动)
python -m winvoice
```

---

## 7️⃣ 声纹注册 (首次运行必须)

> 启动日志里 `sv_initialized ... enrolled=[]` 说明**还没注册声纹**。
> 未注册时 `verify()` 返回 `None`，说话人分级不生效（不做拦截），
> 功能可用但**没有声纹保护**。要启用请先注册。

```powershell
cd C:\Users\<you>\Desktop\windows_voice_assistant

# 注册 8 个样本 (每个 3-5 秒，变化音量/距离/内容)
python -m winvoice.enroll --speaker me --samples 8 --duration 4

# 可选参数：
#   --max-inter 0.35   降低「冒充者相似度」估计（见下方故障说明）
#   --min-gap   0.05   要求的最小间隔
#   --force            覆盖已有注册
```

**阈值怎么来的：**

```
T_high = min_intra - offset_high      # score >= T_high -> full  (完整权限)
T_low  = max_inter + offset_low       # score >= T_low  -> guest (访客权限)
                                      # 否则            -> rejected
```

- `min_intra`：你自己 8 个样本两两余弦相似度的**最小值**（实测得出）
- `max_inter`：与**最相似的非目标说话人**的相似度（**只能是估计值**，来自
  AISHELL-3 / CN-Celeb 参考区间，无法从单个注册者身上测得）

> ⚠️ **T_high 必须大于 T_low**。若反过来，分级阶梯会塌陷：
> `score >= T_high` 先判定，于是**任何高于较低门槛的分数都被直接提升为
> full**（可写文件、可执行脚本）。这是安全漏洞，不是显示问题。
> 因此注册时会强制校验，不通过就**拒绝写入**并给出修复方法。

**正常输出：**
```
================================================================
Speaker enrollment
  speaker          : me
  samples          : 8 x 4s
  min speech floor : 0.40  (samples scoring below this are rejected)
  max_inter        : 0.45  (estimated impostor similarity)
  required gap     : 0.05  (T_high must exceed T_low by this)
================================================================
[*] Sample 1/8 - speak for 4s
    (vary wording, volume and distance)
    starting in 3... recording...
    done.
...
[OK] Enrollment complete for 'me'
     samples         : 8
     threshold HIGH  : 0.623   (>= this -> full)
     threshold LOW   : 0.450   (>= this -> guest)
     gap             : 0.173   (T_high - T_low, must be > 0)
     profile saved   : models/sv/profiles/me.json
================================================================
```

### 7.1 注册失败：「cannot separate」（T_high 会低于 T_low）

实测样例：
```
[X] Enrollment failed:
Enrolment cannot separate you from other speakers with these settings.
  min_intra        = 0.524  (your worst self-similarity)
  max_inter        = 0.450  (estimated impostor similarity)
  T_high would be  = 0.474
  T_low  would be  = 0.500
  gap              = -0.026  (need >= 0.05)
```

含义：**你自己的最差自相似度（0.524）离「冒充者估计值」（0.450）太近了**，
两个分数区间重叠，无法划分权限。两种修法：

| 方案 | 命令 | 效果 |
|------|------|------|
| **A. 降低冒充者估计**（`max_inter` 本来只是猜测） | `python -m winvoice.enroll --speaker me --samples 8 --force --max-inter 0.37` | 立即可用，但余量很薄（gap≈0.05），`T_high` 仅 0.474 |
| **B. 重新录制**（推荐） | 安静环境、贴近麦克风、音量稳定，再跑一次不带 `--max-inter` 的注册 | `min_intra` 可达 0.65+，得到健康余量（T_high≈0.60 / T_low≈0.50） |

> `min_intra` 只有 0.524 本身就说明**录音一致性不够**（距离/音量/噪声变化大）。
> CAM++ 在稳定录音上，同一人的自相似度通常在 0.7 以上。
> 方案 A 能让你立刻用上，但保护强度有限 —— 相似嗓音在 0.48 左右就会被判为 full。

### 7.2 注册成功但余量偏薄

若 `gap` 小于 `2 × min_gap`（默认 0.10），会打印：

```
     [!] CAUTION: this margin is thin.
         Your genuine and impostor score ranges are close together,
         so T_high is low and a similar-sounding voice could reach
         the 'full' tier. ...
```

此时功能正常，但建议按方案 B 重新录制以提高保护强度。

### 7.3 坏 profile 会被自动拒绝

若磁盘上已存在**阈值倒置**的 profile（例如早期版本生成的），启动时会：

```
[error] sv_profile_rejected_inverted_thresholds
        action='re-enroll: python -m winvoice.enroll --speaker me --samples 8 --force'
        detail="T_high must exceed T_low; this profile would promote low scores to the 'full' tier"
```

该 profile **不会被加载**（`enrolled=[]`），因此不会误放行；
重新注册即可恢复。

> **其他提示**
> - 内容多样化：数字、指令、闲聊
> - 若 `min_intra < 0.40` 会报错并提示重录
> - 注册后再跑 `python -m winvoice --check`，`enrolled` 应显示 `['me']`

---

## 8️⃣ 验证与测试

### 8.1 启动自检（真实模型，最快）

```powershell
python -m winvoice --check
# 期望: [OK] Startup check PASSED - all engines initialize with the current config
```

### 8.2 引擎冒烟测试（真实模型，含实际推理）

```powershell
python scripts/smoke_test_models.py
# 期望: 18/18 passed
```

### 8.3 LLM 层检查（需 llama-server 运行中）

```powershell
python scripts/check_llm.py
# 期望: 9/9 intents matched
#       grammar-constrained decoding: ACTIVE (GBNF accepted)
```

### 8.4 完整测试套件

```powershell
python -m pytest tests/ -q
# 期望: 55 passed, 1 skipped
```

### 8.5 端到端手动测试

```powershell
# 确保 llama-server 运行中，然后运行主程序
python -m winvoice

# 成功启动后应看到：
#   microphone_open
#   Assistant is listening. Say the wake word (default: 'assistant'). Ctrl+C to stop.
#   pipeline_started

# 说唤醒词："assistant" 或 "hey assistant"
# 然后发指令："打开记事本"、"音量调大 20"、"搜索 Python 教程"
# 观察日志输出
```

> **实测启动日志（2026-09-19）**
> ```
> kws_initialized   keywords=['assistant', 'hey assistant'] threshold=0.25
> vad_initialized   model=...\silero_vad_v5.onnx
> asr_initialized   language=auto mode=sense_voice
> sv_initialized    dim=192 enrolled=[]          <- 尚未注册声纹
> tts_initialized   num_speakers=174 sample_rate=8000
> pipeline_initialized stub=False
> audio_input_started blocksize=1600 sample_rate=16000
> microphone_open
> Assistant is listening. Say the wake word (default: 'assistant'). Ctrl+C to stop.
> pipeline_started
> ```
> `enrolled=[]` 表示**还没注册声纹**：此时 `verify()` 返回 `None`，
> 说话人分级不生效（不做拦截），功能可用但**没有声纹保护**。
> 要启用请执行第 7 节的注册命令

---

## 9️⃣ 常用运维命令

| 场景 | 命令 |
|------|------|
| 启动自检（真实模型） | `python -m winvoice --check` |
| 引擎冒烟测试 | `python scripts/smoke_test_models.py` |
| LLM 层检查 | `python scripts/check_llm.py` |
| 从任意目录运行 | `& <repo>\run.ps1 --check` |
| 更新依赖 | `pip install -e .[dev] --upgrade` |
| 重新下载模型 | `python scripts/download_models.py --force --all` |
| 重新注册声纹 | `python -m winvoice.enroll --speaker me --samples 8 --force` |
| 查看可用模型 | `python scripts/download_models.py --list` |
| 运行类型检查 | `mypy winvoice` |
| 代码格式化 | `ruff check --fix winvoice` |

---

## 🔟 故障排查速查

| 现象 | 排查步骤 |
|------|----------|
| `ModuleNotFoundError: sherpa_onnx` | `pip install sherpa-onnx==1.13.8` |
| `ModuleNotFoundError: sentencepiece` / `pypinyin` | KWS 唤醒词转 token 需要：`pip install sentencepiece pypinyin` |
| `400 Failed to parse grammar` | GBNF 语法不被该 llama.cpp 版本接受。本项目已改为**自动回退**到 `json_object` 模式，日志会打印 `local_llm_grammar_rejected`；仍想用 GBNF 请换用实测版本（见 2.2） |
| `llama-server: unrecognized option '-mgf'` | 该版本无此参数。**改用客户端下发语法**（本项目默认方式），或换用 2.2 节的实测版本 |
| llama-server 连不上（`health_check` 失败） | 1) 确认服务在 `http://127.0.0.1:8080` 2) 确认 `config.yaml` 的 `llm.local.base_url` 为 `http://localhost:8080/v1` |
| `sounddevice.PortAudioError` | 检查麦克风权限/驱动，或用 `--stub-audio` |
| `Unresolved environment variable at 'llm.remote.api_key'` | 设置 `setx REMOTE_API_KEY "sk-..."`，或把 `llm.remote.enabled` 设为 `false`（默认已是 false） |
| `min_intra < 0.4` 注册失败 | 换安静环境、贴近麦克风、重录 8 遍 |
| `numpy` 版本冲突 | `pip install "numpy<2"` 或确认所有依赖支持 numpy 2.x |
| 意图分类总是 UNKNOWN | 1) `python scripts/check_llm.py` 确认 LLM 层 2) 检查 `llm.local.model` 是否与 llama-server 加载的一致 |
| TTS 只有 8 kHz 音质 | icefall aishell3 原生即为 8 kHz，属正常现象 |

---

## 📁 关键路径速查

```
项目根目录/
├── config/config.yaml                       # 主配置
├── grammar.gbnf                             # GBNF 语法（客户端逐请求下发，非必需）
├── run.ps1                                  # 从任意目录启动
├── models/
│   ├── kws/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20/
│   ├── vad/silero_vad_v5.onnx
│   ├── asr/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/
│   ├── tts/vits-icefall-zh-aishell3/
│   ├── sv/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx
│   ├── sv/profiles/me.json                  # 声纹档案（注册后生成）
│   └── llm/qwen2.5-3b-instruct-q4_k_m.gguf
├── tools/                                   # 本地解压的 llama.cpp（gitignore）
├── logs/                                    # 结构化日志（每日轮转）
├── snapshots/                               # 破坏性操作快照
├── scripts/
│   ├── download_models.py                   # 下载模型
│   ├── smoke_test_models.py                 # 引擎冒烟测试（真实模型）
│   ├── check_llm.py                         # LLM 层检查
│   └── verify_install.py                    # 依赖检查
└── winvoice/                                # 源码包
```

---

## 🎉 部署完成标志

- [ ] `python -m winvoice --check` → `Startup check PASSED`（5 个引擎全部加载）
- [ ] `python scripts/smoke_test_models.py` → `18/18 passed`
- [ ] `python -m pytest tests/ -q` → `55 passed, 1 skipped`
- [ ] `llama-server` 在 8080 端口监听，`main: server is listening on http://127.0.0.1:8080`
- [ ] `python scripts/check_llm.py` → `9/9 intents matched`，`grammar-constrained decoding: ACTIVE`
- [ ] `python -m winvoice` 启动 → `microphone_open` + `pipeline_started`
- [ ] `python -m winvoice.enroll --speaker me --samples 8` → 生成 `models/sv/profiles/me.json`，`--check` 显示 `enrolled=['me']`
- [ ] 说 "assistant" → 唤醒 → 指令执行 → TTS 回复

---

**🎯 完成！** 现在你拥有了一个在灵耀14 Air 上本地运行的、支持声纹验证、工具调用、云端回退的 Windows 语音助手。

> 后续迭代：Roadmap 步骤 1 验证意图分类器准确率 → 步骤 2 音频管道联调 → 步骤 3 PySide6 UI...