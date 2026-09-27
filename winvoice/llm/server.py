"""
llama-server lifecycle: the assistant starts the local model server when it
needs it, and reuses one that is already running.

Before this module existed the deployment demanded a second terminal
(`llama-server -m … --port 8080`) *before* `python -m winvoice` was worth
starting — and forgetting it degraded three features at once, quietly: the
intent classifier (tier 2), the Q&A path (`IntentName.ASK`) and the local DSH
agent all talk to the same `llm.local.base_url`. The rule tier kept working,
so the assistant *looked* alive while half of it was dead.

Ownership rules, because a server manager that kills things it does not own is
a footgun:

* a server that answers `/health` before we spawn anything is **not ours** —
  it is reused, and `shutdown()` leaves it running;
* a server we spawned is recorded in `self._process` and terminated on
  shutdown;
* every failure here is a logged warning, never an exception: the assistant
  must keep starting with the features it still has (rules, small talk,
  deterministic tools), and the paths that need the model already say so out
  loud (「这个问题我现在答不上来。」).
"""

from __future__ import annotations

import glob
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from winvoice.logging import get_logger

logger = get_logger(__name__)

# Where the server's own output goes. It is a long-running child that chatters
# (tokens/s, prompt evals) — piping it into the assistant's structured log
# would bury both, so it gets a plain file next to the other logs.
SERVER_LOG_PATH = Path("logs") / "llama-server.log"

# Discovery order after an explicit (and missing) `llm.local.server_binary`:
# the extracted llama.cpp release the deployment docs use, then PATH.
_SERVER_GLOBS = ("tools/*/llama-server.exe", "tools/*/llama-server")

# llama.cpp answers 503 from /health while the model is still loading, so
# "reachable" means a 200 there — or, on builds without /health, a 200 from
# the OpenAI-compatible /models endpoint.
_HEALTH_PATH = "/health"
_MODELS_FALLBACK_PATH = "/models"


def _no_window_flags() -> int:
    """Don't pop a console window for the child (Windows-only flag; 0 elsewhere)."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


class LlamaServerManager:
    """
    Owns at most one llama-server child process.

    Everything is read from config at call time through the injected manager
    (tests pass a stub with a `.get()`), so a config edit plus a restart
    changes what gets spawned.
    """

    def __init__(self, config: Optional[Any] = None):
        self._cfg = config
        self._process: Optional[subprocess.Popen] = None
        self._log_handle = None

    # ── config ─────────────────────────────────────────────────

    def _get(self, key: str, default: Any = None) -> Any:
        if self._cfg is not None:
            return self._cfg.get(key, default)
        from winvoice.config import get_config

        return get_config().get(key, default)

    @property
    def base_url(self) -> str:
        return str(self._get("llm.local.base_url", "http://localhost:8080/v1")).rstrip("/")

    @property
    def server_root(self) -> str:
        """`http://localhost:8080/v1` → `http://localhost:8080` (health lives there)."""
        parsed = urlparse(self.base_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def port(self) -> int:
        return urlparse(self.base_url).port or (443 if self.base_url.startswith("https") else 80)

    def _find_binary(self) -> Optional[str]:
        configured = str(self._get("llm.local.server_binary", "") or "").strip()
        if configured:
            return configured if Path(configured).exists() else None
        for pattern in _SERVER_GLOBS:
            hits = sorted(glob.glob(pattern))
            if hits:
                return hits[0]
        return shutil.which("llama-server")

    def _build_command(self, binary: str) -> list[str]:
        command = [
            binary,
            "-m", str(self._get("llm.local.model_file", "")),
            "--port", str(self.port),
            "-c", str(self._get("llm.local.server_context", 4096)),
        ]
        extra = self._get("llm.local.server_args", []) or []
        command += [str(arg) for arg in extra]
        return command

    # ── health ─────────────────────────────────────────────────

    def is_healthy(self) -> bool:
        """Public health probe (`--check` and tests)."""
        return self._health_ok()

    def _health_ok(self) -> bool:
        try:
            if httpx.get(f"{self.server_root}{_HEALTH_PATH}", timeout=2.0).status_code == 200:
                return True
        except Exception:
            pass
        try:
            return httpx.get(f"{self.base_url}{_MODELS_FALLBACK_PATH}", timeout=2.0).status_code == 200
        except Exception:
            return False

    # ── lifecycle ──────────────────────────────────────────────

    def ensure_running(self) -> bool:
        """
        True when a llama-server is reachable on return.

        Reuses a healthy server whatever started it. Spawning is best-effort:
        a missing binary or model logs a warning and returns False, and the
        assistant keeps going without the model-backed features.
        """
        if self._health_ok():
            logger.info("llama_server_reused", root=self.server_root)
            return True

        if not bool(self._get("llm.local.auto_start", True)):
            logger.info("llama_server_auto_start_disabled", root=self.server_root)
            return False

        binary = self._find_binary()
        if binary is None:
            logger.warning("llama_server_binary_not_found", hint="set llm.local.server_binary")
            return False
        model_file = Path(str(self._get("llm.local.model_file", "")))
        if not model_file.exists():
            logger.warning("llama_server_model_missing", path=str(model_file))
            return False

        return self._spawn_and_wait(binary)

    def _spawn_and_wait(self, binary: str) -> bool:
        command = self._build_command(binary)
        SERVER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = open(SERVER_LOG_PATH, "ab")

        logger.info("llama_server_starting", binary=binary, port=self.port, log=str(SERVER_LOG_PATH))
        try:
            self._process = subprocess.Popen(
                command,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=_no_window_flags(),
            )
        except OSError as e:
            logger.warning("llama_server_spawn_failed", error=str(e))
            self._close_log()
            return False

        timeout_s = float(self._get("llm.local.start_timeout_s", 60.0))
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                logger.warning(
                    "llama_server_failed",
                    returncode=self._process.returncode,
                    log_tail=self._log_tail(),
                )
                return False
            if self._health_ok():
                logger.info(
                    "llama_server_started",
                    pid=self._process.pid,
                    port=self.port,
                    model=str(self._get("llm.local.model_file", "")),
                )
                return True
            time.sleep(0.5)

        # Still loading after the budget: leave the child alone (it may finish
        # loading and serve later turns), but report that *we* are not ready.
        logger.warning(
            "llama_server_start_timeout",
            timeout_s=timeout_s,
            pid=self._process.pid,
            log_tail=self._log_tail(),
        )
        return False

    def _log_tail(self, limit: int = 600) -> str:
        try:
            with open(SERVER_LOG_PATH, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - limit * 4))
                text = f.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
        return " ".join(text.split())[-limit:]

    def shutdown(self) -> None:
        """Stop the child **we** spawned; a reused server is not ours to stop."""
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            logger.info("llama_server_stopping", pid=process.pid)
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self._close_log()

    def _close_log(self) -> None:
        handle, self._log_handle = self._log_handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
