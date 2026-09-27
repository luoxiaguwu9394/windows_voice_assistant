"""
The llama-server manager: reuse a running server, spawn one when needed,
never touch a server it does not own.

Every test drives the manager with a fake `Popen` and a scripted health
endpoint — no real llama-server is started, and nothing binds a port. The one
behaviour that must never regress is ownership: `shutdown()` terminates only a
child this manager spawned, because killing the user's own terminal-run server
would be worse than not managing the server at all.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from winvoice.llm import server as server_module
from winvoice.llm.server import LlamaServerManager

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class FakeConfig:
    def __init__(self, **values) -> None:
        self.values = {
            "llm.local.base_url": "http://localhost:8091/v1",
            "llm.local.auto_start": True,
            "llm.local.server_context": 4096,
            "llm.local.start_timeout_s": 2.0,
            "llm.local.server_args": [],
        }
        self.values.update(values)

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakePopen:
    """Records the spawn; the 'server becomes healthy' when it is started."""

    instances: list["FakePopen"] = []

    def __init__(self, command, stdout=None, stderr=None, stdin=None, creationflags=0):
        self.command = command
        self.pid = 4321
        self.terminated = False
        self.killed = False
        self._returncode = None
        self.creationflags = creationflags
        FakePopen.instances.append(self)

    @property
    def returncode(self):
        return self._returncode

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = 0

    def kill(self):
        self.killed = True
        self._returncode = 0

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def scratch():
    """A repo-internal scratch tree with a fake binary + model (no tmp_path, see §0)."""
    base = PROJECT_ROOT / "runtime" / "_pytest_work"
    directory = base / f"llmsrv_{uuid.uuid4().hex[:10]}"
    (directory / "bin").mkdir(parents=True)
    (directory / "bin" / "llama-server.exe").write_bytes(b"MZ fake")
    (directory / "model.gguf").write_bytes(b"gguf fake")
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def health():
    """Scriptable health endpoint."""
    state = {"status": 503}

    def fake_get(url, timeout=None):
        code = state["status"]
        if code == "raise":
            raise OSError("connection refused")
        return SimpleNamespace(status_code=code)

    yield state


@pytest.fixture(autouse=True)
def _fakes(monkeypatch, health):
    monkeypatch.setattr(server_module.httpx, "get", _fake_get(health))
    monkeypatch.setattr(server_module.subprocess, "Popen", FakePopen)
    FakePopen.instances.clear()


def _fake_get(health):
    def get(url, timeout=None):
        code = health["status"]
        if code == "raise":
            raise OSError("connection refused")
        return SimpleNamespace(status_code=code)

    return get


def _manager(scratch: Path, **values) -> LlamaServerManager:
    return LlamaServerManager(
        FakeConfig(
            **{
                "llm.local.server_binary": str(scratch / "bin" / "llama-server.exe"),
                "llm.local.model_file": str(scratch / "model.gguf"),
                "llm.local.server_args": ["-fa", "on"],
                **values,
            }
        )
    )


# ── reuse vs spawn ────────────────────────────────────────────


def test_a_running_server_is_reused_never_respawned(scratch):
    health = {"status": 200}
    manager = LlamaServerManager(FakeConfig())
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(server_module.httpx, "get", _fake_get(health))
        assert manager.ensure_running() is True
    assert FakePopen.instances == [], "spawned over a healthy server"


def test_a_down_server_is_spawned_and_waited_for(scratch, health):
    manager = _manager(scratch, **{"llm.local.start_timeout_s": 0.3})

    assert manager.ensure_running() is False, "health never turned OK in this test"

    # The spawn happened with the configured command…
    assert len(FakePopen.instances) == 1
    command = FakePopen.instances[0].command
    assert command[0] == str(scratch / "bin" / "llama-server.exe")
    assert "-m" in command and str(scratch / "model.gguf") in command
    assert "--port" in command and "8091" in command
    assert "-c" in command and "4096" in command
    assert "-fa" in command and "on" in command
    assert FakePopen.instances[0].creationflags == getattr(
        server_module.subprocess, "CREATE_NO_WINDOW", 0
    )

    # …and the manager cleans up its own child.
    manager.shutdown()
    assert FakePopen.instances[0].terminated is True


def test_the_server_coming_up_is_reported_as_started(scratch, health):
    def spawn_makes_healthy(command, **kwargs):
        health["status"] = 200
        return FakePopen(command, **kwargs)

    original = server_module.subprocess.Popen
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(server_module.subprocess, "Popen", spawn_makes_healthy)
        manager = _manager(scratch)
        assert manager.ensure_running() is True

    assert manager.shutdown() is None
    assert manager.is_healthy() is True, "the fake endpoint stays healthy"


# ── graceful degradation ──────────────────────────────────────


def test_auto_start_disabled_leaves_everything_alone(scratch, health):
    manager = _manager(scratch, **{"llm.local.auto_start": False})

    assert manager.ensure_running() is False
    assert FakePopen.instances == []


def test_a_missing_binary_degrades_gracefully(scratch, health):
    manager = LlamaServerManager(
        FakeConfig(
            **{
                "llm.local.server_binary": str(scratch / "nope.exe"),
                "llm.local.model_file": str(scratch / "model.gguf"),
            }
        )
    )

    assert manager.ensure_running() is False
    assert FakePopen.instances == []


def test_a_missing_model_degrades_gracefully(scratch, health):
    manager = _manager(scratch, **{"llm.local.model_file": str(scratch / "nope.gguf")})

    assert manager.ensure_running() is False
    assert FakePopen.instances == []


def test_a_failing_spawn_is_not_fatal(scratch, health, monkeypatch):
    def refused(command, **kwargs):
        raise OSError("Access is denied")

    monkeypatch.setattr(server_module.subprocess, "Popen", refused)
    manager = _manager(scratch)

    assert manager.ensure_running() is False
    manager.shutdown()  # must be a no-op, not a crash


def test_a_child_that_exits_early_is_reported(scratch, health):
    manager = _manager(scratch)

    # FakePopen stays alive; simulate a crash right after spawn.
    original = server_module.subprocess.Popen

    class _Dying(FakePopen):
        def __init__(self, command, **kwargs):
            super().__init__(command, **kwargs)
            self._returncode = 1

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(server_module.subprocess, "Popen", _Dying)
        manager = LlamaServerManager(
            FakeConfig(
                **{
                    "llm.local.server_binary": str(scratch / "bin" / "llama-server.exe"),
                    "llm.local.model_file": str(scratch / "model.gguf"),
                    "llm.local.start_timeout_s": 1.0,
                }
            )
        )
        assert manager.ensure_running() is False


def test_a_slow_model_load_is_not_killed(scratch, health):
    """Timeout reports 'not ready' but leaves the child loading; shutdown reaps it."""
    manager = _manager(scratch, **{"llm.local.start_timeout_s": 0.2})

    assert manager.ensure_running() is False
    assert manager.is_healthy() is False

    manager.shutdown()
    assert FakePopen.instances[0].terminated is True


def test_shutdown_without_spawn_is_a_noop():
    LlamaServerManager(FakeConfig()).shutdown()


# ── URL / discovery helpers ───────────────────────────────────


def test_the_port_and_root_come_from_base_url():
    manager = LlamaServerManager(FakeConfig())
    assert manager.port == 8091
    assert manager.server_root == "http://localhost:8091"


def test_binary_discovery_order(scratch, monkeypatch):
    """Configured-and-existing wins; a missing configured name falls through."""
    manager = _manager(scratch)
    assert manager._find_binary() == str(scratch / "bin" / "llama-server.exe")

    monkeypatch.setattr(server_module, "_SERVER_GLOBS", ())
    monkeypatch.setattr(server_module.shutil, "which", lambda name: None)
    manager = LlamaServerManager(
        FakeConfig(
            **{
                "llm.local.server_binary": str(scratch / "nope.exe"),
                "llm.local.model_file": str(scratch / "model.gguf"),
            }
        )
    )
    assert manager._find_binary() is None
