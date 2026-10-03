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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

from winvoice.contracts import has_latin
from winvoice.logging import get_logger
from ._coreaudio import build_set_script
from ._explorer import close_windows as close_explorer_windows

logger = get_logger(__name__)

# How long a graceful `taskkill` may take before we assume the application is
# waiting for the user (its own "save changes?" prompt). A bare timeout here
# would read as a fault when nothing is actually wrong.
CLOSE_TIMEOUT_S = 12.0

# Processes that must never be force-killed, whatever the request says.
# `explorer.exe` is the one that has actually bitten this project: it is the
# Windows shell, so `taskkill /f /im explorer.exe` removes the desktop, taskbar
# and Start menu. It is listed here as a backstop *as well as* being routed to
# the window-closing path above, because the allowlist is data and someone will
# eventually add a shell-adjacent name to it.
PROTECTED_PROCESSES = {
    "explorer.exe",
    "winlogon.exe",
    "csrss.exe",
    "wininit.exe",
    "services.exe",
    "lsass.exe",
    "smss.exe",
    "dwm.exe",
    "sihost.exe",
    "ctfmon.exe",
}


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

@dataclass(frozen=True)
class AppEntry:
    """One openable application: how to launch it, how to say it, how to kill it."""

    id: str                       # stable internal id (latin; never spoken)
    label: str                    # how TTS says it (Chinese)
    command: str                  # launch: absolute path, App Paths/PATH exe name, or URI
    image: Optional[str] = None   # process image when it differs from command (code shim → Code.exe)
    guest: bool = True            # custom apps default host-only; built-ins keep the old behaviour

    @property
    def process_image(self) -> str:
        """
        The process name `taskkill /im` and the verifiers match on.

        An explicit `image` wins (VS Code launches via the `code` shim but
        runs as `Code.exe`); an .exe command is its own image basename; a URI
        (`ms-settings:`) has no process and comes back unchanged.
        """
        if self.image:
            return self.image
        return Path(self.command).name if self.command.lower().endswith(".exe") else self.command


# The nine built-ins. They cannot be shadowed or removed by config entries
# (see `current_apps`), and their launch resolution (App Paths → PATH) is what
# makes them machine-adaptive: chrome/edge/vscode are found wherever their
# installers registered them, and a missing install is reported honestly
# instead of launched blind.
BUILTIN_APPS: Dict[str, AppEntry] = {
    "notepad":    AppEntry("notepad", "记事本", "notepad.exe"),
    "calculator": AppEntry("calculator", "计算器", "calc.exe"),
    "explorer":   AppEntry("explorer", "资源管理器", "explorer.exe"),
    "cmd":        AppEntry("cmd", "命令提示符", "cmd.exe"),
    "powershell": AppEntry("powershell", "命令行窗口", "powershell.exe"),
    "vscode":     AppEntry("vscode", "代码编辑器", "code", image="Code.exe"),
    "chrome":     AppEntry("chrome", "谷歌浏览器", "chrome.exe"),
    "edge":       AppEntry("edge", "微软浏览器", "msedge.exe"),
    "settings":   AppEntry("settings", "系统设置", "ms-settings:"),
}

# Historical name, kept alive for readers and old imports: the launch command
# of each *built-in*. The openable set today is `current_apps()` — built-ins
# plus the settings UI's `tools.apps`, resolved fresh on every call.
ALLOWED_APPS = {app_id: entry.command for app_id, entry in BUILTIN_APPS.items()}


def _config_apps() -> list:
    """`tools.apps` entries, read fresh so the settings UI's changes are hot."""
    try:
        from winvoice.config import get_config

        raw = get_config().get("tools.apps")
    except Exception:  # no config yet (unit tests, early init) — built-ins only
        return []
    return raw if isinstance(raw, list) else []


def current_apps() -> Dict[str, AppEntry]:
    """
    Built-ins merged with `tools.apps`, resolved on every call.

    The settings UI writes the config file; the watchdog reloads it within
    0.5 s and the next utterance sees the new table — no restart. A config
    entry may not shadow a built-in id (redefining `cmd` must not be able to
    repoint `close_app` at an unrelated binary); such entries are skipped.
    """
    apps = dict(BUILTIN_APPS)
    for raw in _config_apps():
        if not isinstance(raw, dict):
            logger.warning("config_app_entry_ignored", reason="not a mapping")
            continue
        app_id = str(raw.get("id") or "").strip().lower()
        command = str(raw.get("command") or "").strip()
        if not app_id or not command:
            logger.warning("config_app_entry_ignored", reason="missing id or command", id=app_id)
            continue
        if app_id in apps:
            logger.warning("config_app_entry_ignored", reason="cannot shadow a built-in", id=app_id)
            continue
        label = str(raw.get("label") or "").strip() or "这个程序"
        image = str(raw.get("image") or "").strip() or None
        apps[app_id] = AppEntry(
            id=app_id, label=label, command=command, image=image,
            guest=bool(raw.get("guest", False)),
        )
    return apps


def sensitive_app_ids() -> set:
    """
    The ids `tools.sensitive_apps` names — never openable below the full tier.

    Read fresh like the app table. When the config is unavailable or the key
    is missing, the shipped default stands, so the gate fails closed.
    """
    default = {"cmd", "powershell"}
    try:
        from winvoice.config import get_config

        raw = get_config().get("tools.sensitive_apps")
    except Exception:
        return default
    if not isinstance(raw, list):
        return default
    return {str(item).strip().lower() for item in raw if str(item).strip()} or default


def _allowlist_examples() -> str:
    """
    A short, spoken sample of what can be opened. Reciting the whole table
    takes ~20 s of speech — far too long for a refusal the user then has to
    talk over.
    """
    labels = [entry.label for entry in list(current_apps().values())[:3]]
    return "、".join(labels) + "这类程序"


def _speakable_name(value: str) -> str:
    """
    The requested app name as the TTS can actually say it.

    The name comes from ASR, so it may be English ("wechat"). The Chinese VITS
    lexicon has no Latin entries and silently drops them, which would leave a
    hole where the name should be — so fall back to a generic phrase instead.
    """
    return _speakable(value, "这个程序")


# Spoken synonyms pinning the user's words onto a built-in id. The Chinese
# labels, the raw ids and each entry's process-image stem are registered
# automatically by `resolve_app`; this table is for the words that appear in
# neither (「浏览器」 for chrome). Custom apps are additionally reachable by
# partial name and one-character slips — see `resolve_app`.
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


def _normalize_app_name(value: str) -> str:
    """The comparison key for a spoken or registered name: lowercase, no
    spaces, trailing particle removed (「记事本吧」 → 记事本)."""
    normalized = str(value or "").strip().lower().replace(" ", "")
    for particle in ("吧", "呢", "啊", "呀"):
        normalized = normalized.removesuffix(particle)
    return normalized


def _one_edit_apart(a: str, b: str) -> bool:
    """True when a and b differ by one substitution, insertion or deletion.

    Covers the single-character slips real ASR makes on Chinese names
    (「记事版」 for 记事本); a two-character name may only be substituted
    (「威信」 for 微信), never stretched, so 「ps4」 cannot become powershell.
    """
    if a == b:
        return True
    if len(a) == len(b):
        return sum(1 for x, y in zip(a, b) if x != y) == 1
    if len(a) != len(b) + 1:
        if len(b) != len(a) + 1:
            return False
        a, b = b, a  # a is the longer one from here
    if len(b) < 3:
        return False
    index_a = index_b = 0
    skipped = False
    while index_b < len(b):
        if a[index_a] != b[index_b]:
            if skipped:
                return False
            skipped = True
            index_a += 1
            continue
        index_a += 1
        index_b += 1
    return True


def resolve_app(name: str, *, partial: bool = True) -> Optional[str]:
    """
    Map a spoken app name onto an allowlisted id, or None if it is not one.

    Deliberately forgiving, because real ASR and real users are both imprecise.
    Exact matches win; then a *containment* match (「网易云」→ 网易云音乐,
    「微信电脑版」→ 微信); then a single-character slip (「记事版」→ 记事本,
    「Notpa」→ notepad — seen in the live log). Names are case- and
    space-insensitive, and every app also answers to its process-image stem
    (「weixin」 → 微信). The alias table is rebuilt from `current_apps()` on
    every call, so an app the user just enabled in the settings UI is
    reachable on the next utterance.

    Containment needs the shorter side to be at least two characters and to
    cover at least 40 % of the longer one — 「网易云」 ⊂ 网易云音乐 passes, a
    single stray character does not. Two apps can both contain what was said
    (「音乐」 in 网易云音乐 and QQ音乐); the closer-length candidate wins and
    the reply names the app actually opened, so a wrong pick is audible and
    correctable instead of silent.

    `partial=False` is the stricter contract the rule tier's pre-check uses:
    no containment, and fuzzy/edit matches need three characters. 「文件」 must
    not resolve to 「文件夹」 there, or 「打开文件」 stops meaning READ_FILE.
    """
    import difflib

    candidate = str(name or "").strip()
    if not candidate:
        return None

    apps = current_apps()
    aliases: Dict[str, str] = {}

    def register(key: str, app_id: str) -> None:
        normalized = _normalize_app_name(key)
        if normalized:
            aliases.setdefault(normalized, app_id)

    for app_id, entry in apps.items():
        register(app_id, app_id)
        register(entry.label, app_id)
        image = entry.process_image
        if image.lower().endswith(".exe"):
            register(image[:-4], app_id)  # weixin.exe → weixin
    for synonym, app_id in _EXTRA_APP_ALIASES.items():
        if app_id in apps:
            register(synonym, app_id)

    spoken = _normalize_app_name(candidate)
    if not spoken:
        return None
    if spoken in aliases:
        return aliases[spoken]

    best_id: Optional[str] = None
    best_score = 0.0
    for key, app_id in aliases.items():
        shorter, longer = sorted((len(spoken), len(key)))
        score = 0.0
        if partial and shorter >= 2 and (spoken in key or key in spoken):
            # The closer the two lengths are, the more of the name was said.
            score = 0.55 + 0.3 * (shorter / longer) if shorter / longer >= 0.4 else 0.0
        if not score and (partial or len(spoken) >= 3):
            ratio = difflib.SequenceMatcher(None, spoken, key).ratio()
            if ratio >= 0.75:
                score = ratio
            elif _one_edit_apart(spoken, key):
                score = 0.72
        if score > best_score:
            best_score = score
            best_id = app_id
    return best_id


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


def _is_console_app(path: str) -> bool:
    """
    True when the executable is a console-subsystem binary.

    A console program started without a console of its own *inherits* the
    caller's: 「打开命令提示符」 used to print cmd's banner into the assistant's
    own window and sit there sharing its stdin — no new window, just the
    assistant's console "refreshing in place" (live report, 2026-09-27).
    The subsystem byte is read from the PE header
    (IMAGE_SUBSYSTEM_WINDOWS_CUI = 3) rather than maintained as a name list,
    so the next console app added to the allowlist is correct without anyone
    having to remember this. Unreadable/foreign binaries are treated as GUI:
    the launch then behaves exactly as before.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0x3C)  # DOS header: e_lfanew, the offset of the PE header
            pe_offset = int.from_bytes(f.read(4), "little")
            # signature (4) + COFF header (20) + OptionalHeader.Subsystem (68)
            f.seek(pe_offset + 4 + 20 + 68)
            subsystem = int.from_bytes(f.read(2), "little")
    except OSError:
        return False
    return subsystem == 3  # IMAGE_SUBSYSTEM_WINDOWS_CUI


def _launch(command: str) -> None:
    """Start a resolved program; raises when the OS refuses."""
    if Path(command).suffix.lower() in (".cmd", ".bat"):
        # CreateProcess cannot run a script file: it needs a shell. VS Code's
        # `code.cmd` is reached this way when it has no App Paths entry.
        subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/c", command], check=True)
    elif _is_console_app(command):
        # cmd.exe, powershell.exe: a console program must get a console of its
        # own, or it borrows this process's — printing its banner into the
        # assistant's window and sharing its stdin.
        subprocess.Popen([command], creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        # No shell: a shell would start, print 「不是内部或外部命令」 and exit 0,
        # which is how a failed launch was reported as success.
        subprocess.Popen([command])


def _resolve_launch_target(entry: AppEntry) -> Optional[str]:
    """
    The on-disk program (or URI) to launch for `entry`, or None when absent.

    An absolute command from the settings UI is used as-is when it exists —
    that is how a per-user install like WeChat is reached regardless of where
    it was installed. Anything else goes through App Paths → PATH, the same
    machine-adaptive chain the built-ins rely on.
    """
    if entry.command.startswith("ms-"):
        return entry.command  # URI-style: launched through `start`
    if Path(entry.command).is_absolute():
        return entry.command if Path(entry.command).exists() else None
    return resolve_app_command(entry.command)


def open_app(args: Dict[str, Any]) -> Dict[str, Any]:
    """Open an application from the allowlist, by Chinese or English name."""
    spoken = str(args.get("app", ""))
    if not spoken.strip():
        # The classifier lost the name (「把微信打开」 before the rule tier
        # learned the inverted form) — ask for it instead of claiming it is
        # not on a list the user never named.
        return {
            "success": False,
            "error": "No app named",
            "message": "要打开哪个程序？可以说「打开记事本」「打开浏览器」。",
        }
    app = resolve_app(spoken)
    entry = current_apps().get(app) if app else None
    if entry is None:
        logger.info("open_app_rejected", app=spoken)
        return {
            "success": False,
            "error": f"App not allowed: {spoken}",
            "message": f"{_speakable_name(spoken)}不在我能打开的名单里。我能打开{_allowlist_examples()}。",
        }

    try:
        target = _resolve_launch_target(entry)
        if target is None:
            logger.warning("open_app_not_found", app=app, command=entry.command)
            return {
                "success": False,
                "error": f"Cannot locate {entry.command}: not on PATH and not in App Paths",
                "message": f"我没找到{entry.label}的安装位置。",
            }
        if target.startswith("ms-"):
            # URI-style targets (e.g. ms-settings:) must go through `start`.
            subprocess.run(["cmd", "/c", "start", "", target], check=True, capture_output=True)
        else:
            _launch(target)
        # Nothing beyond "the OS accepted the start": Chrome exits immediately
        # when an instance is already running, so polling the process would
        # report failures for launches that worked.
        return {"success": True, "message": f"已经打开{entry.label}了。"}
    except Exception as e:
        logger.warning("open_app_failed", app=app, error=str(e))
        return {"success": False, "error": str(e), "message": f"打开{entry.label}的时候出错了。"}


def close_app(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Close an application from the allowlist, by Chinese or English name.

    Two rules keep this from doing something the user did not ask for:

    * **Graceful by default.** `taskkill` is run *without* `/f`, so the
      application gets the chance to ask about unsaved work. `/f` skips that
      prompt and silently discards it, so it is only used when the request
      explicitly asked to force the close (`force: true`).
    * **The shell is not an application.** `explorer.exe` carries the desktop,
      taskbar and Start menu as well as the folder windows, so forcing it down
      takes the whole GUI with it. 「关闭文件资源管理器」 means "close the folder
      windows", and that is done through Explorer's own automation object
      instead — see `_explorer.py`. No code path here passes `explorer.exe` to
      `taskkill`.
    """
    spoken = str(args.get("app", ""))
    force = bool(args.get("force", False))
    if not spoken.strip():
        return {
            "success": False,
            "error": "No app named",
            "message": "要关闭哪个程序？可以说「关闭记事本」「关闭浏览器」。",
        }
    app = resolve_app(spoken)
    entry = current_apps().get(app) if app else None
    if entry is None:
        logger.info("close_app_rejected", app=spoken)
        return {
            "success": False,
            "error": f"App not allowed: {spoken}",
            "message": f"{_speakable_name(spoken)}不在我能关闭的名单里。我能关闭{_allowlist_examples()}。",
        }

    # What `taskkill` matches is the process image, not the launch command:
    # VS Code launches via the `code` shim but runs as `Code.exe`
    # (`process_image`). A URI target has no process here to kill.
    exe = entry.process_image

    # ── the shell: close its windows, never its process ───────────
    if app == "explorer":
        remaining, error = close_explorer_windows()
        if remaining is None:
            logger.warning("close_explorer_failed", error=error)
            return {
                "success": False,
                "error": error or "explorer automation unavailable",
                "message": "我没能关掉资源管理器的窗口。",
            }
        if remaining == 0:
            return {
                "success": True,
                "message": "已经关上资源管理器的窗口了。",
                "windows_remaining": 0,
                "force_requested": force,
            }
        return {
            "success": False,
            "error": f"{remaining} explorer window(s) still open",
            "message": f"还有{remaining}个资源管理器窗口没关上。",
            "windows_remaining": remaining,
        }

    # ── a process we must never force down ────────────────────────
    if exe.lower() in PROTECTED_PROCESSES:
        logger.warning("close_app_protected_process", app=app, exe=exe, force=force)
        return {
            "success": False,
            "error": f"{exe} is a protected system process and is never killed",
            "message": f"{entry.label}是系统进程，我不能关掉它。",
        }

    if not exe.lower().endswith(".exe"):
        # URI targets ("ms-settings:") are not processes; there is nothing
        # to kill, and 「已经关闭」 would be a claim about something that
        # never happened.
        logger.info("close_app_unsupported_target", app=app, target=exe)
        return {
            "success": False,
            "error": f"{app} has no process to kill ({exe})",
            "message": f"我关不掉{entry.label}。",
        }

    command = ["taskkill"]
    if force:
        command.append("/f")
    command += ["/im", exe]

    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=CLOSE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # A graceful close on an app with unsaved work waits for the user to
        # answer the application's own prompt. Saying so is more useful than a
        # bare "timed out": from the user's side, nothing is wrong.
        logger.info("close_app_awaiting_user", app=app)
        return {
            "success": False,
            "error": f"taskkill did not return within {CLOSE_TIMEOUT_S:.0f}s",
            "message": f"{entry.label}好像在等你确认，可能有没保存的内容。",
        }
    except OSError as e:
        return {"success": False, "error": str(e), "message": f"关闭{entry.label}的时候出错了。"}

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        # 128 is taskkill's "no process matches"; anything else (access
        # denied, a driver process, a sandbox) is a different story and
        # must not be reported as 「没有在运行」.
        not_running = proc.returncode == 128 or "not found" in detail.lower() or "找不到" in detail
        logger.info("close_app_failed", app=app, returncode=proc.returncode, force=force)
        return {
            "success": False,
            "error": detail[:400] or f"taskkill exit {proc.returncode}",
            "message": (
                f"{entry.label}好像没有在运行。"
                if not_running
                else f"我没能关掉{entry.label}。"
            ),
        }
    return {"success": True, "message": f"已经关闭{entry.label}了。"}


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
    # only part of the target expression in the relative case. The COM interop
    # itself lives in `_coreaudio.py`, shared with the verifier that reads the
    # level back — two copies of those GUIDs is exactly the pairing that drifts.
    script = build_set_script(target_expr)
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

# Spoken folder words → the real folders under the user directory. 「帮我在桌面
# 建一个txt」 used to produce the literal relative path 桌面\新建.txt, which —
# with the assistant's working directory inside the home directory — slipped
# through the confinement check and would have written into a *folder named
# 桌面 inside the repo* (live report 2026-09-27). Only the first path segment
# is mapped; the confinement check in each handler still applies afterwards.
_FOLDER_ALIASES = {
    "桌面": "Desktop",
    "desktop": "Desktop",
    "下载": "Downloads",
    "downloads": "Downloads",
    "文档": "Documents",
    "我的文档": "Documents",
    "documents": "Documents",
    "图片": "Pictures",
    "照片": "Pictures",
    "pictures": "Pictures",
    "音乐": "Music",
    "music": "Music",
    "视频": "Videos",
    "videos": "Videos",
}


def resolve_user_path(raw: Any) -> Path:
    """
    A spoken path as a real path under the user directory.

    A leading folder word (「桌面」「下载」…) is replaced with the actual folder
    (`Path.home()/Desktop` …), because that is what the speaker means and a
    bare relative path would otherwise resolve against the assistant's working
    directory. Everything else — absolute paths, deeper relative paths — comes
    through unchanged; the confinement check in each handler still applies
    after this.
    """
    text = str(raw or "").strip().strip("\"'")
    if not text:
        return Path.home()
    parts = [part for part in Path(text).parts if part not in ("", ".", "/")]
    if parts:
        target = _FOLDER_ALIASES.get(parts[0]) or _FOLDER_ALIASES.get(parts[0].lower())
        if target is not None:
            base = Path.home() / target
            rest = [part for part in parts[1:] if part != "\\"]
            return base / Path(*rest) if rest else base
    # No folder word: behave like the old code — a relative path resolves
    # against the working directory (the repo lives inside the home directory,
    # so the confinement check keeps accepting it).
    return Path(text).expanduser().resolve()


def read_file(args: Dict[str, Any]) -> Dict[str, Any]:
    """Read a text file."""
    path = resolve_user_path(args.get("path", ""))
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


# How many entry names a directory listing may speak. A folder with sixty
# files must not become sixty seconds of TTS: the count carries the information
# and a handful of names makes it concrete.
LIST_DIR_SPOKEN_NAMES = 4


def list_dir(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    List the entries of a folder, by count plus a few speakable names.

    Same confinement as `read_file`: only under the user directory. Without a
    path the user directory itself is listed — that is what 「当前目录下有什么
    文件」 means for a voice assistant, whose working directory is meaningless
    to the person talking to it.

    The spoken message needs the same care as every other `message`: file names
    are precisely where Latin text lives (`Desktop`, `setup.py`), and the
    Chinese TTS lexicon drops every Latin word silently. So the count is always
    spoken, names are filtered through `has_latin`, and a listing with nothing
    pronounceable in it says so instead of reading a number followed by holes.
    """
    raw = str(args.get("path") or "").strip()
    user_dir = Path.home()
    try:
        target = resolve_user_path(raw) if raw else user_dir
        target.relative_to(user_dir)
    except ValueError:
        return {
            "success": False,
            "error": f"Path not allowed (outside user directory): {raw}",
            "message": "我只能列出你自己目录下的文件夹。",
        }
    except Exception as e:
        logger.warning("list_dir_failed", path=raw, error=str(e))
        return {"success": False, "error": str(e), "message": "查看这个文件夹的时候出错了。"}

    if not target.exists():
        return {"success": False, "error": f"Not found: {target}", "message": "没有找到这个文件夹。"}
    if not target.is_dir():
        return {"success": False, "error": f"Not a directory: {target}", "message": "这个路径不是文件夹。"}

    try:
        with os.scandir(target) as entries:
            # Directories first, then files, each alphabetical — the order the
            # names are spoken in.
            names = [entry.name for entry in sorted(
                entries, key=lambda e: (not e.is_dir(), e.name.lower()),
            )]
    except OSError as e:
        logger.warning("list_dir_failed", path=str(target), error=str(e))
        return {"success": False, "error": str(e), "message": "查看这个文件夹的时候出错了。"}

    total = len(names)
    if total == 0:
        return {"success": True, "message": "这个文件夹是空的。", "count": 0, "entries": [], "path": str(target)}

    speakable = [name for name in names if not has_latin(name)]
    head = speakable[:LIST_DIR_SPOKEN_NAMES]
    if head:
        message = f"一共有{total}项，前面几项是{'、'.join(head)}"
        if total > len(head):
            message += "，还有其他"
        message += "。"
    else:
        message = f"一共有{total}项，名字念不出来，请看屏幕。"
    return {"success": True, "message": message, "count": total, "entries": names, "path": str(target)}


def write_file(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Write content to a file — or create an empty one when none was dictated.

    「帮我在桌面建立一个txt文件」 names no content, and that request means
    exactly what Windows' 新建文本文档 means: an empty file. So `content` is
    optional. The one dangerous combination — no content **and** an existing
    file — is refused instead of silently truncating it: a confirmation
    question cannot distinguish 「create it」 from 「wipe it」, so the tool
    asks for the content rather than guessing.
    """
    path = resolve_user_path(args.get("path", ""))
    has_content = "content" in args and args.get("content") is not None
    content = str(args.get("content")) if has_content else ""

    try:
        # Security: only allow under user directory
        user_dir = Path.home()
        path.relative_to(user_dir)

        if not has_content and path.exists():
            logger.info("write_file_overwrite_refused", path=str(path))
            return {
                "success": False,
                "error": "target exists and no content was dictated",
                "message": "这个文件已经存在，请说清楚要写入什么内容。",
            }

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
# Power Actions
# ──────────────────────────────────────────────────────────────

# The action spoken to the user — one source for the confirmation question the
# pipeline asks (`我将要关机，确认请说确认…`) and the refusal wording below.
POWER_ACTION_SPEECH = {
    "shutdown": "关机",
    "restart": "重启",
    "sleep": "进入睡眠",
    "hibernate": "休眠",
    "lock": "锁屏",
    "signout": "注销",
}

# Seconds between the confirmed command and the machine acting. Long enough to
# say 「等等!」 and abort (`shutdown /a`), short enough to feel obedient.
POWER_DELAY_S = 5

# Each action's command and what to say once it has been *initiated*. These
# claim no outcome beyond the start — sleep may degrade to hibernate depending
# on the machine's power configuration, and there is no way to observe the
# difference from here (same honesty class as `media_control`).
POWER_COMMANDS = {
    "shutdown": (["shutdown", "/s", "/t", str(POWER_DELAY_S)], f"机器将在 {POWER_DELAY_S} 秒后关机。"),
    "restart": (["shutdown", "/r", "/t", str(POWER_DELAY_S)], f"机器将在 {POWER_DELAY_S} 秒后重启。"),
    "sleep": (["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], "正在进入睡眠。"),
    "hibernate": (["shutdown", "/h"], "正在休眠。"),
    "lock": (["rundll32.exe", "user32.dll,LockWorkStation"], "已经锁定屏幕。"),
    "signout": (["shutdown", "/l"], "正在注销。"),
}


def system_power(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Power actions on this machine: shutdown, restart, sleep, hibernate, lock,
    sign out.

    Three gates stand between a spoken 「关机」 and the machine acting, and all
    three are inherited rather than reimplemented here: the registry demands a
    spoken confirmation (`requires_confirmation`), the speaker tier must be the
    owner's (`guest_allowed=False` — a guest reaching this handler is already
    impossible), and the agent's MCP path can never supply that confirmation,
    so no model tier can shut the machine down either.

    Fire-and-forget on purpose: `subprocess.run` here would hold the audio loop
    for the whole action (a `sleep` command does not return until the machine
    wakes). Like `media_control`, the message says the action was *initiated* —
    sleep degrading to hibernate on some power configurations is not
    observable from this side.
    """
    action = str(args.get("action", "")).strip().lower()
    if action not in POWER_COMMANDS:
        return {
            "success": False,
            "error": f"Unknown power action: {action}",
            "message": "我没听清要执行哪种电源操作。",
        }

    command, spoken = POWER_COMMANDS[action]
    try:
        subprocess.Popen(command)
    except OSError as e:
        logger.warning("system_power_failed", action=action, error=str(e))
        return {"success": False, "error": str(e), "message": "执行这个操作的时候出错了。"}
    return {"success": True, "message": spoken, "action": action}


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
