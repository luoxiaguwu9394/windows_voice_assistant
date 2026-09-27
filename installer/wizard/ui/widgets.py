"""Small reusable Tk widgets: a streaming log box and a progress row."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Optional


class LogBox(ttk.Frame):
    """Read-only scrolling log; `append` is safe to call from any thread."""

    MAX_CHARS = 400_000  # keep the widget bounded across a 3 GB download

    def __init__(self, master, height: int = 14, **kwargs):
        super().__init__(master, **kwargs)
        self._text = tk.Text(
            self, height=height, wrap="word", state="disabled",
            font=("Microsoft YaHei UI", 9), background="#101418", foreground="#d8e0e8",
            insertbackground="#d8e0e8",
        )
        scroll = ttk.Scrollbar(self, orient="vertical", command=self._text.yview)
        self._text.configure(yscrollcommand=scroll.set)
        self._text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def append(self, line: str) -> None:
        # Only ever call this on the Tk thread (pages post it through app.post).
        self._text.configure(state="normal")
        self._text.insert("end", line.rstrip("\n") + "\n")
        if int(self._text.index("end-1c").split(".")[0]) > self.MAX_CHARS // 100:
            # Trim the head once the box gets large; the tail is what matters.
            self._text.delete("1.0", "200.0")
        self._text.configure(state="disabled")
        self._text.see("end")

    def clear(self) -> None:
        self._text.configure(state="normal")
        self._text.delete("1.0", "end")
        self._text.configure(state="disabled")


class ProgressRow(ttk.Frame):
    """Label + determinate bar + percent text, for one long-running task."""

    def __init__(self, master, label: str = "", **kwargs):
        super().__init__(master, **kwargs)
        self._label_var = tk.StringVar(value=label)
        self._status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self._label_var, width=28).pack(side="left")
        self._bar = ttk.Progressbar(self, mode="determinate", maximum=100.0, length=260)
        self._bar.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Label(self, textvariable=self._status_var, width=16, anchor="e").pack(side="left")

    def set_label(self, text: str) -> None:
        self._label_var.set(text)

    def set_status(self, text: str) -> None:
        self._status_var.set(text)

    def set_fraction(self, done: float, total: float) -> None:
        if total <= 0:
            self._bar.configure(mode="indeterminate")
            if not self._bar.cget("mode") == "indeterminate":
                self._bar.start(60)
            return
        if str(self._bar.cget("mode")) == "indeterminate":
            self._bar.stop()
            self._bar.configure(mode="determinate")
        percent = max(0.0, min(100.0, done / total * 100.0))
        self._bar.configure(value=percent)
        self._status_var.set(f"{percent:.0f}%")

    def reset(self, label: str = "") -> None:
        if str(self._bar.cget("mode")) == "indeterminate":
            self._bar.stop()
            self._bar.configure(mode="determinate")
        self._bar.configure(value=0)
        if label:
            self._label_var.set(label)
        self._status_var.set("")


def make_note(master, text: str, **kwargs) -> ttk.Label:
    """A wrapped, muted explanation line."""
    return ttk.Label(master, text=text, wraplength=560, foreground="#666666",
                     justify="left", **kwargs)


class EntryRow(ttk.Frame):
    """Label + entry (+ optional browse button) in one row."""

    def __init__(self, master, label: str, value: str = "", width: int = 46,
                 browse: bool = False, **kwargs):
        super().__init__(master, **kwargs)
        ttk.Label(self, text=label, width=14, anchor="w").pack(side="left")
        self.var = tk.StringVar(value=value)
        self.entry = ttk.Entry(self, textvariable=self.var, width=width)
        self.entry.pack(side="left", fill="x", expand=True)
        self._browser: Optional[ttk.Button] = None
        if browse:
            self._browser = ttk.Button(self, text="浏览…", width=8, command=self._browse)

    def _browse(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(initialdir=self.var.get() or str(Path_home()))
        if chosen:
            self.var.set(chosen)

    def pack_full(self) -> "EntryRow":
        self.pack(fill="x", pady=3)
        if self._browser is not None:
            self._browser.pack(side="left", padx=(6, 0))
        return self


def Path_home():
    import os

    return os.path.expanduser("~")
