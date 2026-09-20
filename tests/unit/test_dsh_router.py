"""
Escalation policy: when is the cloud model actually worth a round trip?

`new_way.md` asks for two things that pull against each other — "minimize cloud
usage while maintaining reliability" — and resolves the tension by insisting the
decision be programmatic rather than a feeling:

    「尤其是不要让本地模型自己说"我很确定"然后决定自己是否可信。」

The cases below are the whole policy. Each one is a way a naive implementation
gets it wrong:

* escalating a *deterministic* refusal (the cloud is refused identically, so the
  round trip buys nothing and costs latency);
* escalating an *unobservable* action (every web search would go to the cloud);
* escalating something the machine already confirmed, just because the tool's own
  `success` flag disagreed;
* and the reverse — accepting a local turn that the machine contradicted.

The local and cloud agents are `RecordingBackend`s, so none of this needs an API
key, a Node runtime or a network.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import pytest

from winvoice.dsh.backend import DSHTurn, RecordingBackend
from winvoice.dsh.router import DSHRouter, build_escalation_context
from winvoice.dsh.settings import BridgeSettings, DSHSettings, TierSettings
from winvoice.dsh.validation import assess_turn, extract_tool_activity

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def work():
    """
    A scratch directory inside the repository.

    Not pytest's `tmp_path`: that lives outside the workspace and is refused by
    the file sandbox this project is developed under, which would make every
    test here error out before it asserted anything.
    """
    base = PROJECT_ROOT / "runtime" / "_pytest_work"
    base.mkdir(parents=True, exist_ok=True)
    directory = base / uuid.uuid4().hex[:12]
    directory.mkdir(parents=True, exist_ok=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# ──────────────────────────────────────────────────────────────
# Event fixtures
# ──────────────────────────────────────────────────────────────

def tool_result_event(
    success: bool,
    status: str | None = None,
    retryable: bool = False,
    reason: str = "",
    speak: str = "",
    error: str = "",
    name: str = "",
) -> dict:
    """
    One session event carrying a tool result, shaped like the payload the MCP
    server emits (which the extractor recognises by its `success` key).
    """
    payload = {"success": success}
    if speak:
        payload["speak"] = speak
    if error:
        payload["error"] = error
    if status:
        payload["verification"] = {
            "status": status,
            "verified": status == "verified",
            "reason": reason,
            "retryable": retryable,
            "checks": [],
        }
    if name:
        payload["tool"] = name

    return {
        "type": "tool/result",
        "data": {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]},
    }


def settings(
    work: Path,
    local_attempts: int = 2,
    cloud: bool = True,
    cloud_guest_allowed: bool = False,
) -> DSHSettings:
    def tier(name: str, enabled: bool, guest_allowed: bool = False) -> TierSettings:
        return TierSettings(
            name=name,
            enabled=enabled,
            dsh_home=work / name,
            workspace=PROJECT_ROOT,
            guest_allowed=guest_allowed,
        )

    return DSHSettings(
        enabled=True,
        local=tier("local", True),
        cloud=tier("cloud", cloud, cloud_guest_allowed),
        bridge=BridgeSettings(
            bundle_dir=work / "bundle",
            utterance_file=work / "utterance.json",
        ),
        escalation_enabled=True,
        max_local_attempts=local_attempts,
    )


# ──────────────────────────────────────────────────────────────
# Assessment
# ──────────────────────────────────────────────────────────────

def test_verified_failure_does_not_escalate() -> None:
    """`close_app` on something already closed: the goal holds."""
    events = [tool_result_event(success=False, status="verified", reason="notepad.exe is not running")]

    assessment = assess_turn("记事本好像没有在运行。", "completed", events)

    assert assessment.resolved is True
    assert assessment.verification_failures == []


def test_observed_mismatch_escalates() -> None:
    events = [
        tool_result_event(
            success=True,
            status="failed",
            reason="chrome.exe did not appear within 1s",
            retryable=True,
            name="open_app",
        )
    ]

    assessment = assess_turn("已经打开谷歌浏览器了。", "completed", events)

    assert assessment.resolved is False
    assert assessment.reason == "verification_failed"
    assert assessment.retryable is True
    assert len(assessment.verification_failures) == 1


def test_uncertain_and_unverifiable_do_not_escalate() -> None:
    for status in ("uncertain", "not_verifiable"):
        assessment = assess_turn(
            "已经打开浏览器了。", "completed", [tool_result_event(success=True, status=status)]
        )
        assert assessment.resolved is True, status


def test_deterministic_refusal_does_not_escalate() -> None:
    """
    A refusal with no verification verdict is the allowlist or the confirmation
    gate talking. The cloud model gets the same refusal, so escalating would
    spend a cloud call to learn nothing.
    """
    events = [tool_result_event(success=False, error="App not allowed: wechat", speak="微信不在我能打开的名单里。")]

    assessment = assess_turn("微信不在我能打开的名单里。", "completed", events)

    assert assessment.resolved is True
    assert assessment.verification_failures == []


def test_truncated_turn_escalates() -> None:
    assessment = assess_turn("已经打开了。", "max-tokens", [])

    assert assessment.resolved is False
    assert assessment.reason == "turn_ended_max-tokens"
    assert assessment.retryable is True


def test_error_turn_escalates_without_a_local_retry() -> None:
    assessment = assess_turn("嗯……", "error", [])

    assert assessment.resolved is False
    assert assessment.retryable is False


def test_empty_response_escalates() -> None:
    assessment = assess_turn("   ", "completed", [])

    assert assessment.resolved is False
    assert assessment.reason == "empty_response"


def test_extraction_ignores_prose_and_deduplicates() -> None:
    """The extractor must not mistake ordinary text — or a repeated payload — for
    a tool result."""
    events = [
        {"type": "assistant/message", "data": {"content": [{"type": "text", "text": "我把文件放好了。"}]}},
        {"type": "note", "data": {"text": "not json at all"}},
        {"type": "note", "data": {"text": '{"unrelated": true}'}},
        tool_result_event(success=True, status="verified", name="write_file"),
        tool_result_event(success=True, status="verified", name="write_file"),
    ]

    activity = extract_tool_activity(events)

    assert len(activity) == 1
    assert activity[0].name == "write_file"
    assert activity[0].verification_status == "verified"


def test_extraction_is_depth_and_size_bounded() -> None:
    """A deeply nested or huge event list must not stall the voice path."""
    nested: dict = {"type": "x"}
    node = nested
    for _ in range(40):
        node["data"] = {}
        node = node["data"]
    node["text"] = json.dumps({"success": True})

    assert extract_tool_activity([nested]) == []


# ──────────────────────────────────────────────────────────────
# Router
# ──────────────────────────────────────────────────────────────

async def test_local_success_is_returned_without_escalating(work: Path) -> None:
    local = RecordingBackend("local", [DSHTurn(final_response="记事本已经打开了。", finish_reason="completed")])
    cloud = RecordingBackend("cloud", [])
    router = DSHRouter(settings(work), local=local, cloud=cloud)

    outcome = await router.route("打开记事本", trace_id="t1")

    assert outcome.resolved is True
    assert outcome.source == "local"
    assert outcome.response == "记事本已经打开了。"
    assert cloud.prompts == []


async def test_verification_failure_escalates_with_context(work: Path) -> None:
    local = RecordingBackend(
        "local",
        [
            DSHTurn(
                final_response="已经打开了。",
                finish_reason="completed",
                events=[tool_result_event(True, "failed", retryable=False, reason="not installed", name="open_app")],
            )
        ],
    )
    cloud = RecordingBackend("cloud", [DSHTurn(final_response="我没找到这个程序。", finish_reason="completed")])
    router = DSHRouter(settings(work), local=local, cloud=cloud)

    outcome = await router.route("打开微信", trace_id="t2")

    assert outcome.resolved is True
    assert outcome.source == "cloud"
    assert outcome.escalation_reason == "verification_failed"
    assert len(cloud.prompts) == 1
    # The cloud agent must be told what the local one already tried.
    assert "打开微信" in cloud.prompts[0]
    assert "open_app" in cloud.prompts[0]
    assert "not installed" in cloud.prompts[0]


async def test_retryable_failure_gets_a_local_retry_first(work: Path) -> None:
    """A slow app deserves a second local attempt, not a cloud round trip."""
    failed = DSHTurn(
        final_response="已经打开了。",
        finish_reason="completed",
        events=[tool_result_event(True, "failed", retryable=True, reason="too slow", name="open_app")],
    )
    local = RecordingBackend(
        "local",
        [failed, DSHTurn(final_response="谷歌浏览器已经打开了。", finish_reason="completed")],
    )
    cloud = RecordingBackend("cloud", [])
    router = DSHRouter(settings(work), local=local, cloud=cloud)

    outcome = await router.route("打开谷歌浏览器", trace_id="t3")

    assert outcome.resolved is True
    assert outcome.source == "local"
    assert outcome.attempts == 2
    assert cloud.prompts == []


async def test_non_retryable_failure_escalates_after_one_attempt(work: Path) -> None:
    failed = DSHTurn(
        final_response="好了。",
        finish_reason="completed",
        events=[tool_result_event(True, "failed", retryable=False, reason="gone", name="write_file")],
    )
    local = RecordingBackend("local", [failed, DSHTurn(final_response="unused", finish_reason="completed")])
    cloud = RecordingBackend("cloud", [DSHTurn(final_response="写不进去。", finish_reason="completed")])
    router = DSHRouter(settings(work), local=local, cloud=cloud)

    outcome = await router.route("写入文件", trace_id="t4")

    assert outcome.attempts == 1
    assert outcome.source == "cloud"


async def test_local_retries_are_capped(work: Path) -> None:
    failed = DSHTurn(
        final_response="好了。",
        finish_reason="completed",
        events=[tool_result_event(True, "failed", retryable=True, name="open_app")],
    )
    local = RecordingBackend("local", [failed, failed, failed, failed])
    cloud = RecordingBackend("cloud", [DSHTurn(final_response="还是不行。", finish_reason="completed")])
    router = DSHRouter(settings(work, local_attempts=2), local=local, cloud=cloud)

    outcome = await router.route("打开东西", trace_id="t5")

    assert outcome.attempts == 2
    assert outcome.source == "cloud"


async def test_unavailable_local_backend_escalates_immediately(work: Path) -> None:
    """A runtime that will not start does not improve on a retry."""
    local = RecordingBackend("local", [DSHTurn(failure="dsh_unavailable: FileNotFoundError")])
    cloud = RecordingBackend("cloud", [DSHTurn(final_response="我先用云端的模型来做。", finish_reason="completed")])
    router = DSHRouter(settings(work), local=local, cloud=cloud)

    outcome = await router.route("打开记事本", trace_id="t6")

    assert outcome.source == "cloud"
    assert outcome.escalation_reason.startswith("dsh_unavailable")


async def test_cloud_disabled_reports_failure_instead_of_pretending(work: Path) -> None:
    local = RecordingBackend(
        "local",
        [
            DSHTurn(
                final_response="好了。",
                finish_reason="completed",
                events=[tool_result_event(True, "failed", retryable=False, reason="nope", name="open_app")],
            )
        ],
    )
    router = DSHRouter(settings(work, cloud=False), local=local, cloud=RecordingBackend("cloud", []))

    outcome = await router.route("打开东西", trace_id="t7")

    assert outcome.resolved is False
    assert outcome.failure == "verification_failed"
    assert outcome.response == ""


async def test_disabled_router_does_not_call_any_model(work: Path) -> None:
    local = RecordingBackend("local", [])
    router = DSHRouter(
        DSHSettings(
            enabled=False,
            local=TierSettings("local", True, work / "l", PROJECT_ROOT),
            cloud=TierSettings("cloud", False, work / "c", PROJECT_ROOT),
            bridge=BridgeSettings(utterance_file=work / "u.json"),
        ),
        local=local,
        cloud=RecordingBackend("cloud", []),
    )

    outcome = await router.route("打开记事本", trace_id="t8")

    assert outcome.resolved is False
    assert outcome.failure == "dsh_disabled"
    assert local.prompts == []


async def test_local_and_cloud_share_the_session_and_the_published_tier(work: Path) -> None:
    """
    The cloud turn must continue the same conversation, and the tier published
    for the MCP server must be the speaker's — that file is the only channel
    that survives DSH's process boundary.

    Read *during* the turn, by a backend that looks at the file as the real MCP
    server would, because the record is cleared once the turn ends.
    """
    observed: list[dict] = []
    file_path = work / "utterance.json"

    class WatchingBackend(RecordingBackend):
        async def run(self, prompt: str, session_id: str) -> DSHTurn:
            observed.append(json.loads(file_path.read_text(encoding="utf-8")))
            return await super().run(prompt, session_id)

    local = WatchingBackend(
        "local",
        [
            DSHTurn(
                final_response="好了。",
                finish_reason="completed",
                events=[tool_result_event(True, "failed", retryable=False, reason="x", name="open_app")],
            )
        ],
    )
    cloud = RecordingBackend("cloud", [DSHTurn(final_response="好。", finish_reason="completed")])
    cfg = settings(work)
    router = DSHRouter(cfg, local=local, cloud=cloud)

    await router.route("打开东西", trace_id="trace-9", tier="full")

    assert local.session_ids == ["trace-9"]
    # Derived, not identical: the tiers are separate runtimes, so reusing the
    # exact id would have them both writing one session.
    assert cloud.session_ids == ["trace-9-cloud"]

    assert observed == [{"trace_id": "trace-9", "tier": "full", "expires_at": observed[0]["expires_at"]}]
    # And the record does not outlive its turn: a stale `full` left behind is
    # exactly what a guest's next command would read.
    assert not file_path.exists()


async def test_a_published_tier_is_cleared_even_when_the_backend_explodes(work: Path) -> None:
    """
    A backend that *raises* is a bug in the backend (`HarnessBackend` catches its
    own failures and reports them as `DSHTurn.failure`), so the exception is
    allowed to propagate — the pipeline is what guards the voice loop. What must
    not happen is the published tier surviving the crash.
    """
    class Exploding:
        name = "boom"
        enabled = True

        async def run(self, prompt, session_id):
            raise RuntimeError("kaboom")

        async def close(self):
            pass

    cfg = settings(work)
    router = DSHRouter(cfg, local=Exploding(), cloud=RecordingBackend("cloud", []))
    assert cfg.bridge.utterance_file.exists() is False

    with pytest.raises(RuntimeError, match="kaboom"):
        await router.route("随便说说", trace_id="t-boom", tier="guest")

    assert not cfg.bridge.utterance_file.exists()


def test_escalation_context_lists_what_already_happened() -> None:
    assessment = assess_turn(
        "已经打开了。",
        "completed",
        [tool_result_event(False, "failed", reason="chrome.exe missing", name="open_app")],
    )

    context = build_escalation_context("打开谷歌浏览器", assessment, "verification_failed", "local")

    assert "打开谷歌浏览器" in context
    assert "open_app" in context
    assert "chrome.exe missing" in context
    assert "Do not repeat steps that already succeeded" in context


# ──────────────────────────────────────────────────────────────
# The cloud tier is gated on the speaker tier too
# ──────────────────────────────────────────────────────────────

def _unresolved_local() -> RecordingBackend:
    """A local agent whose tool result the machine contradicted."""
    return RecordingBackend(
        "local",
        [
            DSHTurn(
                final_response="好了。",
                finish_reason="completed",
                events=[tool_result_event(True, "failed", retryable=False, reason="nope", name="open_app")],
            )
        ],
    )


async def test_a_guest_does_not_reach_the_cloud_tier(work: Path) -> None:
    """
    `new_way.md`: 「The cloud model must NOT automatically receive broader
    permissions simply because it is more capable.」 A guest who cannot write a
    file locally must not acquire that by escalating — the cloud tier is gated on
    the same speaker tier as everything else.
    """
    cloud = RecordingBackend("cloud", [DSHTurn(final_response="好。", finish_reason="completed")])
    router = DSHRouter(settings(work), local=_unresolved_local(), cloud=cloud)

    outcome = await router.route("打开东西", trace_id="g1", tier="guest")

    assert outcome.resolved is False
    assert outcome.source == "local"
    assert outcome.escalation_reason == "verification_failed"
    assert cloud.prompts == [], "a guest reached the cloud tier"


async def test_a_guest_reaches_the_cloud_only_when_the_config_says_so(work: Path) -> None:
    """The control for the test above — the gate is a setting, not a wall."""
    cloud = RecordingBackend("cloud", [DSHTurn(final_response="我用云端的模型处理了。", finish_reason="completed")])
    router = DSHRouter(
        settings(work, cloud_guest_allowed=True), local=_unresolved_local(), cloud=cloud
    )

    outcome = await router.route("打开东西", trace_id="g2", tier="guest")

    assert outcome.source == "cloud"
    assert outcome.resolved is True
    assert len(cloud.prompts) == 1


async def test_a_full_tier_speaker_escalates_normally(work: Path) -> None:
    """The gate must not block the case escalation exists for."""
    cloud = RecordingBackend("cloud", [DSHTurn(final_response="我没找到这个程序。", finish_reason="completed")])
    router = DSHRouter(settings(work), local=_unresolved_local(), cloud=cloud)

    outcome = await router.route("打开东西", trace_id="g3", tier="full")

    assert outcome.source == "cloud"
    assert len(cloud.prompts) == 1
