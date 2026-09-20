"""
The Windows Core Audio endpoint volume, reached through PowerShell.

There is exactly one copy of the COM interop here, because there are now two
callers that must agree about what the volume *is*: the `set_volume` tool that
changes it (`winvoice/tools/builtin.py`) and the verifier that reads it back to
confirm the change happened (`winvoice/tools/verifier.py:SetVolumeVerifier`).
Two copies of a `[Guid(...)]` interface block is precisely the kind of pair that
drifts silently — the failure mode would be a verifier that reads a *different*
endpoint than the one the tool wrote, and reports every volume change as broken.

`pywin32` is already a dependency, but the Core Audio COM interfaces are not
wrapped by it, so raw `Add-Type` interop is the shortest path that needs no new
dependency.
"""

from __future__ import annotations

import subprocess
from typing import Optional

# The interop is compiled per invocation (`Add-Type`) because each PowerShell
# process is fresh; the cost is ~300 ms and is paid only by the volume tool.
_TYPE_DEFINITION = r"""
using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume {
  int NotImpl1();
  int NotImpl2();
  int GetChannelCount(out uint c);
  int SetMasterVolumeLevel(float level, ref System.Guid ctx);
  int SetMasterVolumeLevelScalar(float level, ref System.Guid ctx);
  int GetMasterVolumeLevel(out float level);
  int GetMasterVolumeLevelScalar(out float level);
}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice {
  int Activate(ref System.Guid id, int clsCtx, System.IntPtr activationParams, [System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.IUnknown)] out object iface);
}
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator {
  int NotImpl1();
  int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice endpoint);
}
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorComObject { }
public class Audio {
  public static void SetVolumeScalar(float level) {
    var enumerator = (IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
    IMMDevice dev; enumerator.GetDefaultAudioEndpoint(0, 1, out dev);
    var guid = typeof(IAudioEndpointVolume).GUID;
    object o; dev.Activate(ref guid, 23, System.IntPtr.Zero, out o);
    var vol = (IAudioEndpointVolume)o;
    var ctx = System.Guid.Empty;
    vol.SetMasterVolumeLevelScalar(level, ref ctx);
  }
  public static float GetVolumeScalar() {
    var enumerator = (IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
    IMMDevice dev; enumerator.GetDefaultAudioEndpoint(0, 1, out dev);
    var guid = typeof(IAudioEndpointVolume).GUID;
    object o; dev.Activate(ref guid, 23, System.IntPtr.Zero, out o);
    var vol = (IAudioEndpointVolume)o;
    float level; vol.GetMasterVolumeLevelScalar(out level);
    return level;
  }
}
"""


def _preamble() -> str:
    return "$ErrorActionPreference = 'Stop'\nAdd-Type -TypeDefinition @'\n" + _TYPE_DEFINITION + "\n'@\n"


def build_set_script(target_expr: str) -> str:
    """
    Script that moves the volume and prints the level it actually reached.

    `target_expr` is a PowerShell expression resolving to 0.0-1.0, evaluated
    once so `$current` is read either way; it is only part of the target for a
    relative change. The absolute case must never build `$current + ...` —
    that bug turned 「调到百分之十」 into "+10 points"
    (`tests/unit/test_volume_absolute.py`).
    """
    return _preamble() + f"""
$current = [Audio]::GetVolumeScalar()
$target = [Math]::Max(0.0, [Math]::Min(1.0, {target_expr}))
[Audio]::SetVolumeScalar($target)
Write-Output ([int]($target * 100))
"""


def build_read_script() -> str:
    """Script that prints the current master volume as an integer percentage."""
    return _preamble() + """
$current = [Audio]::GetVolumeScalar()
Write-Output ([int]($current * 100))
"""


def read_volume_percent(timeout_s: float = 20.0) -> Optional[int]:
    """
    The endpoint volume right now, or None when it cannot be read.

    Returning None rather than raising is deliberate: a verifier that cannot
    observe must report `NOT_VERIFIABLE`, and an exception here would instead
    fail the tool call it was only supposed to be checking.
    """
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", build_read_script()],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if proc.returncode != 0:
        return None

    lines = (proc.stdout or "").strip().splitlines()
    if not lines:
        return None
    last = lines[-1].strip()
    return int(last) if last.isdigit() else None


__all__ = ["build_read_script", "build_set_script", "read_volume_percent"]
