# Windows Voice Assistant — 部署指南

**目标环境**：Windows 10/11 x64 (Intel Core Ultra / 灵耀14 Air 推荐)
**项目版本**：0.1.0-dev
**更新时间**：2026-09-27

---

## 0️⃣ 推荐路线：安装向导（一键部署）

**从 Releases 下载 `WinVoice-Setup-<版本>.exe`，双击，跟着向导走完即可**，无需
Python、pip、Git 或编译。向导依次完成：

1. 系统检查 + 按内存推荐本地模型档位（0.5B/1.5B/3B）
2. 安装位置（默认 `%LOCALAPPDATA%\WinVoice`；需要可写目录，勿装 Program Files）
3. 网络（HTTP 代理 / Hugging Face 镜像 `hf-mirror.com`，带连通性测试）
4. 解压内嵌运行时（Python 3.12 + 全部依赖 + llama.cpp b7376，**用户侧零 pip 零编译**）
5. 模型下载（断点续传；跑完自动 `--seal` 生成 `models/integrity.json`）
6. 音频设备选择 + 播放/录音实测（用与助手完全相同的播放路由逻辑）
7. 个性化：默认城市、**唤醒词自定义**（2–12 字，最多 8 个，改词重启即生效）、
   DSH Agent 开关、云端 key（`REMOTE_API_KEY` / `DEEPSEEK_API_KEY` 自动 `setx`）
8. DSH 桥接安装（走捆绑运行时，无需 npm/Node）
9. 声纹注册（可选，可后补；重新注册自动覆盖旧档案）
10. `python -m winvoice --check` 自检 + 桌面/开始菜单快捷方式 + 可选开机自启

**升级**：重新运行安装器 → 识别已装版本 → 升级/修复（模型与配置保留；载荷不含
用户数据，重解压天然安全）。**更新发现**：向导启动时自动比对新 Release 资产。
**唤醒词后补修改**：编辑 `config/config.yaml` 的 `kws.keywords`（每行一条），
重启助手即可——关键词缓存按内容自动重建。

排障（向导路线）：

| 现象 | 处理 |
|---|---|
| 双击 exe 无反应数秒 | onefile 每次启动要解压 ~160MB 载荷，等待即可 |
| SmartScreen 拦截 | 「更多信息」→「仍要运行」（exe 未签名） |
| 杀软报毒 | 未签名 PyInstaller 的常见误报；可加白名单。若误报严重换 onedir+zip 分发 |
| 下载失败/超时 | 网络页配代理或 HF 镜像后重试；下载断点保留，重跑向导接着下 |
| 路径含中文/空格警告 | 一般可用；遇到异常换纯英文无空格路径 |
| 注册声纹没声音 | 检查 设置 → 隐私 → 麦克风 权限 |
| 播放测试音无声、报 `number of channels must match` | **v0.1.5 已知缺陷**（向导探针未随播放立体声化更新，v0.1.6 修复）；升级安装器即可 |
| 声纹注册窗口一闪就退，向导却显示「已注册成功」 | **v0.1.5 已知缺陷**：升级保留的旧档案让注册程序报「already enrolled」退出、又让向导误判成功（v0.1.6 起自动覆盖重录并如实报告）；临时可用手工命令 `python\python.exe -m winvoice.enroll --speaker me --force` |

以下 §1–§8 为**手动部署路线**（开发者 / 需要完全掌控时使用）。

---

## 📋 部署清单概览（手动路线）

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

### 2.4 安装 DeepSeek Harness 桥接（可选：让 DSH 做 Agent 层）

> **默认关闭。** 不装这一节，助手照常运行（规则层 + 12 个工具）。装上之后，
> 规则层没有命中的请求会交给 DSH Agent 来规划、调用工具并组织回答。

DSH 的 Agent 循环跑在 Node 里，它通过 **MCP** 调用本项目的工具
（`winvoice/mcp_server.py`）。工具的实现、白名单、权限分级、快照和验证器全都
不变 —— MCP 只是一条协议，不是第二套工具系统。

**1) 装可选依赖**（约 90 MB，装在仓库内的 `.pylibs/`，不影响全局环境：

```powershell
cd C:\Users\<you>\Desktop\windows_voice_assistant

python -m pip install --target .pylibs --upgrade mcp deepseek-harness-sdk deepseek-harness-runtime-bin
```

> `deepseek-harness-runtime-bin` 是官方自带的 DSH 运行时（~72 MB）。
> 也可以用系统已装的 `dsh`，但 SDK 需要的是可执行文件，而 npm 的 `dsh.cmd`
> 是批处理外壳、不能直接 CreateProcess，所以这里用官方运行时最省事。
>
> 这些包**不放进 `requirements.txt`**：不启用 DSH 的人不该为了说「打开记事本」
> 而装一个 Node 运行时包装器。`winvoice/_vendor.py` 会把 `.pylibs/` 追加到
> `sys.path`（**追加**，不前置 —— 它有自己的一份 pydantic/anyio，覆盖掉主环境
> 的版本会引入比它解决的问题更糟的 bug）。

**2) 安装桥接 bundle**（生成 + 装进本项目的 DSH home）：

```powershell
python scripts/install_dsh_bridge.py --install
# 期望最后两行：
#   [OK] bridge installed. Restart the assistant to pick it up.
#   [OK] mcp-winvoice is present in the composed configuration
```

**3) 打开配置**（`config/config.yaml`）：

```yaml
dsh:
  enabled: true
  local:
    enabled: true
    provider: "deepseek-official"   # 必须是所选 profile 注册过的 provider
    model: "qwen2.5-3b-instruct"    # 与你的本地端点一致
    base_url: "http://localhost:8080/v1"
    api_key: "ollama"               # llama-server 忽略这个值
    request_timeout_s: 90
```

**4) 重启助手。** 启动日志里应出现：

```
dsh_enabled  model=qwen2.5-3b-instruct provider=deepseek-official escalation=False max_local_attempts=2
```

> **为什么需要 bundle，而不能直接改配置？**
> DSH 的插件树由「补丁层」组成，而带 `id` 的补丁条目是**覆盖**已存在的行。
> 所以最直观的写法 `- id: mcp-winvoice / name: ...` 会在启动时报
> `patch: entry "mcp-winvoice" not found`。新增行必须用 `insert:` 包一层，
> 而补丁层本身（含 `--patch`）不能新增行 —— 必须由一个 profile bundle 提供。
> 细节见 `winvoice/dsh/bridge.py` 的注释。
>
> bundle 是**生成**的（不是提交进仓库的），因为它写死了仓库绝对路径和 Python
> 解释器路径；提交一份必然是「只在生成它的那台机器上正确」。

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
# 一键下载 (约 700MB，需科学上网；TTS 一项就占 153MB)
python scripts/download_models.py --all

# 或分步下载 (可断点续传)
python scripts/download_models.py --kws zipformer-zh-en
python scripts/download_models.py --vad silero
python scripts/download_models.py --asr sense-voice
python scripts/download_models.py --tts          # Matcha 22.05k + 声码器 + 8k 回退
python scripts/download_models.py --tts-fallback # 只下 8kHz 的老模型
python scripts/download_models.py --sv campplus
```

> **TTS 现在要下三个文件（共约 153 MB）**：
> `matcha-icefall-zh-baker.tar.bz2`（72 MB，22.05 kHz 声学模型）、
> `vocos-22khz-univ.onnx`（51 MB，**声码器，缺了模型发不出声音**）、
> `vits-icefall-zh-aishell3`（30 MB，8 kHz 回退模型）。
> 只下了老模型也能跑：引擎会回退到 8 kHz 并打一条 `tts_primary_model_missing` 警告。
> 下载中途断线不用重来，重跑同一条命令即从 `.part` 续传
> （GitHub 偶尔会 reset 连接，实测需要多试几次）。

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
    auto_start: true                        # 启动时自动拉起 llama-server（已在跑则复用）
    server_binary: "tools/llama-b7376-bin-win-cpu-x64/llama-server.exe"
    model_file: "models/llm/qwen2.5-3b-instruct-q4_k_m.gguf"
    server_context: 4096                    # llama-server -c
    start_timeout_s: 60                     # 模型加载等待上限（超时不杀进程，只报告未就绪）

audio:
  input_device: "default"                   # 或指定设备名
  sample_rate: 16000

weather:
  enabled: true                             # false → 说「天气查询没有打开。」
  city: "北京"                               # 句子里没说城市时用这个
  timeout_s: 5                              # wttr.in 无需 API key；别调大，等的时候是静音

llm:
  ask:                                      # 问答/闲聊（「什么是量子力学」）
    enabled: true                           # false → 问答明说没打开；打招呼不受影响
    max_chars: 80                           # 答案念出来的上限（再过滤英文/格式）
    timeout_s: 20                           # 超时明说「答不上来」，不静默

tools:
  confirm_timeout_s: 15                     # 写文件/跑脚本的确认等待窗口
  search_web_confirm: true                  # 「搜索 X」先问一句再开浏览器
```

> `weather.*`、`llm.ask.*`、`tools.confirm_timeout_s`、`tools.search_web_confirm`
> 每次调用/挂起时重新读取，改完**不用重启**（引擎类配置只在启动时读一次，
> 其余配置项基本都要重启才生效）。

### 5.2 设置环境变量 (仅云端启用时需要)

```powershell
# PowerShell 永久设置
setx REMOTE_API_KEY "sk-xxx-your-api-key"

# 当前会话临时设置
$env:REMOTE_API_KEY = "sk-xxx-your-api-key"
```

### 5.3 唤醒词与灵敏度（实测调优）

```yaml
kws:
  keywords:
    - "assistant"            # 英文关键词按英语音素匹配
    - "小助手"                # 中文关键词走模型的拼音 token，对中文口音宽容得多
    - "你好助手"
  threshold: 0.25            # 越低越灵敏，代价是误唤醒变多
  use_int8: true             # false = fp32 编码器，更准，CPU 约 2.5 倍
```

- **英文关键词要求按英语音素发音**。若「assistant」念成中文腔（“阿西斯坦特”），
  音素序列和模型里的 `AH0 S IH1 S T AH0 N T` 对不上，就会不灵敏 ——
  换成中文关键词可绕开这个问题。
- **阈值**：先用模型自带参考音频验证过引擎本身正常（英文 2/2、中文 5/7 触发，
  阈值 0.25、100 ms 分块）。若你的唤醒率仍偏低，先 `threshold: 0.20`，
  仍不行再 `use_int8: false`。
- **中文关键词已实测可分词**：
  ```
  AH0 S IH1 S T AH0 N T @ASSISTANT
  x iǎo zh ù sh ǒu @小助手
  n ǐ h ǎo zh ù sh ǒu @你好助手
  ```
- 关键词会编译成 `models/kws/<模型>/winvoice_keywords_<sha1>.txt` 并缓存，
  **模型目录需要可写**。
- sherpa-onnx **不暴露每次命中分数**，所以无法从日志判断「差多少」；
  要量化只能录一段你自己的 wav 离线跑（做法同 §8.2 的冒烟测试思路）。

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
----------------------------------------------------------------
  Each sample shows one line to read. Say it at a normal pace.
  Vary wording, volume and distance between takes.
  Running past the timer mid-sentence is fine - say what you can.
================================================================
[*] Sample 1/8 - speak for 4s
    【数字】一三五七九，二四六八十，今天二十三度。
    starting in 3...
    recording...
    done.
    next up: 【指令】打开记事本，再帮我查一下天气。
[*] Sample 2/8 - speak for 4s
    【指令】打开记事本，再帮我查一下天气。
...
[OK] Enrollment complete for 'me'
     samples         : 8
     threshold HIGH  : 0.623   (>= this -> full)
     threshold LOW   : 0.450   (>= this -> guest)
     gap             : 0.173   (T_high - T_low, must be > 0)
     profile saved   : models/sv/profiles/me.json
================================================================
```

**内置引导词（8 条，数字/指令/闲聊 交替）：**

| # | 类别 | 念这句 |
|---|------|--------|
| 1 | 数字 | 一三五七九，二四六八十，今天二十三度。 |
| 2 | 指令 | 打开记事本，再帮我查一下天气。 |
| 3 | 闲聊 | 你好，今天过得怎么样？ |
| 4 | 数字 | 播放第八首歌，音量调到三十。 |
| 5 | 指令 | 把屏幕亮度调低，然后打开浏览器。 |
| 6 | 闲聊 | 我最近在学语音识别，挺有意思的。 |
| 7 | 数字 | 二零二六年九月十九日，晚上八点四十五。 |
| 8 | 指令 | 设个十分钟闹钟，提醒我喝水。 |

> 类别交替是刻意的：只用命令句录音，建出来的声纹对别的说话风格泛化更差。
> `--samples 12` 这类超出 8 条的情况会**循环复用**引导词，不会出现某条没有提示。
> 每条录完会预告下一句，利用写入 embedding 的空档给你留出阅读时间
> （3 秒倒计时不够读完一句 20 字的中文）。

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
> - 内容多样化：引导词已按数字/指令/闲聊交替（见上表），照着念即可
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
# 期望: 344 passed, 1 skipped
```

> 沙箱/受限权限环境下 `tmp_path` 与家目录写入会被拒，表现为若干
> `PermissionError` 报错（本机正常终端里不会出现）。

### 8.5 端到端手动测试

```powershell
# 确保 llama-server 运行中，然后运行主程序
python -m winvoice

# 成功启动后应看到：
#   microphone_open
#   Assistant is listening. Say the wake word (default: 'assistant'). Ctrl+C to stop.
#   pipeline_started

# 说唤醒词："assistant" / "小助手" / "你好助手"（关键词集见 §5.3）
# 然后发指令："打开记事本"、"音量调大 20"、"音量调到百分之十"、"搜索 Python 教程"
# 查询类："现在几点了"（念出当前时间）、"今天天气怎么样"（念出今日天气）
# 文件类："当前目录下有什么文件"（念出数量和前几项）
# 问答类："什么是量子力学"（本地模型自由生成，无工具能力，不弹浏览器）
# 确认类："写入文件 …" → 助手问确认 → 说「确认」执行 /「取消」放弃
# 收尾："没事了" → 「好的。」并回到等待唤醒
# 观察日志输出
```

> **查询类的期望回应**
> ```
> 现在几点了        → 「现在是下午 3 点 25 分。」
> 今天天气怎么样    → 「北京今天晴，气温 10 到 20 度，现在 15 度。」
> 明天佛山天气怎么样 → 「佛山明天晴，气温 27 到 35 度。」（明天/后天都支持；
>                      「明天佛山**的**天气」同样命中，2026-09-27 修复）
> （断网/超时）      → 「暂时查不到天气。」
> ```
> 天气走 `wttr.in`（无需 API key），城市取句子里说的那个，说不出来就用
> `weather.city`。天气没打开时回「天气查询没有打开。」

> **新交互的期望回应（2026-09-27 起）**
> ```
> 当前目录下有什么文件 → 「一共有12项，前面几项是文档、图片，还有其他。」
> （空文件夹）         → 「这个文件夹是空的。」
> 什么是量子力学      → 一两句中文口语答案（llama-server 在跑时）
> （LLM 不可用/超时）  → 「抱歉，这个问题我现在答不上来。」
> 你好                → 「你好！有什么可以帮你的吗？」（不经过模型）
> 搜索量子力学        → 「你要我搜索量子力学吗？确认请说确认，取消请说取消。」
>   ↳ 说「确认」        → 打开浏览器
>   ↳ 说「取消」        → 「好的，先不做了。」
>   ↳ 说别的           → 该句作废挂起请求，并按新指令处理
>   ↳ 沉默 15 秒       → 请求作废（日志 confirmation_expired）
> 写入文件 X 内容 Y    → 「我将要写入一个文件，确认请说确认，取消请说取消。」
>   ↳ 说「确认」        → 真的写入，snapshots/ 下有快照
> 没事了              → 「好的。」回到等待唤醒状态，什么都不做
> 关机                → 「我将要关机，确认请说确认，取消请说取消。」
>   ↳ 说「确认」        → 5 秒后关机（shutdown /a 可中止）；重启/睡眠/休眠/锁屏/注销同理
>   ↳ 访客说「确认」     → 被声纹分级拒绝
> ```
> 确认等待期间**不需要唤醒词**——回答的是助手刚问出的问题。挂起的请求绑定
> 说话人：换个声纹说「确认」会被拒绝。

> **实测启动日志（2026-09-26）**
> ```
> kws_initialized   keywords=['assistant', '小助手', '你好助手'] threshold=0.25
> vad_initialized   model=...\silero_vad_v5.onnx
> asr_initialized   language=auto mode=sense_voice
> sv_initialized    dim=192 enrolled=[]          <- 尚未注册声纹
> tts_initialized   backend=matcha sample_rate=22050 num_speakers=1 trim_silence=True
> audio_output_route route='wasapi shared' sample_rate=48000 blocksize=960
> audio_output_started sample_rate=48000 blocksize_ms=20 host_api=auto
> pipeline_initialized stub=False
> audio_input_started blocksize=1600 sample_rate=16000
> microphone_open
> Assistant is listening. Say the wake word (default: 'assistant'). Ctrl+C to stop.
> pipeline_started
> ```
> `audio_output_route` 这两行是这次改造的重点之一：输出流整会话**只开一次**，走 **WASAPI 共享模式**、
> 用**该路由自己的采样率**（本机 48 000 Hz），由播放层把模型的 22 050 Hz 重采样上去。
> 老实现是「8 kHz 的 MME 流 + 200 ms blocksize + 每次 100 ms 一块」，音质与延迟都不可控。
>
> **同一个扬声器在不同 host API 下采样率不同**：本机 WASAPI 是 48 000 Hz、MME 是 44 100 Hz，
> 而且向 WASAPI 要 44 100 会直接报 `Invalid sample rate` —— 所以采样率必须按路由分别解析，
> 不能一次性算好（见 `open_output_stream`）。
>
> `enrolled=[]` 表示**还没注册声纹**：此时 `verify()` 返回 `None`，
> 说话人分级不生效（不做拦截），功能可用但**没有声纹保护**。
> 要启用请执行第 7 节的注册命令
>
> 该快照记录的是当时的配置；关键词集此后已更新为
> `assistant / 小助手 / 你好助手`（见 §5.3）。

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
| 装/刷新 DSH 桥接 | `python scripts/install_dsh_bridge.py --install` |
| 检查桥接是否生效 | `python scripts/install_dsh_bridge.py --verify` |
| **看一句话会被怎么念**（不需模型/设备） | `python scripts/show_segmentation.py "我先把文件保存好了，接下来告诉你结果。"` |
| **标定当前 TTS 模型的停顿** | `python scripts/calibrate_tts_pauses.py` |

---

## 9.5️⃣ 语音调参：断句与停顿

听感由两部分决定，都不在模型里，都在配置里：

| 决定 | 配置键 | 说明 |
|---|---|---|
| **切在哪里** | `tts.first_chunk_max_chars` (16)、`tts.clause_max_chars` (24)、`tts.chunk_max_chars` (40)、`tts.hard_max_chars` (80)、`tts.chunk_min_chars` (10) | 首块短 → 出声快；两次可闻停顿之间最多说 `clause_max_chars` 字（到逗号换气）；没有任何标点的长句只在 `hard_max_chars` 处硬切 |
| **停多久** | `tts.pause_*`（句号 240 / 问号 280 / 感叹 260 / 省略 420 / 分号 200 / 逗号 140 / 顿号 100 / 冒号 180 / 换行 420 / 硬切 60 / 连词前 90） | 模型词表里**没有任何标点**、`silence_scale` 实测无效，所以这些数字是唯一来源：播放层把它们写成真实静音帧 |
| **剪掉模型自带的死气** | `tts.trim_silence`、`tts.trim_ratio` (0.02)、`tts.trim_guard_ms` (30) | 实测每次 `generate()` 首尾各带噪声底（Matcha 60–110 ms / 8k 回退 80–250 ms，占峰值 0.24–1.74 %，不是数字零）；不剪就会叠加到每个停顿上 |
| **压掉句中的卡顿洞** | `tts.max_internal_gap_ms` (60)、`tts.internal_gap_keep_ms` (30) | 模型在句中**自掏 1–7 个 40–240 ms 的静音洞**（词表无标点，它看不到逗号），是「语流碎片化」的来源之一。超过 60 ms 的句中静音压回 30 ms（实测 140–170 ms 的洞全部压到 30，30–40 ms 的自然微停顿与塞音闭合保留）；0 = 关闭 |
| **音高** | `tts.pitch` (1.0 = 原声) | baker 原生**中位 F0 = 276 Hz**。锚点：0.9 ≈ 248 Hz、0.85 ≈ 235 Hz、0.8 ≈ 221 Hz——觉得偏「童声」往低调、偏「中性/沉」往上调。机制：chunk 的采样率标称为模型率 × pitch（播放层重采样即整体降调，连共振峰一起降），引擎用 `speed/pitch` 补偿语速——`speed` 管语速、`pitch` 管音高，两个旋钮互不影响。**只在启动时读一次** |

**前两个键段是热生效的**（每回合重新读取），改完不用重启；`tts.speed` / `tts.pitch` 要重启。

```powershell
# 1) 先看：不加载模型、不出声，只打印切点与停顿
python scripts/show_segmentation.py --table "我先把文件保存好了，接下来告诉你结果。"
python scripts/show_segmentation.py --json "好的。"          # 便于对比改动前后

# 2) 再量：测量当前配置的模型（首尾静音、语速、句内自然停顿、修剪效果）
python scripts/calibrate_tts_pauses.py
#    输出会给建议值 —— 脚本**不会**自动改写 config.yaml：
#    ConfigManager 用 yaml.safe_dump 落盘，会把这文件里的注释全部删掉。

# 3) 改 config/config.yaml 的 tts.pause_* / tts.clause_max_chars，回到 1) 复核
```

实测参考（10 ms RMS 包络，两个模型都量过）：

| 指标 | Matcha（默认，22.05 kHz） | VITS aishell3（回退，8 kHz） |
|---|---|---|
| 语速 | **176–239 ms/字（speed 1.0）；配置默认 0.9 时 ≈ 208 ms/字**（240 字 ≈ 50 s） | 262–315 ms/字（speed 1.0，240 字 ≈ 60 s）；0.9 时 ≈ 331 ms/字（≈ 79 s） |
| 每段自带首尾静音 | lead 60–90 ms / tail 60–110 ms | lead 0–120 ms / tail 180–210 ms |
| 噪声底（占峰值） | 0.24–1.68 % | 0.78–1.74 % |
| 起音电平 | 35–53 % | 37–46 % |
| 句内自然停顿 | 40–190 ms | 40–230 ms |
| 修剪掉的每段死气 | 72–98 ms | 172–279 ms |
| `tts.speed` | 1.15 → 87 %、1.3 → 77 % | 1.15 → 81 %、1.3 → 66 %（对 VITS 是更强的杠杆） |

> 换模型后一定要重跑 `calibrate_tts_pauses.py`：这些数字是模型相关的
> （两个模型的噪声底相差 3 倍、语速相差 35 %）。`tts.trim_ratio` 的默认 0.02
> 就是「高于两者最高的噪声底 1.74 %」选出来的，这样每段都会被一致地修剪。

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
| **音质像电话 / 只有 8 kHz** | 说明跑的是**回退模型**（`vits-icefall-zh-aishell3` 原生 8 000 Hz）。看启动日志：`tts_initialized backend=vits sample_rate=8000` 且带 `tts_primary_model_missing` 警告 → 下新模型 `python scripts/download_models.py --tts`。正确状态是 `backend=matcha sample_rate=22050` |
| **播报有咔哒声 / 断续** | 1) 日志 `player_utterance_done` 里的 `underruns`（饿死补静音）与 `dropped_blocks`（设备卡死丢块）：前者大说明合成跟不上，调小 `tts.clause_max_chars` 或加大 `audio.output_prebuffer_ms`；后者大说明输出设备有问题 2) 确认 `audio_output_started` 的 `sample_rate` 等于设备原生率（本机 44100）3) 换输出设备试试：`audio.output_device` |
| **没有声音 / `audio_output_unavailable`** | 输出设备打不开（被独占、被拔掉、驱动问题）。播放层会逐级回退（WASAPI 共享 → WASAPI auto-convert → 系统默认/MME），全失败就降级为静音继续跑，并把每条路由的失败原因写进 `error`。先看设备是否可见：`python -c "import sounddevice as sd; print(sd.query_devices())"` |
| **打断之后就没声音了** | 不应该发生：`abort()` 撞上正在 `write()` 的喂音线程时，某些驱动会拒绝立刻 `start()`，播放层会重试（0/20/50/100 ms）并最终**重开流**（日志 `audio_restart_failed` → `audio_output_reopened`）。若真出现无声，看这两条日志与 `player_utterance_done` 的 `frames` |
| **安装器/升级卡在 92 %** | 实测该偏移正是载荷里 `tools/` 的起点（91.9 %）。原因是 `tools\...\llama-server.exe` 正被占用——助手**复用但不持有** llama-server，所以助手退了它还在。2026-09-28 起向导会在解压前自动停掉安装目录内的进程（日志「已停止 llama-server…」）；若仍卡住，先手动结束 `llama-server.exe` / 该安装目录下的 `python.exe` 再重试。解压进度条下会显示当前写入的文件名，慢盘或杀软扫描时属正常等待 |
| **自定义安装目录无法升级/卸载** | 请从该目录内双击 `WinVoice-Setup-<版本>.exe`；向导按相邻安装标记识别目录。若从更新提示下载后启动，新安装器会继承原安装路径。旧版已复制的向导可能仍只识别默认 `%LOCALAPPDATA%\WinVoice`，请从原自定义目录里的向导启动新版 |
| **想彻底卸载** | 双击安装目录里的 `WinVoice-Setup-<版本>.exe` → 欢迎页选「卸载此安装」→ 确认。会停止助手与 llama-server、删除运行时与三个快捷方式；`models/`（2–3 GB）与 `config/config.yaml` 默认保留，可勾选删除。环境变量（`DEEPSEEK_API_KEY` 等）不会被删除；安装目录在向导窗口关闭后由后台 PowerShell 清理 |
| **走的是 MME 还是 WASAPI？** | 启动日志 `audio_output_route route=…`：`wasapi shared` 最好（低延迟），`wasapi auto-convert` 次之（Windows 做重采样），`system default` 表示 WASAPI 打不开、退回 MME（延迟最差）。`audio.output_host_api` 可强制 `wasapi` 或 `default` |
| **发音太快 / 换模型后比以前快很多** | 默认模型从 8 kHz VITS（≈277 ms/字）换成了 Matcha（speed 1.0 时 ≈193 ms/字，快 45 %）。调 `tts.speed`：时长 ∝ 1/speed，默认 0.9 ≈ 208 ms/字（4.8 字/秒）；嫌快往 0.8（≈241）、嫌慢往 0.95–1.0（≈193，播报腔）。**该键引擎只在启动时读一次，改完要重启**（`pause_*`/`chunk_*` 才是热生效的）。先排除播放链路问题：`audio_output_started` 的 `sample_rate` 应为设备原生率、`audio_playback_done` 的 `audio_ms` 应 ≈ 字数 × ms/字 |
| **音调高 / 声音尖、像小孩**（或反过来：过于中性/低沉） | baker 原生中位 F0 = 276 Hz（p10–p90 196–345）。`tts.pitch` 是纯口味旋钮：1.0 = 原声（当前默认）；嫌高用 0.85–0.9，嫌低用 1.0–1.05。语速不受影响（引擎自动用 `speed/pitch` 补偿）。**要重启**。音色本身（读腔、flat pitch contour）不可调，不满意只能换模型（见「访客和主人声音一样」行） |
| **一句话里有卡顿 / 语流碎片化（词→断点→词）** | 两个来源，都已处理：①**逐块重采样的边缘伪影**（每 100 ms 块独立 `resample_poly`，接缝处有 20–50 % 峰值的振幅台阶——一个字 2–3 下，即「字内断点」；已改为**按段整段重采样**，接缝只剩段边界且被停顿掩蔽，伪影实测归零到量化底噪）；②**模型自己的句内静音洞**（1–7 个 40–240 ms，位置随机；由 `tts.max_internal_gap_ms` 60 / `internal_gap_keep_ms` 30 压掉，热生效）。排查顺序：先看 `audio_playback_done` 的 `underruns`（>0 见「播报有咔哒声」行）；`underruns=0` 还碎就是②，把阈值降到 50 再试 |
| **回答上叠加了一层杂音（同一个音、断续规律、忽有忽无）** | **已实锤：Windows「空间音效」（Spatial Sound）**。该 DSP 层在设备上处理所有音频，会周期性产生卡顿杂音；关闭后杂音消失（2026-09-27 用户实测），与黑匣子取证「应用写入设备的数据干净」互相印证。排查入口：设置 → 系统 → 声音 → 属性 → 空间音效 → 关。若关闭后仍有（**第二台机器实锤的形态**）：日志刷 `audio_write_failed error=…AUDCLNT_E_DEVICE_INVALIDATED`——Windows 在播放中途作废了音频端点（驱动重置/效果管线重载/设备切换），每次作废就是一声卡顿；这是设备驱动层问题（华为等 OEM 内建扬声器常见），**更新声卡驱动到 OEM 最新版**、关掉 OEM 音频控制台里的全部音效；2026-09-28 起播放器遇到该错误会**自动重开流继续播**（日志 `audio_output_recovered`），旧版会在首次失败后静音到重启——旧版遇到就重启应用恢复。其他候选：蓝牙 A2DP↔HFP 切换、其他「音频增强」、USB 省电，取证命令**按安装形态**：开发机直接 `python`；**安装机用内嵌解释器、且必须是安装目录为工作目录**（系统没有 python；config/models 按 CWD 解析）。PowerShell **单行**（分号连接，别粘贴多行，聊天客户端会把换行变成 `<br/>` 而报 「`<` 运算符保留」）：`cd $env:LOCALAPPDATA\WinVoice; $env:WINVOICE_DIAG_PLAYBACK="runtime\diag.pcm"; `.\python\python.exe -m winvoice`，复现后把 `runtime\diag.pcm` 发回。要录设备实放（需先 `.\python\python.exe -m pip install soundcard`）：`cd $env:LOCALAPPDATA\WinVoice; .\python\python.exe scripts\diagnose_playback.py` |
| **句间停顿太长/太短** | 改 `tts.pause_*`（这些键**热生效**，下一句就变），用 `python scripts/show_segmentation.py "文本"` 先看不合成；`pause_comma_ms` 默认 140 ms 落在实测的自然停顿带 40–190 ms 上沿。**想在任何逗号处都停顿**就把 `tts.clause_max_chars` 调到 14–16（默认 24 表示「一句话不超过 24 字就不在逗号处换气」）；每段自带的噪声底由 `tts.trim_ratio`（默认 0.02）剪掉 |
| **首句延迟大** | 首块越大越慢：调小 `tts.first_chunk_max_chars`（默认 16）；也确认 `tts.num_threads` 没被设成 1。日志里 `tts_segment` 的 `latency_ms` 是每段合成耗时，`first_segment_ms` 决定出声时间 |
| **夹嗓子 + 杂音 + 高频断续，但同一台机器上视频音乐正常** | **已实锤并修复（2026-10-01）：输出流被开成了单声道。** 症状是「同一台机器、同一个扬声器：助手夹、播放器不夹」，根因是播放流用 `channels=1` 打开端点——音频引擎会对 mono 流做上混，某些机器的这条上混链会把音频毁掉。**判定实验**（`runtime/vm_probe/route_probe.py`：同一句话、同一端点、只换一个变量）四段结果——`channels=1` 坏、`channels=2` 好、`mono + auto_convert` 好、MME 好 ⇒ 唯一变量是声道数（2026-10-01 在 Win11 VM 实测，四段全部落在 `扬声器 (High Definition Audio Device)` WASAPI #8）。修复：设备流固定以 **2 声道**打开（`winvoice/audio/playback.py` 的 `OUT_CHANNELS`），语音本身仍是单声道、每个声道写同样样本；送设备的 buffer 必须是 `(frames, channels)` 二维，PortAudio 会拒收扁平数组并报 `number of channels must match`。**排查要点**：①先用系统播放器播同一个文件，不夹就说明问题在这条播放流而不是设备；②`audio_output_started` 只印采样率，声道数要看 `open_output_stream`；③`scripts/diagnose_playback.py` 的 `VERDICT` **不覆盖声道数**，别用它排除这一条 |
| **播报夹嗓子（仅当音频经过虚拟声卡，或经「侦听此设备」转播时）** | 附带实锤：**VB-Cable 本身会毁掉音频**。本机回环实测：440 Hz 正弦送进 CABLE Input，录回来频率跑到 422 Hz、14140 个半周期里 8103 个不规则、RMS 掉到 0.00001；干净语音回环逐样本相关仅 −0.0077。所以「助手输出到 CABLE，再靠『侦听此设备』转给扬声器」这条路径必然出杂音。**排查**：`python -c "import sounddevice as sd; d=sd.default.device; print(d, sd.query_devices(d[1])['name'])"`——若输出端点含 `CABLE In`/`VB-Audio`/`Virtual`，就把 `audio.output_device` 指定为物理扬声器或耳机的索引/名字。**教训：装了 VB-Cable 的机器上，环回录音不能当作「应用输出的代表」**（历史频谱结论见 `voice_problem.md`，已作废） |
| **句子被切在奇怪的地方** | 断句规则在 `winvoice/text/segment.py`（禁区：数字内部、括号内、`的` 之后、`了` 之前）。先用 `show_segmentation.py` 复现，再改 `tts.clause_max_chars`（越大越少切）或补连词表 |
| **访客和主人声音一样** | 单说话人模型的必然结果：matcha zh-baker 只有 1 个说话人，访客只能用 `tts.guest_speed` 区分语速。要真两种音色就换多说话人模型（`models/tts/vits-zh-hf-fanchen-C`，16 kHz，187 说话人，需要改 `tts.model` 并自行下载） |
| **回复很长（几十秒）** | `tts.reply_max_chars` 默认 240，默认语速（speed 0.9 ≈ 208 ms/字）下 240 字约 50 秒（回退的 8 kHz 模型在 0.9 时 ≈331 ms/字 → 约 79 秒）。嫌长就调小（热生效），或在播报中说唤醒词打断 |
| 控制台刷 `Unknown token: shei2` | **无害的上游数据缺陷**，可以忽略：Matcha 词表里有 4 条词条（`谁的`/`谁都`/`人生自古谁无死`/`鹿死谁手`）标了 pinyin `shei2`，但模型 `tokens.txt` 里只有 `shui2`。实测：这行**每次 `generate()` 都恰好打一次、与文本无关**，而且 `谁的`(580 ms) ≈ `谁`(362 ms) + `的`(269 ms)，两个音节都念出来了 —— 不是丢字。真正会丢字的是拉丁词（见 `OOV ... Ignore it!`） |
| 唤醒词不灵敏 | 见 §5.3：优先换成中文关键词，其次降 `kws.threshold` 到 0.20，再不行 `kws.use_int8: false` |
| 控制台刷 `OOV ... Ignore it!`（如 `OOV 90.`、`OOV app.`） | sherpa 中文 VITS 的**词表里没有任何拉丁词条**，数字靠 `number.fst` 展开。① 原文含英文 → 说明有工具把英文错误直接送进了 TTS（应走 `ToolResult.message`，中文面向用户，`error` 只进日志）② 数字被丢 → 检查 TTS 模型目录里 `number.fst` / `date.fst` / `phone.fst` 是否存在（引擎会把它们作为 `rule_fsts` 传入） |
| 说「打开记事本」被拒绝 | 应用名按中文标签/英文 id/近似拼写解析（`记事本`、`notepad`、`Notpa` 都能命中）；不在白名单内的（微信/QQ）仍会拒绝。若要新增，改 `ALLOWED_APPS` + `APP_SPEECH` |
| 关得掉记事本却关不掉代码编辑器（VS Code） | **已修复（2026-09-27）**：`ALLOWED_APPS["vscode"]` 存的是**启动命令** `code`（App Paths/PATH shim），而 `close_app` 与验证器把它当**进程名**用——不以 `.exe` 结尾直接进「我关不掉」分支。现在新增 `APP_PROCESS_IMAGES`（`vscode` → `Code.exe`）区分启动命令与进程映像，关闭与验证都盯真实进程。回归：`pytest tests/unit/test_close_app_safety.py` |
| 说「在桌面建立一个txt文件」没有建成 | **已修复（2026-09-27）**：旧链路三处断点——①`content` 必填，而这句话本来就没有内容；②「桌面」没映射到真实桌面文件夹（会写到仓库里名为「桌面」的目录）；③校验失败念的是权限话术「这个操作我暂时不能替你做」。现在：无内容=建空文件（已存在则拒绝不覆盖）、口语文件夹词映射（桌面/下载/文档/图片/音乐/视频）、校验失败明说「没听清具体要求」。验证器与快照同样用映射后的路径。回归：`pytest tests/unit/test_write_file_flow.py` |
| 本地 agent 回合全部失败（日志 `dsh_local_unresolved reason=turn_ended_error`） | 看 `logs/llama-server.log`：若是 `400 JSON schema conversion failed`，说明 llama.cpp 构建太旧、转换不了某个工具 schema（已修复的根因：`set_volume` 的 anyOf 裸 required 子模式；`mcp_server.to_json_schema` 现渲染完整对象模式）。换新构建或新增工具后复现 → 用 `python -m winvoice --check` 后对 8080 直接 POST 带 tools 的请求复现，看是哪个 schema |
| 云端升级失败 `MISSING_CREDENTIAL`（日志 `dsh_cloud_failed`） | DEEPSEEK_API_KEY 未持久化：`setx DEEPSEEK_API_KEY "sk-..."` 后重开终端再启动助手，或在 DSH 的 Models 页把 key 写入凭证库（`runtime/dsh_home/.credentials.yaml`）。通路本身（升级触发/适配器/路由）与之无关，勿反复重启排查 |
| 启动日志出现 `llama_server_failed` / `llama_server_start_timeout` | 自动拉起没成功：看 `logs/llama-server.log` 尾部（8080 已被占用但不健康、模型路径错、内存不足都可能）。服务加载慢只是超时 → 调大 `llm.local.start_timeout_s`。不想自动管理就 `llm.local.auto_start: false` 回手动模式 |
| 启动日志出现 `model_integrity_failed` | 某个模型文件与封存不符（截断/损坏/被替换），`python -m winvoice --check` 会列出具体文件与原因。重新下载该模型；若变更有意（如自己换了 gguf）→ 重跑 `python scripts/download_models.py --seal`（`--model <key>` 只重封一个）。从未封存 → 日志是 `model_integrity_unsealed`，跑一次 `--seal` 即可 |
| 其他人被识别成 full（非主人的声音能通过权限） | **修复指引（2026-09-27）**：两个来源——①guest 档核验曾触发自适应更新，档案被 household 常客的声音拖偏（已修：只有 full 判定才允许更新档案）；②主人注册时 `threshold_high` 由最差一对样本决定（min_intra−0.05），一个坏样本会把门槛拖到 ≈0.63，同性别他人即可越过。**重录声纹**（安静环境、距离稳定、语气自然）：`python -m winvoice.enroll --speaker me --samples 8 --force`，看输出的 `threshold HIGH`——≥0.70 为健康；若仍 <0.70，重录一遍。被污染的旧档案已备份为 `models/sv/profiles/me.json.bak-20260927`（已移除漂移嵌入） |
| 确认时主人被判 guest 甚至 rejected（score 0.3x） | **已修复（2026-09-27，两层根因）**：①核验原来跑在麦克风**滚动窗口**上（大半是判停静音+回声，嵌入退化）；②更隐蔽的是**档案被自适应更新污染**——每次 guest 误判都会把 `me.json` 最新嵌入向垃圾音频 EMA 漂移 5% 并写盘，多次尝试后主人对自己只剩 0.32 分。现在：核验用 **VAD 段 speech-only 音频**、问题播完后**丢弃 0.4 s 回声衰减期**、确认短语改为「确认执行」（更长更稳）、**把关型核验不再修改档案**、rejected 自动重问一次（第二次才作废）。若仍有波动 → 重录声纹 `python -m winvoice.enroll --speaker me --samples 8 --force` |
| 说「关机」没有反应 / 回「没实现」 | 规则层 `system_power` 触发词：`关机/重启/重新启动/睡眠/休眠/锁屏/锁定屏幕/注销/退出登录`（「怎么关机」是问句，走问答）。执行需主人声纹 + 口头「确认」；关机/重启带 5 秒缓冲（`shutdown /a` 可中止）。日志 `confirmation_armed tool=system_power` |
| 说「打开谷歌浏览器」回「我没找到…的安装位置」 | 程序既不在 `PATH`、也不在 `App Paths` 注册表里。查 `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe`（Chrome/Edge/VS Code 安装时都会写这个键）；绿色版/免安装版需要手动加进 `PATH`。**这是如实回答**：旧版拿裸名字 `Popen(..., shell=True)` 启动，`chrome.exe` 不在 `PATH` 时 cmd.exe 只打印「不是内部或外部命令」，而工具照样报成功 |
| 说「关闭记事本」回「好像没有在运行」 | `taskkill` 退出码非 0（128 = 没找到该进程）：现在是如实回答，旧版无论有没有关掉都说「已经关闭」。系统设置是 URI 不是进程，会回「我关不掉系统设置。」 |
| 说「关闭资源管理器」桌面/任务栏消失 | **旧版缺陷，已修**（2026-09-20）。`explorer.exe` 是 Windows 外壳，旧代码对它执行 `taskkill /f /im explorer.exe`，把整个 GUI 一起杀了。现在改成用 Explorer 自己的自动化对象只关**文件夹窗口**，并且 `explorer.exe` 在 `PROTECTED_PROCESSES` 里、任何情况下都不会被强杀。桌面已丢失的话：`Ctrl+Shift+Esc` → 文件 → 运行新任务 → `explorer.exe` |
| 「关闭记事本」没有弹出「是否保存」 | 现在**默认优雅关闭**，应该会弹。若真的没弹，说明请求里带了 `force`（句子里有「强制/强行/硬关」）。`taskkill /f` 会跳过保存提示 |
| 说「关闭 X」后卡了十几秒 | 优雅关闭在等**应用自己**的「是否保存？」对话框。这是预期行为，助手会说「…好像在等你确认，可能有没保存的内容。」（超时 12 秒）。点掉那个对话框即可 |
| 想强制关闭 | 说「**强制**关闭记事本」。`force` 只在你明确要求时才用 —— 它不弹保存提示，未保存的内容会直接被丢掉 |
| 「用浏览器搜索天气」没开浏览器 / 「查一下天气」却开了浏览器 | 规则层按触发词分流：`搜索/搜一下/百度/google/search` 属**显式搜索**，优先于一切话题（开浏览器）；`查一下/查询` 是弱触发词，由话题决定（问天气）。改 `_EXPLICIT_SEARCH` / `_WEAK_SEARCH` 后跑 `pytest tests/unit/test_weather_speech.py tests/unit/test_web_search.py` |
| 音量「调高」「调低」方向不对 | 方向词已覆盖 `调高/调低/调大/调小/减小/降低/小声/小一点/down/lower…`；绝对量走 `level`（0–100），相对量走 `delta`，两者不可混用 |
| 写文件/跑脚本/搜索没执行，先听到一个问题 | 这就是**确认回路**：说「确认」执行、「取消」放弃、说别的等于改主意（该句作废挂起请求并按新指令处理）、沉默 15 秒作废（`tools.confirm_timeout_s`，热生效）。`search_web` 的先问后开由 `tools.search_web_confirm` 控制；关掉即恢复「说了就开」。Agent 经 MCP 调用不受确认回路影响 |
| 说「确认」没有反应 | 确认等待期麦克风直接进 ASR（无需唤醒词），但窗口只有 15 秒，超时已作废（日志 `confirmation_expired`）。另检查 `tools.confirm_required` 是否为 `true`、说话人是否与当初请求的一致（换人会被拒，日志 `confirmation_speaker_mismatch`） |
| 问答（「什么是X」）回「答不上来」 | llama-server 没起或超时（`llm.ask.timeout_s`，默认 20 s，3B CPU 生成约 10–20 token/s）。`python scripts/check_llm.py` 确认 LLM 层；答案里若全是英文会被可朗读过滤掉，同样回这句。`llm.ask.enabled: false` 也会明说没打开。打招呼（「你好/谢谢/再见」）不经过模型，任何时候都能回 |
| 说「没事了」没回到待唤醒 | 规则层 DISMISS 是**兜底位**：句子里若带具体指令（「算了，打开记事本」）会按指令走。纯「没事了/不用了/算了/退下」应回「好的。」并回到等待唤醒（日志 `intent_rule_matched intent=dismiss`） |
| 启动日志出现 `dsh_not_enabled` | 正常：`dsh.enabled` 或 `dsh.local.enabled` 为 `false`（默认都是 false）。要启用见 §2.4 |
| 说了复杂指令却回「抱歉，这个请求我还没有实现。」 | 规则层没命中、而 DSH 没启用：此时没有模型层可以接。开 `dsh.enabled` + `dsh.local.enabled`，或让说法落到 12 个工具之一 |
| `dsh_settings_unavailable` / `ModuleNotFoundError: deepseek_harness` | 可选依赖没装。`python -m pip install --target .pylibs --upgrade mcp deepseek-harness-sdk deepseek-harness-runtime-bin` |
| Agent 完全不知道有工具（只会聊天） | 桥接 bundle 没装或没进 profile：跑 `python scripts/install_dsh_bridge.py --verify`。它应输出 `[OK] mcp-winvoice is present in the composed configuration` |
| `patch: entry "mcp-winvoice" not found` | 有人手写了补丁行而没用 `insert:` 包一层。删掉手写的那份，改用 `scripts/install_dsh_bridge.py` 生成 |
| `dsh_escalating` 后仍失败 | 本地与云端都失败。看日志里的 `escalation_reason`：`verification_failed` 表示机器状态与目标不符（最常见是程序没真的起来）；`dsh_unavailable` 表示运行时没起来 |
| 云端那一层从不触发 | `dsh.cloud.enabled` 与 `dsh.escalation_enabled` 都要为 `true`，且说话人分级必须是 `full`（guest 不能走云端） |
| 说「关闭 X」回「我没能关掉…」而不是「好像没有在运行」 | `taskkill` 的退出码不是 128、输出里也没有「找不到」。这通常是**权限**问题（进程以管理员身份运行），不是「没在运行」—— 两种情况都如实回答，不谎报成功 |
| 工具说成功了但 DSH 仍升级到云端 | 这是**验证器**在起作用：`verification.status == failed` 表示机器状态与目标不符。日志里搜 `tool_verified` 看是哪一项 check 没过（例如 `process_running`） |
| 问「现在几点了」回「这个请求我还没有实现」 | 该意图没有映射到工具。检查 `AudioPipeline._intent_to_tool_calls` 里有 `get_time`，且 `ToolName.GET_TIME` 已注册（`pytest tests/unit/test_time_speech.py`） |
| 天气一直回「暂时查不到天气」 | 1) 有没有网：`python -c "import httpx;print(httpx.get('https://wttr.in/Beijing?format=j1&timeout=5').status_code)"` 2) `weather.enabled` 是否为 `true` 3) `weather.timeout_s` 太小（默认 5 s）。句子里说的地名 wttr.in 认不出时（它会返回 500），会自动改用 `weather.city` 再查一次；只有**网络**类失败才会直接回这句兜底 |
| 打开命令提示符/命令行窗口没弹新窗口，助手窗口里刷出 cmd 横幅 | **已修复（2026-09-27）**：控制台子系统程序用 `Popen` 启动时会**继承调用方的控制台**——cmd 的横幅打进助手窗口、还共享它的 stdin。现在 `_launch` 读 PE 头的 Subsystem 字段（CUI=3，免维护名单），给控制台程序加 `CREATE_NEW_CONSOLE` 弹独立窗口；GUI 程序不受影响。回归：`pytest tests/unit/test_app_launch.py` |
| 问「明天X天气」回了北京的今天 | **已修复（2026-09-27）**：旧版两个叠加缺陷——①城市提取的 4 字贪婪窗口在「明天佛山**的**天气」里错位成「天佛山」，wttr.in 500 后静默回退 `weather.city`；②「明天」被无视、永远答今天。现在「的」在提取窗口外、`day` 参数选择预报条目（payload 只有今天时会如实说「今天」）。回归：`pytest tests/unit/test_weather_speech.py` |
| 天气念成英文 / 缺一半字 | 天气描述必须走内置中文对照表：wttr.in 即使带 `lang=zh` 也只返回英文（`weatherDesc` 与 `lang_zh` 均为英文，已实测）。若出现新的 `weatherCode`，在 `winvoice/tools/weather.py` 的 `WEATHER_CODE_ZH` 补中文，并用 `pytest tests/unit/test_speech_is_pronounceable.py -k weather` 验证这些字在 TTS 词表里 |
| 工具明明成功了，却只听到「好的，已为您完成。」 | 该工具没有写 `ToolResult.message`，或写的是英文（含拉丁字母会被拒绝并回退）。成功也念 `message`，见 README「Spoken output」 |
| 答话被截断在半句 | `winvoice/contracts/speech.py` 的 `clip_for_speech` 会截到 80 字（优先在句号处）。工具该给短句，长内容请改成「一共有 N 项，前几项是…」 |

---

## 📁 关键路径速查

```
项目根目录/
├── config/config.yaml                       # 主配置
├── grammar.gbnf                             # GBNF 语法（客户端逐请求下发，非必需）
├── run.ps1                                  # 从任意目录启动
├── models/
│   ├── kws/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20/
│   │   └── winvoice_keywords_<sha1>.txt     # 运行时生成的唤醒词 token（需目录可写）
│   ├── vad/silero_vad_v5.onnx
│   ├── asr/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/
│   ├── tts/matcha-icefall-zh-baker/          # 默认：22.05 kHz
│   │   ├── model-steps-3.onnx · lexicon.txt · tokens.txt
│   │   ├── vocos-22khz-univ.onnx             # 声码器（缺了发不出声音）
│   │   └── number.fst / date.fst / phone.fst # 数字、日期、电话的文本规范化规则
│   ├── tts/vits-icefall-zh-aishell3/         # 回退：8 kHz（174 说话人）
│   │   ├── model.onnx · lexicon.txt
│   │   └── number.fst / date.fst / phone.fst
│   ├── sv/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx
│   ├── sv/profiles/me.json                  # 声纹档案（注册后生成）
│   └── llm/qwen2.5-3b-instruct-q4_k_m.gguf
├── tools/                                   # 本地解压的 llama.cpp（gitignore）
├── .pylibs/                                 # DSH 可选依赖（gitignore，见 §2.4）
├── runtime/                                 # DSH bridge/DSH home/在途话语记录（gitignore）
│   ├── dsh_bridge/                          #   生成的桥接 bundle（scripts/install_dsh_bridge.py）
│   ├── dsh_home/                            #   本项目自己的 DSH home（不动 ~/.dsh）
│   └── utterance.json                       #   当前这句话的说话人分级（供 MCP 子进程读）
├── logs/                                    # 结构化日志（每日轮转）
├── snapshots/                               # 破坏性操作快照
├── scripts/
│   ├── download_models.py                   # 下载模型
│   ├── smoke_test_models.py                 # 引擎冒烟测试（真实模型）
│   ├── show_segmentation.py                 # 看一句话会被怎么切、停多久（不需模型）
│   ├── calibrate_tts_pauses.py              # 标定当前模型的边缘静音/语速/自然停顿
│   ├── check_llm.py                         # LLM 层检查
│   ├── install_dsh_bridge.py                # 生成并安装 DSH 桥接 bundle
│   └── verify_install.py                    # 依赖检查
└── winvoice/                                # 源码包
    ├── mcp_server.py                        # 把工具注册表暴露给 DSH（MCP stdio）
    ├── dsh/                                 # DSH 客户端、升级策略、配置、桥接 bundle
    ├── text/                                # 规范化/语义断句/短句分块/停顿表（纯函数）
    ├── contracts/speech.py                  # 可朗读规则（中文、长度预算 → 见 text/）
    ├── audio/playback.py                    # 常驻输出流 + 缓冲 + 停顿 + 打断
    ├── tools/verifier.py                    # 验证器：用机器状态核对工具结果
    └── tools/weather.py                     # 天气工具 + WWO 码中文对照表
```

---

## 🎉 部署完成标志

- [ ] `python -m winvoice --check` → `Startup check PASSED`（5 个引擎全部加载）
- [ ] `python scripts/smoke_test_models.py` → `18/18 passed`
- [ ] `python -m pytest tests/ -q` → 全绿（2026-09-27：561 passed, 1 skipped）
- [ ] `llama-server` 在 8080 端口监听，`main: server is listening on http://127.0.0.1:8080`
- [ ] `python scripts/check_llm.py` → `9/9 intents matched`，`grammar-constrained decoding: ACTIVE`
- [ ] `python -m winvoice` 启动 → `microphone_open` + `pipeline_started`
- [ ] `python -m winvoice.enroll --speaker me --samples 8` → 按 8 条引导词念完，生成 `models/sv/profiles/me.json`，`--check` 显示 `enrolled=['me']`
- [ ] 说 `assistant` / `小助手` → 唤醒 → 指令执行 → TTS 中文回复
- [ ] （可选，启用 DSH 时）`python scripts/install_dsh_bridge.py --verify` → `[OK] mcp-winvoice is present in the composed configuration`，且启动日志有 `dsh_enabled`

---

**🎯 完成！** 现在你拥有了一个在灵耀14 Air 上本地运行的、支持声纹验证、工具调用、云端回退的 Windows 语音助手。

> 后续迭代：Roadmap 步骤 1 验证意图分类器准确率 → 步骤 2 音频管道联调 → 步骤 3 PySide6 UI...
