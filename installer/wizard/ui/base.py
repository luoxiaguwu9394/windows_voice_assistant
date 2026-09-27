"""Page base class and frozen-resource resolution — imported by app and pages."""

from __future__ import annotations

import sys
from pathlib import Path
from tkinter import ttk
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..app import WizardApp


def base_dir() -> Path:
    """Where bundled resources live: _MEIPASS when frozen, installer/ in dev."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    # installer/wizard/ui/base.py -> installer/
    return Path(__file__).resolve().parents[2]


class Page(ttk.Frame):
    """
    One wizard step.

    Nav is derived, not shared: each page records its own intent (back/next
    enabled, busy, hint, label) and `WizardApp.refresh_nav` renders the bar
    from the CURRENT page on every navigation. Pages must never touch the
    buttons directly — a page that did, would leak its state into every page
    constructed after it (the 「上一步没反应 / 下一步被标成关闭向导」 bug).

    `set_nav` must be called on the Tk thread; background workers wrap it in
    `post()`.
    """

    title: str = ""
    #: Custom next-button label; None = 「下一步 >」.
    next_label: Optional[str] = None

    def __init__(self, app: "WizardApp", master: ttk.Frame, index: int):
        super().__init__(master, padding=16)
        self.app = app
        self.state = app.state
        self.index = index
        self._back_ok = True
        self._next_ok = True
        self._busy = False
        self._hint = ""
        self._build()

    # ── hooks for subclasses ───────────────────────────────────

    def _build(self) -> None:  # pragma: no cover - UI
        raise NotImplementedError

    def on_enter(self) -> None:
        """Called when the page becomes visible (start background work here)."""

    def can_proceed(self) -> bool:
        """Validate inputs; show a messagebox and return False on problems."""
        return True

    def on_proceed(self) -> bool:
        """Commit the page's choices (render config etc.). False blocks nav."""
        return True

    # ── nav intent (Tk thread only) ────────────────────────────

    def set_nav(self, *, back: Optional[bool] = None, next_: Optional[bool] = None,
                next_label: Optional[str] = None, busy: Optional[bool] = None,
                hint: Optional[str] = None) -> None:
        if back is not None:
            self._back_ok = bool(back)
        if next_ is not None:
            self._next_ok = bool(next_)
        if next_label is not None:
            self.next_label = next_label
        if busy is not None:
            self._busy = bool(busy)
        if hint is not None:
            self._hint = hint
        self.app.refresh_nav()

    # ── helpers for subclasses ─────────────────────────────────

    def post(self, fn: Callable[[], None]) -> None:
        self.app.post(fn)

    def run_bg(self, work: Callable[[], None]) -> None:
        self.app.run_bg(work)

    def _errorbox(self, title: str, message: str) -> None:
        from tkinter import messagebox

        messagebox.showerror(title, message, parent=self.app.root)
