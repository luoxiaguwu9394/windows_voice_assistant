# DSH Integration Design Document

**Version**: 1.0  
**Date**: 2025-09-19  
**Status**: Design Phase — Ready for Review

---

## 1. Executive Summary

This document describes the integration of **DeepSeek Harness (DSH)** into the existing `windows_voice_assistant` project. The goal is to replace the current intent classification + tool execution pipeline with DSH as the unified agent layer, while preserving:

- The existing audio pipeline (KWS → SV → VAD → ASR → TTS)
- The three-tier routing: **Rules → Local DSH → Cloud DSH**
- The existing tool allowlist, confirmation, and snapshot safety mechanisms
- The Chinese-only TTS speech contract

**Key principle**: DSH becomes the "brain" (reasoning, planning, tool calling), while the existing audio pipeline remains the "ears and mouth".

---

## 1a. Implementation notes (updated after building it)

This document was written before implementation. Four decisions changed once the
real DSH 0.1.2-rc.1 surface was read and exercised; the reasoning is recorded here
so the document describes the system rather than the plan.

| Planned | As built | Why |
|---|---|---|
| Generate a TypeScript plugin per tool + an HTTP bridge back into Python (§4.4) | **MCP stdio bridge** (`winvoice/mcp_server.py`) | DSH ships `@deepseek-ai/dsh-mcp-client`. A second tool implementation in another language is exactly what `new_way.md` forbids, and it would have been a second place for the allowlist, snapshots and verifiers to drift. The MCP server is a thin adapter onto the *same* `ToolExecutor`. |
| Verifier rewrites `ToolResult.success` (§5.4) | Verifier **never** rewrites `success`; it decides escalation | `success = execution.ok and verification.verified` would have destroyed honest refusals: `close_app` reports 「好像没有在运行。」, which is true and useful, while the postcondition genuinely holds (`VERIFIED`). The user-facing message stays the tool's; verification drives the retry/escalate decision. |
| Every tool gets a verifier | Only tools with **observable** state do | A verifier for `get_time` could only ever answer "nothing to check" while adding tokens to every tool result the model reads. Absence is the honest encoding of that. |
| Separate `state_capture` layer guessing what a verifier wants | Each verifier owns its `capture()` | Asking the verifier directly removes a layer that could disagree with it. `state_capture.py` remains, holding the probe seam rather than the policy. |
| Escalate on `FAILED` / `UNCERTAIN` / `NOT_VERIFIABLE`, and on "tool execution failed" (§3.4) | Escalate on `FAILED` only, plus an abnormal turn end, an empty answer, or a backend that will not start | `UNCERTAIN`/`NOT_VERIFIABLE` mean "this layer cannot observe it", not "it did not work" — escalating on them would send every web search and every media keypress to the cloud. A failure with **no** verdict is a deterministic refusal (an app outside the allowlist, the unimplemented confirmation gate), and the cloud model is refused identically. This is narrower than `new_way.md` lists; the reasoning lives in `verifier.unresolved` and `validation.TurnAssessment`. |
| (not considered) | The cloud tier is gated on the speaker tier, via `dsh.cloud.guest_allowed` (default `false`) | `new_way.md`: 「The cloud model must NOT automatically receive broader permissions simply because it is more capable.」 Without this gate, a guest who cannot write a file locally could acquire the capability by escalating. |

Two further findings worth recording:

- **`--patch` cannot add a plugin row.** The overlay is id-targeted, so the
  obvious row list fails with `patch: entry "..." not found`. Adding rows needs a
  profile bundle whose patch uses the `insert:` wrapper
  (`winvoice/dsh/bridge.py`).
- **The speaker tier must cross a process boundary.** Tools now run in a process
  DSH spawns, so the tier travels through the in-flight utterance record
  (`winvoice/tools/utterance.py`); without it, DSH would be a way *around* the
  permission model rather than a user of it.

### Known limitation: the agent has DSH's own tools as well

`dsh.local.profile` defaults to `sdk`, and that profile ships DSH's **own**
filesystem and shell tools alongside the MCP bridge. The agent therefore has two
routes to the machine: `mcp__winvoice__*` (which goes through `ToolExecutor`, with
the allowlist, tiers, snapshots and verifiers) and DSH's built-ins (which do not).

This does not breach the project's rules today — those tools act on this
repository and the user's own machine, and the user asked for an agent that can
act — but it does mean "the allowlist is the only way to the machine" is not
literally true while a DSH profile with built-ins is selected. Closing it means
selecting `sdk-minimal` (platform shell + local execution + JSONL sessions, no
filesystem tools) or disabling the built-in tool rows in a patch layer. Not done
here because whether `sdk-minimal` retains the JSON-RPC server row that the SDK
requires has not been verified on this machine, and a wrong guess fails at
startup. Tracked in `UNIMPLEMENTED.md` §3.

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        WINDOWS VOICE ASSISTANT (Single Process)              │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌────────────┐   │
│  │   Audio      │   │   Intent     │   │   DSH Agent  │   │  Tool      │   │
│  │   Pipeline   │──▶│   Router     │──▶│   (DSH SDK)  │──▶│  Executor  │   │
│  │  (KWS/VAD/   │   │  (Rules +    │   │  Local/Cloud │   │  + Verifier│   │
│  │   ASR/TTS)   │   │   DSH)       │   │  with Fallback)               │   │
│  └──────────────┘   └──────────────┘   └──────────────┘   └────────────┘   │
│         ▲                                      │                    │       │
│         │                                      ▼                    ▼       │
│         │                               ┌─────────────┐         ┌────────┐ │
│         │                               │  Verifier   │         │ Windows│ │
│         └───────────────────────────────│ (State Check)         │  Ops   │ │
│          (Barge-in / State)             └─────────────┘         └────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 Process Model Decision

**Decision**: Keep **single-process asyncio** for MVP. Run DSH SDK in a `ThreadPoolExecutor` to avoid blocking the audio event loop.

- Audio pipeline runs on the main asyncio loop (real-time critical)
- DSH SDK calls (`harness.run()`) are blocking subprocess calls → offload to thread pool
- Tool execution stays in-process (existing `ToolExecutor`)
- Verifier runs in-process after tool execution

**Rationale**: 
- Avoids IPC complexity for MVP
- Audio jitter stays < 20ms (current architecture validated)
- DSH subprocess startup latency (~2-3s) only hits on first call; subsequent calls reuse runtime

---

## 3. Three-Tier Routing with DSH

### 3.1 Tier 1: Rules (Unchanged)

```
User Text → Rule Engine (regex/keyword) → IntentResult → Direct Tool Execution
```

- Zero latency, deterministic
- Handles: volume, media, open/close known apps, time, weather, explicit search
- **No DSH involvement** — rules bypass LLM entirely

### 3.2 Tier 2: Local DSH Agent

```
User Text → Local DSH (llama.cpp qwen2.5-3b) → Tool Calls → ToolExecutor → Verifier
```

- DSH SDK configured with:
  - `provider: "deepseek-official"` (or local llama.cpp via pi-ai adapter)
  - `model: "qwen2.5-3b-instruct"` (matches current local model)
  - `dsh_home`: isolated per-tier directory (`~/.dsh_local`, `~/.dsh_cloud`)
  - Same tool registry exposed to DSH via plugin

### 3.3 Tier 3: Cloud DSH Agent (Fallback)

```
Local DSH Failed → Cloud DSH (DeepSeek API / NVIDIA) → Tool Calls → ToolExecutor → Verifier
```

- Same tool registry, same verification
- Triggered by **programmatic validation failure**, not LLM self-assessment
- Preserves context: user request + local tool calls + results + errors

### 3.4 Escalation Conditions (Programmatic)

Local DSH result escalates to Cloud DSH when **any** of:

| Condition | Detection |
|-----------|-----------|
| Invalid tool call (tool not in registry) | `ToolExecutor` validation |
| Schema validation failure | Pydantic schema check |
| Tool execution failure (non-retryable) | `ToolResult.success == False` + `retryable == False` |
| Verification failure (state mismatch) | `Verifier.status == FAILED` |
| Local DSH timeout | `asyncio.wait_for(..., timeout=30s)` |
| Local DSH process crash | Subprocess return code ≠ 0 |
| Explicit "cannot determine" in response | Parsed from DSH output |

**Context passed to Cloud DSH**:
```python
{
    "user_request": "...",
    "local_attempt": {
        "tool_calls": [...],
        "tool_results": [...],
        "verification_results": [...],
        "errors": [...]
    },
    "escalation_reason": "verification_failed" | "tool_failure" | "timeout" | ...
}
```

---

## 4. DSH SDK Integration

### 4.1 Installation

```bash
pip install deepseek-harness-sdk
# Installs matching deepseek-harness-runtime-bin wheel for Windows
```

### 4.2 Runtime Configuration

Two isolated DSH homes:
- `DSH_HOME_LOCAL` = `%APPDATA%\windows_voice_assistant\dsh_local`
- `DSH_HOME_CLOUD` = `%APPDATA%\windows_voice_assistant\dsh_cloud`

Each with its own profile (`sdk` profile) and plugin configuration.

### 4.3 Python Wrapper

```python
# winvoice/dsh/client.py
from deepseek_harness import DeepSeekHarness
from dataclasses import dataclass
from typing import Optional
import asyncio
from concurrent.futures import ThreadPoolExecutor

@dataclass
class DSHConfig:
    dsh_home: str
    provider: str
    model: str
    profile: str = "sdk"
    max_tokens: int = 49152
    reasoning_effort: str = "max"
    request_timeout_seconds: float = 60.0

class DSHClient:
    """Async wrapper around blocking DeepSeekHarness SDK."""
    
    def __init__(self, config: DSHConfig):
        self.config = config
        self._harness = None
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._lock = asyncio.Lock()
    
    async def initialize(self):
        """Lazy initialization — starts DSH subprocess."""
        async with self._lock:
            if self._harness is None:
                self._harness = await asyncio.get_event_loop().run_in_executor(
                    self._executor,
                    lambda: DeepSeekHarness(
                        dsh_home=self.config.dsh_home,
                        profile=self.config.profile,
                        provider=self.config.provider,
                        model=self.config.model,
                        max_tokens=self.config.max_tokens,
                        reasoning_effort=self.config.reasoning_effort,
                    )
                )
    
    async def run(self, prompt: str, session_id: str) -> str:
        """Run a single turn, return final_response text."""
        await self.initialize()
        return await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                self._executor,
                lambda: self._harness.run(prompt, session_id=session_id).final_response
            ),
            timeout=self.config.request_timeout_seconds
        )
    
    async def close(self):
        if self._harness:
            await asyncio.get_event_loop().run_in_executor(
                self._executor, self._harness.close
            )
        self._executor.shutdown(wait=True)
```

### 4.4 Tool Registration for DSH — the MCP bridge

> **Superseded by the built design.** The generated-TypeScript-plugin plan below
> was replaced by an MCP stdio bridge; see §1a. The implementation is
> `winvoice/mcp_server.py` (server), `winvoice/dsh/bridge.py` (the bundle that
> mounts it) and `scripts/install_dsh_bridge.py` (the installer).

DSH reaches this project's tool registry over **MCP**, which DSH supports
natively through `@deepseek-ai/dsh-mcp-client`:

```
DSH agent loop (Node)
      │  tools/call  mcp__winvoice__open_app
      ▼
@deepseek-ai/dsh-mcp-client
      │  JSON-RPC over stdio
      ▼
python -m winvoice.mcp_server
      │
      ▼
ToolExecutor  →  allowlist · tier · snapshot · handler · verifier
```

This keeps a single execution path: the MCP server builds the *same* `ToolCall`
the in-process pipeline builds and hands it to the *same* `ToolExecutor`, so
there is exactly one implementation of the allowlist, the permission tiers, the
snapshot/rollback logic and the verifiers.

Mounting it into DSH requires a profile bundle, because a plugin row cannot be
added by a `--patch` overlay (that layer is id-targeted). The bundle is generated
from configuration rather than committed, since it embeds absolute paths:

```yaml
# runtime/dsh_bridge/cordis.patch.yml   (generated)
- insert:
    - id: mcp-winvoice
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: 'winvoice'
        transport: stdio
        command: '<python>'
        args: ['-m', 'winvoice.mcp_server']
        cwd: '<repo root>'
        toolCallTimeoutMs: 120000
        env:
          WINVOICE_UTTERANCE_FILE: '<repo>/runtime/utterance.json'
```

```powershell
python scripts/install_dsh_bridge.py --install   # writes the bundle, runs
                                                 # `dsh plugin --profile sdk add`
python scripts/install_dsh_bridge.py --verify    # asserts the row composes
```

Tools appear to the model as `mcp__winvoice__<tool>`. The tool *list* is not
tier-filtered — filtering it would churn the prompt prefix between an owner's and
a guest's turn and, because DSH only re-syncs an MCP server's tools on a
`list_changed` notification, would leave the agent holding a stale list. The tier
is enforced at `tools/call` instead, from the in-flight utterance record.

---

## 5. Verifier Design (Project Extension Layer)

### 5.1 Verifier Interface

```python
# winvoice/tools/verifier.py
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional
from abc import ABC, abstractmethod

class VerificationStatus(Enum):
    VERIFIED = "verified"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    NOT_VERIFIABLE = "not_verifiable"

@dataclass
class VerificationCheck:
    name: str
    passed: bool
    details: Optional[str] = None

@dataclass
class VerificationResult:
    status: VerificationStatus
    verified: bool
    checks: List[VerificationCheck]
    reason: str
    retryable: bool = False
    evidence: Dict[str, Any] = None

class Verifier(ABC):
    @abstractmethod
    def verify(
        self,
        tool_name: str,
        args: Dict[str, Any],
        execution_result: Dict[str, Any],
        before_state: Optional[Dict[str, Any]] = None
    ) -> VerificationResult:
        pass
```

### 5.2 Built-in Verifiers

| Tool | Verification Logic |
|------|-------------------|
| `open_app` | Check process exists via `tasklist` / `psutil`; optionally check foreground window |
| `close_app` | Check process no longer exists |
| `set_volume` | Query Core Audio API → compare actual level |
| `media_control` | NOT_VERIFIABLE (no reliable state query) |
| `search_web` | UNCERTAIN (browser opened, but query executed?) |
| `read_file` | Verify file exists, readable, content matches |
| `write_file` | Verify file exists, content matches, path correct |
| `run_script` | Verify return code 0; optionally check stdout for expected patterns |
| `get_time` | VERIFIED (no side effects) |
| `get_weather` | VERIFIED (no side effects) |

### 5.3 State Capture (Before/After)

```python
# winvoice/tools/state_capture.py
class StateCapture:
    @staticmethod
    def capture_before(tool_name: str, args: Dict) -> Dict:
        """Capture relevant Windows state before tool execution."""
        if tool_name == "open_app":
            return {"processes": list_running_processes(args["app"])}
        elif tool_name == "write_file":
            path = Path(args["path"])
            return {"exists": path.exists(), "content": path.read_text() if path.exists() else None}
        # ... per-tool state capture
        return {}
    
    @staticmethod
    def capture_after(tool_name: str, args: Dict, before: Dict) -> Dict:
        """Capture state after execution for comparison."""
        # Similar structure
        return {}
```

### 5.4 Integration Point

**Decision**: Verifier runs **inside `ToolExecutor.execute()`** after tool execution, before returning `ToolResult`.

```python
# winvoice/tools/executor.py (modified)
async def execute(self, call: ToolCall, confirmed: bool = False) -> ToolResult:
    # ... existing validation, confirmation, snapshot ...
    
    # Execute
    result = await _maybe_await(spec.handler(call.args))
    
    # NEW: Verification
    before_state = StateCapture.capture_before(call.tool.value, call.args)
    # (execute tool)
    after_state = StateCapture.capture_after(call.tool.value, call.args, before_state)
    
    verifier = VERIFIER_REGISTRY.get(call.tool)
    if verifier:
        verification = verifier.verify(
            tool_name=call.tool.value,
            args=call.args,
            execution_result=result,
            before_state=before_state
        )
        # Attach verification to result
        result["verification"] = verification.__dict__
        
        # Override success based on verification
        if verification.status == VerificationStatus.FAILED:
            result["success"] = False
            result["error"] = f"Verification failed: {verification.reason}"
            # Restore snapshot if destructive
            if snapshot_id and spec.destructive:
                self.snapshot_mgr.restore_snapshot(snapshot_id)
    
    return ToolResult(...)
```

---

## 6. Speech Contract Compliance (Chinese-Only TTS)

### 6.1 Problem
DSH output may contain English, markdown, code blocks → TTS drops them silently.

### 6.2 Solution: System Prompt Constraint + Post-Processor

**DSH System Prompt Addition** (injected per-request):
```
You are a Windows voice assistant. Your FINAL RESPONSE to the user must be:
- Pure Chinese (no English words, no Latin letters)
- No markdown, no code blocks, no formatting
- Short (≤ 80 characters)
- Natural spoken language
- If you need to report a file path, say "目标文件" instead of the path
- If you need to report an error, say what happened in plain Chinese
```

**Post-Processor** (fallback safety net):
```python
# winvoice/contracts/speech.py
def sanitize_for_tts(text: str) -> str:
    """Strip unpronounceable content, clip to 80 chars."""
    import re
    # Remove markdown
    text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    text = re.sub(r'`.*?`', '', text)
    text = re.sub(r'\[.*?\]\(.*?\)', '', text)
    # Remove Latin letters (keep digits)
    text = re.sub(r'[A-Za-z]+', '', text)
    # Remove special chars
    text = re.sub(r'[#*_~\[\]{}|\\^<>]+', ' ', text)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    # Clip
    return clip_for_speech(text, MAX_SPEECH_CHARS=80)
```

**Integration**: In `AudioPipeline._default_reply()`, apply `sanitize_for_tts()` to DSH's `final_response` before TTS.

---

## 7. Configuration

### 7.1 New Config Keys (config.yaml)

```yaml
dsh:
  enabled: true
  
  local:
    enabled: true
    dsh_home: "%APPDATA%/windows_voice_assistant/dsh_local"
    provider: "deepseek-official"  # or "ollama" via pi-ai
    model: "qwen2.5-3b-instruct"
    max_tokens: 49152
    reasoning_effort: "max"
    timeout_seconds: 60
  
  cloud:
    enabled: true
    dsh_home: "%APPDATA%/windows_voice_assistant/dsh_cloud"
    provider: "deepseek-official"
    model: "deepseek-v4-flash"
    api_key: "${DEEPSEEK_API_KEY}"
    base_url: "https://api.deepseek.com/v1"
    max_tokens: 49152
    reasoning_effort: "max"
    timeout_seconds: 90
    guest_allowed: false
  
  # Shared
  session_id_prefix: "wva_"  # trace_id from pipeline
  verification_enabled: true
  escalation_enabled: true
```

### 7.2 Hot-Reload Decision

**Decision**: DSH config changes **require restart** (added to `REQUIRES_RESTART` set).

Rationale: DSH subprocess must restart to pick up new `dsh_home`, `provider`, `model`, or plugin changes. Hot-reload would require complex runtime reconfiguration.

---

## 8. Modified Components

| File | Change Type | Description |
|------|-------------|-------------|
| `winvoice/dsh/client.py` | NEW | Async DSH SDK wrapper |
| `winvoice/dsh/plugin_generator.py` | NEW | Codegen: ToolSpec → DSH plugin TypeScript |
| `winvoice/tools/verifier.py` | NEW | Verifier ABC + built-in verifiers |
| `winvoice/tools/state_capture.py` | NEW | Before/after state capture |
| `winvoice/tools/registry.py` | MODIFY | Add `verifier` field to `ToolSpec` |
| `winvoice/tools/executor.py` | MODIFY | Integrate verifier, pass tier, handle escalation context |
| `winvoice/intent/router.py` | MODIFY | Replace `LlmRouter` with `DSHRouter` (Local→Cloud fallback) |
| `winvoice/audio/pipeline.py` | MODIFY | Call `DSHRouter.route()` instead of old intent router |
| `winvoice/config.py` | MODIFY | Add DSH keys to `REQUIRES_RESTART` |
| `config/config.yaml` | MODIFY | Add `dsh:` section |
| `scripts/generate_dsh_plugin.py` | NEW | Build-time plugin generation |
| `tests/unit/test_dsh_integration.py` | NEW | Unit tests for DSH client, verifier, escalation |
| `tests/integration/test_dsh_e2e.py` | NEW | Integration test with stub DSH |

---

## 9. Implementation Phases

### Phase 1: Foundation (Week 1)
- [ ] Add DSH config keys to `config.yaml` and `ConfigManager.REQUIRES_RESTART`
- [ ] Implement `DSHClient` wrapper with thread pool
- [ ] Write plugin generator (`ToolSpec` → DSH plugin)
- [ ] Unit test: DSH client initialize, run, close

### Phase 2: Local DSH Integration (Week 2)
- [ ] Replace `LlmRouter` with `DSHRouter` (local only)
- [ ] Integrate DSH plugin with 10 tools
- [ ] Wire `AudioPipeline` → `DSHRouter` → `ToolExecutor`
- [ ] Test: ASR text → Local DSH → tool → TTS (stub audio)

### Phase 3: Verifier & Safety (Week 3)
- [ ] Implement `Verifier` ABC + 10 built-in verifiers
- [ ] Integrate into `ToolExecutor.execute()`
- [ ] Add `StateCapture` for before/after comparison
- [ ] Test: verification passes/fails, snapshot restore

### Phase 4: Cloud Fallback & Escalation (Week 4)
- [ ] Add Cloud DSH client (separate `dsh_home`)
- [ ] Implement escalation logic in `DSHRouter`
- [ ] Context preservation (tool calls, results, errors → cloud prompt)
- [ ] Test: local failure → cloud retry → success

### Phase 5: Speech Compliance & Polish (Week 5)
- [ ] Add Chinese-only system prompt to DSH requests
- [ ] Implement `sanitize_for_tts()` post-processor
- [ ] Fix `search_web` confirmation (reuse confirmation loop)
- [ ] Enable guest tier enforcement (pass `sv_result.tier` to tools)

### Phase 6: Testing & Hardening (Week 6)
- [ ] End-to-end smoke test: voice → ASR → DSH → tool → verifier → TTS
- [ ] Load test: 50 consecutive requests, measure latency
- [ ] Failure injection: DSH timeout, tool failure, verification failure
- [ ] Update README, deployment.md, spec.md

---

## 10. Risk Assessment & Mitigation

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| DSH subprocess startup latency (2-3s) | High | User perceives delay | Pre-warm DSH at startup; show "思考中..." TTS |
| DSH output not Chinese-compliant | Medium | TTS drops words | System prompt + post-processor double guard |
| Tool schema mismatch (DSH vs local) | Medium | Tool calls fail | Automated plugin generation from single source (`ToolSpec`) |
| Verifier false negatives | Medium | Unnecessary cloud escalation | Conservative verifiers; `UNCERTAIN` → local retry first |
| DSH SDK version drift | Low | Build breaks | Pin `deepseek-harness-sdk==0.1.5rc1` in requirements |
| Audio loop blocked by DSH | Low | Audio glitches | Thread pool isolation; `asyncio.wait_for` timeout |

---

## 11. Acceptance Criteria (Phase 1-6 Complete)

1. **Voice command**: "打开记事本" → Rules match → `open_app` → Verifier confirms notepad.exe running → TTS "已经打开记事本了"
2. **Complex command**: "在桌面创建 test.txt 写入 hello" → Rules miss → Local DSH → `write_file` → Verifier confirms file exists with content → TTS "已经写好了"
3. **Escalation**: Local DSH fails (e.g., wrong tool) → Cloud DSH receives context → Cloud DSH corrects → succeeds
4. **Chinese-only**: All spoken responses pass `sanitize_for_tts()` — no OOV warnings in logs
5. **Guest tier**: Guest speaker asks "读取文件 X" → rejected with Chinese message
6. **Confirmation**: "运行脚本 X" → asks "确认运行脚本 X 吗？" → user says "确认" → executes
7. **Barge-in**: TTS playing → user says wake word → TTS stops → new utterance processed

---

## 12. Open Questions for Review

1. **DSH Profile**: Use `sdk` profile (full toolset) or `sdk-minimal` (shell only)? `sdk` recommended for built-in filesystem tools.
2. **Local Provider**: Use `deepseek-official` with local llama.cpp via pi-ai, or direct `ollama`? Current config uses llama.cpp server — need pi-ai adapter config.
3. **Session Persistence**: Keep `session_id` across restarts? Current `trace_id` is per-utterance. DSH session could be per-assistant-session.
4. **Verifier Granularity**: Start with per-tool verifiers, or add "goal verification" (Level 3 in new_way.md)? Start simple.
5. **Cloud Provider Credentials**: DeepSeek API key vs NVIDIA — support both via `provider` config?

---

## 13. Appendix: DSH Plugin Generation Script (Sketch)

```python
# scripts/generate_dsh_plugin.py
from winvoice.tools.registry import get_tool_registry, ToolSpec
from winvoice.contracts import ToolName
import json
from pathlib import Path

TS_TEMPLATE = '''
import {{ defineTool }} from '@deepseek-ai/dsh-tools'

export const {name} = defineTool({{
  name: '{dsh_name}',
  description: '{description}',
  parameters: {schema_json},
  async execute(args, {{ exec, signal }}) {{
    // Call into Python via stdio bridge or HTTP
    // For MVP: use a local HTTP bridge server
    const resp = await fetch('http://localhost:8765/tools/{dsh_name}', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(args),
      signal
    }})
    return resp.json()
  }},
  output: {{ schema: {{ type: 'string' }}, render: (_, v) => v }}
}}
'''

def generate_plugin():
    registry = get_tool_registry()
    plugin_dir = Path("dsh_plugin_winvoice_tools/src/tools")
    plugin_dir.mkdir(parents=True, exist_ok=True)
    
    for tool_name in ToolName:
        spec = registry.get(tool_name)
        if not spec:
            continue
        dsh_name = f"winvoice_{tool_name.value}"
        schema_json = spec.schema.model_dump_json(indent=2)
        content = TS_TEMPLATE.format(
            name=tool_name.value,
            dsh_name=dsh_name,
            description=spec.description,
            schema_json=schema_json
        )
        (plugin_dir / f"{tool_name.value}.ts").write_text(content)
    
    # Generate index.ts, package.json, cordis.patch.yml
    # ...
```

---

**End of Design Document**

*Please review and confirm before implementation begins.*