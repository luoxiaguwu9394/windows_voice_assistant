"""
Unit tests for core modules.
"""

import pytest
from winvoice.contracts import (
    AudioFrame, KwsTriggered, SvResult, VadSegment, AsrResult,
    IntentResult, ToolCall, ToolResult, TtsRequest, TtsChunk,
    InterruptTTS, ConfigChanged, SystemState, ErrorReport,
    IntentName, ToolName, SpeakerTier, SystemStateName, ProcessName,
)
from winvoice.config import ConfigManager
from winvoice.tools.registry import scan_for_irreversible, ToolRegistry
from winvoice.tools.snapshot import SnapshotManager
from winvoice.intent.rules import match_rules, RULE_PATTERNS
from winvoice.intent.classifier import IntentClassifier


class TestContracts:
    """Test Pydantic message contracts."""

    def test_audio_frame_creation(self):
        frame = AudioFrame(
            trace_id="abc123",
            timestamp_ms=1000,
            data=b"\x00\x00" * 160,
        )
        assert frame.trace_id == "abc123"
        assert frame.sample_rate == 16000
        assert frame.channels == 1

    def test_intent_result_creation(self):
        result = IntentResult(
            trace_id="trace1",
            intent=IntentName.OPEN_APP,
            args={"app": "notepad"},
            confidence=0.9,
            source="rules",
        )
        assert result.intent == IntentName.OPEN_APP
        assert result.confidence == 0.9

    def test_tool_call_validation(self):
        call = ToolCall(
            trace_id="trace1",
            tool=ToolName.WRITE_FILE,
            args={"path": "test.txt", "content": "hello"},
            requires_confirmation=True,
        )
        assert call.requires_confirmation is True
        assert call.tool == ToolName.WRITE_FILE

    def test_enum_values(self):
        assert IntentName.OPEN_APP.value == "open_app"
        assert ToolName.WRITE_FILE.value == "write_file"
        assert SpeakerTier.FULL.value == "full"
        assert SystemStateName.IDLE.value == "idle"
        assert ProcessName.AUDIO.value == "audio"


class TestConfig:
    """Test configuration management."""

    def test_config_load(self, tmp_path):
        config_file = tmp_path / "test.yaml"
        config_file.write_text("""
audio:
  sample_rate: 16000
llm:
  local:
    model: "test-model"
""")
        cfg = ConfigManager(config_file)
        assert cfg.get("audio.sample_rate") == 16000
        assert cfg.get("llm.local.model") == "test-model"

    def test_env_var_expansion(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "expanded_value")
        config_file = tmp_path / "test.yaml"
        config_file.write_text("value: ${TEST_VAR}")
        cfg = ConfigManager(config_file)
        assert cfg.get("value") == "expanded_value"

    def test_missing_env_var_raises(self, tmp_path):
        config_file = tmp_path / "test.yaml"
        config_file.write_text("value: ${NONEXISTENT_VAR}")
        with pytest.raises(ValueError, match="Unresolved environment variables"):
            ConfigManager(config_file)

    def test_nested_get_set(self, tmp_path):
        config_file = tmp_path / "test.yaml"
        config_file.write_text("a:\n  b: 1")
        cfg = ConfigManager(config_file)
        assert cfg.get("a.b") == 1
        cfg.set("a.c", 2)
        assert cfg.get("a.c") == 2


class TestIrreversibleScan:
    """Test irreversible operation detection."""

    def test_detects_reg_add(self):
        content = "reg add HKLM\\Software\\Test /v Value /t REG_SZ /d Data"
        matches = scan_for_irreversible(content)
        assert any("reg add" in m for m in matches)

    def test_detects_msiexec_uninstall(self):
        content = "msiexec /x {GUID} /quiet"
        matches = scan_for_irreversible(content)
        assert any("msiexec.*uninstall" in m for m in matches)

    def test_detects_rm_rf(self):
        content = "rm -rf /important/path"
        matches = scan_for_irreversible(content)
        assert any("rm.*-rf" in m for m in matches)

    def test_clean_script_passes(self):
        content = "echo hello\nmkdir test\npython script.py"
        matches = scan_for_irreversible(content)
        assert len(matches) == 0


class TestToolRegistry:
    """Test tool registry and validation."""

    def test_registry_has_all_tools(self):
        registry = ToolRegistry()
        assert len(registry._tools) == 8  # 8 builtin tools

    def test_get_allowed_tools_full_tier(self):
        registry = ToolRegistry()
        allowed = registry.get_allowed("full")
        assert len(allowed) == 8

    def test_get_allowed_tools_guest_tier(self):
        registry = ToolRegistry()
        allowed = registry.get_allowed("guest")
        # Guest denied: read_file, write_file, run_script
        assert len(allowed) == 5

    def test_get_allowed_tools_rejected_tier(self):
        registry = ToolRegistry()
        allowed = registry.get_allowed("rejected")
        assert len(allowed) == 0

    def test_validate_write_file_path_restriction(self):
        registry = ToolRegistry()
        # Outside user directory should fail
        error = registry.validate_call(ToolName.WRITE_FILE, {"path": "C:/Windows/test.txt", "content": "x"})
        assert error is not None
        assert "not allowed" in error

    def test_validate_run_script_irreversible(self, tmp_path):
        registry = ToolRegistry()
        script = tmp_path / "bad.bat"
        script.write_text("reg add HKLM\\Test")
        error = registry.validate_call(ToolName.RUN_SCRIPT, {"path": str(script)})
        assert error is not None
        assert "irreversible" in error.lower()


class TestSnapshotManager:
    """Test snapshot creation and restore."""

    def test_create_and_restore_snapshot(self, tmp_path):
        # Create test files
        test_file = tmp_path / "test.txt"
        test_file.write_text("original content")

        mgr = SnapshotManager(base_path=tmp_path / "snapshots")
        snapshot = mgr.create_snapshot([test_file])

        assert snapshot is not None
        assert len(snapshot.files) == 1

        # Modify file
        test_file.write_text("modified content")

        # Restore
        success = mgr.restore_snapshot(snapshot.snapshot_id)
        assert success
        assert test_file.read_text() == "original content"


class TestIntentRules:
    """Test rule-based intent matching."""

    def test_match_open_app(self):
        result = match_rules("打开记事本")
        assert result is not None
        assert result.intent == IntentName.OPEN_APP
        assert result.args["app"] == "记事本"

    def test_match_close_app(self):
        result = match_rules("关闭计算器")
        assert result is not None
        assert result.intent == IntentName.CLOSE_APP

    def test_match_set_volume(self):
        result = match_rules("音量调大 20")
        assert result is not None
        assert result.intent == IntentName.SET_VOLUME
        assert result.args["delta"] == 20

    def test_match_media_control(self):
        for text, action in [("播放音乐", "play"), ("暂停", "pause"), ("下一首", "next"), ("上一首", "prev")]:
            result = match_rules(text)
            assert result is not None
            assert result.intent == IntentName.MEDIA_CONTROL
            assert result.args["action"] == action

    def test_match_search_web(self):
        result = match_rules("搜索 Python 教程")
        assert result is not None
        assert result.intent == IntentName.SEARCH_WEB
        assert result.args["query"] == "Python 教程"

    def test_no_match_returns_none(self):
        result = match_rules("随便聊聊天气")
        assert result is None


class TestIntentClassifier:
    """Test local intent classifier (stubbed)."""

    @pytest.mark.asyncio
    async def test_classifier_builds_prompt(self):
        classifier = IntentClassifier()
        prompt = classifier._build_prompt("打开记事本")
        assert "open_app" in prompt
        assert "记事本" in prompt
        assert "JSON" in prompt

    @pytest.mark.asyncio
    async def test_parse_valid_json(self):
        classifier = IntentClassifier()
        content = '{"intent": "open_app", "args": {"app": "notepad"}}'
        result = classifier._parse_response(content)
        assert result.intent == IntentName.OPEN_APP
        assert result.args["app"] == "notepad"
        assert result.confidence > 0

    @pytest.mark.asyncio
    async def test_parse_invalid_json(self):
        classifier = IntentClassifier()
        result = classifier._parse_response("not json")
        assert result.intent == IntentName.UNKNOWN
        assert result.confidence == 0.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])