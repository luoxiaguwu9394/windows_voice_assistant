# Windows Voice Assistant — Technical Specification

**Version**: 0.1.0-draft
**Status**: Design consensus from grilling session
**Last Updated**: 2025-01-16

---

## 1. Overview

A local-first Windows voice assistant built on **sherpa-onnx** with pluggable LLM backends (Ollama local, OpenAI-compatible remote). Features wake-word detection, speaker verification, streaming ASR/TTS, three-tier intent routing, tool allowlist, and destructive-action protection with snapshots.

**Target**: Single-user Windows 10/11 desktop. No installer, no auto-update, no telemetry in MVP.

---

## 2. Architecture

### 2.1 Process Model (MVP: Single Process)

| Phase | Approach |
|-------|----------|
| **Prototype** | Single process, `asyncio` + `ThreadPoolExecutor` for blocking calls (KWS/VAD/ASR/TTS/LLM). Measure end-to-end latency & jitter. |
| **Split trigger** | If audio thread jitter > 20 ms or GIL contention measurable → split into 4 processes: Main (UI), Audio, LLM, Execution. |

### 2.2 Future Multi-Process IPC (Post-Prototype)

| Channel | Transport | Payload |
|---------|-----------|---------|
| Audio frames | `shared_memory.SharedMemory` ring buffer (8 slots, 10 ms @ 16 kHz) + `Semaphore` | Raw PCM `int16` |
| Control plane | ZeroMQ `PUSH/PULL` + `PUB/SUB` | `orjson`-serialized Pydantic models (`winvoice/contracts/`) |

**Message schema**: every message carries `schema_version: int`; consumers use `pydantic` `model_validator(mode='before')` for forward compatibility.

---

## 3. Audio Pipeline

```
Microphone (16 kHz, mono)
   │
   ▼
┌─────────────────────────────────────┐
│ KWS (Zipformer, always-on)          │
│   keywords: "assistant", "hey assistant"            │
│   threshold: 0.25 (config)          │
└─────────────┬───────────────────────┘
              │ trigger
              ▼
┌─────────────────────────────────────┐
│ Speaker Verification (CAM++)        │
│   embeddings → cosine similarity    │
│   thresholds: T_high, T_low         │
└─────────────┬───────────────────────┘
              ▼
┌─────────────────────────────────────┐
│ VAD (Silero)                        │
│   min_silence_ms: 500               │
│   min_speech_ms: 250                │
└─────────────┬───────────────────────┘
              ▼
┌─────────────────────────────────────┐
│ ASR (SenseVoice streaming)          │
│   language: auto                    │
└─────────────┬───────────────────────┘
              │ text
              ▼
```

**Half-duplex**: ASR/VAD paused during TTS playback. **KWS stays active** and can interrupt TTS immediately (barge-in).

**Barge-in implementation**: Audio process holds `tts_stream`; on `InterruptTTS` → `tts_stream.stop()` + write silence frames to drain DMA.

---

## 4. Speaker Verification

### 4.1 Enrollment
- 8 samples, 3–5 s each, varied content/volume/distance
- Compute pairwise cosine similarities → `min_intra`
- Estimate `max_inter` from AISHELL-3 / CN-Celeb (offline script)
- `T_high = min_intra - 0.05` (configurable offset)
- `T_low  = max_inter + 0.05` (configurable offset)
- If `min_intra < 0.4` → prompt re-record

### 4.2 Runtime Verification
| Score | Tier | Capabilities |
|-------|------|--------------|
| `≥ T_high` | Full | Cloud API, all tools, destructive actions (with confirm) |
| `T_low ≤ s < T_high` | Guest | Local queries, media control, non-sensitive apps, **no cloud, no file/write/script** |
| `< T_low` | Rejected | None |

### 4.3 Adaptive Update
- Enabled by default: `update_weight: 0.05` on successful verification
- Anchor check every 30 days; if similarity < 0.6 → reset + re-enroll
- 3 consecutive failures → password/phrase fallback (not implemented yet)

### 4.4 UI for Threshold Tuning
Enrollment wizard final step: histogram of intra/inter scores + suggested `[T_low, T_high]` band + manual sliders → writes final `threshold_high` / `threshold_low` to `config.yaml`.

---

## 5. Intent Routing (Three-Tier Waterfall)

```
① Rules (regex + keyword tables)
    └─> 0 latency, high-frequency commands (open/close app, volume, media, time, weather)

② Intent Classifier (local small model)
    ├─ Model: Qwen2.5 7B Instruct (Ollama)
    ├─ Constrained decoding: GBNF grammar via llama.cpp server
    ├─ Output: {"intent": "...", "args": {...}} — **no tool selection**
    ├─ Confidence threshold: 0.70 (to be calibrated with ≥200 real queries)
    └─ < 0.70 or allowlist miss → escalate to ③

③ Cloud LLM (OpenAI-compatible)
    ├─ Only for ② failures or allowlist misses
    ├─ Guest tier: cloud disabled
    └─ Same structured output contract
```

**Hard constraint**: small model **never selects tools**. Tool selection & argument filling happen in code.

**Failure handling**: constrained decode retry ≤ 2 times or > 2 s → mark `needs_cloud: true`.

---

## 6. Tool Allowlist & Execution

### 6.1 Tools

| Tool | Arguments | Destructive | Guest |
|------|-----------|-------------|-------|
| `open_app` | `app: Enum[...]` | ❌ | Non-sensitive only |
| `close_app` | `app: Enum[...]` | ❌ | Non-sensitive only |
| `set_volume` | `delta: int` | ❌ | ✅ |
| `media_control` | `action: Enum[play,pause,next,prev]` | ❌ | ✅ |
| `search_web` | `query: str` | ❌ | ✅ |
| `read_file` | `path: str` | ❌ | ❌ |
| `write_file` | `path: str, content: str` | ✅ | ❌ |
| `run_script` | `path: str` | ✅ | ❌ |

### 6.2 Destructive Action Flow
1. Double confirmation (voice + UI toast)
2. Snapshot target files (declared in `modified_paths: string[]`)
3. Execute
4. On failure → auto-restore from snapshot

### 6.3 Irreversible Operation Blocklist
- `run_script` content scanned for `IRREVERSIBLE_PATTERNS` (reg add/delete, msiexec /uninstall, etc.) → reject + audit log
- `write_file` restricted to `C:\Users\<user>\` subtree
- Registry, software uninstall, system config changes **never allowed**

---

## 7. LLM Backends

### 7.1 Local (Ollama)
- **Allowed models**: `qwen2.5:7b-instruct`, `qwen2.5:14b-instruct`, ... (configurable whitelist)
- **Base URL**: `http://localhost:11434/v1`
- **API Key**: `ollama`
- **Constrained decoding**: `llama.cpp` server with GBNF grammar (`llama-server -mgf grammar.gbnf`)

### 7.2 Remote (OpenAI-compatible)
- Enabled via `llm.remote.enabled: true`
- `base_url`, `api_key` (from `${REMOTE_API_KEY}` env var), `model`
- **Guest tier**: `guest_allowed: false` (default)

### 7.3 Routing Logic
```
if local_unavailable or local_confidence < threshold:
    if remote_enabled and (tier == Full or remote.guest_allowed):
        route_to_cloud()
    else:
        reply("暂时无法处理")
```

---

## 8. TTS

| Engine | Model | Voice |
|--------|-------|-------|
| Piper | `piper-zh` | `default` (Full), `guest` (Guest) |
| Kokoro | (future) | — |

**Interruptible**: see §3 barge-in.

---

## 9. Configuration

### 9.1 File: `config/config.yaml`
Full schema in README. Key points:
- `${VAR}` syntax → `os.path.expandvars` at load; missing var → explicit error with field path
- Hot-reloadable fields (via `watchdog` → ZeroMQ PUB/SUB):
  - `llm.local.confidence_threshold`
  - `tools.whitelist`
  - `tts.voice`
  - `kws.threshold` (requires Audio restart → logged warning)
- Non-hot-reload changes → warning + "needs restart" toast

### 9.2 Model Manifest
`scripts/download_models.py` embeds:
```python
MANIFEST = {
    "kws/zipformer-zh-en": {"url": "...", "sha256": "...", "size": 123456},
    ...
}
```
- Download to `models/.tmp/<name>.part` → verify SHA256 → atomic `os.replace`
- Resume via HTTP Range requests
- Startup integrity: quick `size+mtime` check; mismatch → full SHA256 → corrupt backup to `models/.corrupt/` + modal "Auto-repair" dialog

---

## 10. Logging & Observability

- **Format**: JSON Lines via `structlog` + `orjson` renderer
- **Fields**: `timestamp, level, process, trace_id, span_id, event, **fields`
- **Trace ID**: generated at KWS trigger (`uuid4().hex[:16]`), propagated through all stages
- **Metrics**: Prometheus pushgateway (Main process `/metrics` + child processes push every 10 s)
- **Rotation**: `TimedRotatingFileHandler` daily, retain 7 `.jsonl.gz`
- **Disk quota**: `storage.max_total_gb: 10` (models + logs + snapshots); cleanup order: snapshots → logs → audio cache → models (never)

---

## 11. Testing Strategy

| Layer | Scope | Command | CI |
|-------|-------|---------|----|
| Unit | Pure logic (intent classification, tool validation, SV scoring) | `pytest -m unit` | ✅ Required |
| Integration | Audio pipeline with synthetic WAV → KWS/VAD/ASR output text | `pytest -m integration` (requires `models/`) | ⚠️ Optional |
| E2E | Real mic, real models, real network | `pytest -m manual` | ❌ Manual only |

**Synthetic audio**: `pytest-audio` fixtures inject WAV into Audio process stdin.

---

## 12. Open Decisions (Post-MVP)

- Final intent enum (≤ 15) — derived from Roadmap step 1 validation
- Final tool allowlist — trim after real usage observed
- Sensitive app list for Guest tier
- Password/phrase fallback mechanism
- Snapshot scope extension (registry exports?)
- Acceptance criteria (wake rate, false-wake rate, E2E latency)
- Packaging (Inno Setup), auto-update, crash reporting, offline docs — **deferred per user**

---

## 13. File Structure (Planned)

```
windows_voice_assistant/
├── AGENTS.md
├── config/
│   └── config.yaml
├── docs/
│   ├── agents/
│   │   ├── issue-tracker.md
│   │   └── domain.md
│   └── adr/
├── prototype/                 # single-process asyncio prototype
├── scripts/
│   ├── download_models.py
│   └── analyze_sv_scores.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── e2e/
├── winvoice/
│   ├── contracts/             # Pydantic message models (schema_version=1)
│   ├── audio/                 # KWS, VAD, ASR, SV, TTS
│   ├── llm/                   # local + remote clients, constrained decoding
│   ├── intent/                # rules, classifier, router
│   ├── tools/                 # allowlist, registry, execution, snapshots
│   ├── config.py              # ConfigManager + watchdog + hot-reload
│   ├── logging.py             # structlog setup
│   └── main.py                # entry point
└── models/                    # gitignored, downloaded at runtime
```

---

## 14. Roadmap (Unchanged from README)

1. **Validate intent classifier accuracy** (keyboard input) ← highest risk
2. Audio pipeline: KWS → VAD → ASR → print text
3. Ollama integration: text → reply → TTS
4. Execution layer: three non-destructive tools first
5. Cloud routing
6. Speaker verification + confirmation + snapshot
7. Guest mode + voice switching
8. PySide6 UI

---

*This spec reflects the consensus reached in the grilling session. Items marked "deferred" are explicitly not part of MVP.*