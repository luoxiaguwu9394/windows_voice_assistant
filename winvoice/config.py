"""
Configuration management with hot-reload support.

- Loads config/config.yaml with ${VAR} expansion (os.path.expandvars)
- Watchdog monitors file changes
- Publishes ConfigChanged events via ZeroMQ PUB/SUB (or callback in single-process mode)
- Field allowlist controls which keys are hot-reloadable vs require restart
"""

from __future__ import annotations

import logging as _stdlib_logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

import yaml
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .contracts import ConfigChanged

# NOTE: using stdlib logging (not winvoice.logging) because winvoice.logging
# imports this module — a structlog import here would be circular.
_log = _stdlib_logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────
# Hot-reloadable field allowlist
# ──────────────────────────────────────────────────────────────

HOT_RELOADABLE: Set[str] = {
    "llm.local.confidence_threshold",
    # The Q&A path reads these per question: the switch, the spoken-length cap,
    # and the timeout all tune without a restart.
    "llm.ask.enabled",
    "llm.ask.max_chars",
    "llm.ask.max_tokens",
    "llm.ask.timeout_s",
    "tools.apps",
    "tools.sensitive_apps",
    # Read when a confirmation is armed / a search is intercepted, so the
    # window and the ask-first behaviour tune without a restart.
    "tools.confirm_timeout_s",
    "tools.search_web_confirm",
    "tts.voice",
    # The speech path reads these per utterance (`speech_text_config()`), so a
    # change really does apply to the next reply — which is what makes prosody
    # tunable instead of guessed at. `scripts/show_segmentation.py` previews them.
    "tts.reply_max_chars",
    "tts.first_chunk_max_chars",
    "tts.clause_max_chars",
    "tts.chunk_max_chars",
    "tts.chunk_min_chars",
    "tts.hard_max_chars",
    "tts.tail_silence_ms",
    "tts.pause_sentence_ms",
    "tts.pause_question_ms",
    "tts.pause_exclaim_ms",
    "tts.pause_ellipsis_ms",
    "tts.pause_semicolon_ms",
    "tts.pause_comma_ms",
    "tts.pause_enumeration_ms",
    "tts.pause_colon_ms",
    "tts.pause_paragraph_ms",
    "tts.pause_forced_ms",
    "tts.pause_conjunction_ms",
    # Read per utterance with the pause table: the gap collapse shapes the same
    # rhythm, so it tunes the same way.
    "tts.max_internal_gap_ms",
    "tts.internal_gap_keep_ms",
    "llm.remote.enabled",
    "llm.remote.base_url",
    "llm.remote.model",
    "audio.kws_during_tts",
    # `get_weather` reads these on every call, so a change really does apply
    # without a restart — unlike anything an engine loads in initialize().
    "weather.enabled",
    "weather.city",
    "weather.timeout_s",
}

REQUIRES_RESTART: Set[str] = {
    "audio.input_device",
    "audio.sample_rate",
    "audio.half_duplex",
    # The output stream is opened once, at startup, and the player holds it for
    # the whole session — a new rate or device means re-opening it, i.e. a
    # restart, not a hot reload.
    "audio.output_device",
    "audio.output_sample_rate",
    "audio.output_host_api",
    "audio.output_prebuffer_ms",
    "audio.output_blocksize_ms",
    "kws.model",
    "kws.keywords",
    "kws.threshold",
    "vad.model",
    "vad.min_silence_ms",
    "vad.min_speech_ms",
    "asr.model",
    "asr.language",
    "sv.model",
    "sv.enabled",
    "sv.threshold_high",
    "sv.threshold_low",
    "sv.adaptive_update",
    "sv.update_weight",
    "sv.anchor_check_days",
    "llm.local.base_url",
    "llm.local.api_key",
    "llm.local.model",
    # llama-server lifecycle: the manager reads these when the assistant
    # starts (and only spawns from a fresh config), so a change means restart.
    "llm.local.auto_start",
    "llm.local.server_binary",
    "llm.local.model_file",
    "llm.local.server_context",
    "llm.local.start_timeout_s",
    "llm.local.server_args",
    "tts.model",
    "tts.fallback_models",
    "tts.backend",
    "tts.vocoder",
    "tts.speaker_id",
    "tts.guest_speaker_id",
    "tts.speed",
    "tts.guest_speed",
    # Read by TtsEngine.__init__ (the claimed-rate trick needs it before the
    # first synthesize), so a change only applies to a fresh engine.
    "tts.pitch",
    "tts.num_threads",
    "tts.trim_silence",
    "tts.trim_ratio",
    "tts.trim_guard_ms",
    "tools.destructive",
    "tools.confirm_required",
    # Read once when the executor and the MCP server are constructed, so a change
    # cannot reach a running process.
    "tools.verification_enabled",
    "tools.utterance_file",
    # DSH settings are consumed when the runtime subprocess is launched; changing
    # the model, provider, home or bridge in place would leave the live agent on
    # the old one, so these require a restart rather than pretending to apply.
    "dsh.enabled",
    "dsh.escalation_enabled",
    "dsh.max_local_attempts",
    "dsh.local.enabled",
    "dsh.local.dsh_home",
    "dsh.local.profile",
    "dsh.local.provider",
    "dsh.local.model",
    "dsh.local.reasoning_effort",
    "dsh.local.max_tokens",
    "dsh.local.base_url",
    "dsh.local.api_key",
    "dsh.local.initialize_timeout_s",
    "dsh.local.request_timeout_s",
    "dsh.cloud.enabled",
    "dsh.cloud.dsh_home",
    "dsh.cloud.profile",
    "dsh.cloud.provider",
    "dsh.cloud.model",
    "dsh.cloud.reasoning_effort",
    "dsh.cloud.max_tokens",
    "dsh.cloud.base_url",
    "dsh.cloud.api_key",
    "dsh.cloud.initialize_timeout_s",
    "dsh.cloud.request_timeout_s",
    "dsh.cloud.guest_allowed",
    "dsh.bridge.server_name",
    "dsh.bridge.tool_timeout_ms",
    "dsh.bridge.bundle_dir",
    "dsh.bridge.utterance_file",
    "snapshot.enabled",
    "snapshot.path",
    "snapshot.max_size_gb",
    "context.enabled",
    "context.scope",
    "context.max_turns",
    "storage.max_total_gb",
}


# ──────────────────────────────────────────────────────────────
# ConfigManager
# ──────────────────────────────────────────────────────────────

class ConfigManager:
    """
    Thread-safe configuration manager with file watching and change callbacks.
    """

    def __init__(
        self,
        config_path: str | Path = "config/config.yaml",
        on_change: Optional[Callable[[ConfigChanged], None]] = None,
    ):
        self.config_path = Path(config_path).resolve()
        self._config: Dict[str, Any] = {}
        self._lock = threading.RLock()
        self._on_change = on_change
        self._observer: Optional[Observer] = None
        self._load()

    # ─── Public API ────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """Get nested config value by dot-notation key (e.g., 'llm.local.model')."""
        with self._lock:
            return self._get_nested(self._config, key.split("."), default)

    def get_all(self) -> Dict[str, Any]:
        with self._lock:
            return self._config.copy()

    def set(self, key: str, value: Any) -> None:
        """Set nested config value and persist to disk."""
        with self._lock:
            self._set_nested(self._config, key.split("."), value)
            self._persist()

    def is_hot_reloadable(self, key: str) -> bool:
        return key in HOT_RELOADABLE

    def requires_restart(self, key: str) -> bool:
        return key in REQUIRES_RESTART

    def start_watching(self) -> None:
        """Start file system watcher for hot-reload."""
        if self._observer:
            return

        class Handler(FileSystemEventHandler):
            def __init__(self, manager: ConfigManager):
                self.manager = manager
                self._last_modified = 0

            def on_modified(self, event):
                if event.src_path == str(self.manager.config_path):
                    now = time.time()
                    if now - self._last_modified > 0.5:  # debounce
                        self._last_modified = now
                        self.manager._reload()

        self._observer = Observer()
        self._observer.schedule(Handler(self), str(self.config_path.parent), recursive=False)
        self._observer.start()

    def stop_watching(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None

    # ─── Internal ──────────────────────────────────────────────

    def _load(self, strict: bool = False) -> None:
        """
        Read, expand and parse the YAML config.

        `${VAR}` placeholders are expanded from the environment. An
        unresolved placeholder is tolerated when it sits inside a section
        that is explicitly disabled (e.g. `llm.remote.enabled: false`),
        because an unused feature must not stop the assistant from booting.
        Any other unresolved placeholder raises, naming the offending key
        path so the fix is obvious.

        `WINVOICE_CONFIG_TOLERANT=1` downgrades that error to the tolerated
        path for every placeholder. The MCP tool server sets it, because it is
        spawned by DeepSeek Harness with the environment **scrubbed of anything
        matching /KEY|PASSWORD|SECRET|TOKEN/i** — so `DEEPSEEK_API_KEY` is gone
        by design, and a config that names it would otherwise kill the tool
        server at startup. That server needs the tool registry and nothing else;
        refusing to serve tools because an unrelated cloud key is absent is a
        worse failure than any key it could read.
        """
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config not found: {self.config_path}")

        # `utf-8-sig` over `utf-8`: users edit this file with Notepad and patch
        # it with PowerShell — both routinely leave a BOM (EF BB BF) that plain
        # utf-8 turns into a ParserError at line 1 and a dead assistant.
        try:
            with open(self.config_path, "r", encoding="utf-8-sig") as f:
                raw = f.read()
        except UnicodeDecodeError as error:
            # A GBK/ANSI save (Notepad's default "ANSI" on a Chinese system)
            # is not recoverable here — say so instead of a codec traceback.
            raise ValueError(
                f"配置文件不是有效的 UTF-8：{self.config_path}\n"
                f"  多半是被记事本以 ANSI/GBK 保存过。请用「重新配置」重新生成，"
                f"或以 UTF-8（无 BOM）重新保存。\n  原始错误：{error}"
            ) from error

        try:
            expanded = os.path.expandvars(raw)
            self._config = yaml.safe_load(expanded) or {}
        except yaml.YAMLError as error:
            raise ValueError(
                f"配置文件 YAML 无法解析：{self.config_path}\n"
                f"  {error}\n"
                f"  最近一次手工编辑（记事本 / PowerShell Set-Content）常是原因："
                f"中文 Windows 下它们可能改变编码或破坏缩进。"
                f"可重跑安装器选「重新配置此安装」重新生成（模型与声纹保留）。"
            ) from error

        unresolved = self._find_unresolved(self._config)
        tolerated: List[str] = []
        lenient = strict is False and os.environ.get("WINVOICE_CONFIG_TOLERANT") == "1"

        for path in unresolved:
            if strict or not (lenient or self._is_in_disabled_section(path)):
                raise ValueError(
                    f"Unresolved environment variable at '{path}'.\n"
                    f"  Set it, e.g.:  setx {path.rsplit('.', 1)[-1].upper()} \"<value>\"\n"
                    f"  …or disable the feature that needs it in {self.config_path.name}."
                )
            tolerated.append(path)

        if tolerated:
            for path in tolerated:
                self._blank_out(path)
            # The project logger renders to stdout. A stdlib-logging warning
            # here lands on stderr, and Windows PowerShell 5.1 wraps every
            # stderr line of `native.exe 2>&1 | Tee-Object` into a red
            # NativeCommandError — a benign startup note read as a failure.
            # (config.py cannot import .logging at module level: logging
            # imports config.) The stdlib fallback keeps working when even the
            # lazy import is impossible.
            try:
                from .logging import get_logger  # noqa: PLC0415 - see above

                get_logger(__name__).warning(
                    "config_unresolved_env_vars_tolerated",
                    paths=", ".join(tolerated),
                )
            except Exception:
                _log.warning(
                    "config: unresolved env var(s) %s ignored because the owning "
                    "section is disabled; substituted empty string",
                    ", ".join(tolerated),
                )

    @staticmethod
    def _find_unresolved(config: Dict[str, Any], prefix: str = "") -> List[str]:
        """Return dotted key paths whose (string) value still has a ${VAR} token."""
        out: List[str] = []
        for key, value in (config or {}).items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                out.extend(ConfigManager._find_unresolved(value, path))
            elif isinstance(value, str) and "${" in value:
                out.append(path)
        return out

    def _is_in_disabled_section(self, path: str) -> bool:
        """True when `path` belongs to a section carrying `enabled: false`."""
        parts = path.split(".")
        for depth in range(len(parts) - 1, 0, -1):
            node: Any = self._config
            for key in parts[:depth]:
                if not isinstance(node, dict) or key not in node:
                    node = None
                    break
                node = node[key]
            if isinstance(node, dict) and node.get("enabled") is False:
                return True
        return False

    def _blank_out(self, path: str) -> None:
        """Replace a `${VAR}` placeholder with an empty string."""
        parts = path.split(".")
        node: Any = self._config
        for key in parts[:-1]:
            node = node[key]
        value = node[parts[-1]]
        node[parts[-1]] = re.sub(r"\$\{[^}]+\}", "", value)

    def _reload(self) -> None:
        old_config = self._config.copy()
        self._load()
        changed = self._diff(old_config, self._config)
        if changed and self._on_change:
            event = ConfigChanged(
                changed_keys=list(changed.keys()),
                new_values=changed,
            )
            self._on_change(event)

    def _persist(self) -> None:
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self._config, f, allow_unicode=True, sort_keys=False)

    @staticmethod
    def _get_nested(d: Dict, keys: List[str], default: Any) -> Any:
        for k in keys:
            if isinstance(d, dict) and k in d:
                d = d[k]
            else:
                return default
        return d

    @staticmethod
    def _set_nested(d: Dict, keys: List[str], value: Any) -> None:
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = value

    @staticmethod
    def _diff(old: Dict, new: Dict, prefix: str = "") -> Dict[str, Any]:
        """Return flat dict of changed keys with new values."""
        changes = {}
        all_keys = set(old.keys()) | set(new.keys())
        for k in all_keys:
            path = f"{prefix}.{k}" if prefix else k
            o, n = old.get(k), new.get(k)
            if isinstance(o, dict) and isinstance(n, dict):
                changes.update(ConfigManager._diff(o, n, path))
            elif o != n:
                changes[path] = n
        return changes


# ──────────────────────────────────────────────────────────────
# Singleton accessor
# ──────────────────────────────────────────────────────────────

_config_manager: Optional[ConfigManager] = None
_config_lock = threading.Lock()


def get_config(config_path: str | Path = "config/config.yaml") -> ConfigManager:
    global _config_manager
    with _config_lock:
        if _config_manager is None:
            _config_manager = ConfigManager(config_path)
        return _config_manager


def reset_config() -> None:
    """For testing."""
    global _config_manager
    with _config_lock:
        if _config_manager:
            _config_manager.stop_watching()
        _config_manager = None