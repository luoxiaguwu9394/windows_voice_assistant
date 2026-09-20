# Windows Voice Assistant — Technical Specification

**Version**: 0.1.0-dev
**Status**: As-built — synced with the implementation (`winvoice/`) on 2026-09-19
**Last Updated**: 2026-09-19

> **Status legend used throughout**
> ✅ implemented as written · 🔶 implemented, but differs from the original design (the
> difference is stated inline) · ⛔ designed but **not implemented** yet.
>
> Sections 1–14 describe the system as it is. Section 15 lists what is still missing
> and the known defects, so a divergence is never silent.

---

## 1. Overview

A local-first Windows voice assistant built on **sherpa-onnx**. Wake-word detection,
speaker verification, ASR/TTS, three-tier intent routing, a tool allowlist, and
snapshot-protected destructive actions all run in **one asyncio process**
(🔶 the 4-process split stays a future option — see §2). The intent classifier talks to
a local **llama.cpp `llama-server`**; any OpenAI-compatible endpoint can serve as the
cloud tier.

**Target**: Single-user Windows 10/11 desktop. No installer, no auto-update, no
telemetry in MVP.

---

## 2. Architecture

### 2.1 Process Model (MVP: Single Process)

| Phase | Approach |
|-------|----------|
| **Prototype** ✅ | Single process, `asyncio` + `ThreadPoolExecutor` for blocking calls (KWS/VAD/ASR/TTS/LLM). Measure end-to-end latency & jitter. |
| **Split trigger** ⛔ | If audio thread jitter > 20 ms or GIL contention measurable → split into 4 processes: Main (UI), Audio, LLM, Execution. |

🔶 Current implementation: one `asyncio` loop (`winvoice/audio/pipeline.py`). The
microphone callback pushes 100 ms int16 blocks into a bounded deque; `run()` drives
KWS → SV → VAD → ASR → intent → tools → TTS. No thread pool is used yet — the
sherpa-onnx calls are fast enough on CPU (ASR ≈ 40-50 ms, LLM ≈ 0.8-1.1 s, TTS
synthesis ≈ 100-300 ms) for a single user.

### 2.2 Future Multi-Process IPC (Post-Prototype)

| Channel | Transport | Payload |
|---------|-----------|---------|
| Audio frames | `shared_memory.SharedMemory` ring buffer (8 slots, 10 ms @ 16 kHz) + `Semaphore` | Raw PCM `int16` |
| Control plane | ZeroMQ `PUSH/PULL` + `PUB/SUB` | `orjson`-serialized Pydantic models (`winvoice/contracts/`) |

**Message schema**: every message carries `schema_version: int`; consumers use `pydantic` `model_validator(mode='before')` for forward compatibility.

---

## 3. Audio Pipeline

```
Microphone (16 kHz, mono, int16)  — callback, 100 ms blocks
   │
   ▼
┌─────────────────────────────────────────────┐
│ KWS (Zipformer, always-on)  🔶            │
│   keywords: "assistant" / "小助手" / "你好助手" │
│   threshold: 0.25, use_int8: true (config)  │
└─────────────┬───────────────────────────────┘
              │ trigger
              ▼
┌─────────────────────────────────────────────┐
│ Speaker Verification (CAM++, 192-d)         │
│   last ~100 blocks → embedding → cosine     │
│   thresholds: T_high, T_low                 │
└─────────────┬───────────────────────────────┘
              ▼
┌─────────────────────────────────────────────┐
│ VAD (Silero v5)                             │
│   min_silence_ms: 500                       │
│   min_speech_ms: 250                        │
└─────────────┬───────────────────────────────┘
              ▼
┌─────────────────────────────────────────────┐
│ ASR (SenseVoice int8, offline)  🔶          │
│   language: auto, use_itn: true             │
└─────────────┬───────────────────────────────┘
              │ text
              ▼
```

🔶 **Wake words are bilingual.** English keywords are matched against English
phonemes (`AH0 S IH1 S T AH0 N T`), Chinese ones against the model's pinyin tokens
(`x iǎo zh ù sh ǒu`), which tolerates a Chinese accent far better. Keywords are
compiled into `models/kws/<model>/winvoice_keywords_<sha1>.txt` and cached.

🔶 **ASR is SenseVoice (offline), not streaming.** `AsrEngine` supports a streaming
Zipformer path, but the configured model is the offline SenseVoice one; it recognises
a VAD-delimited segment in one pass. `use_itn: true` is what turns
「百分之十」 into `10%` before intent routing sees it.

**Half-duplex**: ASR/VAD paused during TTS playback. **KWS stays active** and can
interrupt TTS immediately (barge-in).

🔶 **Barge-in implementation** is cooperative, not DMA-level: `TtsEngine.interrupt()`
sets a flag that the synthesis generator checks at each ~100 ms chunk boundary, so
playback stops within one chunk (≈100 ms) rather than draining the audio device.
Speech that *overlapped* the TTS output is not captured — the wake word must be
detected for the user to break in.

---

## 4. Speaker Verification

### 4.1 Enrollment ✅
- 8 samples, ~4 s each. 🔶 The CLI now **shows one line to read per sample**
  (8 built-in prompts rotating through 数字 / 指令 / 闲聊) and previews the next line
  while the previous take is embedded, instead of asking the user to invent content.
- Compute pairwise cosine similarities → `min_intra`
- Estimate `max_inter` from AISHELL-3 / CN-Celeb (offline script)
- `T_high = min_intra - 0.05` (configurable offset)
- `T_low  = max_inter + 0.05` (configurable offset)
- If `min_intra < 0.4` → prompt re-record
- 🔶 If `T_high - T_low < min_gap` (0.05) the profile is **refused, not written** —
  inverted thresholds would promote any score above the lower bar to `full`.

### 4.2 Runtime Verification ✅
| Score | Tier | Capabilities |
|-------|------|--------------|
| `≥ T_high` | Full | Cloud API, all tools, destructive actions (with confirm) |
| `T_low ≤ s < T_high` | Guest | Local queries, media control, non-sensitive apps, **no cloud, no file/write/script** |
| `< T_low` | Rejected | None |

✅ **The tier now reaches the tool layer.** It is carried on `ToolCall.tier` and
applied by `ToolExecutor.execute` → `ToolRegistry.validate_call(tier=…)`, which
has always understood tiers and previously never received one. A `guest` cannot
call `read_file` / `write_file` / `run_script`; `rejected` cannot call anything;
and the read-only queries (`get_time`, `get_weather`) remain available to a guest,
as their `guest_allowed=True` registration always claimed.

🔶 Two gaps remain, both in the README's Known limitations: there is still no
sensitive-app list, so a guest may open any allowlisted app; and a guest can reach
the cloud tier if `dsh.cloud.guest_allowed` is turned on.

🔶 **The tier crosses a process boundary.** Because the DSH agent runs tools in a
process DSH spawns, the audio process publishes the in-flight tier to
`runtime/utterance.json` (`winvoice/tools/utterance.py`) and the MCP server reads
it per call. Without it, enabling the agent would have bypassed the permission
model instead of using it.

### 4.3 Adaptive Update ✅
- Enabled by default: `update_weight: 0.05` on successful verification
- Anchor check every 30 days; if similarity < 0.6 → reset + re-enroll ⛔ (the config
  key `sv.anchor_check_days` exists, nothing schedules the check)
- 3 consecutive failures → password/phrase fallback ⛔ not implemented

### 4.4 UI for Threshold Tuning ⛔
Enrollment wizard final step: histogram of intra/inter scores + suggested
`[T_low, T_high]` band + manual sliders → writes final `threshold_high` / `threshold_low`
to `config.yaml`.

🔶 **Not implemented.** Thresholds are derived automatically during enrollment and the
CLI prints the resulting band and gap; tuning means re-running `python -m winvoice.enroll`
with `--max-inter` / `--min-gap`. There is no histogram and no slider UI (there is no
GUI at all yet).

---

## 5. Intent Routing (Three-Tier Waterfall)

```
① Rules (regex + keyword tables) ✅
    └─> 0 latency, high-frequency commands (open/close app, volume, media, time, weather)
        Volume understands both relative ("音量调大 20" → delta) and absolute
        ("音量调到百分之十" → level 0-100) requests; app names accept Chinese
        labels, English ids, and near-miss spellings from ASR.

② Intent Classifier (local small model) 🔶
    ├─ Model: qwen2.5-3b-instruct on llama.cpp llama-server (config: llm.local.*)
    ├─ Constrained decoding: GBNF sent per request in the `grammar` field
    │  (no server-side -mgf needed); a build that rejects it falls back once to
    │  `response_format: json_object` and logs local_llm_grammar_rejected
    ├─ Output: {"intent": "...", "args": {...}} — **no tool selection**
    ├─ Confidence threshold: 0.65 (config: llm.local.confidence_threshold)
    └─ < 0.65 or allowlist miss → escalate to ③

③ Cloud LLM (OpenAI-compatible) 🔶
    ├─ Only for ② failures or allowlist misses
    ├─ Guest tier: cloud disabled
    ├─ Same structured output contract
    └─ **Disabled by default** (`llm.remote.enabled: false`), and it needs a real
       endpoint + `${REMOTE_API_KEY}` before it can run
```

**Hard constraint** ✅: small model **never selects tools**. Tool selection & argument
filling happen in code (`AudioPipeline._intent_to_tool_calls`).

🔶 Lines ①-③ can all miss. The router then returns `intent=unknown`, and since
`unknown` has no tool mapping, the spoken reply is
「抱歉，这个请求我还没有实现。」 There is no chat/QA fallback: the assistant routes
commands and answers time and weather, it does not answer questions.

The rule layer resolves conflicts in three tiers, because the interesting cases
collide: (1) intents whose argument is a literal path (`run_script`, `read_file`,
`write_file`) — the path may itself contain a search word, and 「运行脚本 search.py」
must not become a browser search for 「py」; (2) an explicit search request
(`用浏览器`/`搜索`/`搜一下`/`百度`/`google`/`search`, the Latin ones not followed by a
path separator) — it names the tool it wants, so it outranks the topic, which is what
「用浏览器搜索天气」 needs; (3) everything else in declaration order, so
`get_weather`/`get_time` sit above the *weak* search verbs (`查一下`/`查询`):
「查一下天气」 is a question this assistant can answer and must not open a browser.
The extracted query is everything after the **last** verb, so 「google 搜索天气」
searches for 「天气」 rather than for the verb fragment 「搜索天气」.

🔶 **Failure handling**: the local call makes at most two attempts (grammar, then
`json_object`); a transport error, a timeout, or a low-confidence parse is caught by
the router and either escalated to ③ or returned as `IntentResult(unknown,
needs_cloud=True)`. There is no dedicated decode-retry loop beyond those two attempts,
and no 2 s deadline beyond the client's 60 s HTTP timeout.

---

## 6. Tool Allowlist & Execution

### 6.1 Tools

| Tool | Arguments | Destructive | Guest |
|------|-----------|-------------|-------|
| `open_app` 🔶 | `app: str` — allowlisted id or Chinese name, fuzzy-matched (`记事本`, `notepad`, `Notpa` all resolve) | ❌ | Non-sensitive only ⛔ |
| `close_app` 🔶 | `app: str` — as above | ❌ | Non-sensitive only ⛔ |
| `set_volume` 🔶 | `delta: int` (relative) **or** `level: int` 0–100 (absolute); at least one required | ❌ | ✅ |
| `media_control` | `action: Enum[play,pause,next,prev]` | ❌ | ✅ |
| `search_web` 🔶 | `query: str` — percent-encoded; opens the default browser immediately, no confirmation | ❌ | ✅ |
| `read_file` 🔶 | `path: str` (must resolve under `C:\Users\<you>\`, ≤10 MB, UTF-8) | ❌ | ❌ |
| `write_file` | `path: str, content: str` | ✅ | ❌ |
| `run_script` 🔶 | `path: str` with suffix `.py` / `.ps1` / `.bat` / `.cmd` (under `C:\Users\<you>\`) | ✅ | ❌ |
| `get_time` | — (no arguments) | ❌ | ✅ |
| `get_weather` | `city: str` optional — the rule layer fills it when the sentence names a city (`北京的天气` → `北京`), otherwise `weather.city` from the config | ❌ | ✅ |

The 9 allowlisted apps and their spoken Chinese names live in
`winvoice/tools/builtin.py` (`ALLOWED_APPS` / `APP_SPEECH`). Adding an app means adding
to both, plus optionally an alias.

🔶 Apps are launched by their **resolved path** (`resolve_app_command`: the `App Paths`
registry key first, then `PATH`), because `chrome.exe`, `msedge.exe` and `Code.exe` are
not on `PATH` — launching the bare name through a shell printed
`'chrome.exe' 不是内部或外部命令` while the tool reported success (live report,
2026-09-19). A program that cannot be located is reported in Chinese, never launched
optimistically; `close_app` reports a non-zero `taskkill` (nothing was running) and
refuses URI targets instead of claiming success; `search_web` percent-encodes its query.
`Popen` runs without a shell, so a missing program raises instead of being swallowed.
Nothing beyond "the OS accepted the start" is claimed: Chrome exits immediately when an
instance already runs, so polling the process would report false failures.

`get_time` and `get_weather` are the only read-only *query* tools: they answer with a
sentence rather than acting on the machine, so they need no confirmation and no
snapshot, and a guest may ask them (that flag is inert until §15's guest-tier gap is
closed). `get_weather` lives in `winvoice/tools/weather.py` — it is the only tool whose
reason to change is an external provider — and is the only **async** handler
(`httpx.AsyncClient` inside an `asyncio.wait_for` deadline; httpx's own `timeout` bounds
connect and read separately, so it is not a bound on the whole exchange).
`ToolExecutor.execute` awaits either handler shape. Its answers are built by
`WeatherSummary.speech()`, which maps the numeric WWO code to Chinese itself — `wttr.in`
returns English descriptions even with `lang=zh` (verified against Beijing/Lhasa/Sanya/
Mohe, 2026-09-19).

### 6.2 Destructive Action Flow ⛔
1. Double confirmation (voice + UI toast)
2. Snapshot target files (declared in `modified_paths: string[]`)
3. Execute
4. On failure → auto-restore from snapshot

⛔ **Steps 1 and 2 are only half-built.** The snapshot half works
(`ToolExecutor.execute` snapshots `modified_paths` for destructive tools and restores on
failure), but the confirmation round trip does not exist: `execute()` is always called
with `confirmed=False`, so `write_file` and `run_script` return
`CONFIRMATION_REQUIRED: ...` and **never run**. `ToolExecutor.get_pending_confirmation()`
and `clear_pending_confirmation()` exist with no caller in the voice path.

### 6.3 Irreversible Operation Blocklist ✅
- `run_script` content scanned for `IRREVERSIBLE_PATTERNS` (reg add/delete, msiexec /uninstall, etc.) → reject + audit log
- `write_file` restricted to `C:\Users\<user>\` subtree
- Registry, software uninstall, system config changes **never allowed**

### 6.4 Speech contract for tool output 🔶

The TTS model is Chinese-only: `vits-icefall-zh-aishell3`'s lexicon contains **zero
Latin entries**, so sherpa-onnx drops every English word it is asked to say
(`lexicon.cc: OOV ... Ignore it!`). A tool error containing English therefore produced
an audible sentence with holes in it.

The contract that fixes this: `ToolResult` carries two strings.

| Field | Audience | Language |
|---|---|---|
| `error` | logs and callers — may name tools, argument keys, paths | free (often English) |
| `message` | **spoken to the user** | plain Chinese, digits allowed (expanded by `number.fst`) |

`message` carries both outcomes. On failure `AudioPipeline._default_reply` speaks
`message` when present, otherwise strips the unpronounceable parts of `error` and falls
back to 「抱歉，这个操作没有成功。」. On success it speaks `_spoken_success(results)` — the
successful `message`s — and falls back to 「好的，已为您完成。」 when a tool has nothing
to report. That is how the query tools answer at all: `get_time` and `get_weather` write
their whole answer into `message`.

Two guards apply to anything on its way to the speaker, both from
`winvoice/contracts/speech.py` so the tool layer and the speech layer cannot drift apart:

- a `message` containing ASCII letters is **refused** (with an `unspeakable_tool_message`
  warning) and the generic sentence is used instead. English in `message` is a bug in the
  tool, and cleaning it would leave a sentence full of holes;
- the joined text is clipped to `MAX_SPEECH_CHARS = 80` — at the last sentence boundary
  past the midpoint when there is one, otherwise at the limit — because
  `TtsEngine.synthesize` synthesises the whole utterance before it yields its first
  chunk: length is dead air. With several results each gets an equal share of the
  budget, so one long message cannot clip the others away.

`tests/unit/test_speech_is_pronounceable.py` checks every handler's success *and* failure
message against the model's real `lexicon.txt` (and the whole 60-entry weather condition
table with it). Anything new that reaches the speaker must respect this contract.

### 6.5 Result verification ✅

A tool's `success` is a *claim*; `winvoice/tools/verifier.py` supplies the
evidence. After a tool with an observable postcondition runs, its verifier reads
the machine:

| Tool | Checks |
|---|---|
| `open_app` | the resolved `.exe` appears in `tasklist` (polled, ~1 s budget) |
| `close_app` | the process is absent |
| `write_file` | exists · is a regular file · bytes match what was requested |
| `set_volume` | the Core Audio endpoint reads back the target (±3 points) |
| `run_script` | the process exit status |

`VerificationStatus` is one of `verified` / `failed` / `uncertain` /
`not_verifiable`, and the verdict is returned on `ToolResult.verification`.

✅ **Verification does not rewrite `success`.** `close_app` on a program that is
not running reports failure with 「好像没有在运行。」 — true and useful — while the
postcondition ("not running") genuinely holds, i.e. `verified`. Overwriting
`success` from the verdict would have turned that honest refusal into the false
claim 「已经关闭了」. The tool's message is what the user hears; the verdict is
what decides whether the task is unresolved.

✅ **Only an observed mismatch escalates.** `unresolved()` is true for `failed`
alone. `uncertain` and `not_verifiable` mean "this layer cannot observe it", not
"it did not work", so escalating on them would send every web search and media
keypress to the cloud. A failure with *no* verdict (an allowlisted-app refusal,
the unimplemented confirmation gate) is deterministic — the cloud model would be
refused identically — so it does not escalate either.

The five query/observation-free tools (`get_time`, `get_weather`, `read_file`,
`search_web`, `media_control`) deliberately have **no** verifier: a verifier for
them could only ever answer "nothing to check" while adding tokens to every tool
result the model reads.

`tests/unit/test_verifier.py` pins these rules against a scripted machine
(`SystemProbe`), because launching Chrome inside a test suite is not an option and
"the process exists on the dev machine" is not an assertion.

---

## 7. LLM Backends

### 7.0 Agent layer: DeepSeek Harness (optional) 🔶

Added after the sections below were written. It does not delete them: the
classifier/cloud path in §7.1–§7.3 remains as the *fallback* used when the agent
is disabled, which is the default.

With `dsh.enabled` + `dsh.local.enabled`, the assistant has two tiers and one of
them answers every request:

```
rule matched       → direct tool call (unchanged, zero model latency)
no rule matched    → DSH agent → tools via MCP → verifier → spoken reply
                     (on an observed mismatch or abnormal turn end → Cloud DSH)
```

✅ **A rule match is never overridden** (`AudioPipeline._routes_to_agent`): 「音量调大
20」 must not acquire a model's latency. Only `intent == unknown` — a rule miss —
goes to the agent.

✅ **One tool system.** The agent runs in Node, so it reaches this project's tools
over **MCP**: `winvoice/mcp_server.py` (stdio JSON-RPC) builds the *same*
`ToolCall` and calls the *same* `ToolExecutor`, mounted into DSH by a generated
profile bundle (`winvoice/dsh/bridge.py` + `scripts/install_dsh_bridge.py`). Tools
appear to the model as `mcp__winvoice__<tool>`. The allowlist, tiers, snapshots and
verifiers therefore have exactly one implementation.

🔶 The tool *list* is not tier-filtered, by design: filtering it would churn the
prompt prefix between an owner's and a guest's turn and, since DSH only re-syncs an
MCP server's tools on a `list_changed` notification, would leave the agent holding
a stale list. The tier is enforced at `tools/call` instead.

✅ **Escalation is programmatic** (`winvoice/dsh/validation.py`). The local model is
never asked whether it is confident. Escalation fires on an observed verification
mismatch, a `turn/end` whose reason is not `completed`, an empty final response, or
a backend that would not start; a retryable mismatch gets one local retry first
(`dsh.max_local_attempts`). The cloud agent receives a brief of what the local
attempt did (`build_escalation_context`) so it continues rather than starting over.

🔶 **Tool-result parsing is tolerant by design.** The extractor walks the event tree
for JSON carrying a `success` key — the shape *this project's* MCP server authors
(`render_tool_result`) — rather than hard-coding a pre-release event envelope. A
parser pinned to a guessed envelope would silently stop detecting failures, which
is the worst possible failure mode for a safety check.

✅ **The agent's reply is sanitised before speech** (`sanitize_for_tts`): markdown,
code fences, Latin words and ASCII punctuation are removed, digits and Chinese
survive, and an answer with nothing speakable left yields `""` so the pipeline says
something honest instead of a sentence of holes. `ToolResult.message` — authored by
the tool, which knows the lexicon rules — is relayed verbatim in preference.

⛔ **Known gap.** The agent turn is awaited, so the wake word is queued rather than
acted on while the agent thinks (audio is not lost — the deque holds ~200 s — only
barge-in is delayed). The same was already true of the ~1 s classifier call; an
agent turn is simply longer. Tracked in `UNIMPLEMENTED.md`.

🔶 **The agent also has DSH's own tools.** The `sdk` profile ships DSH's built-in
filesystem and shell tools next to the MCP bridge, so "the allowlist is the only
route to the machine" holds for `mcp__winvoice__*` but not for those. Selecting
`sdk-minimal` would close it; see `docs/dsh_integration_design.md` and
`UNIMPLEMENTED.md` §3.

🔶 **Escalation conditions are narrower than `new_way.md` lists** — deliberately.
See the design document's §1a for the reasoning: an unobservable action and a
deterministic refusal both escalate to nothing, because the cloud model meets the
same wall.

### 7.1 Local (llama.cpp `llama-server`) 🔶
- **Transport**: OpenAI-compatible `/v1/chat/completions` on `http://localhost:8080/v1`
- **Model**: `qwen2.5-3b-instruct` (`config: llm.local.model`); `ALLOWED_MODELS` in
  `winvoice/llm/local.py` lists the known-good names and logs a warning for anything
  else. The documented floor is ~1.5B — below that the argument keys drift badly.
- **API Key**: any placeholder (`ollama` in config) — llama-server ignores it
- **Constrained decoding**: GBNF is sent **per request** in the `grammar` field, so the
  server needs no `-mgf`/`--grammar-file`; a build that rejects it triggers one fallback
  to `response_format: json_object` and a `local_llm_grammar_rejected` warning
- 🔶 Ollama is *not* required: it works only as an alternative OpenAI-compatible
  endpoint, but the project's docs, models and scripts all assume llama.cpp.

### 7.2 Remote (OpenAI-compatible) ✅
- Enabled via `llm.remote.enabled: true` (default **false**)
- `base_url`, `api_key` (from `${REMOTE_API_KEY}` env var), `model`
- **Guest tier**: `guest_allowed: false` (default)

### 7.3 Routing Logic ✅
```
if local_unavailable or local_confidence < threshold:
    if remote_enabled and (tier == Full or remote.guest_allowed):
        route_to_cloud()
    else:
        return IntentResult(unknown, needs_cloud=True)   # spoken: 抱歉，这个请求我还没有实现。
```

---

## 8. TTS 🔶

| Engine | Model | Voice | Sample rate |
|--------|-------|-------|-------------|
| sherpa-onnx VITS | `vits-icefall-zh-aishell3` (174 speakers) | `default` (Full), `guest` (Guest) | **8 kHz** |

- 🔶 Not Piper, and not Kokoro. The Chinese icefall aishell3 VITS model is natively
  8 kHz — the telephone-grade output is expected, not a misconfiguration.
- **Lexicon**: Chinese-only (`lexicon.txt`, 66 377 entries, **no Latin entries**).
  English words are dropped at synthesis time; see §6.4 for the speech contract.
- **Text normalisation**: `number.fst`, `date.fst`, `phone.fst` are passed as
  `rule_fsts`, which is what makes digits and dates speakable at all. Without them
  「调到90」 is synthesised as 「调到」 — the number silently disappears.
- **Interruptible**: see §3 barge-in.

---

## 9. Configuration

### 9.1 File: `config/config.yaml` ✅
Full schema in README. Key points:
- `${VAR}` syntax → `os.path.expandvars` at load; missing var → explicit error with field path.
  🔶 An unresolved placeholder inside a section that is explicitly disabled (e.g.
  `llm.remote.enabled: false`) is tolerated and substituted with an empty string, so an
  unused feature cannot stop the assistant from booting.
- Hot-reloadable fields (`HOT_RELOADABLE` in `winvoice/config.py`):
  - `llm.local.confidence_threshold`
  - `tools.whitelist`
  - `tts.voice`
  - `kws.threshold` (requires Audio restart → logged warning)
  - `weather.enabled` / `weather.city` / `weather.timeout_s` — `get_weather` reads them
    per call, so these are the only settings that genuinely take effect without a restart
- `weather:` (new section): `enabled` (default `true`), `city` (default `北京`, used when
  the sentence names no city), `timeout_s` (default 5 s, the ceiling for the `wttr.in`
  request). `wttr.in` needs no API key, so there is no key to configure.
- Non-hot-reload changes → warning + "needs restart" toast
- 🔶 **Reality check**: `ConfigManager.start_watching()` does start a `watchdog`
  observer and re-reads the file into the config object on modification. But nothing
  *consumes* the reload — engines read their settings once during `initialize()`, and
  there is no PUB/SUB fan-out. So a hot reload changes what `get_config().get(...)`
  returns and nothing else; in practice **restart the assistant to apply a config
  change**.

### 9.2 Model Manifest 🔶
`scripts/download_models.py` embeds a `MANIFEST` of `{url, sha256, size}` per model:
- Download to `<dest>.part` → verify SHA256 → `Path.replace()` into place (atomic on
  the same volume)
- Resume via HTTP Range requests; a non-satisfiable range restarts the transfer clean
- 🔶 **Only the KWS entry pins a real SHA256** (`68447f4f…`); every other entry has
  `sha256: None`, and the script prints `(no pinned SHA256 - skipping verification)`.
  The `--list` output marks each model `sha256` or `no-hash`.
- ⛔ Startup integrity check (`size+mtime` quick check → full SHA256 → `models/.corrupt/`
  + "Auto-repair" dialog) is **not implemented**; only the download path verifies.

---

## 10. Logging & Observability

- **Format**: JSON Lines via `structlog` + `orjson` renderer
- **Fields**: `timestamp, level, process, trace_id, span_id, event, **fields`
- **Trace ID**: generated at KWS trigger (`uuid4().hex[:16]`), propagated through all stages
- **Metrics**: Prometheus pushgateway (Main process `/metrics` + child processes push every 10 s)
- **Rotation**: `TimedRotatingFileHandler` daily, retain 7 `.jsonl.gz`
- **Disk quota**: `storage.max_total_gb: 10` (models + logs + snapshots); cleanup order: snapshots → logs → audio cache → models (never)

🔶 **Known defects in this area (verified 2026-09-19):**

1. **Structured events do not reach the log file.** Every module builds its logger at
   import time, before `configure_logging()` runs, and `cache_logger_on_first_use=True`
   freezes structlog's *default* configuration into those loggers. The events are
   therefore rendered to **stdout** (pretty console format), while `logs/main.jsonl`
   only ever receives stdlib records from third-party libraries (httpx request lines).
   Diagnosing anything from the file log alone is currently impossible.
2. **The `process` field is wrong.** `add_process_name` and `add_timestamp` take
   `(logger, name, event_dict)`, but structlog passes the *method name* as the second
   argument, so `process` is always the log level — e.g.
   `{"event": "hello_event", "process": "info", "level": "info"}`.
3. ⛔ **Prometheus metrics are dead code.** `winvoice/logging.py:init_metrics()` is
   never called, so `observe_latency` / `inc_request` are no-ops and nothing is pushed.
4. ⛔ **The disk quota is not enforced.** `storage.max_total_gb` is read into the
   config but no code computes or prunes usage; rotation is the only bound on log growth.

---

## 11. Testing Strategy

| Layer | Scope | Command | CI |
|-------|-------|---------|----|
| Unit | Pure logic (intent classification, tool validation, SV scoring) | `pytest -m unit` | ✅ Required |
| Integration | Audio pipeline with synthetic WAV → KWS/VAD/ASR output text | `pytest -m integration` (requires `models/`) | ⚠️ Optional |
| E2E | Real mic, real models, real network | `pytest -m manual` | ❌ Manual only |

🔶 **Markers are declared but barely used.** `pytest.ini` registers `unit`,
`integration` and `manual`, but only `tests/e2e/test_e2e.py` carries a marker
(`manual`). `pytest -m unit` therefore selects **nothing** — select by directory or
file instead. The suite today is **344 passed, 1 skipped**; the skip is
`test_core.py`'s LLM probe, which skips itself when `llama-server` is not reachable.
Tests that load a real engine `skipif` when the model is absent.

🔶 **Synthetic audio**: `pytest-audio` is not used. Integration-level coverage instead
drives the real `AudioPipeline` with **stub engines** plus real models where the test
needs them (e.g. the SV frame-conversion regression loads the real CAM++ model and
skips if it is missing).

> Repository-relative commands are the reliable interface: `pytest tests/unit -q`,
> `pytest tests/integration -q`, `pytest tests -q`.

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

## 13. File Structure (as built)

```
windows_voice_assistant/
├── AGENTS.md
├── README.md / deployment.md / spec.md
├── config/
│   └── config.yaml
├── docs/
│   └── agents/                # issue-tracker.md, domain.md
│                              # (docs/adr/ is referenced by AGENTS.md but not created yet)
├── grammar.gbnf               # reference copy; the live grammar is in winvoice/llm/grammar.py
├── run.ps1
├── scripts/
│   ├── download_models.py     # MANIFEST + Range resume + SHA256 (partially pinned)
│   ├── smoke_test_models.py   # every engine against the real models
│   ├── check_llm.py           # local LLM tier check
│   ├── analyze_sv_scores.py   # offline intra/inter score analysis
│   ├── verify_install.py      # dependency check
│   └── test_stub_pipeline.py
├── tests/
│   ├── unit/                  # pure logic + model-backed regressions (skipif)
│   ├── integration/           # real pipeline with stub engines
│   └── e2e/                   # marked `manual`
├── tools/                     # locally extracted llama.cpp (gitignored)
├── winvoice/
│   ├── contracts/             # Pydantic message models (schema_version=1)
│   │                          # speech.py: what the Chinese TTS can pronounce (§6.4)
│   │                          #            + sanitize_for_tts for agent answers
│   ├── audio/                 # kws, vad, asr, sv, tts, stream, pipeline, _common
│   ├── llm/                   # local + remote clients, GBNF grammar, router
│   ├── dsh/                   # DeepSeek Harness: client, escalation, config, bundle
│   ├── intent/                # rules, classifier, router
│   ├── tools/                 # registry, builtin handlers, executor, snapshot
│   │                          # verifier.py: check a tool against the machine (§6.5)
│   │                          # state_capture.py: the SystemProbe seam
│   │                          # utterance.py: in-flight tier across processes (§4.2)
│   │                          # weather.py: the one network-backed tool, with its
│   │                          # own WWO → Chinese condition table
│   ├── mcp_server.py          # the tool registry, as DSH sees it (§7.0)
│   ├── enroll/                # speaker enrollment CLI (+ guided prompts)
│   ├── _vendor.py             # puts optional .pylibs deps on sys.path
│   ├── config.py              # ConfigManager + watchdog file watcher
│   ├── logging.py             # structlog setup (see the §10 defects)
│   ├── context.py
│   └── __main__.py            # entry point (`python -m winvoice`)
└── models/                    # gitignored, downloaded at runtime
```

🔶 No `prototype/` directory: the single-process prototype **is** the implementation.
The entry point is `winvoice/__main__.py`, not `main.py`.

---

## 14. Roadmap (as executed)

1. ✅ **Validate intent classifier accuracy** — the local tier runs against
   llama-server with GBNF; `scripts/check_llm.py` is the check
2. ✅ Audio pipeline: KWS → SV → VAD → ASR → intent
3. ✅ LLM integration: text → reply → TTS
4. ✅ Execution layer: the non-destructive tools
5. 🔶 Cloud routing — implemented but disabled by default, never exercised end to end
6. 🔶 Speaker verification: enrollment, tiers, adaptive update — **confirmation and
   snapshot are only half-built** (§6.2)
7. ⛔ Guest mode + voice switching — the guest *voice* works, the guest *permissions* do not
8. ⛔ PySide6 UI — not started; everything is a CLI

---

## 15. As-built status

### Implemented
Voice pipeline (KWS → SV → VAD → ASR → intent → tools → TTS), half-duplex with
barge-in, bilingual wake words, speaker enrollment with guided prompts and threshold
derivation, three-tier intent routing (rules → local LLM → cloud), 10-tool allowlist with
schema validation, destructive-tool snapshotting, JSON-Lines logging with trace IDs,
stub-engine mode for development. Spoken replies carry successful results too
(`ToolResult.message`), and the two query tools — `get_time` (spoken 12-hour clock with a
period word) and `get_weather` (one Chinese sentence about today, from `wttr.in`, offline
in Chinese) — answer questions rather than acting on the machine.

### Not implemented
> `UNIMPLEMENTED.md` is the working backlog for these: it carries the per-item constraints,
> code anchors and acceptance criteria. Keep the two lists in step.

| Item | Where it would live |
|---|---|
| Confirmation round trip (blocks `write_file` / `run_script`) | `ToolExecutor` + pipeline |
| Sensitive-app list for the Guest tier (the tier itself is now enforced) | `ALLOWED_APPS` + `ToolRegistry` |
| Barge-in while the agent is thinking (an awaited turn queues the wake word) | `AudioPipeline` state machine |
| Question answering / chat, directory listing | new intent + tools |
| A weather provider with an API key (wttr.in is keyless but rate-limited, and untranslated) | `winvoice/tools/weather.py` |
| Hot-reload fan-out to running engines | config watcher → engines |
| Prometheus metrics, disk quota, model integrity check, anchor check | §10, §9.2 |
| Password/phrase fallback, threshold-tuning UI, PySide6 UI | §4.3, §4.4, §14 |
| `docs/adr/` | AGENTS.md references it |

### Known defects
| Defect | Impact |
|---|---|
| Structured log events go to stdout, not `logs/main.jsonl` (§10.1) | file log is useless for diagnosis |
| `process` field always contains the log level (§10.2) | any tooling keyed on it is wrong |
| `search_web` opens the browser immediately, unconfirmed (§6.1) | a misrouted question pops a browser window |
| Failures anywhere in the tick loop are swallowed by `except Exception` + `error=str(e)` (no traceback) | silent breakage — two bugs in this spec's history were only found by reading code |
| TTS is 8 kHz and Chinese-only (§8) | English in any spoken string is dropped |

### Verified numbers (2026-09-19, this machine)
| Metric | Value |
|---|---|
| Test suite | 344 passed, 1 skipped |
| Local LLM latency | 0.8–1.1 s per intent classification |
| ASR latency | 40–50 ms per VAD segment |
| TTS synthesis | 100–300 ms, 8 kHz |
| KWS on the model's own reference wavs | English 2/2, Chinese 5/7 at threshold 0.25 |
| `get_time` | < 1 ms (no I/O) |
| `get_weather` (wttr.in) | ~1–2 s (measured 1.8 s for Beijing); the whole exchange is capped by `weather.timeout_s` (5 s), and a timeout says 「暂时查不到天气。」 |

---

*This spec started as the consensus from the grilling session and is now maintained as
the as-built description of `winvoice/`. Where the implementation diverges, the
divergence is marked inline (🔶 / ⛔) rather than quietly rewritten, and §15 collects
what is still missing.*