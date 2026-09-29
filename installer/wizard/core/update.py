"""
Update discovery: is there a newer WinVoice-Setup somewhere?

The source of truth is the project's GitHub Releases: the newest non-draft
release whose assets carry a `WinVoice-Setup-<version>.exe`. The **list**
endpoint is used, not `/releases/latest` — GitHub's "latest" means the most
recent *non-prerelease* release, and this project marks its dev builds as
pre-releases, so `latest` 404s here and the banner would silently never appear
(measured). No side-car version file to forget to bump — but the **version in
`pyproject.toml` must be bumped for any build to be offered at all**, since the
comparison is strictly-greater and the version rides on the asset name.

Network access goes through urllib so system (registry) proxy settings on
Windows apply without the wizard having a proxy UI of its own.

A failed check is a banner that simply never appears — never an error dialog;
an `on_error` callback, when the caller passes one, still hears the reason so
the failure can land in a log. Downloads carry a sha256 sidecar asset
(`WinVoice-Setup-<version>.exe.sha256`, published by `build_installer.py`)
that is verified before the file is allowed to survive; releases without one
(older builds) install as before.

The build-time counterpart of the strict-version trap lives in
`build_installer.py`: it refuses to stamp a version that is already the newest
published one (via `newest_published_version`), because a same-numbered
rebuild would never be offered to installed machines.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .pins import UPDATE_API_URL, UPDATE_ASSET_PREFIX

_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")

_USER_AGENT = "WinVoice-Setup-Wizard"


class UpdateError(Exception):
    """A download the wizard must not hand to the user (truncated, bad hash)."""


@dataclass
class UpdateInfo:
    version: str
    download_url: str
    notes: str = ""
    sha256_url: Optional[str] = None


def version_tuple(version: str) -> tuple:
    """'0.2.1-dev' -> (0, 2, 1); anything unparseable sorts as (0, 0, 0)."""
    match = _VERSION_RE.search(version or "")
    if not match:
        return (0, 0, 0)
    parts = [int(g) if g else 0 for g in match.groups()]
    return tuple(parts)  # type: ignore[return-value]


def is_newer(latest: str, current: str) -> bool:
    return version_tuple(latest) > version_tuple(current)


def _sha256_asset_url(payload: dict, exe_name: str) -> Optional[str]:
    """The `<exe>.sha256` sidecar asset in the same release, if it has one."""
    expected = f"{exe_name}.sha256"
    for asset in payload.get("assets") or []:
        if str(asset.get("name") or "") == expected:
            url = str(asset.get("browser_download_url") or "")
            if url:
                return url
    return None


def parse_release(payload: dict, current_version: str) -> Optional[UpdateInfo]:
    """
    Pick the setup exe out of one release payload.

    An asset-less release yields None (nothing to install). The matching
    `.sha256` sidecar, when present, rides along in `UpdateInfo` so
    `download_update` can verify what it writes.
    """
    tag = str(payload.get("tag_name") or payload.get("name") or "").strip()
    for asset in payload.get("assets") or []:
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if name.startswith(UPDATE_ASSET_PREFIX) and name.endswith(".exe") and url:
            version = name[len(UPDATE_ASSET_PREFIX) : -len(".exe")]
            if not is_newer(version, current_version):
                return None
            return UpdateInfo(
                version=version,
                download_url=url,
                notes=tag,
                sha256_url=_sha256_asset_url(payload, name),
            )
    return None


def parse_releases(payload: list, current_version: str) -> Optional[UpdateInfo]:
    """
    The newest non-draft release carrying a newer setup exe.

    **Pre-releases count.** This project marks its dev builds as pre-releases,
    and GitHub's "latest" is by definition the most recent *non*-prerelease
    release — so anything that only ever consults `/releases/latest` sees a 404
    here and the update banner silently never appears (measured on this repo:
    `/releases/latest` → HTTP 404 while `/releases` lists v0.1.0-dev). Drafts
    are excluded: they are not downloadable by anyone else.
    """
    best: Optional[UpdateInfo] = None
    for release in payload:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        info = parse_release(release, current_version)
        if info is None:
            continue
        if best is None or version_tuple(info.version) > version_tuple(best.version):
            best = info
    return best


def newest_published_version(payload: list) -> Optional[str]:
    """
    The newest WinVoice-Setup version among non-draft releases, or None.

    Unlike `parse_releases` there is no current-version cutoff: build time
    calls this to refuse stamping a version that is already out there, since a
    same-numbered rebuild can never win the strictly-greater comparison on
    installed machines.
    """
    best: Optional[str] = None
    for release in payload:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        for asset in release.get("assets") or []:
            name = str(asset.get("name") or "")
            if name.startswith(UPDATE_ASSET_PREFIX) and name.endswith(".exe"):
                version = name[len(UPDATE_ASSET_PREFIX) : -len(".exe")]
                if best is None or version_tuple(version) > version_tuple(best):
                    best = version
                break
    return best


def fetch_release_pages(api_url: str = UPDATE_API_URL,
                       timeout: float = 6.0) -> list:
    """Fetch all GitHub release pages (100 entries per page)."""
    list_url = api_url.replace("/releases/latest", "/releases")
    if "?" in list_url:
        list_url += "&per_page=100"
    else:
        list_url += "?per_page=100"
    releases: list = []
    page = 1
    while True:
        separator = "&" if "?" in list_url else "?"
        page_url = f"{list_url}{separator}page={page}"
        request = urllib.request.Request(
            page_url,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, list):
            raise ValueError(f"unexpected payload type: {type(payload).__name__}")
        releases.extend(payload)
        if len(payload) < 100:
            return releases
        page += 1


def fetch_latest(current_version: str, api_url: str = UPDATE_API_URL,
                 timeout: float = 6.0,
                 on_error: Optional[Callable[[str], None]] = None) -> Optional[UpdateInfo]:
    """
    Ask GitHub what the newest release carrying a setup exe is.

    The **list** endpoint is queried rather than `/releases/latest`, for the
    reason spelled out in `parse_releases`: a pre-release-only repo (this one)
    404s on `latest`, which turns the whole update path into a silent no-op.
    Any failure returns None — a missing banner, never an error dialog. The
    `on_error` callback (if given) receives a one-line reason first, so the
    failure can reach a log; it must never raise, and is guarded regardless.
    """

    def emit(message: str) -> None:
        if on_error is None:
            return
        try:
            on_error(message)
        except Exception:
            pass

    try:
        payload = fetch_release_pages(api_url, timeout)
    except Exception as exc:
        emit(f"{type(exc).__name__}: {exc}")
        return None
    if isinstance(payload, list):
        return parse_releases(payload, current_version)
    if isinstance(payload, dict):  # an API url pointing straight at one release
        return parse_release(payload, current_version)
    emit(f"unexpected payload type: {type(payload).__name__}")
    return None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch_sha256(url: str, timeout: float) -> str:
    """Read the sidecar checksum file (sha256sum format) and return the hex."""
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", "replace")
    token = text.split()[0].lower() if text.split() else ""
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise UpdateError("校验和文件内容无效（不是 sha256sum 格式）")
    return token


def download_update(info: UpdateInfo, dest_dir: Path,
                    on_progress: Optional[Callable[[int, int], None]] = None,
                    timeout: float = 30.0) -> Path:
    """
    Stream the new setup exe into `dest_dir` (created as needed) and return
    its path. Caller decides whether to run it — the wizard exits first so
    the new installer never fights the old one over the install dir.

    When the release carries a `.sha256` sidecar the downloaded file is
    verified before it is allowed to survive: on mismatch the broken file is
    deleted and `UpdateError` raised — a truncated 150+ MB exe must not sit in
    the Downloads folder waiting to be double-clicked. Releases without a
    sidecar (older builds) download as before.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{UPDATE_ASSET_PREFIX}{info.version}.exe"

    try:
        request = urllib.request.Request(info.download_url, headers={"User-Agent": _USER_AGENT})
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
            raise UpdateError(f"下载被截断：{done}/{total} 字节")

        if info.sha256_url:
            expected = _fetch_sha256(info.sha256_url, timeout)
            actual = _file_sha256(dest)
            if expected != actual:
                raise UpdateError(
                    f"安装器校验和不匹配（期望 {expected[:12]}…，实际 {actual[:12]}…）"
                    "，已删除损坏的下载文件，请重试"
                )
    except Exception:
        dest.unlink(missing_ok=True)
        raise
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
