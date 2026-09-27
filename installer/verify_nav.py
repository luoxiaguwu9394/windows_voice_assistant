#!/usr/bin/env python3
"""
Headless regression check for the wizard nav bar (the 「上一步没反应 /
下一步被标成关闭向导」 bug class: nav state leaking across pages).

Runs the real WizardApp with a withdrawn root, stubs out every hook with
machine side effects (extraction, --check, marker/shortcuts), and asserts the
derived button states per page. Needs a display (any Windows session has one).

    python installer/verify_nav.py
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

INSTALLER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(INSTALLER_DIR))

from wizard.app import WizardApp  # noqa: E402
from wizard.ui.pages import CheckPage, ExtractPage, FinishPage  # noqa: E402


def nav(app: WizardApp) -> tuple:
    return (
        str(app._back_btn["state"]),
        str(app._next_btn["state"]),
        str(app._next_btn["text"]),
    )


def main() -> int:
    # Side-effect-free stubs: none of these may touch disk or spawn processes.
    ExtractPage.on_enter = lambda self: None
    CheckPage.on_enter = lambda self: None
    FinishPage.on_enter = lambda self: None

    root = tk.Tk()
    root.withdraw()
    app = WizardApp(root)
    app.state.install_dir = Path(__file__).parent / "_nav_scratch"

    failures: list[str] = []

    def expect(label: str, actual: tuple, want: tuple) -> None:
        ok = all(a == w for a, w in zip(actual, want))
        detail = f"back={actual[0]} next={actual[1]} text={actual[2]!r}" if len(actual) == 3 else f"current={actual[0]}"
        print(f"  {'OK ' if ok else '[X]'} {label}: {detail}")
        if not ok:
            failures.append(label)

    print("[1] welcome (page 0)")
    expect("fresh welcome", nav(app), ("disabled", "normal", "开始 >"))

    print("[2] install dir (page 1)")
    app.go_next()
    expect("back re-enabled, default label", nav(app), ("normal", "normal", "下一步 >"))

    print("[3] extract page busy semantics")
    extract = app._pages[3]
    app.goto(3)
    extract.set_nav(busy=True)
    expect("busy disables both", nav(app), ("disabled", "disabled", "下一步 >"))
    extract.set_nav(busy=False, next_=True)
    expect("done re-enables", nav(app), ("normal", "normal", "下一步 >"))

    print("[4] finish page and back")
    app.goto(app._total - 1)
    expect("finish label", nav(app), ("normal", "normal", "关闭向导"))
    app.go_back()
    # The regression: the label used to STICK as 「关闭向导」 on every earlier page.
    expect("label resets after leaving finish", nav(app), ("normal", "normal", "下一步 >"))

    print("[5] welcome back-pressed is a no-op at page 0")
    app.goto(0)
    app.go_back()
    expect("still on page 0", (app._current,), (0,))

    root.destroy()
    if failures:
        print(f"\n[X] {len(failures)} nav check(s) failed: {failures}")
        return 1
    print("\n[OK] all nav checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
