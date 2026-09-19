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
    """Open an application from the allowlist."""
    app = str(args.get("app", "")).lower()
    if app not in ALLOWED_APPS:
        return {
            "success": False,
            "error": f"App not allowed: {app}. Allowed: {list(ALLOWED_APPS.keys())}",
        }

    cmd = ALLOWED_APPS[app]
    try:
        if cmd.startswith("ms-"):
            # URI-style targets (e.g. ms-settings:) must go through `start`.
            subprocess.run(["cmd", "/c", "start", "", cmd], check=True, capture_output=True)
        else:
            subprocess.Popen(cmd, shell=True)
        return {"success": True, "message": f"Opened {app}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def close_app(args: Dict[str, Any]) -> Dict[str, Any]:
    """Close an application from the allowlist."""
    app = str(args.get("app", "")).lower()
    if app not in ALLOWED_APPS:
        return {"success": False, "error": f"App not allowed: {app}"}

    exe = ALLOWED_APPS[app]
    try:
        if exe.endswith(".exe"):
            subprocess.run(["taskkill", "/f", "/im", exe], capture_output=True)
        return {"success": True, "message": f"Closed {app}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ──────────────────────────────────────────────────────────────
# Volume Control
# ──────────────────────────────────────────────────────────────

def set_volume(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Adjust the system volume.

    Implemented via the Windows Core Audio endpoint volume through
    PowerShell, which needs no extra dependency beyond pywin32's runtime.
    """
    try:
        delta = int(args.get("delta", 0))
    except (TypeError, ValueError):
        return {"success": False, "error": f"delta must be an integer, got {args.get('delta')!r}"}

    # 1 unit of `delta` == 1 percentage point.
    step = max(-100, min(100, delta))
    script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume {{
  int NotImpl1();
  int NotImpl2();
  int GetChannelCount(out uint c);
  int SetMasterVolumeLevel(float level, ref System.Guid ctx);
  int SetMasterVolumeLevelScalar(float level, ref System.Guid ctx);
  int GetMasterVolumeLevel(out float level);
  int GetMasterVolumeLevelScalar(out float level);
}}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice {{
  int Activate(ref System.Guid id, int clsCtx, System.IntPtr activationParams, [System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.IUnknown)] out object iface);
}}
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator {{
  int NotImpl1();
  int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice endpoint);
}}
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorComObject {{ }}
public class Audio {{
  public static void SetVolumeScalar(float level) {{
    var enumerator = (IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
    IMMDevice dev; enumerator.GetDefaultAudioEndpoint(0, 1, out dev);
    var guid = typeof(IAudioEndpointVolume).GUID;
    object o; dev.Activate(ref guid, 23, System.IntPtr.Zero, out o);
    var vol = (IAudioEndpointVolume)o;
    var ctx = System.Guid.Empty;
    vol.SetMasterVolumeLevelScalar(level, ref ctx);
  }}
  public static float GetVolumeScalar() {{
    var enumerator = (IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
    IMMDevice dev; enumerator.GetDefaultAudioEndpoint(0, 1, out dev);
    var guid = typeof(IAudioEndpointVolume).GUID;
    object o; dev.Activate(ref guid, 23, System.IntPtr.Zero, out o);
    var vol = (IAudioEndpointVolume)o;
    float level; vol.GetMasterVolumeLevelScalar(out level);
    return level;
  }}
}}
'@
$current = [Audio]::GetVolumeScalar()
$target = [Math]::Max(0.0, [Math]::Min(1.0, $current + ({step} / 100.0)))
[Audio]::SetVolumeScalar($target)
Write-Output ([int]($target * 100))
"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "volume control timed out"}

    if proc.returncode != 0:
        return {
            "success": False,
            "error": (proc.stderr or "volume control failed").strip()[:400],
        }

    applied = (proc.stdout or "").strip().splitlines()[-1:] or [""]
    return {
        "success": True,
        "message": f"Volume adjusted by {step} (now ~{applied[0]}%)",
    }


# ──────────────────────────────────────────────────────────────
# Media Control
# ──────────────────────────────────────────────────────────────

def media_control(args: Dict[str, Any]) -> Dict[str, Any]:
    """Control media playback (global media keys)."""
    action = str(args.get("action", "")).lower()
    key_map = {
        "play": 0xB3,   # VK_MEDIA_PLAY_PAUSE
        "pause": 0xB3,
        "next": 0xB0,   # VK_MEDIA_NEXT_TRACK
        "prev": 0xB1,   # VK_MEDIA_PREV_TRACK
    }

    if action not in key_map:
        return {"success": False, "error": f"Unknown action: {action}"}

    vk = key_map[action]
    script = f"""
$ErrorActionPreference = 'Stop'
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
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "media key timed out"}

    if proc.returncode != 0:
        return {"success": False, "error": (proc.stderr or "media key failed").strip()[:400]}

    return {"success": True, "message": f"Media {action}"}


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