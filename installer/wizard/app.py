"""
The wizard's main window: a linear stack of pages with bottom navigation.

Threads: every long operation runs in a worker thread and communicates ONLY
through `app.post(callable)` — callables are drained on the Tk thread by an
`after` poller, which keeps every widget touch on the main thread without
each page re-implementing marshaling.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable, List, Optional

from .core.state import InstallState
from .ui.base import Page
from .ui.pages import PAGE_CLASSES


class WizardApp:
    PAGES: List[type] = PAGE_CLASSES

    def __init__(self, root: tk.Tk):
        self.root = root
        self.state = InstallState()
        self._events: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._current = 0
        self._pages: List[Page] = []

        root.title("WinVoice 语音助手 — 安装向导")
        root.geometry("780x680")
        root.minsize(720, 600)

        outer = ttk.Frame(root, padding=0)
        outer.pack(fill="both", expand=True)

        self._header = ttk.Frame(outer, padding=(16, 12))
        self._header.pack(fill="x")
        self._title_var = tk.StringVar(value="")
        self._step_var = tk.StringVar(value="")
        ttk.Label(self._header, textvariable=self._title_var,
                  font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        ttk.Label(self._header, textvariable=self._step_var,
                  foreground="#666666").pack(anchor="w")
        ttk.Separator(outer).pack(fill="x")

        # The nav bar is created BEFORE the pages: a page's `_build` runs at
        # construction time and may call set_nav (e.g. the welcome page).
        # Bottom-anchored packing keeps it visually below the page body.
        nav = ttk.Frame(outer, padding=(16, 10))
        nav.pack(side="bottom", fill="x")
        ttk.Separator(outer).pack(side="bottom", fill="x")
        self._back_btn = ttk.Button(nav, text="< 上一步", command=self.go_back, width=12)
        self._back_btn.pack(side="left")
        self._next_btn = ttk.Button(nav, text="下一步 >", command=self.go_next, width=12)
        self._next_btn.pack(side="right")
        self._busy_label = ttk.Label(nav, text="", foreground="#0a7d32")
        self._busy_label.pack(side="right", padx=12)

        self.body = ttk.Frame(outer)
        self.body.pack(fill="both", expand=True)
        self.body.grid_rowconfigure(0, weight=1)
        self.body.grid_columnconfigure(0, weight=1)

        for index, page_cls in enumerate(self.PAGES):
            page = page_cls(self, self.body, index)
            page.grid(row=0, column=0, sticky="nsew")
            self._pages.append(page)

        self._total = len(self._pages)
        self.goto(0)

    # ── navigation ─────────────────────────────────────────────

    @property
    def current_page(self) -> Page:
        return self._pages[self._current]

    def goto(self, index: int) -> None:
        index = max(0, min(index, self._total - 1))
        self._current = index
        page = self._pages[index]
        page.tkraise()
        self._title_var.set(page.title)
        self._step_var.set(f"步骤 {index + 1} / {self._total}")
        self.refresh_nav()
        page.on_enter()

    def refresh_nav(self) -> None:
        """
        Render the nav bar from the CURRENT page's recorded intent.

        This is the single writer of button states: nothing persists across
        page changes, so a page that disabled (or renamed) the buttons during
        its own workflow can never affect another page.
        """
        page = self.current_page
        if page._busy:
            self._back_btn.configure(state="disabled")
            self._next_btn.configure(state="disabled")
            self._busy_label.configure(text="正在处理…")
            return
        back_ok = page._back_ok and self._current > 0
        self._back_btn.configure(state="normal" if back_ok else "disabled")
        self._next_btn.configure(
            state="normal" if page._next_ok else "disabled",
            text=page.next_label or "下一步 >",
        )
        self._busy_label.configure(text="" if page._next_ok else page._hint)

    def go_next(self) -> None:
        page = self.current_page
        if not page.can_proceed():
            return
        if not page.on_proceed():
            return
        if self._current + 1 < self._total:
            self.goto(self._current + 1)
        else:
            self.root.destroy()

    def go_back(self) -> None:
        if self._current > 0:
            self.goto(self._current - 1)

    # ── thread marshaling ──────────────────────────────────────

    def post(self, fn: Callable[[], None]) -> None:
        self._events.put(fn)

    def run_bg(self, work: Callable[[], None]) -> None:
        """Run `work` on a daemon thread; exceptions surface on the Tk thread."""

        def _wrapper():
            try:
                work()
            except Exception as error:  # noqa: BLE001 - the UI shows everything
                self.post(lambda: self._show_worker_error(error))

        threading.Thread(target=_wrapper, daemon=True).start()

    def _show_worker_error(self, error: Exception) -> None:
        from tkinter import messagebox

        self.current_page._busy = False
        self.refresh_nav()
        messagebox.showerror("向导出错", f"{type(error).__name__}: {error}", parent=self.root)

    def _poll(self) -> None:
        try:
            while True:
                fn = self._events.get_nowait()
                fn()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def run(self) -> None:
        self._poll()
        self.root.mainloop()


def main() -> int:
    # Crisp text on high-DPI displays; harmless if the call fails.
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    try:
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    WizardApp(root).run()
    return 0
