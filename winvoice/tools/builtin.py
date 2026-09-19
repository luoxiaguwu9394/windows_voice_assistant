"""
Builtin Tool Implementations.

Each tool is a simple function that takes args dict and returns result.

Two rules govern every string a handler returns (see `spec.md` §6.4):

  * `error` is for logs and callers — it may name tools, argument keys and
    paths, so English is fine there;
  * `message` is *spoken*. The TTS model (`vits-icefall-zh-aishell3`) has no
    Latin entries in its lexicon and drops every English word silently, so a
    message is plain Chinese (digits are fine: `number.fst` expands them).
    Anything else must be run through `_speakable()` (or dropped) first.

Handlers may be sync or async: `ToolExecutor.execute` awaits whatever a
handler returns, which is what lets a network tool do I/O without blocking the
audio loop. `winvoice/tools/weather.py` is that one tool, and it lives apart
because its reason to change is the provider, not the machine.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

from winvoice.contracts import has_latin
from winvoice.logging import get_logger

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────
# Speech helpers
# ──────────────────────────────────────────────────────────────

def _speakable(value: Any, fallback: str = "") -> str:
    """
    `value` when it can be spoken, `fallback` when it cannot.

    Used for anything that comes from outside — an ASR'd app name, a path —
    because names are precisely where Latin text sneaks into a sentence and
    leaves a hole in it. The predicate itself is shared with the pipeline
    (`winvoice/contracts/speech.py`); this only chooses the fallback wording.
    """
    text = str(value or "").strip()
    return text if text and not has_latin(text) else fallback


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

# How each allowlisted app is *said*. The TTS model is Chinese-only, so the
# English ids and executable names above must never reach the speaker.
APP_SPEECH = {
    "notepad": "记事本",
    "calculator": "计算器",
    "explorer": "资源管理器",
    "cmd": "命令提示符",
    "powershell": "命令行窗口",
    "vscode": "代码编辑器",
    "chrome": "谷歌浏览器",
    "edge": "微软浏览器",
    "settings": "系统设置",
}


# A short, representative sample derived from APP_SPEECH. Reciting all nine
# allowlisted apps takes ~20 s of speech, far too long for a refusal the user
# then has to talk over.
_ALLOWLIST_EXAMPLES = "、".join(APP_SPEECH[k] for k in ("notepad", "calculator", "chrome")) + "这类程序"


def _speakable_name(value: str) -> str:
    """
    The requested app name as the TTS can actually say it.

    The name comes from ASR, so it may be English ("wechat"). The Chinese VITS
    lexicon has no Latin entries and silently drops them, which would leave a
    hole where the name should be — so fall back to a generic phrase instead.
    """
    return _speakable(value, "这个程序")


# Spoken synonyms pinning the user's words onto an allowlisted id. The Chinese
# labels from APP_SPEECH and the raw ids are registered automatically below.
_EXTRA_APP_ALIASES = {
    # notepad
    "笔记本": "notepad", "记事薄": "notepad", "便笺": "notepad", "文本编辑器": "notepad",
    # calculator
    "计算机": "calculator", "calc": "calculator", "算数": "calculator",
    # explorer
    "文件管理器": "explorer", "我的电脑": "explorer", "此电脑": "explorer", "文件夹": "explorer",
    # cmd
    "终端": "cmd", "命令行": "cmd", "cmd.exe": "cmd", "命令窗口": "cmd",
    # powershell
    "powershell.exe": "powershell", "ps": "powershell", "命令行工具": "powershell",
    # vscode
    "编辑器": "vscode", "代码": "vscode", "vs code": "vscode", "visual studio code": "vscode",
    # browsers
    "浏览器": "chrome", "谷哥浏览器": "chrome", "google": "chrome",
    "edge浏览器": "edge", "微软浏览器edge": "edge",
    # settings
    "设置": "settings", "系统设置面板": "settings",
}

APP_ALIASES: Dict[str, str] = {**{app_id: app_id for app_id in ALLOWED_APPS},
                               **{label: app_id for app_id, label in APP_SPEECH.items()},
                               **_EXTRA_APP_ALIASES}


def resolve_app(name: str) -> Optional[str]:
    """
    Map a spoken app name onto an allowlisted id, or None if it is not one.

    Handles the three things ASR and the rule layer actually produce: a Chinese
    label ("记事本"), a raw English id ("notepad"), or a close-but-wrong spelling
    of either ("Notpa" — seen in the live log). Exact matches win, then a fuzzy
    match with a high cutoff so unrelated words are still refused.
    """
    import difflib

    candidate = str(name or "").strip()
    if not candidate:
        return None

    # Exact, case-insensitive, ignoring a trailing particle ("记事本吧") and
    # spaces inside an English name ("vs code").
    normalized = candidate.lower().replace(" ", "")
    for particle in ("吧", "呢", "啊", "呀"):
        normalized = normalized.removesuffix(particle)
    if normalized in APP_ALIASES:
        return APP_ALIASES[normalized]

    matches = difflib.get_close_matches(normalized, list(APP_ALIASES), n=1, cutoff=0.75)
    return APP_ALIASES[matches[0]] if matches else None


def _app_paths_entry(exe_name: str) -> Optional[str]:
    """
    The program Windows itself would start for `exe_name`, or None.

    `HK*\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\<exe>` is the
    registry key installers write so that `start chrome` works from anywhere.
    Chrome, Edge and VS Code rely on it (`chrome.exe`, `msedge.exe`, `Code.exe`
    are all absent from PATH — measured on this machine), which is why launching
    them by bare name failed.
    """
    try:
        import winreg
    except ImportError:  # pragma: no cover - the assistant is Windows-only
        return None

    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"{key_path}\{exe_name}") as key:
                value = winreg.QueryValueEx(key, "")[0]
        except OSError:
            continue  # not registered in this hive
        candidate = str(value).strip().strip('"')
        if candidate and Path(candidate).exists():
            return candidate
    return None


# Registry key names that differ from the command we run. VS Code registers
# `Code.exe`; its command — and the PATH shim — is `code`. Without this the
# App Paths lookup would miss an install whose shim was not added to PATH.
_APP_PATHS_NAMES = {"code": "Code.exe"}


def resolve_app_command(command: str) -> Optional[str]:
    """
    The real, on-disk program for an allowlisted command, or None.

    `App Paths` first, then PATH: the registry knows where Chrome, Edge and VS
    Code are, while PATH covers the system tools (`notepad`, `cmd`,
    `powershell`). Returning None is a normal outcome — it means "not
    installed", which the caller has to *say* rather than launching a name that
    cmd.exe will not recognise.
    """
    if not command:
        return None

    for name in filter(None, (_APP_PATHS_NAMES.get(command), command)):
        entry = _app_paths_entry(name)
        if entry:
            return entry
    return shutil.which(command) or None


def _launch(command: str) -> None:
    """Start a resolved program; raises when the OS refuses."""
    if Path(command).suffix.lower() in (".cmd", ".bat"):
        # CreateProcess cannot run a script file: it needs a shell. VS Code's
        # `code.cmd` is reached this way when it has no App Paths entry.
        subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/c", command], check=True)
    else:
        # No shell: a shell would start, print 「不是内部或外部命令」 and exit 0,
        # which is how a failed launch was reported as success.
        subprocess.Popen([command])


def open_app(args: Dict[str, Any]) -> Dict[str, Any]:
    """Open an application from the allowlist, by Chinese or English name."""
    spoken = str(args.get("app", ""))
    app = resolve_app(spoken)
    if app is None:
        logger.info("open_app_rejected", app=spoken, allowed=list(ALLOWED_APPS))
        return {
            "success": False,
            "error": f"App not allowed: {spoken}. Allowed: {list(ALLOWED_APPS)}",
            "message": f"{_speakable_name(spoken)}不在我能打开的名单里。我能打开{_ALLOWLIST_EXAMPLES}。",
        }

    cmd = ALLOWED_APPS[app]
    try:
        if cmd.startswith("ms-"):
            # URI-style targets (e.g. ms-settings:) must go through `start`.
            subprocess.run(["cmd", "/c", "start", "", cmd], check=True, capture_output=True)
            return {"success": True, "message": f"已经打开{APP_SPEECH[app]}了。"}

        path = resolve_app_command(cmd)
        if path is None:
            logger.warning("open_app_not_found", app=app, command=cmd)
            return {
                "success": False,
                "error": f"Cannot locate {cmd}: not on PATH and not in App Paths",
                "message": f"我没找到{APP_SPEECH[app]}的安装位置。",
            }

        _launch(path)
        # Nothing beyond "the OS accepted the start": Chrome exits immediately
        # when an instance is already running, so polling the process would
        # report failures for launches that worked.
        return {"success": True, "message": f"已经打开{APP_SPEECH[app]}了。"}
    except Exception as e:
        logger.warning("open_app_failed", app=app, error=str(e))
        return {"success": False, "error": str(e), "message": f"打开{APP_SPEECH[app]}的时候出错了。"}


def close_app(args: Dict[str, Any]) -> Dict[str, Any]:
    """Close an application from the allowlist, by Chinese or English name."""
    spoken = str(args.get("app", ""))
    app = resolve_app(spoken)
    if app is None:
        logger.info("close_app_rejected", app=spoken, allowed=list(ALLOWED_APPS))
        return {
            "success": False,
            "error": f"App not allowed: {spoken}",
            "message": f"{_speakable_name(spoken)}不在我能关闭的名单里。我能关闭{_ALLOWLIST_EXAMPLES}。",
        }

    exe = ALLOWED_APPS[app]
    try:
        if not exe.lower().endswith(".exe"):
            # URI targets ("ms-settings:") are not processes; there is nothing
            # to kill, and 「已经关闭」 would be a claim about something that
            # never happened.
            logger.info("close_app_unsupported_target", app=app, target=exe)
            return {
                "success": False,
                "error": f"{app} has no process to kill ({exe})",
                "message": f"我关不掉{APP_SPEECH[app]}。",
            }

        proc = subprocess.run(["taskkill", "/f", "/im", exe], capture_output=True, text=True)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            # 128 is taskkill's "no process matches"; anything else (access
            # denied, a driver process, a sandbox) is a different story and
            # must not be reported as 「没有在运行」.
            not_running = proc.returncode == 128 or "not found" in detail.lower() or "找不到" in detail
            logger.info("close_app_failed", app=app, returncode=proc.returncode)
            return {
                "success": False,
                "error": detail[:400] or f"taskkill exit {proc.returncode}",
                "message": (
                    f"{APP_SPEECH[app]}好像没有在运行。"
                    if not_running
                    else f"我没能关掉{APP_SPEECH[app]}。"
                ),
            }
        return {"success": True, "message": f"已经关闭{APP_SPEECH[app]}了。"}

    except Exception as e:
        return {"success": False, "error": str(e), "message": f"关闭{APP_SPEECH[app]}的时候出错了。"}


# ──────────────────────────────────────────────────────────────
# Volume Control
# ──────────────────────────────────────────────────────────────

def set_volume(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Set or adjust the system volume.

    Two mutually exclusive forms, both in percentage points:

      * `level` — absolute target 0-100 ("音量调到10%"). Takes precedence.
      * `delta` — relative change ("音量调大 20", negative to lower).

    Implemented via the Windows Core Audio endpoint volume through
    PowerShell, which needs no extra dependency beyond pywin32's runtime.
    """
    level = args.get("level")
    delta = args.get("delta")

    if level is None and delta is None:
        return {
            "success": False,
            "error": "set_volume needs 'level' (absolute 0-100) or 'delta' (percentage points)",
        }

    if level is not None:
        try:
            target_pct = max(0, min(100, int(round(float(level)))))
        except (TypeError, ValueError):
            return {
                "success": False,
                "error": f"level must be a number 0-100, got {level!r}",
                "message": "我没听清音量要调到多少，请说一个零到一百之间的数字。",
            }
        target_expr = f"{target_pct} / 100.0"
    else:
        try:
            step = int(delta)
        except (TypeError, ValueError):
            return {
                "success": False,
                "error": f"delta must be an integer, got {delta!r}",
                "message": "我没听清音量要调多少。",
            }
        step = max(-100, min(100, step))
        target_expr = f"$current + ({step} / 100.0)"

    # `$current` is read either way so the script body stays identical; it is
    # only part of the target expression in the relative case.
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
$target = [Math]::Max(0.0, [Math]::Min(1.0, {target_expr}))
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
        return {"success": False, "error": "volume control timed out", "message": "调节音量超时了。"}

    if proc.returncode != 0:
        return {
            "success": False,
            "error": (proc.stderr or "volume control failed").strip()[:400],
            "message": "调节音量失败了。",
        }

    applied = (proc.stdout or "").strip().splitlines()[-1:] or [""]
    # The Windows script prints the level it actually applied, which is what
    # the user asked about — not the requested one.
    reached = applied[0].strip()
    if not reached.isdigit():
        return {"success": True, "message": "音量已经调好了。"}
    return {"success": True, "message": f"音量已经调到百分之{reached}。"}


# ──────────────────────────────────────────────────────────────
# Media Control
# ──────────────────────────────────────────────────────────────

def media_control(args: Dict[str, Any]) -> Dict[str, Any]:
    """Control media playback (global media keys)."""
    action = str(args.get("action", "")).lower()
    # One table per action: the virtual key *and* what to say about it. They
    # were two maps once, and a fifth action would have had to be added to
    # both (or the reply would be silent about a working keypress).
    actions = {
        "play": (0xB3, "正在播放。"),    # VK_MEDIA_PLAY_PAUSE
        "pause": (0xB3, "已经暂停。"),
        "next": (0xB0, "已经切到下一首。"),   # VK_MEDIA_NEXT_TRACK
        "prev": (0xB1, "已经切到上一首。"),   # VK_MEDIA_PREV_TRACK
    }

    if action not in actions:
        return {"success": False, "error": f"Unknown action: {action}"}

    vk, spoken = actions[action]
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

    return {"success": True, "message": spoken}


# ──────────────────────────────────────────────────────────────
# Web Search
# ──────────────────────────────────────────────────────────────

def search_web(args: Dict[str, Any]) -> Dict[str, Any]:
    """Open web search in default browser."""
    query = args.get("query", "").strip()
    if not query:
        return {"success": False, "error": "Empty query", "message": "我没有听清要搜索什么。"}

    try:
        # Percent-encode: ASR produces Chinese, and a raw UTF-8 query in a URL
        # is at best browser-dependent.
        url = f"https://www.bing.com/search?q={quote(query)}"
        if webbrowser.open(url) is False:
            # `open` reports whether a browser was found. Claiming success here
            # would be the same lie as 「已经打开谷歌浏览器了。」 with no window.
            logger.warning("search_web_no_browser", query=query)
            return {
                "success": False,
                "error": f"No browser handled {url}",
                "message": "我没能打开浏览器。",
            }
        # The query only reaches the sentence when it is already Chinese: an
        # English one would be dropped word by word by the TTS lexicon.
        spoken = _speakable(query, "")
        return {
            "success": True,
            "message": f"已经在浏览器里搜索{spoken}了。" if spoken else "已经打开浏览器了。",
        }
    except Exception as e:
        return {"success": False, "error": str(e), "message": "打开浏览器的时候出错了。"}


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
            return {"success": False, "error": "File not found", "message": "没有找到这个文件。"}

        if path.stat().st_size > 10 * 1024 * 1024:  # 10MB limit
            return {"success": False, "error": "File too large", "message": "这个文件太大了。"}

        content = path.read_text(encoding="utf-8")
        return {"success": True, "content": content, "path": str(path)}
    except ValueError:
        return {
            "success": False,
            "error": "Path not allowed (outside user directory)",
            "message": "我只能读取你自己目录下的文件。",
        }
    except Exception as e:
        # `str(e)` is usually English and may quote the path; logging it is
        # fine, saying it is not.
        logger.warning("read_file_failed", path=str(path), error=str(e))
        return {"success": False, "error": str(e), "message": "读取这个文件的时候出错了。"}


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
        # The path itself is never spoken: it is Latin, long, and the user
        # already knows which file they asked for.
        return {"success": True, "message": "已经写好了。", "path": str(path)}
    except ValueError:
        return {
            "success": False,
            "error": "Path not allowed (outside user directory)",
            "message": "我只能写入你自己目录下的文件。",
        }
    except Exception as e:
        logger.warning("write_file_failed", path=str(path), error=str(e))
        return {"success": False, "error": str(e), "message": "写入这个文件的时候出错了。"}


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
            return {"success": False, "error": "Script not found", "message": "没有找到这个脚本。"}

        # Determine interpreter by extension
        suffix = path.suffix.lower()
        if suffix == ".py":
            cmd = ["python", str(path)]
        elif suffix == ".ps1":
            cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(path)]
        elif suffix in (".bat", ".cmd"):
            cmd = ["cmd", "/c", str(path)]
        else:
            return {
                "success": False,
                "error": f"Unsupported script type: {suffix}",
                "message": "我只能运行 Python、PowerShell 和批处理脚本。",
            }

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return {
            "success": result.returncode == 0,
            # stdout/stderr stay in the result for the caller; they are English
            # and unpronounceable often enough that they must never be spoken.
            "message": "脚本已经运行完了。" if result.returncode == 0 else "脚本运行出错了。",
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except ValueError:
        return {
            "success": False,
            "error": "Path not allowed (outside user directory)",
            "message": "我只能运行你自己目录下的脚本。",
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Script timeout (60s)", "message": "脚本运行超过一分钟，我停下来了。"}
    except Exception as e:
        logger.warning("run_script_failed", path=str(path), error=str(e))
        return {"success": False, "error": str(e), "message": "运行这个脚本的时候出错了。"}


# ──────────────────────────────────────────────────────────────
# Time Query
# ──────────────────────────────────────────────────────────────

# Spoken Chinese uses a 12-hour clock with a period word. 24-hour form is
# unusable for speech: 00:30 would come out as 「0 点 30 分」 (or worse, 「24 点」
# if the hour were printed raw), neither of which anyone says out loud.
_TIME_PERIODS = ((5, "凌晨"), (11, "上午"), (12, "中午"), (17, "下午"), (23, "晚上"))


def format_spoken_time(now: datetime) -> str:
    """The current time as a short sentence the Chinese TTS can pronounce."""
    period = next(label for last_hour, label in _TIME_PERIODS if now.hour <= last_hour)
    hour = now.hour % 12 or 12
    tail = "整" if now.minute == 0 else f" {now.minute} 分"
    return f"现在是{period} {hour} 点{tail}。"


def get_time(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Report the current local time.

    Zero arguments, zero dependencies, zero network — and it still needs a
    `message`, because the pipeline speaks `message` and nothing else
    (see §6.4 of spec.md).
    """
    now = datetime.now()
    return {
        "success": True,
        "message": format_spoken_time(now),
        "time": now.strftime("%H:%M"),
        "date": now.strftime("%Y-%m-%d"),
    }
