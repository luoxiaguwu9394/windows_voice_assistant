# Windows Voice Assistant

A Windows voice assistant built on [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx). Local-first, with pluggable local (Ollama) and remote (OpenAI-compatible) LLM backends. Supports speaker verification, wake word detection, and desktop automation.

> **Status**: Early development. Architecture is settled; features are being implemented incrementally. See [Roadmap](#roadmap).

Repository: https://github.com/luoxiaguwu9394/windows_voice_assistant

---

## Features

- **Wake word detection**: sherpa-onnx Zipformer KWS, always-on, custom keywords without retraining
- **Speaker verification**: CAM++ embeddings, three-tier permission model (full / guest / reject), adaptive updates at runtime
- **Local ASR**: SenseVoice / Zipformer streaming, multilingual
- **Local TTS**: Piper / Kokoro, with a separate guest voice
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

### Local LLM (Ollama)

```powershell
ollama pull qwen2.5:7b-instruct
ollama serve
```

For constrained decoding (required for intent classifier), run llama.cpp server with GBNF grammar:
```powershell
# Build llama.cpp, then:
llama-server -m qwen2.5-7b-instruct.gguf -mgf grammar.gbnf --port 8080
```

### Speaker enrollment

First run requires recording **8 samples**, 3–5 seconds each. Vary content, volume, and distance to cover real usage conditions.

```powershell
python -m winvoice.enroll --speaker me --samples 8
```

Thresholds `T_high` and `T_low` are computed automatically. If `min_intra < 0.4`, the tool prompts for re-recording.

### Run

```powershell
# Verify every model loads, then exit (no microphone required)
python -m winvoice --check

# Run for real (needs models + a microphone)
python -m winvoice

# Run with model-free stubs (development / plumbing checks)
python -m winvoice --stub-audio
```

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
  model: models/kws/zipformer-zh-en
  keywords:
    - "assistant"
    - "hey assistant"
  threshold: 0.25

vad:
  model: models/vad/silero
  min_silence_ms: 500
  min_speech_ms: 250

asr:
  model: models/asr/sense-voice
  language: auto

sv:
  model: models/sv/campplus
  enabled: true
  threshold_high: 0.60       # provisional; calibrate after enrollment
  threshold_low: 0.40        # provisional; calibrate after enrollment
  adaptive_update: true
  update_weight: 0.05
  anchor_check_days: 30

llm:
  local:
    base_url: http://localhost:11434/v1
    api_key: ollama
    model: qwen2.5:7b-instruct
    confidence_threshold: 0.70

  remote:
    enabled: true
    base_url: https://your-api-endpoint/v1
    api_key: ${REMOTE_API_KEY}
    model: gpt-4o
    guest_allowed: false     # guests cannot trigger cloud calls

tts:
  model: models/tts/piper-zh
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
| `open_app` | `app: Enum[...]` | ❌ | Non-sensitive only |
| `close_app` | `app: Enum[...]` | ❌ | Non-sensitive only |
| `set_volume` | `delta: int` | ❌ | ✅ |
| `media_control` | `action: Enum[play, pause, next, prev]` | ❌ | ✅ |
| `search_web` | `query: str` | ❌ | ✅ |
| `read_file` | `path: str` | ❌ | ❌ |
| `write_file` | `path: str, content: str` | ✅ | ❌ |
| `run_script` | `path: str` | ✅ | ❌ |

**Destructive flow**: double confirmation → snapshot target files → execute.

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
| Half-duplex | ASR input is paused during TTS; only the wake word can interrupt |
| Snapshot scope | File-level only; registry, uninstall, and system changes are not covered |
| Context | Session-scoped, cleared on sleep; no cross-session memory |
| No AEC | Echo is avoided via half-duplex; full-duplex is not implemented |
| Local LLM | Below 7B, tool-calling accuracy is insufficient; do not downgrade |
| Windows only | Execution layer depends on pywin32 / pyautogui |

---

## Design Decisions (from Grilling Session)

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
| TTS | Piper / Kokoro |
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
