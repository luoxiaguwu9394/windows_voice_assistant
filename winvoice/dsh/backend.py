"""
Talking to DeepSeek Harness, behind an interface the rest of the app can fake.

The SDK is synchronous and owns a subprocess, so every call here goes through a
single-worker thread executor: `harness.run()` blocks until the whole agent turn
settles, and running that on the asyncio loop would freeze the microphone for
however many seconds the model takes. `new_way.md` calls this out directly — the
audio process must not block while the agent thinks — and this is where that is
enforced.

The `DSHBackend` protocol exists so the escalation logic can be tested against a
scripted agent. A test that needs a DeepSeek API key, a Node runtime and a
network round trip is a test that eventually gets deleted, and then the
escalation rules lose their regression coverage. The real backend is one
implementation of the protocol; `RecordingBackend` is the other.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, List, Optional, Protocol

from winvoice.logging import get_logger

logger = get_logger(__name__)


@dataclass
class DSHTurn:
    """One agent turn's outcome, normalised for the router."""

    final_response: str = ""
    finish_reason: Optional[str] = None
    events: List[Any] = field(default_factory=list)
    #: Set when the turn could not run at all (no SDK, no runtime, crash,
    #: timeout). Distinct from a turn that ran and answered badly: the router
    #: must not confuse "the local agent failed" with "the local agent is not
    #: installed", because only the former is worth retrying locally.
    failure: Optional[str] = None

    @property
    def ran(self) -> bool:
        return self.failure is None


class DSHBackend(Protocol):
    """An agent that turns a prompt into a response plus its events."""

    name: str

    async def run(self, prompt: str, session_id: str) -> DSHTurn:
        ...

    async def close(self) -> None:
        ...


class RecordingBackend:
    """
    A scripted backend for tests and for `--check`.

    `responses` is consumed in order; running out means the backend reports a
    failure rather than replaying the last answer, so a test that expects more
    turns than it scripted fails loudly instead of silently reusing one.
    """

    def __init__(self, name: str, responses: Optional[List[DSHTurn]] = None):
        self.name = name
        self.responses = list(responses or [])
        self.prompts: List[str] = []
        self.session_ids: List[str] = []
        self.closed = False

    async def run(self, prompt: str, session_id: str) -> DSHTurn:
        self.prompts.append(prompt)
        self.session_ids.append(session_id)
        if not self.responses:
            return DSHTurn(failure=f"{self.name}: no scripted response left")
        return self.responses.pop(0)

    async def close(self) -> None:
        self.closed = True


class HarnessBackend:
    """
    The real thing: `deepseek-harness-sdk` driving a `dsh` runtime subprocess.

    Import of the SDK is deferred to `start()` so that a deployment which never
    enables DSH does not need the package installed at all, and so that a
    missing package produces one clear message instead of an ImportError at
    application start.
    """

    def __init__(self, name: str, settings: Any):
        self.name = name
        self.settings = settings
        self._harness: Any = None
        self._executor: Optional[ThreadPoolExecutor] = None
        self._lock = asyncio.Lock()

    # ── lifecycle ──────────────────────────────────────────────

    def _build(self) -> Any:
        from winvoice import _vendor

        _vendor.ensure("deepseek_harness")
        from deepseek_harness import DeepSeekHarness

        return DeepSeekHarness(
            dsh_home=str(self.settings.dsh_home),
            cwd=str(self.settings.workspace),
            runtime_cwd=str(self.settings.workspace),
            profile=self.settings.profile,
            provider=self.settings.provider,
            model=self.settings.model,
            reasoning_effort=self.settings.reasoning_effort or None,
            max_tokens=self.settings.max_tokens,
            base_url=self.settings.base_url or None,
            api_key=self.settings.api_key or None,
            patches=tuple(str(p) for p in self.settings.patches),
            # Placeholder credentials for keyless routes travel here: the
            # runtime subprocess inherits the caller's environment, and this
            # dict is merged on top of it.
            env=dict(getattr(self.settings, "runtime_env", {}) or {}),
            initialize_timeout_seconds=self.settings.initialize_timeout_seconds,
            # A bounded turn is what keeps a wedged model from holding the
            # microphone hostage; the router turns the resulting failure into
            # an escalation.
            request_timeout_seconds=self.settings.request_timeout_seconds,
        )

    async def start(self) -> None:
        """Launch the runtime once; safe to call concurrently."""
        async with self._lock:
            if self._harness is not None:
                return
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix=f"dsh-{self.name}"
                )
            loop = asyncio.get_running_loop()
            self._harness = await loop.run_in_executor(self._executor, self._build)

    async def run(self, prompt: str, session_id: str) -> DSHTurn:
        try:
            await self.start()
        except Exception as exc:
            logger.error("dsh_start_failed", backend=self.name, error=str(exc))
            return DSHTurn(failure=f"dsh_unavailable: {type(exc).__name__}: {exc}")

        assert self._executor is not None and self._harness is not None
        loop = asyncio.get_running_loop()

        def _call() -> Any:
            return self._harness.run(prompt, session_id=session_id)

        try:
            outcome = await loop.run_in_executor(self._executor, _call)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("dsh_run_failed", backend=self.name, error=str(exc))
            return DSHTurn(failure=f"dsh_error: {type(exc).__name__}: {exc}")

        return DSHTurn(
            final_response=getattr(outcome, "final_response", "") or "",
            finish_reason=getattr(outcome, "finish_reason", None),
            events=list(getattr(outcome, "events", []) or []),
        )

    async def close(self) -> None:
        if self._harness is not None and self._executor is not None:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(self._executor, self._harness.close)
            except Exception as exc:
                logger.warning("dsh_close_failed", backend=self.name, error=str(exc))
        self._harness = None
        if self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None


def build_backend(name: str, settings: Any) -> DSHBackend:
    """
    The backend for one tier.

    A tier that is switched off gets a `RecordingBackend` with no scripted
    answers, which reports a clean `failure` on every call. That keeps the
    router's shape identical whether or not the cloud tier exists, instead of
    scattering `if enabled` branches through the escalation policy.
    """
    if not getattr(settings, "enabled", False):
        logger.info("dsh_backend_disabled", backend=name)
        return RecordingBackend(name)
    return HarnessBackend(name, settings)


__all__ = ["DSHBackend", "DSHTurn", "HarnessBackend", "RecordingBackend", "build_backend"]
