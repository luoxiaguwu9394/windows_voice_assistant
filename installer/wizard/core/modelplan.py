"""
Which models to fetch, and the exact `download_models.py` invocation for them.

The tier names ("3b"/"1.5b"/"0.5b") are wizard-facing; the manifest keys match
`scripts/download_models.py`. The streaming-Zipformer ASR (458 MB, a fallback
the default config never loads) stays out of the default set — the advanced
page can add it.
"""

from __future__ import annotations

from typing import Dict, List

CORE_KEYS: List[str] = [
    "kws/zipformer-zh-en",
    "vad/silero",
    "asr/sense-voice",
    "tts/matcha-zh-baker",
    "tts/vocos-22khz",
    "tts/vits-zh",
    "sv/campplus",
]

STREAMING_ASR_KEY = "asr/zipformer"

LLM_TIERS: Dict[str, Dict[str, str]] = {
    "3b": {
        "key": "llm/qwen2.5-3b-instruct",
        "model": "qwen2.5-3b-instruct",
        "file": "qwen2.5-3b-instruct-q4_k_m.gguf",
        "size_gb": "2.2",
        "label": "Qwen2.5-3B（约 2.2 GB，推荐：质量最好）",
    },
    "1.5b": {
        "key": "llm/qwen2.5-1.5b-instruct",
        "model": "qwen2.5-1.5b-instruct",
        "file": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "size_gb": "1.2",
        "label": "Qwen2.5-1.5B（约 1.2 GB，更快）",
    },
    "0.5b": {
        "key": "llm/qwen2.5-0.5b-instruct",
        "model": "qwen2.5-0.5b-instruct",
        "file": "qwen2.5-0.5b-instruct-q4_k_m.gguf",
        "size_gb": "0.5",
        "label": "Qwen2.5-0.5B（约 0.5 GB，最小）",
    },
}


def model_keys(tier: str, include_streaming_asr: bool = False) -> List[str]:
    if tier not in LLM_TIERS:
        raise ValueError(f"未知模型档位: {tier}")
    keys = list(CORE_KEYS)
    if include_streaming_asr:
        keys.append(STREAMING_ASR_KEY)
    keys.append(LLM_TIERS[tier]["key"])
    return keys


def download_argv(models_dir: str, tier: str, include_streaming_asr: bool = False) -> List[str]:
    """The machine-progress invocation the wizard runs via the embedded python."""
    keys = ",".join(model_keys(tier, include_streaming_asr))
    return [
        "scripts/download_models.py",
        "--models-dir",
        models_dir,
        "--only",
        keys,
        "--progress-fmt",
        "machine",
    ]
