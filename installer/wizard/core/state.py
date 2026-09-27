"""
The wizard's shared state and the default config values.

`DEFAULT_CONFIG_VALUES` renders `config/config.template.yaml` into a working
default `config/config.yaml` right after the runtime payload is extracted —
so a half-finished install still boots (default devices, 3B model), and the
audio probes run against a real config. The user's choices from the wizard
pages overwrite it at the config step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

from . import pins
from .modelplan import LLM_TIERS
from .template import yaml_keyword_block

DEFAULT_WAKE_WORDS = ["小助手", "你好助手", "assistant"]
DEFAULT_CITY = "北京"


def default_config_values() -> Dict[str, str]:
    tier = LLM_TIERS["3b"]
    return {
        "AUDIO_INPUT_DEVICE": "default",
        "AUDIO_OUTPUT_DEVICE": "default",
        "AUDIO_OUTPUT_SAMPLE_RATE": "0",
        "AUDIO_OUTPUT_HOST_API": "auto",
        "LLM_MODEL": tier["model"],
        "LLM_MODEL_FILE": f"models/llm/{tier['file']}",
        "LLM_SERVER_BINARY": f"tools/{pins.LLAMA_DIR}/llama-server.exe",
        "LLM_REMOTE_ENABLED": "false",
        "LLM_REMOTE_BASE_URL": "https://your-api-endpoint/v1",
        "DSH_ENABLED": "false",
        "DSH_LOCAL_ENABLED": "false",
        "DSH_LOCAL_MODEL": tier["model"],
        "WEATHER_CITY": DEFAULT_CITY,
        "KWS_KEYWORDS": yaml_keyword_block(DEFAULT_WAKE_WORDS),
    }


@dataclass
class InstallState:
    """Everything the pages collect; the config step turns this into YAML."""

    install_dir: Path = Path(r"%LOCALAPPDATA%\WinVoice")

    # network page
    proxy_url: str = ""
    hf_endpoint: str = ""

    # models page
    llm_tier: str = "3b"
    include_streaming_asr: bool = False

    # devices page
    audio_input: str = "default"
    audio_output: str = "default"
    output_rate: int = 0
    output_host_api: str = "auto"

    # config page
    wake_words: List[str] = field(default_factory=lambda: list(DEFAULT_WAKE_WORDS))
    city: str = DEFAULT_CITY

    # cloud page
    remote_key: str = ""
    remote_base_url: str = "https://api.deepseek.com/v1"
    deepseek_key: str = ""

    # optional components
    dsh_enabled: bool = True
    autostart: bool = False
    check_updates: bool = True

    def config_values(self) -> Dict[str, str]:
        values = default_config_values()
        values.update(
            {
                "AUDIO_INPUT_DEVICE": self.audio_input or "default",
                "AUDIO_OUTPUT_DEVICE": self.audio_output or "default",
                "AUDIO_OUTPUT_SAMPLE_RATE": str(int(self.output_rate or 0)),
                "AUDIO_OUTPUT_HOST_API": self.output_host_api or "auto",
                "LLM_MODEL": LLM_TIERS[self.llm_tier]["model"],
                "LLM_MODEL_FILE": f"models/llm/{LLM_TIERS[self.llm_tier]['file']}",
                "LLM_REMOTE_ENABLED": "true" if self.remote_key else "false",
                "LLM_REMOTE_BASE_URL": self.remote_base_url or "https://your-api-endpoint/v1",
                "DSH_ENABLED": "true" if self.dsh_enabled else "false",
                "DSH_LOCAL_ENABLED": "true" if self.dsh_enabled else "false",
                "DSH_LOCAL_MODEL": LLM_TIERS[self.llm_tier]["model"],
                "WEATHER_CITY": self.city or DEFAULT_CITY,
                "KWS_KEYWORDS": yaml_keyword_block(self.wake_words or DEFAULT_WAKE_WORDS),
            }
        )
        return values
