"""
Update discovery: is there a newer WinVoice-Setup somewhere?

The source of truth is the project's GitHub Releases: the latest release whose
assets carry a `WinVoice-Setup-<version>.exe`. No side-car version file to
forget to bump. Network access goes through urllib so system (registry) proxy
settings on Windows apply without the wizard having a proxy UI of its own.

A failed check is a banner that simply never appears — never an error dialog.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .pins import UPDATE_API_URL, UPDATE_ASSET_PREFIX

_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")


@dataclass
class UpdateInfo:
    version: str
    download_url: str
    notes: str = ""


def version_tuple(version: str) -> tuple:
    """'0.2.1-dev' -> (0, 2, 1); anything unparseable sorts as (0, 0, 0)."""
    match = _VERSION_RE.search(version or "")
    if not match:
        return (0, 0, 0)
    parts = [int(g) if g else 0 for g in match.groups()]
    return tuple(parts)  # type: ignore[return-value]


def is_newer(latest: str, current: str) -> bool:
    return version_tuple(latest) > version_tuple(current)


def parse_release(payload: dict, current_version: str) -> Optional[UpdateInfo]:
    """
    Pick the setup exe out of a GitHub `/releases/latest` payload.
    Draft/prerelease releases never surface there; an asset-less release
    yields None (nothing to install).
    """
    tag = str(payload.get("tag_name") or payload.get("name") or "").strip()
    for asset in payload.get("assets") or []:
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if name.startswith(UPDATE_ASSET_PREFIX) and name.endswith(".exe") and url:
            version = name[len(UPDATE_ASSET_PREFIX) : -len(".exe")]
            if not is_newer(version, current_version):
                return None
            return UpdateInfo(version=version, download_url=url, notes=tag)
    return None


def fetch_latest(current_version: str, api_url: str = UPDATE_API_URL,
                 timeout: float = 6.0) -> Optional[UpdateInfo]:
    """Ask GitHub what the newest release is. Any failure returns None."""
    try:
        request = urllib.request.Request(
            api_url, headers={"User-Agent": "WinVoice-Setup-Wizard", "Accept": "application/vnd.github+json"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return parse_release(payload, current_version)


def download_update(info: UpdateInfo, dest_dir: Path,
                    on_progress: Optional[Callable[[int, int], None]] = None,
                    timeout: float = 30.0) -> Path:
    """
    Stream the new setup exe into `dest_dir` (created as needed) and return
    its path. Caller decides whether to run it — the wizard exits first so
    the new installer never fights the old one over the install dir.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{UPDATE_ASSET_PREFIX}{info.version}.exe"

    request = urllib.request.Request(
        info.download_url, headers={"User-Agent": "WinVoice-Setup-Wizard"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as file:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                file.write(chunk)
                done += len(chunk)
                if on_progress is not None and total:
                    on_progress(done, total)
    if total and done < total:
        raise OSError(f"download truncated: {done}/{total} bytes")
    return dest


def probe_endpoints(proxy_url: str = "", hf_endpoint: str = "",
                    timeout: float = 5.0) -> dict:
    """
    Reachability check for the network page: the two hosts the model downloads
    actually need (GitHub releases + Hugging Face or its mirror). The probe
    honours the proxy the user just typed, not merely the system one.
    """
    handlers = []
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    opener = urllib.request.build_opener(*handlers)

    def reachable(url: str) -> bool:
        try:
            request = urllib.request.Request(
                url, method="HEAD", headers={"User-Agent": "WinVoice-Setup-Wizard"}
            )
            opener.open(request, timeout=timeout).close()
            return True
        except Exception:
            return False

    return {
        "github": reachable("https://github.com"),
        "hf": reachable(hf_endpoint or "https://huggingface.co"),
    }
