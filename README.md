# Windows Voice Assistant

A Windows voice assistant built on [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx). Local-first, with pluggable local (Ollama) and remote (OpenAI-compatible) LLM backends. Supports speaker verification, wake word detection, and desktop automation.

> **Status**: Early development. Architecture is settled; features are being implemented incrementally. See [Roadmap](#roadmap).

Repository: https://github.com/luoxiaguwu9394/windows_voice_assistant

---

## Features

- **Wake word detection**: sherpa-onnx Zipformer KWS, always-on, custom keywords without retraining; English or Chinese wake words (`assistant` / `小助手` / `你好助手`), tunable sensitivity
- **Speaker verification**: CAM++ embeddings, three-tier permission model (full / guest / reject), adaptive updates at runtime
- **Local ASR**: SenseVoice / Zipformer streaming, multilingual
- **Local TTS**: sherpa-onnx Matcha (Chinese, icefall baker, 22 050 Hz) with the 8 kHz VITS as an automatic fallback; speech is normalised, cut into sentence-sized pieces, and spoken with the pauses the config asks for
- **Dual LLM backends**: local Ollama and any OpenAI-compatible remote API, routed by a fixed escalation chain
- **Three-tier intent routing**: rules → local small model → cloud, balancing latency and accuracy
- **Optional DeepSeek Harness agent**: when enabled, requests no rule answers are planned and executed by a DSH agent that reaches this project's tool registry over MCP
- **Result verification**: each side-effecting tool's postcondition is checked against real machine state (process running? file on disk?), so a claimed success is never taken at its word
- **Tool allowlist**: the LLM can only invoke registered tools; it cannot generate shell commands
- **Destructive action protection**: double confirmation plus file-level snapshot rollback
- **Half-duplex with real barge-in**: KWS stays active while the assistant talks, the wake word aborts the audio device buffer immediately, and the turn it interrupted is disowned rather than spoken over the new command

---

## Architecture

```
Microphone
  │
  ▼
┌─────────────────────────────────────────┐
│ Audio process (sherpa-onnx)               │
│  KWS always-on ── trigger ──▶ SV verify   │
│                              │            │
│                              ▼            │
│                        VAD segmentation   │
│                              │            │
│                              ▼            │
│                        ASR recognition    │
└──────────────────┬──────────────────────┘
                   │ text
                   ▼
┌─────────────────────────────────────────┐
│ Intent routing (three-tier waterfall)     │
│  ① Rule match        → 0 latency          │
│  ② Intent classifier → local small model  │
│  ③ Cloud LLM         → low confidence or  │
│                        allowlist miss     │
│                                           │
│  (with the DSH agent enabled, ②/③ are     │
│   replaced by the agent — see above)      │
└──────────────────┬──────────────────────┘
                   │ structured command
                   ▼
┌─────────────────────────────────────────┐
│ Execution process (tool allowlist)        │
│  Non-destructive → execute directly       │
│  Destructive     → confirm + snapshot     │
│  Every side effect → verify against the   │
│                      machine afterwards   │
└──────────────────┬──────────────────────┘
                   │
                   ▼
              TTS playback (half-duplex)
```

### Process model

| Process | Responsibility |
|---|---|
| Main | PySide6 UI, configuration |
| Audio | KWS / VAD / ASR / speaker verification, real-time threads |
| LLM | Ollama and cloud HTTP calls, blocking isolation |
| Execution | pyautogui / pywin32, serialized queue |

Processes communicate via `multiprocessing.Queue` or ZeroMQ. The split exists to bypass the GIL, isolate crashes, and keep audio scheduling stable.

---

## Agent layer (optional): DeepSeek Harness

Off by default. With `dsh.enabled: true` the assistant has exactly two tiers, and
one of them serves every request:

```
Speech → ASR → Rule matching
                   │
        ┌──────────┴───────────┐
   rule matched            no rule matched
        │                       │
   direct tool call        DSH agent  ── planning, tool calling, reply
        │                       │
        │              tools via MCP (mcp__winvoice__*)
        │                       │
        └───────────┬───────────┘
                    ▼
             tool result → Verifier
                    │
            verified ─┴─ machine disagrees
                    │              │
                 speak        local retry → Cloud DSH
```

A rule match is **never** overridden — 「音量调大 20」 must not acquire a model's
latency. Everything else goes to the agent.

### The tool system does not change

DSH runs its agent loop in Node, so it reaches this project's tools over **MCP**
(`winvoice/mcp_server.py`). That server is a thin adapter: it builds the *same*
`ToolCall` the in-process pipeline builds and hands it to the *same*
`ToolExecutor`. There is exactly one implementation of the allowlist, the
speaker tiers, the snapshot/rollback logic and the verifiers — the constraint
`new_way.md` calls out, and the reason the original "generate a TypeScript plugin
per tool" plan was dropped (see `docs/dsh_integration_design.md` §1a).

Tools therefore appear to the model as `mcp__winvoice__open_app`, and a call is
refused by exactly the same code that refuses it when the rules path invokes it.

### Verification: the machine decides, not the model

Every tool whose postcondition is observable — `open_app`, `close_app`,
`set_volume`, `write_file`, `run_script` — is checked against real machine state
after it runs:

| Tool | What is actually checked |
|---|---|
| `open_app` | the resolved `.exe` is in the process list (polled for ~1s) |
| `close_app` | the process is *gone* |
| `write_file` | the file exists, is a regular file, and its bytes match |
| `set_volume` | the Core Audio endpoint reads back the requested level (±3) |
| `run_script` | the process exit status |

The verdict is one of `verified` / `failed` / `uncertain` / `not_verifiable`, and
it is **not** allowed to rewrite what the tool said. `close_app` on something
that is not running reports failure with 「好像没有在运行。」 — a true and useful
sentence — while the postcondition ("not running") genuinely holds, which is
`verified`, which means *do not escalate*. Only an observed mismatch escalates.

`uncertain` and `not_verifiable` never escalate either: "this layer cannot
observe it" is not evidence of failure, and escalating on it would send every web
search to the cloud.

### Escalation is programmatic

Escalation to the cloud tier happens on: an observed state mismatch, a turn that
ended abnormally (`error`, `max-tokens`), an empty answer, or a backend that
would not start. It does **not** happen because the model sounded unsure — a
small model that hallucinates success is just as fluent when it hallucinates
certainty. The cloud agent is handed what the local attempt did, so it continues
rather than starting over.

### Configuration

```yaml
dsh:
  enabled: true
  escalation_enabled: true
  max_local_attempts: 2
  local:
    enabled: true
    provider: deepseek-official
    model: qwen2.5-3b-instruct
    base_url: http://localhost:8080/v1
    request_timeout_s: 90
  cloud:
    enabled: false
    api_key: ${DEEPSEEK_API_KEY}
  bridge:
    server_name: winvoice        # tools appear as mcp__winvoice__<tool>
```

Full setup (the optional `.pylibs` dependencies and the bridge bundle) is in
[`deployment.md`](deployment.md) §2.4.

> **Speaker tier reaches the agent.** Tools now run in a process DSH spawns, so
> the tier travels through the in-flight utterance record
> (`winvoice/tools/utterance.py`); without it, DSH would be a way *around* the
> permission model rather than a user of it. The cloud tier is gated on the same
> tier (`dsh.cloud.guest_allowed`, default off).
>
> **The agent also has DSH's own tools.** The default `sdk` profile ships DSH's
> built-in filesystem/shell tools beside the MCP bridge, so `mcp__winvoice__*`
> goes through the allowlist but those do not. `dsh.local.profile: sdk-minimal`
> would close that; see `docs/dsh_integration_design.md`.

---

## Intent routing

```
① Rules
   High-frequency commands: open/close app, volume, media, time, weather
   Regex plus keyword tables, zero latency

② Intent classifier
   Qwen2.5 7B with constrained decoding
   Output: {"intent": "...", "args": {...}}
   Intent enum capped at 15
   Confidence < 0.70 escalates to ③

③ Cloud LLM
   OpenAI-compatible API
   Only handles ② failures or allowlist misses
```

**Hard constraint**: the small model only emits intent labels and structured arguments. It **does not select tools**. Tool selection and argument filling happen in code.

---

## Quick start

> Commands below use PowerShell. The project is under active development; interfaces may change.

### The one-click way: setup wizard (recommended)

Download `WinVoice-Setup-<version>.exe` from the project's Releases page and
double-click it. The wizard handles everything — embedded Python runtime,
llama.cpp, model download (resumable, proxy/HF-mirror aware), audio device
test, config generation, wake-word setup, optional DSH agent layer and cloud
keys, speaker enrollment, and a full `--check` — then creates the desktop
shortcut. No Python, no pip, no compiling.

Re-running the copied exe detects the install in its own directory and offers
upgrade / repair (models and config are preserved), including when you chose a
custom install location. Updates are discovered from new release assets
automatically. Details: `installer/README.md`.

### Requirements

- Windows 10 / 11
- Python 3.10+ (only for the manual path below)
- (Optional) NVIDIA GPU for local LLM acceleration
- (Optional) [Ollama](https://ollama.com/) for local LLM

### Install (manual)

```powershell
git clone https://github.com/luoxiaguwu9394/windows_voice_assistant.git
cd windows_voice_assistant
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# Or with dev dependencies:
pip install -e .[dev]
```

### Download models

```powershell
# Download all models
python scripts/download_models.py --all

# Or download individually
python scripts/download_models.py --kws zipformer-zh-en
python scripts/download_models.py --vad silero
python scripts/download_models.py --asr sense-voice
python scripts/download_models.py --tts              # Matcha 22 kHz + its vocoder + the 8 kHz fallback
python scripts/download_models.py --tts-fallback     # only the old 8 kHz model
python scripts/download_models.py --sv campplus
```

Models are downloaded to `models/` by default; paths are configurable.

> **TTS is the big one now.** `--tts` fetches `matcha-icefall-zh-baker` (72 MB)
> **plus its vocoder** `vocos-22khz-univ.onnx` (51 MB) and the old 8 kHz fallback
> (30 MB) — about 153 MB in total. The Matcha acoustic model cannot make sound
> without the vocoder, and if it is missing the assistant falls back to 8 kHz and
> logs `tts_primary_model_missing` rather than refusing to start.

### Local LLM (llama.cpp server)

The intent classifier talks to `llama-server` over its OpenAI-compatible API.
Start the server with the GGUF downloaded by `download_models.py`:

```powershell
cd tools\llama-b7376-bin-win-cpu-x64

.\llama-server.exe `
  -m ..\..\models\llm\qwen2.5-3b-instruct-q4_k_m.gguf `
  --port 8080 -ngl 0 -c 4096
```

> **No grammar flag needed.** GBNF is sent per-request in the `grammar` field,
> so the server needs no `-mgf` / `--grammar-file` argument. If a build rejects
> the grammar, the client logs `local_llm_grammar_rejected` and automatically
> falls back to `response_format: json_object`.

Verify the LLM tier once the server is up:

```powershell
python scripts/check_llm.py
# -> 9/9 intents matched
#    grammar-constrained decoding: ACTIVE (GBNF accepted)
```

> Ollama is supported as an alternative OpenAI-compatible endpoint; point
> `llm.local.base_url` at it and set `llm.local.model` accordingly.

### Speaker enrollment

First run requires recording **8 samples**, ~4 seconds each. The tool shows one
line to read before every sample, rotating through digits, commands and small
talk, and previews the next line while the previous take is being embedded:

```powershell
python -m winvoice.enroll --speaker me --samples 8
```

```
[*] Sample 1/8 - speak for 4s
    【数字】一三五七九，二四六八十，今天二十三度。
    starting in 3...
    done.
    next up: 【指令】打开记事本，再帮我查一下天气。
```

Read at a normal pace and vary volume and distance between takes — the prompts
are a guide, not a script. Running past the timer mid-sentence is fine.

Thresholds `T_high` and `T_low` are computed automatically. If `min_intra < 0.4`, the tool prompts for re-recording.

### Run

> **No virtualenv required.** Dependencies install into your user
> site-packages (`pip install -r requirements.txt`), and `python` resolves
> from any directory — but the process must start with the **repo root as its
> working directory**, because `config/config.yaml` and `models/` are resolved
> relative to the CWD. `run.ps1` handles that for you.

```powershell
cd C:\Users\<you>\Desktop\windows_voice_assistant   # repo root

# Verify every model loads, then exit (no microphone required)
python -m winvoice --check

# Run for real (needs models + a microphone)
python -m winvoice

# Run with model-free stubs (development / plumbing checks)
python -m winvoice --stub-audio
```

From any other directory, use the helper — it switches to the repo root first:

```powershell
& C:\Users\<you>\Desktop\windows_voice_assistant\run.ps1 --check
& C:\Users\<you>\Desktop\windows_voice_assistant\run.ps1
```

### What you can say

Say a wake word, wait for the acknowledgement, then speak one command.

| Wake words | Volume | Media |
|---|---|---|
| `assistant` · `小助手` · `你好助手` | `音量调大 20` / `音量调低` (relative) | `播放` · `暂停` · `下一首` · `上一首` |
| | `音量调到百分之十` / `音量调到 50%` (absolute) | |
| **Apps** | **Web search** | **Files** |
| `打开记事本` · `打开计算器` · `打开资源管理器` | `搜索今天新闻` · `查一下北京天气` | `读取文件 <path>` · `当前目录下有什么文件` |
| `关闭记事本` (see the allowlist below) | asks 「你要我搜索…吗？」 first (see below) | `在桌面建立一个txt文件` · `写入文件 <path> 内容 …` · `运行脚本 <path>` (all ask 确认 first, see below) |
| **Power** (owner only, asks 确认 first) | | |
| `关机` · `重启` · `睡眠` · `休眠` · `锁屏` · `注销` | | |
| **Time** | **Weather** | **Questions & small talk** |
| `现在几点了` · `现在什么时间` | `今天天气怎么样` · `明天佛山天气怎么样` · `后天北京天气` | `什么是量子力学` · `你好` · `再见` |
| answers e.g. 「现在是晚上 6 点整。」 | answers e.g. 「佛山明天晴，气温 27 到 35 度。」 (needs a network) | see below: answered in short spoken Chinese, never by running tools |

A successful tool answers with what it found or did — 「已经打开记事本了。」,
「音量已经调到百分之45。」, 「现在是上午 9 点 5 分。」 — and only falls back to
「好的，已为您完成。」 when the tool has nothing to add. When an action cannot be
carried out it says so instead of claiming success: a program that cannot be
located answers 「我没找到谷歌浏览器的安装位置。」, and closing something that is
not running answers 「记事本好像没有在运行。」.


Allowlisted apps — say the Chinese or the English name, close spellings are
matched too (ASR slips such as `Notpa` still land on `notepad`):

| Spoken | App |
|---|---|
| 记事本 · 笔记本 | Notepad |
| 计算器 | Calculator |
| 资源管理器 · 文件管理器 · 我的电脑 | Explorer |
| 命令提示符 · 终端 | `cmd` |
| 命令行窗口 | PowerShell |
| 代码编辑器 | VS Code |
| 谷歌浏览器 · 浏览器 | Chrome |
| 微软浏览器 | Edge |
| 系统设置 · 设置 | Windows Settings |

Apps are launched by their real path: the `App Paths` registry key first (how
Windows itself finds Chrome, Edge and VS Code — none of the three is on `PATH`;
VS Code registers `Code.exe` while its command is `code`), then `PATH` for the
system tools. If neither knows the program, the assistant says
「我没找到…的安装位置。」 instead of pretending it opened something. `打开浏览器`
opens Chrome; say `搜索…` to open a search instead. Console programs
(「打开命令提示符」/「打开命令行窗口」) get a **console window of their own** — they are
detected by their PE subsystem and started with `CREATE_NEW_CONSOLE`, so they never
print into (or share input with) the assistant's own console.

**Closing is graceful by default.** 「关闭记事本」 lets Notepad ask about unsaved
work — it does not silently discard it. Say 「**强制**关闭记事本」 to skip that
prompt; forcing is only used when you ask for it explicitly. If the application is
waiting for you to answer its own prompt, the assistant says so
(「…好像在等你确认，可能有没保存的内容。」) rather than claiming a failure.

**`资源管理器` closes folder windows, not your desktop.** This one deserves its own
sentence, because getting it wrong is dramatic. `explorer.exe` is the Windows
*shell* — the desktop, taskbar, Start menu and every folder window are that one
process. 「关闭文件资源管理器」 therefore closes the open folder windows through
Explorer's own automation object and never touches the process; `explorer.exe` is
on a protected list that is never force-killed, whatever the request says. (Before
this was fixed the assistant ran `taskkill /f /im explorer.exe`, which removed the
desktop.)

The weather answer is a single sentence about the day you asked for — 今天 by
default, 明天 or 后天 when the sentence says so (the forecast day's condition
plus its high and low; 明天/后天 sentences carry no 「现在 X 度」, which is only
meaningful about today). The city comes from the sentence when you name one —
including the spoken 「明天佛山的天气怎么样」 form; otherwise it is `weather.city`
from the config — and if the name is not a place the provider recognises
(「外面天气」), the configured city answers instead.

Search routing: `查一下天气` / `今天天气怎么样` reach the weather tool, while
`搜索天气` / `搜一下天气` / `百度一下天气` / `用浏览器搜索天气` open the browser —
an explicit search verb says which tool you want. A search word inside a path is
part of the file name: `运行脚本 search.py` runs that script. An explicit search
asks first (「你要我搜索量子力学吗？确认请说确认，取消请说取消。」) instead of
opening a window unasked; say 「取消」 and nothing opens. (`tools.search_web_confirm:
false` restores the old immediate behaviour for the voice path.)

**The confirmation protocol.** `写入文件` and `运行脚本` — the actions that change
your machine — and explicit web searches are announced before they run and wait
for your answer **without a wake word**: the assistant asks (e.g. 「我将要写入一个
文件，确认请说确认，取消请说取消。」), and you say 「确认」 to proceed or 「取消」
to stop. The pending request expires after 15 seconds of silence
(`tools.confirm_timeout_s`), and any sentence that is neither confirmation nor
cancellation counts as "changed my mind" — the request is dropped and that
sentence is handled as a new command. A pending request is bound to the speaker
who made it: a guest saying 「确认」 cannot release the owner's file write.
Snapshots are still taken before confirmed writes and restored if the tool fails.

**File creation without dictation.** 「帮我在桌面建立一个txt文件」 creates an
empty `新建.txt` on your real Desktop — spoken folder words (桌面/下载/文档/图片/
音乐/视频) are mapped onto the actual folders, and no content means 新建文本文档.
If the target file already exists and you dictated nothing, the assistant refuses
(「这个文件已经存在，请说清楚要写入什么内容。」) rather than wiping it.

**Power actions** (`关机`/`重启`/`睡眠`/`休眠`/`锁屏`/`注销`) are owner-only and
confirmation-gated like writes: the assistant announces the action (「我将要关机，
确认请说确认，取消请说取消。」) and waits. Shutdown and restart carry a 5-second
buffer — `shutdown /a` aborts within it. A guest's 「确认」 is refused at the tier
check, and the agent tier can never supply the confirmation over MCP, so no model
can power the machine down on its own.

**Questions and small talk** (`什么是量子力学`, `为什么会下雨`, `你好`, `再见`) are
answered in one or two short spoken Chinese sentences by the local model — with
**no tool capability**: a question cannot open a browser, touch a file or run
anything. Greetings (`你好`/`谢谢`/`再见`) are answered instantly without a model.
If the LLM server is unreachable or too slow (`llm.ask.timeout_s`, default 20 s),
the assistant says 「这个问题我现在答不上来。」 instead of falling silent. Answers
are cut to `llm.ask.max_chars` (80) and stripped of English words and formatting,
because the TTS speaks Chinese only.

**「没事了」** — after the wake word, saying 「没事了」/「算了」/「退下」 (or
「nevermind」) acknowledges with 「好的。」 and goes back to waiting for the wake
word, running nothing.

Anything else outside the tools above is **not** handled — see
[Known limitations](#known-limitations).


### Development commands

```powershell
# All tests (unit + integration + e2e)
python -m pytest tests/ -q

# Only the fast unit tests
python -m pytest tests/unit -q

# Exercise every audio engine against the real installed models
python scripts/smoke_test_models.py

# Type checking
mypy winvoice

# Lint
ruff check winvoice
```

---

## Configuration

Configuration lives in `config/config.yaml`.

```yaml
audio:
  input_device: default
  sample_rate: 16000
  half_duplex: true          # ASR/VAD paused during TTS; KWS stays active
  kws_during_tts: true       # wake word can interrupt playback

kws:
  model: models/kws/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20
  keywords:
    - "assistant"            # English words are matched by English phonemes
    - "小助手"                # Chinese keywords use the model's pinyin tokens:
    - "你好助手"              # far more forgiving for a Chinese accent
  threshold: 0.25            # lower = more sensitive, more false wakes
  use_int8: true             # false = fp32 encoder: more accurate, ~2.5x CPU

vad:
  model: models/vad/silero_vad_v5.onnx
  min_silence_ms: 500
  min_speech_ms: 250

asr:
  model: models/asr/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/model.int8.onnx
  tokens: models/asr/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17/tokens.txt
  language: auto
  use_itn: true              # spoken numbers become digits, e.g. 百分之十 -> 10%

sv:
  model: models/sv/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx
  enabled: true
  profiles_dir: models/sv/profiles
  threshold_high: 0.60       # provisional; calibrate after enrollment
  threshold_low: 0.40        # provisional; calibrate after enrollment
  max_inter: 0.45            # estimated impostor similarity (see Permission model)
  offset_high: 0.05          # T_high = min_intra - offset_high
  offset_low: 0.05           # T_low  = max_inter + offset_low
  min_gap: 0.05              # T_high must exceed T_low by at least this
  adaptive_update: true
  update_weight: 0.05
  anchor_check_days: 30

llm:
  local:
    base_url: http://localhost:8080/v1   # llama-server (see Quick start)
    api_key: ollama
    model: qwen2.5-3b-instruct
    confidence_threshold: 0.65

  remote:
    enabled: true
    base_url: https://your-api-endpoint/v1
    api_key: ${REMOTE_API_KEY}
    model: gpt-4o
    guest_allowed: false     # guests cannot trigger cloud calls

tts:
  # Output audio: Matcha 22 050 Hz, with the 8 kHz VITS as the fallback.
  model: models/tts/matcha-icefall-zh-baker
  fallback_models:
    - models/tts/vits-icefall-zh-aishell3
  backend: auto                # auto | vits | matcha
  vocoder: ""                  # empty = vocos-22khz-univ.onnx inside the model dir
  voice: default
  guest_voice: guest
  speaker_id: 0
  guest_speaker_id: 1
  speed: 0.9                   # duration ∝ 1/speed; 0.9 ≈ 208 ms/char (4.8 chars/s,
                               # mid-band of natural conversation 4.5-5.5).
                               # Anchors: 1.0 ≈ 193 ms/char (broadcast-y),
                               # 0.8 ≈ 241 (slow), 0.75 ≈ 250 (clearly slow).
                               # Restart to apply
  guest_speed: 0.9             # absolute, not relative: with one speaker, pace is the
                               # only difference left, so it must stay above `speed`
  pitch: 1.0                   # voice pitch as a frequency ratio: 1.0 = the model's
                               # own voice (median F0 276 Hz); 0.9 ≈ 248 Hz, 0.8 ≈
                               # 221 Hz if it reads too high. Done by relabelling
                               # the chunks' sample rate and generating at
                               # speed/pitch, so tempo and pitch stay
                               # independent knobs. Restart to apply
  num_threads: 2

  # Segmentation and pauses. These are what the listener hears: the model
  # contributes no pause of its own (its lexicon has no punctuation and sherpa's
  # silence_scale measured as inert), so every value here is written as real
  # silence by the playback layer.
  first_chunk_max_chars: 16    # short first chunk → early first sound
  clause_max_chars: 24         # most characters between two audible pauses
  chunk_max_chars: 40          # how far to look for a boundary if 24 has none
  chunk_min_chars: 10          # shorter pieces are merged back
  hard_max_chars: 80           # a run with no punctuation is only broken past this
  reply_max_chars: 240         # an agent's answer (tool one-liners keep 80)
  tail_silence_ms: 150
  pause_sentence_ms: 240       # 。   (？ 280 ！ 260 …… 420 ； 200)
  pause_comma_ms: 140          # ，   (、 100 ： 180   换行 420)
  pause_forced_ms: 60          # a cut with no punctuation at all
  pause_conjunction_ms: 90     # a cut before 但是 / 所以 / 然后 …
  trim_silence: true           # each synthesis call carries edge noise (60–110 ms here)
  trim_ratio: 0.02             # threshold = peak × this (above every measured floor)
  trim_guard_ms: 30
  max_internal_gap_ms: 60      # the model also pauses mid-sentence wherever it likes
  internal_gap_keep_ms: 30     # (1–7 holes of 40–240 ms); these cap them (0 = off)

audio:
  output_device: default
  output_sample_rate: 0        # 0 = the device's own rate (44100 here)
  output_host_api: auto        # auto | wasapi | default — auto prefers WASAPI over MME
  output_prebuffer_ms: 250     # buffered before the first sound
  output_blocksize_ms: 20

tools:
  whitelist:
    - open_app
    - close_app
    - set_volume
    - media_control
    - search_web
    - read_file
    - list_dir
    - write_file
    - run_script
    - system_power
    - get_time
    - get_weather
  guest_denied:
    - read_file
    - list_dir
    - write_file
    - run_script
    - system_power
  destructive:
    - write_file
    - run_script
  confirm_required: true       # destructive tools wait for a spoken 确认
  confirm_timeout_s: 15        # a pending request expires after this much silence
  search_web_confirm: true     # explicit searches ask 确认 first (voice path only)

weather:
  enabled: true               # false → 「天气查询没有打开。」
  city: 北京                   # used when the sentence names no city
  timeout_s: 5                # wttr.in needs no API key; keep this small

snapshot:
  enabled: true
  path: snapshots/
  max_size_gb: 10

context:
  enabled: true
  scope: session             # cleared on sleep
  max_turns: 10
```

---

## Permission model

| Capability | Full | Guest | Rejected |
|---|---|---|---|
| Cloud API | ✅ | ❌ | ❌ |
| Queries (time/weather/search) | ✅ | ✅ | ❌ |
| Media control | ✅ | ✅ | ❌ |
| Open app | ✅ | Non-sensitive apps only | ❌ |
| Read file | ✅ | ❌ | ❌ |
| Destructive actions | ✅ + confirm | ❌ | ❌ |
| TTS voice | Default | Guest voice | — |

Speaker score determines the tier:

```
score >= T_high          → full
T_low <= score < T_high  → guest
score < T_low            → rejected
```

**Threshold computation** (performed at enrollment):

```
T_high = min_intra - 0.05
T_low  = max_inter + 0.05
```

- `min_intra`: minimum pairwise cosine similarity across your 8 enrollment samples
- `max_inter`: similarity to the most similar non-target speaker, estimated from AISHELL-3 / CN-Celeb samples

Reference ranges: `T_high` ≈ 0.55–0.65, `T_low` ≈ 0.35–0.45. Actual values depend on your own data.

**Drift handling**:
- 3 consecutive verification failures → prompt for password/phrase fallback
- Audio device change detected → lower threshold by 0.05, prompt to re-enroll
- Every 30 days, anchor check; if similarity < 0.6, reset and prompt re-enrollment

---

## Tool allowlist

The LLM cannot generate shell commands. It can only invoke the tools below. All arguments are validated against a Pydantic schema; failures are rejected outright.

| Tool | Arguments | Destructive | Guest |
|---|---|---|---|
| `open_app` | `app: str` — id or Chinese name, fuzzy-matched | ❌ | Non-sensitive only |
| `close_app` | `app: str` — as above; `force: bool` optional | ❌ | Non-sensitive only |
| `set_volume` | `delta: int` (relative) **or** `level: int` 0–100 (absolute) | ❌ | ✅ |
| `media_control` | `action: Enum[play, pause, next, prev]` | ❌ | ✅ |
| `search_web` | `query: str` | ❌ | ✅ |
| `read_file` | `path: str` (under `C:\Users\<you>\`) | ❌ | ❌ |
| `list_dir` | `path: str` (optional, same confinement; defaults to the user directory) | ❌ | ❌ |
| `write_file` | `path: str` (folder words understood: 桌面/下载/文档/图片/音乐/视频), `content: str` **optional** — no content creates an empty file (新建文本文档); an existing file is never truncated without dictated content | ✅ | ❌ |
| `run_script` | `path: str` (`.py` / `.ps1` / `.bat` / `.cmd`) | ✅ | ❌ |
| `system_power` | `action: Enum[shutdown, restart, sleep, hibernate, lock, signout]` | ❌ | ❌ |
| `get_time` | — | ❌ | ✅ |
| `get_weather` | `city: str` (optional; defaults to `weather.city`) | ❌ | ✅ |

**Speaker tiers are enforced at the tool layer.** `guest` may not read, write or
run anything (`tools.guest_denied`), and `rejected` may run nothing at all. The
tier is carried on the `ToolCall` and, for calls arriving from the agent, through
the in-flight utterance record — so enabling DSH does not widen anyone's access.

**Destructive flow**: spoken confirmation → snapshot target files → execute.
The call is announced (「我将要写入一个文件，确认请说确认，取消请说取消。」) and the
microphone listens for the answer **without a wake word**: 「确认」 replays the
same call, 「取消」 drops it, any other sentence cancels it and is handled as a
new command, and 15 seconds of silence expires it (`tools.confirm_timeout_s`).
A guest cannot confirm the owner's pending request. Explicit web searches use
the same protocol when `tools.search_web_confirm` is true (default).

**Result verification**: the five tools with an observable postcondition are
checked against the machine after they run (see
[Agent layer](#agent-layer-optional-deepseek-harness)). The verdict decides
retry/escalation; it never rewrites the tool's own message.

**Snapshot**: only backs up target files declared by the tool as modified, stored under `snapshots/{timestamp}/`. Registry changes, software uninstalls, and system-level modifications are not covered.

**Spoken output**: `ToolResult.message` is what the assistant says, and it must
be plain Chinese — the TTS lexicon has no Latin entries and drops English words
silently. A successful `message` is spoken too (that is how `get_time` and
`get_weather` answer); a message containing Latin letters is refused and the
generic 「好的，已为您完成。」 is used instead.

How a reply is spoken, in the order the stages run:

1. **normalize** — markdown out, units spoken («25℃» → «25摄氏度», «45%» →
   «百分之45»), ASCII punctuation turned into the Chinese marks that mean a
   pause, decimals and clock times protected («3.5», «15:30»), fragments with
   nothing pronounceable dropped;
2. **segment** — cut at sentence ends, then at commas when a run grows past
   `clause_max_chars`, then before a conjunction, and never inside a number, a
   bracket pair, an attributive (`…的`) or before a trailing particle (`…了`);
3. **chunk** — the first piece aims at ≤ `first_chunk_max_chars` (so sound starts
   early), later pieces at `clause_max_chars`;
4. **synthesise** — one `generate()` per piece, on a worker thread, with the
   model's own 80–250 ms of edge noise trimmed off and its mid-sentence holes
   (1–7 silences of 40–240 ms, placed wherever the model pleases — its lexicon
   has no punctuation) collapsed to at most 30 ms;
5. **play** — one device stream for the whole session, at the device's own sample
   rate, with each piece's pause written as real silence and every segment
   converted to that rate in **one** resample call (per-chunk resampling left a
   20–50 %-of-peak amplitude step at every 100 ms seam — a tick inside every
   sustained vowel, heard as the sentence shattering).

Budgets: a tool one-liner keeps the 80-character contract limit; an agent's
answer gets `tts.reply_max_chars` (240). At the configured pace (`tts.speed`
0.9 ≈ 208 ms per character) that is ~50 s with the default 22 kHz voice and
~79 s with the 8 kHz fallback (which also inherits `tts.speed`), and the
wake word interrupts it — lower `tts.reply_max_chars` if that is too much.

Two scripts exist for this, and neither needs an audio device:
`python scripts/show_segmentation.py "文本"` prints the cut points and pauses for
any reply, and `python scripts/calibrate_tts_pauses.py` measures the model
currently configured (edge silence, speech rate, natural pauses).

---

## Roadmap

Ordered by uncertainty. Skipping steps is not recommended.

- [ ] **Validate intent classifier accuracy** (keyboard input instead of voice) ← highest risk
- [ ] Audio pipeline: KWS → VAD → ASR → print text
- [ ] Ollama integration: text → reply → TTS
- [ ] Execution layer: three non-destructive tools first
- [ ] Cloud routing
- [ ] Speaker verification + confirmation + snapshot
- [ ] Guest mode + voice switching
- [ ] PySide6 UI

If step 1 fails, the architecture changes. It comes first for that reason.

---

## Open decisions

The following are **not yet decided**. They are documented here so they aren't silently assumed.

1. **Final intent enum**: the list of ≤ 15 intents has not been fixed. It will be derived from the validation in Roadmap step 1.
2. **Final tool allowlist**: the current table is a working draft. The set will be trimmed once real usage patterns are observed.
3. **Sensitive app list**: which apps guests cannot open is not defined.
4. **Password / phrase fallback**: the mechanism for voiceprint bypass (after repeated failures) is not implemented or designed.
5. **Snapshot scope**: whether to extend beyond file-level (e.g., registry exports) is undecided.
6. **Completion criteria**: no formal acceptance criteria (wake rate, false-wake rate, end-to-end latency) have been set.

---

## Known limitations

| Limitation | Notes |
|---|---|
| Question answering is offline-only and single-turn | Questions route to the local model with no tools; there is no conversation memory, and a question the small model cannot answer gets 「这个问题我现在答不上来。」. The assistant starts the LLM server itself (`llm.local.auto_start`, default on) and reuses one you started manually |
| Weather needs the internet | `get_weather` queries `wttr.in` (no API key) with a `weather.timeout_s` deadline covering the whole request (5 s by default, and the answer is silent until it arrives). Offline or timed out it says 「暂时查不到天气。」; `weather.enabled: false` disables it entirely. Conditions come from the tool's own Chinese table because `lang=zh` returns English descriptions |
| Agent needs the bridge installed | The optional DSH tier only has tools after `python scripts/install_dsh_bridge.py --install`; without it the agent can chat but cannot act (`deployment.md` §2.4) |
| The agent's *output* is not streamed | The turn is a task now, so a wake word during planning is acted on immediately (the abandoned turn's reply is dropped). What cannot happen yet is speaking the answer while the model is still writing it: DSH returns the response whole, so the first sound waits for the whole reply plus its first sentence's synthesis |
| Guest voice is only a pace | The default Matcha model has a single speaker, so a guest hears the same voice at `tts.guest_speed` (1.0 — faster than the owner's 0.9). Swap in a multi-speaker model (e.g. `vits-zh-hf-fanchen-C`, 16 kHz, 187 speakers) to get a genuinely different voice |
| Verification covers 5 of 12 tools | `get_time`, `get_weather`, `read_file`, `list_dir`, `search_web`, `media_control` and `system_power` have no observable postcondition worth checking (the machine is asleep, the browser is the user's, a power action is fire-and-forget), so they have no verifier and therefore no escalation signal |
| `search_web` cannot verify its target | The ask-first confirmation (「你要我搜索…吗？」) is voice-path only — the agent's own `search_web` over MCP still opens the browser directly — and "did the right page open" is not machine-checkable, so it has no verifier |
| Force-close is opt-in | `taskkill /f` skips the application's own save prompt, so it is only used for an explicit 「强制关闭 X」. A plain 「关闭 X」 waits for the app to decide, which means it can appear to hang while the app asks you to save |
| No window-level app control | There is no tool for "minimise/close *this window*" of an arbitrary app. Explorer is the only case with window-level handling, precisely because killing its process destroys the desktop; other apps are closed as a whole |
| Speech is Chinese-only | Both TTS models' lexicons contain no Latin entries and digits are expanded by `number.fst`/`date.fst`/`phone.fst`. Text handed to TTS must be spoken Chinese; English words are dropped silently (`OOV ... Ignore it!`) |
| TTS pauses are configuration, not the model | The lexicon has no punctuation at all and sherpa's `silence_scale` measured as inert, so the rhythm comes entirely from `tts.pause_*` plus `trim_ratio` trimming each call's 80–250 ms of edge noise. Change them in `config.yaml` and preview with `scripts/show_segmentation.py` |
| Guest tier partly enforced | Tool calls now carry the speaker tier and `tools.guest_denied` is applied, so a guest cannot read/write/run. Two gaps remain: `open_app`/`close_app` still treat every app as non-sensitive (there is no sensitive-app list — see Open decisions), and a guest can still trigger the *cloud* tier if `dsh.cloud.guest_allowed` is flipped on |
| Half-duplex | ASR input is paused during TTS; only the wake word can interrupt |
| Snapshot scope | File-level only; registry, uninstall, and system changes are not covered |
| Context | Session-scoped, cleared on sleep; no cross-session memory |
| No AEC | Echo is avoided via half-duplex; full-duplex is not implemented |
| Local LLM | Below 7B, tool-calling accuracy is insufficient; do not downgrade |
| Windows only | Execution layer depends on pywin32 / pyautogui |

The tracked backlog of unimplemented interaction features — with the constraints and
technical detail needed to add each one — lives in [`UNIMPLEMENTED.md`](UNIMPLEMENTED.md).
Read it before building anything user-visible.

---

## Design Decisions (from Grilling Session)

> **This section is a historical record of the design session, kept verbatim.** Several
> items were later changed during implementation. **What actually shipped:**
>
> | Decision as recorded below | As built |
> |---|---|
> | Local LLM: Ollama + Qwen2.5 **7B** | llama.cpp **`llama-server`** + `qwen2.5-3b-instruct` (Ollama works only as an alternative endpoint) |
> | Confidence threshold **0.70** | **0.65** (`llm.local.confidence_threshold`) |
> | GBNF via `llama-server -mgf grammar.gbnf` | GBNF sent **per request** in the `grammar` field, with a `json_object` fallback |
> | Hot-reload via `watchdog` → ZeroMQ PUB/SUB | `watchdog` re-reads the config object; **no PUB/SUB, engines do not pick it up — restart to apply** |
> | TTS: Piper / Kokoro | sherpa-onnx VITS `vits-icefall-zh-aishell3` (Chinese, 8 kHz) — **2026-09-26:** the default is now Matcha `matcha-icefall-zh-baker` (22.05 kHz) with this model kept as the fallback |
> | Enrollment UI: histogram + sliders | CLI derives thresholds and prints the band; **no UI** |
> | 4 processes + PySide6 UI | Single asyncio process, no GUI |
> | Metrics: Prometheus pushgateway | `init_metrics()` exists but is **never called** |
>
> `spec.md` describes the system as built, section by section.

### Architecture
- **MVP: Single-process asyncio** — Prototype with `asyncio` + `ThreadPoolExecutor` first; split to 4 processes (Main/Audio/LLM/Execution) only if audio jitter > 20ms or GIL contention measured
- **IPC (post-prototype)**: Audio frames via `shared_memory.SharedMemory` ring buffer (8 slots, 10ms @ 16kHz); control plane via ZeroMQ `PUSH/PULL` + `PUB/SUB` with `orjson` + Pydantic schemas (`schema_version` for compat)

### Intent Routing
- **Three-tier waterfall**: Rules (regex, 0 latency) → Local LLM (Qwen2.5 7B, constrained decoding via llama.cpp GBNF) → Cloud LLM (OpenAI-compatible)
- **Constrained decoding**: llama.cpp server with GBNF grammar; HTTP API guarantees valid JSON; failure retry ≤2 times or >2s → escalate to cloud
- **Confidence threshold 0.70**: To be calibrated with ≥200 real queries in Roadmap Step 1; small model outputs only intent+args, never tool selection

### Speaker Verification
- **Thresholds configurable**: `T_high = min_intra - 0.05`, `T_low = max_inter + 0.05` (offsets configurable)
- **Enrollment UI**: Histogram + suggested band + manual sliders → writes final `threshold_high/low` to config
- **Adaptive update**: EMA with `update_weight=0.05` on successful verification; anchor check every 30 days

### Tool System
- **Irreversible ops blocked**: Registry edits, software uninstall, system config changes rejected via `IRREVERSIBLE_PATTERNS` regex scan
- **`run_script`**: Content scanned pre-execution; matches → reject + audit log
- **`write_file`**: Restricted to `C:\Users\<user>\` subtree
- **Destructive flow**: Double confirmation → snapshot declared `modified_paths` → execute → auto-restore on failure

### Audio Pipeline
- **KWS barge-in**: Immediate TTS interrupt on KWS trigger; `tts_stream.stop()` + silence frames to drain DMA; no cooldown
- **Half-duplex**: ASR/VAD paused during TTS; KWS stays active

### LLM Backends
- **Local whitelist**: Only `qwen2.5:7b-instruct`, `qwen2.5:14b-instruct`, etc. (configurable); <7B → direct cloud fallback
- **Cloud fallback**: Dual trigger — local unavailable ∨ confidence < threshold → cloud; cloud also fails → "暂时无法处理"

### Configuration & Observability
- **Env var expansion**: `${VAR}` via `os.path.expandvars`; missing → explicit error with field path
- **Hot-reload**: `watchdog` → ZeroMQ PUB/SUB; allowlist includes `llm.local.confidence_threshold`, `tools.whitelist`, `tts.voice`, and the whole segmentation/pause table (`tts.chunk_*`, `tts.pause_*`, `tts.reply_max_chars` — read per utterance, so prosody is tunable without a restart)
- **Structured logging**: `structlog` + JSON Lines, `trace_id` propagated end-to-end (KWS trigger → UUID)
- **Metrics**: Prometheus pushgateway (Main `/metrics` + children push every 10s)
- **Model integrity**: Startup quick `size+mtime` check; mismatch → full SHA256 → corrupt backup to `.corrupt/` + modal "Auto-repair" dialog

### Testing Strategy
- **Unit**: Pure logic (intent, tool validation, SV scoring) — `pytest -m unit` (CI required, <30s)
- **Integration**: Audio pipeline with synthetic WAV → `pytest -m integration` (requires `models/`)
- **E2E**: Real mic/models/network — `pytest -m manual` (local only)

---

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.10+ |
| Wake word | sherpa-onnx Zipformer KWS |
| VAD | sherpa-onnx Silero-VAD |
| ASR | SenseVoice / Zipformer streaming |
| Speaker verification | 3D-Speaker CAM++ (ONNX) |
| TTS | sherpa-onnx Matcha (zh, icefall baker, 22.05 kHz) + vocos vocoder; VITS aishell3 (8 kHz) as fallback |
| Local LLM | Ollama + Qwen2.5 7B Instruct |
| Cloud LLM | Any OpenAI-compatible API |
| Validation | Pydantic |
| Desktop automation | pyautogui + pywin32 + subprocess |
| UI | PySide6 |
| IPC | multiprocessing.Queue / ZeroMQ |

---

## Contributing

Early-stage project. Interfaces are unstable. Before filing an issue, describe your use case and what you have already tried.

---

## License

MIT

Third-party dependencies and model weights are governed by their own licenses.

---

## Acknowledgements

- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)
- [Ollama](https://ollama.com/)
- [3D-Speaker](https://github.com/modelscope/3D-Speaker)
