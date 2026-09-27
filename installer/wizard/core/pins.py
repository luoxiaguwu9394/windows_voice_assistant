"""
Pinned external assets and the single source of truth they feed.

`build_runtime.py` (dev machine) downloads/extracts exactly these; the wizard's
default config values point at the resulting paths. Changing a pin means
changing it here — nowhere else.
"""

from __future__ import annotations

PYTHON_VERSION = "3.12.10"
PYTHON_EMBED_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_VERSION}/"
    f"python-{PYTHON_VERSION}-embed-amd64.zip"
)
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"

# The llama.cpp build the deployment docs verified (b7376 win-cpu-x64). The
# newer OpenVINO build (b11046) is documented as buggy — do not bump casually.
LLAMA_RELEASE_TAG = "b7376"
LLAMA_ASSET = f"llama-{LLAMA_RELEASE_TAG}-bin-win-cpu-x64.zip"
LLAMA_DOWNLOAD_URL = (
    "https://github.com/ggml-org/llama.cpp/releases/download/"
    f"{LLAMA_RELEASE_TAG}/{LLAMA_ASSET}"
)
LLAMA_DIR = f"llama-{LLAMA_RELEASE_TAG}-bin-win-cpu-x64"

# Installed into <install>/.pylibs at build time (bundled, so the DSH step
# needs no user-side pip and no PyPI reachability). The single-file runtime exe
# inside deepseek-harness-runtime-bin carries its own Node — the end user never
# installs Node.
DSH_PYLIBS_PACKAGES = ("mcp", "deepseek-harness-sdk", "deepseek-harness-runtime-bin")

# Env var download_models.py reads for a Hugging Face mirror (keep in sync
# with HF_ENDPOINT_ENV in scripts/download_models.py).
HF_ENDPOINT_ENV = "WINVOICE_HF_ENDPOINT"

# Where the wizard looks for "a newer build exists". The release carries a
# WinVoice-Setup-<version>.exe asset; the wizard downloads and re-runs it.
UPDATE_API_URL = (
    "https://api.github.com/repos/luoxiaguwu9394/windows_voice_assistant/releases/latest"
)
UPDATE_ASSET_PREFIX = "WinVoice-Setup-"
