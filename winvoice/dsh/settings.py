"""
DSH configuration, resolved from `config/config.yaml`.

Every path is made absolute here, once. The SDK resolves `cwd`, `runtime_cwd`
and `dsh_home` against the *process* working directory if left relative, and the
assistant is documented to be startable from anywhere via `run.ps1` — so a
relative `runtime/dsh_home` would silently land somewhere else depending on how
the user launched it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from winvoice.logging import get_logger

from .bridge import write_agent_patch

logger = get_logger(__name__)

#: Repository root, derived from this file rather than from the CWD.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_DSH_HOME = "runtime/dsh_home"
DEFAULT_PROFILE = "sdk"
DEFAULT_SERVER_NAME = "winvoice"
DEFAULT_TOOL_TIMEOUT_MS = 120_000


@dataclass(frozen=True)
class TierSettings:
    """One model tier: the local small model, or the cloud fallback."""

    name: str
    enabled: bool
    dsh_home: Path
    workspace: Path
    profile: str = DEFAULT_PROFILE
    provider: str = "deepseek-official"
    model: str = ""
    reasoning_effort: str = ""
    max_tokens: Optional[int] = None
    base_url: str = ""
    api_key: str = ""
    patches: Tuple[Path, ...] = ()
    initialize_timeout_seconds: float = 60.0
    request_timeout_seconds: Optional[float] = 90.0
    #: Mirrors `llm.remote.guest_allowed`: a guest may not reach the cloud tier.
    #: Only meaningful on the cloud tier, and defaults to False because
    #: `new_way.md` requires that the cloud agent "must NOT automatically receive
    #: broader permissions simply because it is more capable".
    guest_allowed: bool = False


@dataclass(frozen=True)
class BridgeSettings:
    """How the agent reaches this project's tools."""

    server_name: str = DEFAULT_SERVER_NAME
    tool_timeout_ms: int = DEFAULT_TOOL_TIMEOUT_MS
    bundle_dir: Path = PROJECT_ROOT / "runtime" / "dsh_bridge"
    utterance_file: Path = PROJECT_ROOT / "runtime" / "utterance.json"


@dataclass(frozen=True)
class DSHSettings:
    enabled: bool
    local: TierSettings
    cloud: TierSettings
    bridge: BridgeSettings = field(default_factory=BridgeSettings)
    escalation_enabled: bool = True
    #: Local attempts before escalating. 2 gives one retry, which is what a
    #: `retryable` verification failure is for (a slow app that had not appeared
    #: yet); escalating on the first such failure would spend a cloud round trip
    #: on something a second local attempt usually fixes.
    max_local_attempts: int = 2
    #: Hand-declared OpenAI-compatible routes, keyed by the name a request
    #: selects (`dsh.local.provider` / `dsh.cloud.provider`).
    providers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: Composed-profile row ids to switch off (token cost and capability).
    disabled_rows: Tuple[str, ...] = ()
    #: Deployment policy for the DeepSeek adapter's thinking level.
    reasoning_effort: str = ""
    #: Extra environment for the harness subprocess — placeholder credentials
    #: for routes whose server ignores authorization.
    runtime_env: Dict[str, str] = field(default_factory=dict)


def _path(value: Any, default: str) -> Path:
    """Absolute path for a configured value, resolved against the repo root."""
    raw = str(value).strip() if value not in (None, "") else default
    candidate = Path(raw)
    return candidate if candidate.is_absolute() else (PROJECT_ROOT / candidate)


def _tier(name: str, cfg: Any, workspace: Path) -> TierSettings:
    section = f"dsh.{name}"
    return TierSettings(
        name=name,
        enabled=bool(cfg.get(f"{section}.enabled", False)),
        dsh_home=_path(cfg.get(f"{section}.dsh_home"), DEFAULT_DSH_HOME),
        workspace=workspace,
        profile=str(cfg.get(f"{section}.profile", DEFAULT_PROFILE)),
        provider=str(cfg.get(f"{section}.provider", "deepseek-official")),
        model=str(cfg.get(f"{section}.model", "")),
        reasoning_effort=str(cfg.get(f"{section}.reasoning_effort", "") or ""),
        max_tokens=cfg.get(f"{section}.max_tokens"),
        base_url=str(cfg.get(f"{section}.base_url", "") or ""),
        api_key=str(cfg.get(f"{section}.api_key", "") or ""),
        initialize_timeout_seconds=float(cfg.get(f"{section}.initialize_timeout_s", 60)),
        request_timeout_seconds=cfg.get(f"{section}.request_timeout_s", 90) or None,
        guest_allowed=bool(cfg.get(f"{section}.guest_allowed", False)),
    )


def _placeholder_env(providers: Dict[str, Any]) -> Dict[str, str]:
    """
    Export a dummy credential for routes that declare `placeholder_key: true`.

    pi-ai refuses a route with no key at all (`No API key for provider: x`), and
    refuses one whose declared `apiKeyEnv` is unset (`MISSING_CREDENTIAL`). A
    server that ignores authorization — a local llama.cpp endpoint — therefore
    needs *a* value that is not a secret, and this supplies exactly that.

    Only ever applied to a route that opted in, so a real provider whose key is
    genuinely missing still fails loudly instead of silently authenticating with
    a placeholder. A variable already present in the environment always wins.
    """
    env: Dict[str, str] = {}
    for name, route in providers.items():
        if not isinstance(route, dict) or not route.get("placeholder_key"):
            continue
        variable = route.get("api_key_env")
        if not variable:
            logger.warning("placeholder_key_without_api_key_env", provider=name)
            continue
        env[str(variable)] = os.environ.get(str(variable)) or "winvoice-local-no-key"
    return env


def load_settings(config: Any = None) -> DSHSettings:
    """
    Resolve DSH settings. Safe to call when the config has no `dsh:` section at
    all: the whole feature is then simply off, which is the default.
    """
    if config is None:
        from winvoice.config import get_config

        config = get_config()

    workspace = PROJECT_ROOT
    local = _tier("local", config, workspace)
    cloud = _tier("cloud", config, workspace)

    bridge = BridgeSettings(
        server_name=str(config.get("dsh.bridge.server_name", DEFAULT_SERVER_NAME)),
        tool_timeout_ms=int(config.get("dsh.bridge.tool_timeout_ms", DEFAULT_TOOL_TIMEOUT_MS)),
        bundle_dir=_path(config.get("dsh.bridge.bundle_dir"), "runtime/dsh_bridge"),
        # Derived from the single `tools.utterance_file` rather than configured
        # twice. This file is the channel the guest check travels on: the audio
        # process writes it and the MCP server reads it, so two independent keys
        # for one path is a way to fail the permission check *open*, silently,
        # the moment someone edits one of them.
        utterance_file=_path(config.get("tools.utterance_file"), "runtime/utterance.json"),
    )

    settings = DSHSettings(
        enabled=bool(config.get("dsh.enabled", False)),
        local=local,
        cloud=cloud,
        bridge=bridge,
        escalation_enabled=bool(config.get("dsh.escalation_enabled", True)),
        max_local_attempts=max(1, int(config.get("dsh.max_local_attempts", 2))),
        providers=dict(config.get("dsh.providers", {}) or {}),
        disabled_rows=tuple(config.get("dsh.disabled_rows", []) or ()),
        reasoning_effort=str(config.get("dsh.reasoning_effort", "") or ""),
        runtime_env=_placeholder_env(dict(config.get("dsh.providers", {}) or {})),
    )

    # One generated patch carries every agent-level overlay, and both tiers get
    # it. Built here rather than by the caller so the SDK always receives a file
    # that matches the configuration that produced it.
    patch = write_agent_patch(
        dest=bridge.bundle_dir.parent / "dsh_agent_patch.yaml",
        providers=settings.providers,
        disabled_rows=list(settings.disabled_rows),
        reasoning_effort=settings.reasoning_effort,
    )
    if patch is not None:
        settings = replace(
            settings,
            local=replace(local, patches=(patch,)),
            cloud=replace(cloud, patches=(patch,)),
        )

    logger.info(
        "dsh_settings_loaded",
        enabled=settings.enabled,
        local=local.enabled,
        cloud=cloud.enabled,
        model=local.model or None,
        providers=list(settings.providers),
        disabled_rows=list(settings.disabled_rows),
        reasoning_effort=settings.reasoning_effort or None,
        patch=patch.name if patch else None,
    )
    return settings


__all__ = [
    "BridgeSettings",
    "DSHSettings",
    "DEFAULT_DSH_HOME",
    "PROJECT_ROOT",
    "TierSettings",
    "load_settings",
]
