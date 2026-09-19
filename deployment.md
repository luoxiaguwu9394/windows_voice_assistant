# Windows Voice Assistant — 部署指南

**目标环境**：Windows 10/11 x64 (Intel Core Ultra / 灵耀14 Air 推荐)
**项目版本**：0.1.0-dev
**更新时间**：2026-09-19

---

## 📋 部署清单概览

| 阶段 | 预计耗时 | 关键产出 |
|------|----------|----------|
| 1. 系统前置 | 10-20 min | Python、Git、VS Build Tools、OpenVINO 运行时 |
| 2. 外部运行时 | 15-30 min | Ollama、llama.cpp (b6000+)、sherpa-onnx OpenVINO 版 |
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

### 2.2 获取 llama.cpp (b6000+，支持 `-mgf` GBNF 语法)

**⚠️ 关键：必须用支持 `-mgf` 的版本，旧版本不支持约束解码**

```powershell
# 方案 A: 下载预编译 Release (推荐)
# https://github.com/ggml-org/llama.cpp/releases
# 找最新版 (b6000+)，下载 llama-b6000-win64-openvino.zip (含 OpenVINO 支持)

# 创建工具目录并解压到工作区
mkdir -Force .\tools\llama.cpp
Expand-Archive $env:USERPROFILE\Downloads\llama-b6000-win64-openvino.zip .\tools\llama.cpp

# 方案 B: 自行编译 (需 VS Build Tools + CMake)
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp
mkdir build && cd build
cmake .. -DGGML_OPENVINO=ON -DCMAKE_BUILD_TYPE=Release
cmake --build . --config Release -j
# 生成的 llama-server.exe 在 build/bin/Release/

# 验证
llama-server --version
# 应显示版本号且包含 openvino 支持
```

> **环境变量**：将 `llama-server.exe` 所在目录加入系统 PATH，或在启动脚本中使用相对路径。
> **推荐路径**：`.\tools\llama.cpp\` (项目根目录下)

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

### 3.2 创建虚拟环境并安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 升级 pip
python -m pip install --upgrade pip

# 安装项目依赖 (含开发工具)
pip install -e .[dev]

# 验证核心包
python -c "
import sherpa_onnx, numpy, pydantic, yaml, watchdog, structlog, orjson, httpx, sounddevice, scipy, win32api, pyautogui
print('All core packages OK')
"
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
python scripts/download_models.py --tts piper-zh
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

### 终端 1：llama.cpp server (GBNF 约束解码，**必须先启动**）

```powershell
# 进入 llama.cpp 解压目录 (工作区工具目录)
cd .\tools\llama.cpp

# 启动 OpenVINO 加速 (NPU + GPU)
.\llama-server.exe `
  -m ..\..\models\llm\qwen2.5-3b-instruct-q4_k_m.gguf `
  -mgf ..\..\grammar.gbnf `
  --port 8080 `
  -ngl 99 `                    # 卸载尽可能多层到 GPU/NPU (OpenVINO)
  -c 4096 `                    # 上下文长度
  --host 127.0.0.1

# 看到 "HTTP server listening on 127.0.0.1:8080" 即成功
```

> **参数说明**：
> - `-mgf grammar.gbnf`：启用 GBNF 语法约束，**小模型必须开启**
> - `-ngl 99`：将所有层卸载到 OpenVINO (NPU/Arc GPU)
> - `--host 127.0.0.1`：仅本地访问
> 
> **路径说明**：从 `.\tools\llama.cpp` 出发，`..\..\` 回到项目根目录，再进入 `models\llm\` 和 `grammar.gbnf`。
> 请根据实际路径调整 `-m` 和 `-mgf` 的相对/绝对路径。

### 终端 2：Ollama (可选，云端回退/大模型用)

```powershell
ollama serve
```

### 终端 3：主程序 (开发/测试模式)

```powershell
cd C:\path\to\windows_voice_assistant
.\.venv\Scripts\Activate.ps1

# 存根模式 (无需模型，验证流程)
python -m winvoice --stub-audio --test-pipeline

# 完整模式 (需模型已下载、llama-server 已启动)
python -m winvoice
```

---

## 7️⃣ 声纹注册 (首次运行必须)

```powershell
cd C:\path\to\windows_voice_assistant
.\.venv\Scripts\Activate.ps1

# 注册 8 个样本 (每个 3-5 秒，变化音量/距离/内容)
python -m winvoice.enroll --speaker me --samples 8 --duration 4

# 交互过程：
# 🎤 Sample 1/8 - Speak for 4s...
#    Starting in 3... Recording! Done recording.
#    ...
# ✅ Enrollment complete for 'me'
#    Threshold HIGH: 0.623
#    Threshold LOW:  0.450
```

> **提示**：
> - 安静环境，贴近麦克风
> - 内容多样化：数字、指令、闲聊
> - 若 `min_intra < 0.4` 报错，重新录制

---

## 8️⃣ 验证与测试

### 8.1 安装验证脚本

```powershell
python scripts/verify_install.py
# 应输出: ✅ ALL CHECKS PASSED
```

### 8.2 存根管道测试 (无需模型)

```powershell
python scripts/test_stub_pipeline.py
# 应通过所有意图分类测试
```

### 8.3 单元测试

```powershell
pytest tests/unit -v
# 应全绿
```

### 8.4 端到端手动测试

```powershell
# 确保 llama-server 运行中
# 运行主程序
python -m winvoice

# 说唤醒词："assistant" 或 "hey assistant"
# 然后发指令："打开记事本"、"音量调大 20"、"搜索 Python 教程"
# 观察日志输出
```

---

## 9️⃣ 常用运维命令

| 场景 | 命令 |
|------|------|
| 激活虚拟环境 | `.\.venv\Scripts\Activate.ps1` |
| 更新依赖 | `pip install -e .[dev] --upgrade` |
| 重新下载模型 | `python scripts/download_models.py --force --all` |
| 重新注册声纹 | `python -m winvoice.enroll --speaker me --samples 8` |
| 分析 SV 阈值 | `python scripts/analyze_sv_scores.py --log-dir logs/sv_scores` |
| 查看结构化日志 | `Get-Content logs/main.jsonl -Tail 20` |
| 运行类型检查 | `mypy winvoice` |
| 代码格式化 | `ruff check --fix winvoice` |

---

## 🔟 故障排查速查

| 现象 | 排查步骤 |
|------|----------|
| `ModuleNotFoundError: sherpa_onnx` | 1) 确认安装 OpenVINO 版 2) dll 在 PATH 中 3) `pip install sherpa-onnx==1.12.3` |
| `llama-server: unrecognized option '-mgf'` | llama.cpp 版本太旧，升级到 b6000+ |
| `sounddevice.PortAudioError` | 检查麦克风权限/驱动，或用 `--stub-audio` |
| `Config value unresolved: ${REMOTE_API_KEY}` | 设置环境变量或关闭 `llm.remote.enabled` |
| `min_intra < 0.4` 注册失败 | 换安静环境、贴近麦克风、重录 8 遍 |
| `numpy` 版本冲突 | `pip install "numpy<2"` 或确认所有依赖支持 numpy 2.x |
| OpenVINO 报错缺 DLL | 更新 Intel 显卡/NPU 驱动，重装 `pip install openvino==2024.6.0` |
| 意图分类总是 UNKNOWN | 1) 确认 llama-server 跑通 2) 检查 GBNF 语法 3) 降低 confidence_threshold |

---

## 📁 关键路径速查

```
项目根目录/
├── config/config.yaml          # 主配置
├── grammar.gbnf                # GBNF 语法 (llama-server 必需)
├── models/
│   ├── kws/zipformer-zh-en/
│   ├── vad/silero_vad.onnx
│   ├── asr/sense-voice/
│   ├── tts/piper-zh/
│   ├── sv/campplus.onnx
│   └── llm/qwen2.5-3b-instruct-q4_k_m.gguf
├── logs/                       # 结构化日志 (每日轮转)
├── snapshots/                  # 破坏性操作快照
├── scripts/
│   ├── download_models.py
│   ├── verify_install.py
│   └── test_stub_pipeline.py
└── winvoice/                   # 源码包
```

---

## 🎉 部署完成标志

- [ ] `python scripts/verify_install.py` → ✅ ALL CHECKS PASSED
- [ ] `llama-server` 在 8080 端口监听，加载 GGUF + GBNF 成功
- [ ] `python -m winvoice --stub-audio --test-pipeline` → 流程跑通
- [ ] `python -m winvoice.enroll --speaker me --samples 8` → 阈值生成
- [ ] `python -m winvoice` 启动，说 "assistant" → 唤醒 → 指令执行 → TTS 回复

---

**🎯 完成！** 现在你拥有了一个在灵耀14 Air 上本地运行的、支持声纹验证、工具调用、云端回退的 Windows 语音助手。

> 后续迭代：Roadmap 步骤 1 验证意图分类器准确率 → 步骤 2 音频管道联调 → 步骤 3 PySide6 UI...