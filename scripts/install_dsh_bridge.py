"""
Install (or refresh) the DSH bridge bundle that exposes this project's tools.

DeepSeek Harness only learns about this project's tool registry through an MCP
server it spawns, and it only learns about *that* through a plugin row in its
composed configuration. Rows cannot be added by a `--patch` overlay (see
`winvoice/dsh/bridge.py` for why), so the row ships as a tiny profile bundle
which has to be installed into the DSH home once.

Usage
-----
    python scripts/install_dsh_bridge.py            # write the bundle, show what would run
    python scripts/install_dsh_bridge.py --install  # also install it into the DSH home
    python scripts/install_dsh_bridge.py --verify   # check the installed composition

The bundle is generated from configuration rather than checked in, because it
hard-codes the absolute repository path and the Python interpreter: a committed
copy would be wrong on every machine but the one that generated it, and a stale
one fails in a confusing way (the agent silently has no tools).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from winvoice.dsh.bridge import write_bundle  # noqa: E402
from winvoice.dsh.settings import load_settings  # noqa: E402

ROW_MARKER = "mcp-winvoice"


def find_dsh() -> str | None:
    """The `dsh` launcher on PATH, if any."""
    return shutil.which("dsh")


def run_dsh(dsh_bin: str, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    """
    Invoke `dsh`.

    On Windows `shutil.which("dsh")` resolves to the npm `dsh.cmd` shim, which
    CreateProcess cannot execute directly — it needs a shell. The same reasoning
    as `winvoice/tools/builtin.py:_launch`, and the same guard.
    """
    if os.name == "nt" and Path(dsh_bin).suffix.lower() in (".cmd", ".bat"):
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/c", dsh_bin, *args]
    else:
        command = [dsh_bin, *args]
    # Explicit UTF-8 with replacement: the console default on this platform is
    # GBK, and `--dump-config` output contains characters outside it, which made
    # a plain `text=True` run die with UnicodeDecodeError *after* a successful
    # install and report the install as failed.
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=600,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--install", action="store_true", help="run `dsh plugin add` for the generated bundle")
    parser.add_argument("--verify", action="store_true", help="check that the row is present in the composed config")
    parser.add_argument("--profile", default=None, help="DSH profile (default: dsh.local.profile)")
    parser.add_argument("--dsh-home", default=None, help="DSH home (default: dsh.local.dsh_home)")
    args = parser.parse_args()

    settings = load_settings()
    profile = args.profile or settings.local.profile
    dsh_home = Path(args.dsh_home) if args.dsh_home else settings.local.dsh_home
    dsh_home.mkdir(parents=True, exist_ok=True)

    bundle = write_bundle(
        dest=settings.bridge.bundle_dir,
        workspace=REPO_ROOT,
        server_name=settings.bridge.server_name,
        python_executable=sys.executable,
        tool_timeout_ms=settings.bridge.tool_timeout_ms,
        utterance_file=settings.bridge.utterance_file,
    )

    print(f"bundle        : {bundle}")
    print(f"profile       : {profile}")
    print(f"DSH_HOME      : {dsh_home}")
    print(f"tool server   : {sys.executable} -m winvoice.mcp_server")
    print(f"tools appear  : mcp__{settings.bridge.server_name}__<tool>")

    dsh_bin = find_dsh()
    install_cmd = ["plugin", "--profile", profile, "add", f"file:{bundle.as_posix()}"]

    if not dsh_bin:
        print("\n[!] `dsh` is not on PATH. Install DeepSeek Harness first, then re-run with --install.")
        print(f"    would run: dsh {' '.join(install_cmd)}")
        return 1 if (args.install or args.verify) else 0

    env = dict(os.environ)
    env["DSH_HOME"] = str(dsh_home)

    if args.install:
        print(f"\n$ dsh {' '.join(install_cmd)}")
        proc = run_dsh(dsh_bin, install_cmd, env)
        tail = (proc.stderr or proc.stdout or "").strip()
        if proc.returncode != 0:
            print(f"[X] install failed (exit {proc.returncode}):\n{tail[-1500:]}")
            return 1
        print(tail[-800:] if tail else "installed.")
        print("\n[OK] bridge installed. Restart the assistant to pick it up.")

    if args.verify or args.install:
        print(f"\n$ dsh --profile {profile} --dump-config")
        proc = run_dsh(dsh_bin, ["--profile", profile, "--dump-config"], env)
        composed = (proc.stdout or "") + (proc.stderr or "")
        if ROW_MARKER in composed:
            print(f"[OK] {ROW_MARKER} is present in the composed configuration")
            return 0
        print(f"[X] {ROW_MARKER} is NOT in the composed configuration")
        print(composed[-1500:])
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
