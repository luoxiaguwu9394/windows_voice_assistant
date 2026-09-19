#!/usr/bin/env python3
"""
Installation Verification Script.

Checks that all modules import correctly and basic functionality works.
Run after: pip install -e .[dev]
"""

import sys
import traceback
from pathlib import Path


def check_import(module_name: str, attr: str = None) -> bool:
    """Try to import a module/attribute."""
    try:
        if attr:
            exec(f"from {module_name} import {attr}")
        else:
            exec(f"import {module_name}")
        return True
    except Exception as e:
        print(f"  ❌ {module_name}" + (f".{attr}" if attr else "") + f": {e}")
        return False


def main():
    print("=" * 60)
    print("Windows Voice Assistant - Installation Verification")
    print("=" * 60)
    
    all_ok = True
    
    # 1. Core packages
    print("\n📦 Core Packages:")
    packages = [
        ("sherpa_onnx", None),
        ("numpy", None),
        ("pydantic", None),
        ("yaml", None),
        ("watchdog", None),
        ("structlog", None),
        ("orjson", None),
        ("httpx", None),
        ("tqdm", None),
        ("requests", None),
        ("sounddevice", None),
        ("scipy", None),
        ("win32api", None),  # pywin32
        ("pyautogui", None),
    ]
    
    for pkg, attr in packages:
        ok = check_import(pkg, attr)
        if ok:
            print(f"  ✅ {pkg}")
        all_ok = all_ok and ok
    
    # 2. Project modules
    print("\n📁 Project Modules:")
    project_modules = [
        ("winvoice", None),
        ("winvoice.config", "get_config"),
        ("winvoice.config", "ConfigManager"),
        ("winvoice.logging", "configure_logging"),
        ("winvoice.logging", "get_logger"),
        ("winvoice.context", "ContextManager"),
        ("winvoice.context", "get_context_manager"),
        ("winvoice.contracts", "AudioFrame"),
        ("winvoice.contracts", "IntentResult"),
        ("winvoice.contracts", "ToolCall"),
        ("winvoice.contracts", "ToolResult"),
        ("winvoice.contracts", "KwsTriggered"),
        ("winvoice.contracts", "SvResult"),
        ("winvoice.contracts", "TtsRequest"),
        ("winvoice.contracts", "ConfigChanged"),
        ("winvoice.contracts", "SystemState"),
        ("winvoice.contracts", "ErrorReport"),
        ("winvoice.contracts", "IntentName"),
        ("winvoice.contracts", "ToolName"),
        ("winvoice.contracts", "SpeakerTier"),
        ("winvoice.contracts", "SystemStateName"),
        ("winvoice.contracts", "ProcessName"),
        ("winvoice.audio", "AudioPipeline"),
        ("winvoice.audio", "PipelineState"),
        ("winvoice.audio", "create_kws_engine"),
        ("winvoice.audio", "create_vad_engine"),
        ("winvoice.audio", "create_asr_engine"),
        ("winvoice.audio", "create_sv_engine"),
        ("winvoice.audio", "create_tts_engine"),
        ("winvoice.audio", "create_audio_stream"),
        ("winvoice.audio", "AudioStreamManager"),
        ("winvoice.llm", "create_llm_router"),
        ("winvoice.llm", "LocalLlmBackend"),
        ("winvoice.llm", "RemoteLlmBackend"),
        ("winvoice.llm", "LlmRouter"),
        ("winvoice.intent", "create_intent_router"),
        ("winvoice.intent", "IntentRouter"),
        ("winvoice.intent", "match_rules"),
        ("winvoice.intent", "IntentClassifier"),
        ("winvoice.tools", "ToolRegistry"),
        ("winvoice.tools", "ToolExecutor"),
        ("winvoice.tools", "SnapshotManager"),
        ("winvoice.tools", "IRREVERSIBLE_PATTERNS"),
        ("winvoice.tools", "get_tool_registry"),
        ("winvoice.tools", "get_snapshot_manager"),
        ("winvoice.enroll", "main"),
    ]
    
    for mod, attr in project_modules:
        ok = check_import(mod, attr)
        if ok:
            print(f"  ✅ {mod}.{attr}" if attr else f"  ✅ {mod}")
        all_ok = all_ok and ok
    
    # 3. Config file
    print("\n⚙️  Config File:")
    config_path = Path("config/config.yaml")
    if config_path.exists():
        print(f"  ✅ {config_path}")
        # Try to load it
        try:
            from winvoice.config import get_config, reset_config
            reset_config()
            cfg = get_config("config/config.yaml")
            print(f"  ✅ Config loads: audio.sample_rate={cfg.get('audio.sample_rate')}")
        except Exception as e:
            print(f"  ❌ Config load failed: {e}")
            all_ok = False
    else:
        print(f"  ❌ {config_path} not found")
        all_ok = False
    
    # 4. Grammar file
    print("\n📝 Grammar File:")
    grammar_path = Path("grammar.gbnf")
    if grammar_path.exists():
        print(f"  ✅ {grammar_path}")
        content = grammar_path.read_text()
        if "intent_enum" in content and "open_app" in content:
            print(f"  ✅ Grammar contains intent enums")
        else:
            print(f"  ⚠️  Grammar may be incomplete")
    else:
        print(f"  ❌ {grammar_path} not found")
        all_ok = False
    
    # 5. Scripts
    print("\n📜 Scripts:")
    scripts = [
        "scripts/download_models.py",
        "scripts/analyze_sv_scores.py",
        "scripts/test_stub_pipeline.py",
    ]
    for script in scripts:
        path = Path(script)
        if path.exists():
            print(f"  ✅ {script}")
        else:
            print(f"  ❌ {script} not found")
            all_ok = False
    
    # 6. Tests
    print("\n🧪 Test Files:")
    test_files = [
        "tests/unit/test_core.py",
        "tests/integration/test_pipeline.py",
        "tests/e2e/test_e2e.py",
    ]
    for test in test_files:
        path = Path(test)
        if path.exists():
            print(f"  ✅ {test}")
        else:
            print(f"  ❌ {test} not found")
            all_ok = False
    
    # Summary
    print("\n" + "=" * 60)
    if all_ok:
        print("✅ ALL CHECKS PASSED")
        print("You can now run:")
        print("  python -m winvoice --stub-audio          # Test with stub audio")
        print("  python scripts/test_stub_pipeline.py     # Run stub pipeline test")
        print("  pytest tests/unit -v                     # Run unit tests")
        return 0
    else:
        print("❌ SOME CHECKS FAILED")
        print("Please install missing dependencies:")
        print("  pip install -e .[dev]")
        return 1


if __name__ == "__main__":
    sys.exit(main())