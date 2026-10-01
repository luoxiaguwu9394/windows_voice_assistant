"""
The local settings server (winvoice.webui) — HTTP side.

One server bound to 127.0.0.1 on an ephemeral port, speaking a small versioned
JSON API to the static single-page frontend in `static/`. No new dependencies:
`ThreadingHTTPServer` from the stdlib.

Lifecycle and trust model:

* Every request must carry the run's token (URL `?token=` or `X-WV-Token`);
  comparisons are constant-time. The server never listens on anything but
  127.0.0.1 — the same localhost trust model as llama-server.
* The port and token are recorded in `runtime/webui.json`. A second launch
  finds a live instance through that file, opens another window against it
  and exits — one server, any number of windows.
* The frontend heartbeats while a page is open. When no request arrives for
  `IDLE_EXIT_S`, the server exits by itself: closing the browser tab closes
  the backend, and no python process lingers after the window goes away.
* Config writes go through `winvoice.config_edit.save_config` (atomic,
  comment-preserving). Reads go straight to the file, not the process's
  ConfigManager, so the page always shows what is on disk.
* Only the keys in `MANAGED_KEYS` are readable or writable. The rest of the
  config — including `${...}` credential placeholders — never leaves the
  process.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from winvoice.config import HOT_RELOADABLE, REQUIRES_RESTART
from winvoice.config_edit import save_config
from winvoice.logging import get_logger
from winvoice.tools import appdiscovery

logger = get_logger(__name__)

STATE_FILE = Path("runtime/webui.json")
CONFIG_FILE = Path("config/config.yaml")
STATIC_DIR = Path(__file__).resolve().parent / "static"
IDLE_EXIT_S = 180.0

# The exact keys the frontend may read or write. Anything else is a 400 —
# this list is also what keeps `${REMOTE_API_KEY}`-style secrets inside.
MANAGED_KEYS = (
    "tts.speed",
    "tts.pitch",
    "tts.guest_speed",
    "kws.keywords",
    "weather.city",
    "weather.enabled",
    "tools.apps",
    "tools.sensitive_apps",
)

_SPEED_BOUNDS = (0.5, 2.0)

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def effect_of(key: str) -> str:
    """`hot` when a change applies without a restart, `restart` otherwise."""
    return "hot" if key in HOT_RELOADABLE else "restart"


def read_managed_values(config_path: Path) -> dict:
    """Current values for `MANAGED_KEYS`, read straight from the file."""
    import yaml

    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        data = {}

    values: dict[str, Any] = {}
    for key in MANAGED_KEYS:
        node: Any = data
        missing = False
        for part in key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                missing = True
                break
        values[key] = None if missing else node
    return values


def sanitize_changes(changes: dict) -> tuple[dict, list[str]]:
    """
    Validate a `changes` payload. Returns (clean, errors).

    Unknown keys, wrong shapes and out-of-range numbers are rejected here so
    the config file only ever receives what the handlers understand.
    """
    clean: dict[str, Any] = {}
    errors: list[str] = []
    for key, value in (changes or {}).items():
        if key not in MANAGED_KEYS:
            errors.append(f"unknown key: {key}")
            continue
        if key in ("tts.speed", "tts.pitch", "tts.guest_speed"):
            try:
                number = float(value)
            except (TypeError, ValueError):
                errors.append(f"{key} must be a number")
                continue
            low, high = _SPEED_BOUNDS
            if not low <= number <= high:
                errors.append(f"{key} out of range {low}-{high}")
                continue
            clean[key] = number
        elif key == "weather.enabled":
            clean[key] = bool(value)
        elif key == "weather.city":
            clean[key] = str(value or "").strip() or "北京"
        elif key == "kws.keywords":
            words = [str(word).strip() for word in (value or []) if str(word).strip()]
            if not words or len(words) > 8:
                errors.append("kws.keywords needs 1-8 keywords")
                continue
            if any(len(word) > 16 for word in words):
                errors.append("each keyword must be at most 16 characters")
                continue
            clean[key] = words
        elif key == "tools.sensitive_apps":
            ids = [str(item).strip().lower() for item in (value or []) if str(item).strip()]
            clean[key] = ids
        elif key == "tools.apps":
            entries = []
            ok = True
            for raw in value or []:
                entry = raw if isinstance(raw, dict) else {}
                app_id = str(entry.get("id") or "").strip().lower()
                command = str(entry.get("command") or "").strip()
                if not app_id or not command:
                    ok = False
                    break
                cleaned: dict[str, Any] = {"id": app_id, "command": command}
                if entry.get("label"):
                    cleaned["label"] = str(entry["label"]).strip()
                if entry.get("image"):
                    cleaned["image"] = str(entry["image"]).strip()
                cleaned["guest"] = bool(entry.get("guest", False))
                entries.append(cleaned)
            if not ok:
                errors.append("tools.apps entries need at least id and command")
                continue
            clean[key] = entries
    return clean, errors


# ── the server ───────────────────────────────────────────────────────────────


class SettingsServer:
    """The HTTP server + its state file handshake. Tests drive this directly."""

    def __init__(self, config_path: Path = CONFIG_FILE, state_path: Path = STATE_FILE,
                 static_dir: Path = STATIC_DIR):
        # Resolve here: the static-file guard compares the resolved candidate
        # against this directory, so a CWD-relative base would never match.
        self.config_path = Path(config_path).resolve()
        self.state_path = Path(state_path)
        self.static_dir = Path(static_dir).resolve()
        self.token = secrets.token_urlsafe(16)
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.url = ""
        self._last_seen = time.time()

    # -- lifecycle -----------------------------------------------------------

    def bind(self) -> None:
        """Bind to 127.0.0.1 on an ephemeral port and remember the URL."""
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_factory())
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/?token={self.token}"
        logger.info("settings_server_bound", url=self.url.split("token=")[0])

    def write_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({
            "port": self.httpd.server_port, "token": self.token, "pid": os.getpid(),
        }), encoding="utf-8")

    def serve_forever(self) -> None:  # pragma: no cover - blocking main loop
        assert self.httpd is not None
        watchdog = threading.Thread(target=self._idle_watchdog, daemon=True)
        watchdog.start()
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        if self.httpd is not None:
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()

    def _idle_watchdog(self) -> None:
        while True:
            time.sleep(5)
            if time.time() - self._last_seen > IDLE_EXIT_S:
                logger.info("settings_server_idle_exit")
                self.shutdown()
                return

    # -- request plumbing ----------------------------------------------------

    def _handler_factory(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # structlog, not stderr noise
                logger.debug("settings_http", request=fmt % args)

            def _authorize(self, query: str) -> bool:
                supplied = ""
                for part in query.split("&"):
                    if part.startswith("token="):
                        supplied = part[len("token="):]
                        break
                if not supplied:
                    supplied = self.headers.get("X-WV-Token", "")
                return bool(supplied) and hmac.compare_digest(supplied, server.token)

            def _send(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, payload: dict) -> None:
                self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")

            def do_GET(self) -> None:
                server._last_seen = time.time()
                path, _, query = self.path.partition("?")
                if path.startswith("/api/") and not self._authorize(query):
                    self._json(403, {"error": "bad token"})
                    return

                if path == "/" or path == "/index.html":
                    self._static("index.html")
                elif path.startswith("/static/"):
                    self._static(path[len("/static/"):])
                elif path == "/api/v1/meta":
                    from winvoice import __version__
                    from winvoice.tools.builtin import BUILTIN_APPS

                    self._json(200, {
                        "version": __version__,
                        "effects": {key: effect_of(key) for key in MANAGED_KEYS},
                        "idle_exit_s": IDLE_EXIT_S,
                        "builtin_apps": [
                            {"id": entry.id, "label": entry.label}
                            for entry in BUILTIN_APPS.values()
                        ],
                    })
                elif path == "/api/v1/config":
                    self._json(200, {
                        "values": read_managed_values(server.config_path),
                        "effects": {key: effect_of(key) for key in MANAGED_KEYS},
                    })
                elif path == "/api/v1/apps/scan":
                    self._json(200, {"candidates": appdiscovery.scan_installed_apps()})
                elif path == "/api/v1/ping":
                    self._json(200, {"ok": True})
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self) -> None:
                server._last_seen = time.time()
                path, _, query = self.path.partition("?")
                if not self._authorize(query):
                    self._json(403, {"error": "bad token"})
                    return
                if path != "/api/v1/config":
                    self._json(404, {"error": "not found"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    self._json(400, {"error": "body is not JSON"})
                    return
                clean, errors = sanitize_changes(payload.get("changes") or {})
                if errors:
                    self._json(400, {"errors": errors})
                    return
                result = save_config(server.config_path, clean)
                logger.info("settings_saved", mode=result["mode"], keys=result["keys"])
                self._json(200, {
                    "saved": True,
                    "mode": result["mode"],
                    "effects": {key: effect_of(key) for key in result["keys"]},
                })

            def _static(self, relative: str) -> None:
                candidate = (server.static_dir / relative).resolve()
                if not candidate.is_file() or not candidate.is_relative_to(server.static_dir):
                    self._json(404, {"error": "not found"})
                    return
                body = candidate.read_bytes()
                self._send(200, body, _MIME.get(candidate.suffix, "application/octet-stream"))

        return Handler


# ── single-instance handshake + window opening ──────────────────────────────


def find_live_instance(state_path: Path = STATE_FILE) -> Optional[str]:
    """The running server's URL, or None when no live instance answers."""
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        url = f"http://127.0.0.1:{state['port']}/?token={state['token']}"
        request = urllib.request.Request(f"{url}&probe=1".replace("/?token=", "/api/v1/meta?token="))
        with urllib.request.urlopen(request, timeout=1.5) as response:
            if response.status == 200:
                return url
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        pass
    return None


def open_window(url: str) -> None:
    """Edge `--app` mode when available (a chromeless window), else the browser."""
    edge = shutil.which("msedge") or next(
        (candidate for candidate in (
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ) if Path(candidate).exists()),
        None,
    )
    try:
        if edge:
            subprocess.Popen([edge, f"--app={url}"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return
    except OSError:
        pass
    webbrowser.open(url)


def main() -> int:  # pragma: no cover - the process entry point
    live = find_live_instance()
    if live:
        logger.info("settings_server_reused")
        open_window(live)
        return 0

    server = SettingsServer()
    server.bind()
    server.write_state()
    open_window(server.url)
    server.serve_forever()
    return 0
