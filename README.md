# Windows Voice Assistant

A Windows voice assistant built on [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx). Local-first, with pluggable local (Ollama) and remote (OpenAI-compatible) LLM backends. Supports speaker verification, wake word detection, and desktop automation.

> **Status**: Early development. Architecture is settled; features are being implemented incrementally. See [Roadmap](#roadmap).

Repository: https://github.com/luoxiaguwu9394/windows_voice_assistant

---

## Features

- **Wake word detection**: sherpa-onnx Zipformer KWS, always-on, custom keywords without retraining; English or Chinese wake words (`assistant` / `小助手` / `你好助手`), tunable sensitivity
- **Speaker verification**: CAM++ embeddings, three-tier permission model (full / guest / reject), adaptive updates at runtime
- **Local ASR**: SenseVoice / Zipformer streaming, multilingual
- **Local TTS**: sherpa-onnx VITS (Chinese, icefall aishell3, 174 speakers) with a separate guest voice
- **Dual LLM backends**: local Ollama and any OpenAI-compatible remote API, routed by a fixed escalation chain
- **Three-tier intent routing**: rules → local small model → cloud, balancing latency and accuracy
- **Tool allowlist**: the LLM can only invoke registered tools; it cannot generate shell commands
- **Destructive action protection**: double confirmation plus file-level snapshot rollback
- **Half-duplex with limited barge-in**: KWS stays active during TTS playback; the wake word can interrupt

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
└──────────────────┬──────────────────────┘
                   │ structured command
                   ▼
┌─────────────────────────────────────────┐
│ Execution process (tool allowlist)        │
│  Non-destructive → execute directly       │
│  Destructive     → confirm + snapshot     │
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

### Requirements

- Windows 10 / 11
- Python 3.10+
- (Optional) NVIDIA GPU for local LLM acceleration
- (Optional) [Ollama](https://ollama.com/) for local LLM

### Install

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
python scripts/download_models.py --tts piper-zh
python scripts/download_models.py --sv campplus
```

Models are downloaded to `models/` by default; paths are configurable.

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
| `打开记事本` · `打开计算器` · `打开资源管理器` | `搜索今天新闻` · `查一下北京天气` | `读取文件 <path>` |
| `关闭记事本` (see the allowlist below) | ⚠️ opens your browser immediately, no confirmation | `写入文件 <path> 内容 …` · `运行脚本 <path>` |

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

Anything outside these eight tools is **not** handled — see
[Known limitations](#known-limitations). Questions ("what is X", "what's in
this folder") are not answered: the assistant routes commands, it does not chat.
`write_file` and `run_script` are listed above but currently refuse every
request: the confirmation round trip they depend on does not exist yet.

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
  model: models/tts/vits-icefall-zh-aishell3
  voice: default
  guest_voice: guest

tools:
  whitelist:
    - open_app
    - close_app
    - set_volume
    - media_control
    - search_web
    - read_file
    - write_file
    - run_script
  guest_denied:
    - read_file
    - write_file
    - run_script
  destructive:
    - write_file
    - run_script
  confirm_required: true

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
| `close_app` | `app: str` — as above | ❌ | Non-sensitive only |
| `set_volume` | `delta: int` (relative) **or** `level: int` 0–100 (absolute) | ❌ | ✅ |
| `media_control` | `action: Enum[play, pause, next, prev]` | ❌ | ✅ |
| `search_web` | `query: str` | ❌ | ✅ |
| `read_file` | `path: str` (under `C:\Users\<you>\`) | ❌ | ❌ |
| `write_file` | `path: str, content: str` | ✅ | ❌ |
| `run_script` | `path: str` (`.py` / `.ps1` / `.bat` / `.cmd`) | ✅ | ❌ |

**Destructive flow**: double confirmation → snapshot target files → execute.
**Not yet wired**: the confirmation round trip does not exist, so `write_file`
and `run_script` currently refuse every request instead of executing — see
[Known limitations](#known-limitations).

**Snapshot**: only backs up target files declared by the tool as modified, stored under `snapshots/{timestamp}/`. Registry changes, software uninstalls, and system-level modifications are not covered.

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
| No question answering | No chat/QA path: unknown requests, `get_time` and `get_weather` reply "抱歉，这个请求我还没有实现。"  Only the eight tools in the allowlist above are handled |
| No directory listing | There is no `list_dir` tool; "what is in this folder" cannot be answered |
| Confirmation not wired | `write_file` and `run_script` are registered but the double-confirmation round trip is not implemented, so every call is refused |
| `search_web` is immediate | It opens the browser the moment the intent is classified — no confirmation, and a misrouted question will pop a browser window |
| Speech is Chinese-only | The TTS lexicon contains no Latin entries and digits are expanded by `number.fst`/`date.fst`/`phone.fst`. Text handed to TTS must be spoken Chinese; English words are dropped silently (`OOV ... Ignore it!`) |
| Guest tier not enforced | The `guest_denied` config is declared but no caller applies it; tool calls are validated as `full` |
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
> | TTS: Piper / Kokoro | sherpa-onnx VITS `vits-icefall-zh-aishell3` (Chinese, 8 kHz) |
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
- **Hot-reload**: `watchdog` → ZeroMQ PUB/SUB; allowlist: `llm.local.confidence_threshold`, `tools.whitelist`, `tts.voice`, etc.
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
| TTS | sherpa-onnx VITS (zh, icefall aishell3, 8 kHz) |
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
