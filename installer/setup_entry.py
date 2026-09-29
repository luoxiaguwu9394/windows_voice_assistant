"""PyInstaller entry point for WinVoice-Setup.exe.

Frozen startup errors are invisible under --noconsole, so any exception
before the Tk mainloop lands in %TEMP%\\winvoice-setup-error.log.
"""

import os
import sys
import tempfile
import traceback


def _run() -> int:
    from wizard.app import main

    return main()


if __name__ == "__main__":
    try:
        # A downloaded updater lives in Downloads, so carry the selected
        # installation path explicitly when launching it.
        if "--install-dir" in sys.argv:
            index = sys.argv.index("--install-dir")
            if index + 1 >= len(sys.argv):
                raise ValueError("--install-dir requires a path")
            os.environ["WINVOICE_INSTALL_DIR"] = sys.argv[index + 1]
        raise SystemExit(_run())
    except SystemExit:
        raise
    except Exception:
        error_path = os.path.join(tempfile.gettempdir(), "winvoice-setup-error.log")
        with open(error_path, "w", encoding="utf-8") as file:
            traceback.print_exc(file=file)
        raise
