"""
Builtin Tool Implementations.

Each tool is a simple function that takes args dict and returns result.
"""

from __future__ import annotations

import subprocess
import webbrowser
from pathlib import Path
from typing import Any, Dict

from winvoice.logging import get_logger

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────
# App Control
# ──────────────────────────────────────────────────────────────

ALLOWED_APPS = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "explorer": "explorer.exe",
    "cmd": "cmd.exe",
    "powershell": "powershell.exe",
    "vscode": "code",
    "chrome": "chrome.exe",
    "edge": "msedge.exe",
    "settings": "ms-settings:",
}


def open_app(args: Dict[str, Any]) -> Dict[str, Any]:
    """Open an application."""
    app = args.get("app", "").lower()
    if app not in ALLOWED_APPS:
        return {"success": False, "error": f"App not allowed: {app}. Allowed: {list(ALLOWED_APPS.keys())}"}

    try:
        cmd = ALLOWED_APPS[app]
        if cmd.startswith("ms-"):
            import subprocess
            subprocess.run(["start", "", cmd], shell=True, check=True)
        else:
            subprocess.Popen(cmd, shell=True)
        return {"success": True, "message": f"Opened {app}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def close_app(args: Dict[str, Any]) -> Dict[str, Any]:
    """Close an application."""
    app = args.get("app", "").lower()
    if app not in ALLOWED_APPS:
        return {"success": False, "error": f"App not allowed: {app}"}

    try:
        exe = ALLOWED_APPS[app]
        if exe.endswith(".exe"):
            import subprocess
            subprocess.run(["taskkill", "/f", "/im", exe], capture_output=True)
        return {"success": True, "message": f"Closed {app}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ──────────────────────────────────────────────────────────────
# Volume Control
# ──────────────────────────────────────────────────────────────

def set_volume(args: Dict[str, Any]) -> Dict[str, Any]:
    """Adjust system volume."""
    try:
        delta = int(args.get("delta", 0))
        # Use nircmd or PowerShell for volume control
        # Simplified: use PowerShell
        import subprocess
        # Get current volume and adjust (simplified)
        script = f"""
        Add-Type -TypeDefinition @'
        using System.Runtime.InteropServices;
        public class Audio {{
            [DllImport("winmm.dll")] public static extern int waveOutGetVolume(IntPtr hwo, out uint dwVolume);
            [DllImport("winmm.dll")] public static extern int waveOutSetVolume(IntPtr hwo, uint dwVolume);
        }}
'@
        $vol = 0
        [Audio]::waveOutGetVolume([IntPtr]::Zero, [ref]$vol)
        $newVol = [Math]::Max(0, [Math]::Min(0xFFFF, $vol + {delta * 655}))  # rough scaling
        [Audio]::waveOutSetVolume([IntPtr]::Zero, $newVol)
        """
        subprocess.run(["powershell", "-Command", script], capture_output=True)
        return {"success": True, "message": f"Volume adjusted by {delta}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ──────────────────────────────────────────────────────────────
# Media Control
# ──────────────────────────────────────────────────────────────

def media_control(args: Dict[str, Any]) -> Dict[str, Any]:
    """Control media playback (global media keys)."""
    action = args.get("action", "").lower()
    key_map = {
        "play": "0xB3",      # VK_MEDIA_PLAY_PAUSE
        "pause": "0xB3",
        "next": "0xB0",      # VK_MEDIA_NEXT_TRACK
        "prev": "0xB1",      # VK_MEDIA_PREV_TRACK
    }

    if action not in key_map:
        return {"success": False, "error": f"Unknown action: {action}"}

    try:
        import subprocess
        # Send media key via PowerShell
        vk = key_map[action]
        script = f"""
        Add-Type -TypeDefinition @'
        using System;
        using System.Runtime.InteropServices;
        public class Keys {{
            [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
        }}
'@
        [Keys]::keybd_event({vk}, 0, 0, [UIntPtr]::Zero)
        [Keys]::keybd_event({vk}, 0, 2, [UIntPtr]::Zero)  # KEYEVENTF_KEYUP
        """
        subprocess.run(["powershell", "-Command", script], capture_output=True)
        return {"success": True, "message": f"Media {action}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ──────────────────────────────────────────────────────────────
# Web Search
# ──────────────────────────────────────────────────────────────

def search_web(args: Dict[str, Any]) -> Dict[str, Any]:
    """Open web search in default browser."""
    query = args.get("query", "").strip()
    if not query:
        return {"success": False, "error": "Empty query"}

    try:
        url = f"https://www.bing.com/search?q={query}"
        webbrowser.open(url)
        return {"success": True, "message": f"Searching for: {query}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ──────────────────────────────────────────────────────────────
# File Operations
# ──────────────────────────────────────────────────────────────

def read_file(args: Dict[str, Any]) -> Dict[str, Any]:
    """Read a text file."""
    path = Path(args.get("path", "")).resolve()
    try:
        # Security: only allow under user directory
        user_dir = Path.home()
        path.relative_to(user_dir)

        if not path.exists():
            return {"success": False, "error": "File not found"}

        if path.stat().st_size > 10 * 1024 * 1024:  # 10MB limit
            return {"success": False, "error": "File too large"}

        content = path.read_text(encoding="utf-8")
        return {"success": True, "content": content, "path": str(path)}
    except ValueError:
        return {"success": False, "error": "Path not allowed (outside user directory)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def write_file(args: Dict[str, Any]) -> Dict[str, Any]:
    """Write content to a file."""
    path = Path(args.get("path", "")).resolve()
    content = args.get("content", "")

    try:
        # Security: only allow under user directory
        user_dir = Path.home()
        path.relative_to(user_dir)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"success": True, "message": f"Written to {path}", "path": str(path)}
    except ValueError:
        return {"success": False, "error": "Path not allowed (outside user directory)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ──────────────────────────────────────────────────────────────
# Script Execution
# ──────────────────────────────────────────────────────────────

def run_script(args: Dict[str, Any]) -> Dict[str, Any]:
    """Run a script file (Python, PowerShell, Batch)."""
    path = Path(args.get("path", "")).resolve()

    try:
        # Security: only allow under user directory
        user_dir = Path.home()
        path.relative_to(user_dir)

        if not path.exists():
            return {"success": False, "error": "Script not found"}

        # Determine interpreter by extension
        suffix = path.suffix.lower()
        if suffix == ".py":
            cmd = ["python", str(path)]
        elif suffix == ".ps1":
            cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(path)]
        elif suffix in (".bat", ".cmd"):
            cmd = ["cmd", "/c", str(path)]
        else:
            return {"success": False, "error": f"Unsupported script type: {suffix}"}

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except ValueError:
        return {"success": False, "error": "Path not allowed (outside user directory)"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Script timeout (60s)"}
    except Exception as e:
        return {"success": False, "error": str(e)}