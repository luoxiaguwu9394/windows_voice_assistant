"""
All wizard pages, in navigation order.

Each page is deliberately thin: it collects into `app.state`, runs operations
from `core.flow` on worker threads, and reports into its own widgets. The
page's `index` is assigned by the app and lets a page skip itself (DshPage).
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Dict, List, Optional

from ..core import buildinfo, flow, marker, modelplan, systeminfo, update, wakewords
from ..core.payload import PayloadError, extract_payload, payload_path
from ..core.shortcuts import (
    FOLDER_DESKTOP,
    FOLDER_STARTMENU,
    FOLDER_STARTUP,
    create_shortcut,
    special_folder,
)
from .base import Page, base_dir
from .widgets import EntryRow, LogBox, ProgressRow, make_note

DEFAULT_INSTALL_DIR = Path.home() / "AppData" / "Local" / "WinVoice"
SHORTCUT_NAME = "WinVoice 语音助手.lnk"


def _heading(parent, text: str) -> ttk.Label:
    return ttk.Label(parent, text=text, font=("Microsoft YaHei UI", 11, "bold"),
                     wraplength=620, justify="left")


class WelcomePage(Page):
    title = "欢迎使用 WinVoice 语音助手"
    next_label = "开始 >"

    def _build(self) -> None:
        self.state.install_dir = DEFAULT_INSTALL_DIR
        self._update_info: Optional[update.UpdateInfo] = None

        _heading(self, "本向导将完成全部部署：安装运行时、下载模型、测试音频设备、"
                       "生成配置、注册声纹并自检。完成后从桌面快捷方式即可启动。"
                  ).pack(anchor="w", pady=(0, 10))

        self._facts = tk.StringVar(value="正在收集系统信息…")
        ttk.Label(self, textvariable=self._facts, foreground="#333333",
                  justify="left").pack(anchor="w")

        # Re-entry: an existing install turns this page into a launcher menu.
        self._existing = marker.read_marker(DEFAULT_INSTALL_DIR)
        self._mode = tk.StringVar(value="upgrade" if self._existing else "fresh")
        self._mode_row = ttk.Frame(self)
        if self._existing:
            make_note(self, f"检测到已安装版本 v{self._existing.get('version', '?')}"
                            f"（{DEFAULT_INSTALL_DIR}）。升级会保留模型与配置。"
                      ).pack(anchor="w", pady=(10, 4))
            for value, label in (
                ("upgrade", "升级 / 修复此安装（保留模型和配置）"),
                ("reconfig", "重新配置此安装（重新走一遍向导）"),
                ("launch", "直接启动已安装的助手"),
            ):
                ttk.Radiobutton(self._mode_row, text=label, value=value,
                                variable=self._mode).pack(anchor="w")
        self._mode_row.pack(anchor="w", pady=(0, 6))

        # Update banner (hidden until a newer release is found).
        self._update_row = ttk.Frame(self)
        self._update_var = tk.StringVar(value="")
        ttk.Label(self._update_row, textvariable=self._update_var,
                  foreground="#0a7d32").pack(side="left")
        ttk.Button(self._update_row, text="下载新版本",
                   command=self._download_update).pack(side="left", padx=8)
        self._update_progress = ProgressRow(self, label="下载新版本")

        self.run_bg(self._collect_facts)
        if self._check_updates_enabled():
            self.run_bg(self._check_updates)

    def _check_updates_enabled(self) -> bool:
        return bool(self._existing.get("check_updates", True)) if self._existing else True

    def _collect_facts(self) -> None:
        ram = systeminfo.total_ram_gb()
        tier = systeminfo.recommended_tier(ram)
        disk = systeminfo.free_disk_gb(DEFAULT_INSTALL_DIR.parent)
        text = (
            f"系统：{systeminfo.os_name()}\n"
            f"内存：{ram:.1f} GB → 推荐本地模型：{modelplan.LLM_TIERS[tier]['label']}\n"
            f"默认安装目录可用空间：{disk:.1f} GB（{DEFAULT_INSTALL_DIR}）"
        )
        self.post(lambda: self._facts.set(text))

    def _check_updates(self) -> None:
        info = update.fetch_latest(buildinfo.VERSION)
        if info is None:
            return
        self._update_info = info
        self.post(lambda: self._update_var.set(
            f"发现新版本 v{info.version}（当前 v{buildinfo.VERSION}）"))
        self.post(lambda: self._update_row.pack(anchor="w", pady=(10, 0)))

    def _download_update(self) -> None:
        info = self._update_info
        if info is None:
            return
        self._update_row.pack_forget()
        self._update_progress.pack(anchor="w", pady=(10, 0), fill="x")

        def work():
            dest = update.download_update(
                info, Path.home() / "Downloads",
                on_progress=lambda done, total: self.post(
                    lambda d=done, t=total: self._update_progress.set_fraction(d, t)),
            )
            self.post(lambda: self._update_done(dest))

        self.run_bg(work)

    def _update_done(self, dest: Path) -> None:
        self._update_progress.pack_forget()
        if messagebox.askyesno("更新已下载",
                               f"新版安装器已保存到：\n{dest}\n\n"
                               "现在运行它吗？（本向导将先关闭）", parent=self.app.root):
            import subprocess

            subprocess.Popen([str(dest)], close_fds=True)
            self.app.root.destroy()

    def can_proceed(self) -> bool:
        if self._existing and self._mode.get() == "launch":
            flow.launch_assistant(DEFAULT_INSTALL_DIR)
            self.app.root.destroy()
            return False
        return True

    def on_proceed(self) -> bool:
        if self._existing:
            self.state.install_dir = DEFAULT_INSTALL_DIR
        return True


class InstallDirPage(Page):
    title = "选择安装位置"

    def _build(self) -> None:
        _heading(self, "助手将安装到以下目录（模型、配置、日志都在这里面，"
                       "需要可写权限；不建议 Program Files）。").pack(anchor="w", pady=(0, 10))
        self._row = EntryRow(self, "安装目录：", str(self.state.install_dir),
                             browse=True).pack_full()
        self._hint = make_note(self, "")
        self._hint.pack(anchor="w", pady=(8, 0))
        self._row.var.trace_add("write", lambda *_: self._validate(False))

    def _target(self) -> Path:
        return Path(self._row.var.get().strip().strip('"')).expanduser()

    def _validate(self, report: bool) -> bool:
        target = self._target()
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".wizard-write-test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except Exception as error:
            message = f"目录不可写：{error}"
            if report:
                messagebox.showwarning("无法安装", message, parent=self.app.root)
            self._hint.configure(text=message, foreground="#b3261e")
            return False
        problems = []
        disk = systeminfo.free_disk_gb(target)
        if disk < systeminfo.MIN_DISK_GB:
            problems.append(f"剩余空间仅 {disk:.1f} GB（建议 ≥ {systeminfo.MIN_DISK_GB:.0f} GB）")
        if any(ord(ch) > 127 for ch in str(target)):
            problems.append("路径含中文——一般可用，但若遇到异常请换纯英文路径")
        if " " in str(target):
            problems.append("路径含空格——一般可用，若工具调用异常请考虑无空格路径")
        self._hint.configure(
            text=("注意：" + "；".join(problems)) if problems else f"可用空间 {disk:.1f} GB ✓",
            foreground="#b3261e" if problems else "#0a7d32")
        return not problems

    def can_proceed(self) -> bool:
        if not self._validate(report=True):
            return False
        self.state.install_dir = self._target()
        return True


class NetworkPage(Page):
    title = "网络设置"

    def _build(self) -> None:
        _heading(self, "模型从 GitHub 与 Hugging Face 下载。无法直连时，"
                       "设置代理或 Hugging Face 镜像（GitHub 只能走代理）。"
                  ).pack(anchor="w", pady=(0, 10))
        self._mode = tk.StringVar(value="direct")
        for value, label in (("direct", "直连"), ("proxy", "HTTP 代理")):
            ttk.Radiobutton(self, text=label, value=value, variable=self._mode,
                            command=self._sync).pack(anchor="w")
        self._proxy_row = EntryRow(self, "代理地址：", "http://127.0.0.1:7890")
        self._hf_row = EntryRow(self, "HF 镜像：", "https://hf-mirror.com", width=40)
        make_note(self, "镜像留空 = 直连 huggingface.co。常见镜像：https://hf-mirror.com"
                  ).pack(anchor="w", pady=(2, 6))
        buttons = ttk.Frame(self)
        ttk.Button(buttons, text="测试连接", command=self._test).pack(side="left")
        buttons.pack(anchor="w", pady=6)
        self._result = make_note(self, "")
        self._result.pack(anchor="w")
        self._sync()

    def _sync(self) -> None:
        state = "normal" if self._mode.get() == "proxy" else "disabled"
        self._proxy_row.entry.configure(state=state)

    def _apply(self) -> None:
        self.state.proxy_url = (
            self._proxy_row.var.get().strip() if self._mode.get() == "proxy" else ""
        )
        self.state.hf_endpoint = self._hf_row.var.get().strip()

    def _test(self) -> None:
        self._apply()
        self._result.configure(text="测试中…")
        self.set_nav(busy=True)

        def work():
            result = update.probe_endpoints(self.state.proxy_url, self.state.hf_endpoint)
            self.post(lambda: self._show(result))

        self.run_bg(work)

    def _show(self, result: Dict[str, bool]) -> None:
        self.set_nav(busy=False)

        def mark(ok: bool) -> str:
            return "✓ 可达" if ok else "✗ 不可达"

        self._result.configure(
            text=f"GitHub：{mark(result['github'])}    Hugging Face：{mark(result['hf'])}",
            foreground="#0a7d32" if all(result.values()) else "#b3261e")

    def on_proceed(self) -> bool:
        self._apply()
        return True


class ExtractPage(Page):
    title = "安装运行时"

    def _build(self) -> None:
        _heading(self, "解压内嵌的 Python 运行时、依赖库与 llama.cpp 到安装目录"
                       "（不联网；模型与已生成的配置不受影响）。").pack(anchor="w", pady=(0, 10))
        self._progress = ProgressRow(self, label="解压")
        self._progress.pack(fill="x", pady=4)
        self._log = LogBox(self, height=10)
        self._log.pack(fill="both", expand=True, pady=6)
        self._done = False

    def on_enter(self) -> None:
        if not self._done:
            self.set_nav(busy=True)
            self.run_bg(self._work)

    def _work(self) -> None:
        try:
            archive = payload_path(base_dir())
            self.post(lambda: self._log.append(f"载荷：{archive}"))
            extract_payload(
                archive, self.state.install_dir,
                on_progress=lambda done, total: self.post(
                    lambda d=done, t=total: self._progress.set_fraction(d, t)),
            )
            self.post(lambda: self._log.append("运行时已释放。"))
            flow.write_default_config(self.state.install_dir)
            self.post(lambda: self._log.append("默认配置已写入 config/config.yaml。"))
            flow.copy_setup_exe(self.state.install_dir)
            self._done = True
            self.post(lambda: self.set_nav(busy=False, next_=True))
        except (PayloadError, flow.FlowError) as error:
            self.post(lambda: self._log.append(f"[失败] {error}"))
            self.post(lambda: self._errorbox("解压失败", str(error)))
            self.post(lambda: self.set_nav(
                busy=False, next_=False,
                hint="解压失败 — 可返回上一步检查安装目录，重新进入本页即重试"))


class ModelsPage(Page):
    title = "下载模型"

    def _build(self) -> None:
        _heading(self, "选择本地大模型档位（已按内存预选，可改）；语音模型为必装核心。"
                       "下载支持断点续传，升级时不重复下载。").pack(anchor="w", pady=(0, 8))
        self._tier = tk.StringVar(value=self.state.llm_tier)
        row = ttk.Frame(self)
        for tier_id, meta in modelplan.LLM_TIERS.items():
            ttk.Radiobutton(row, text=meta["label"], value=tier_id,
                            variable=self._tier).pack(anchor="w")
        row.pack(anchor="w")
        self._streaming = tk.BooleanVar(value=self.state.include_streaming_asr)
        ttk.Checkbutton(self, text="附加流式 ASR 备用模型（+458 MB，默认配置用不到）",
                        variable=self._streaming).pack(anchor="w", pady=(4, 8))

        buttons = ttk.Frame(self)
        self._download_btn = ttk.Button(buttons, text="开始下载", command=self._start)
        self._download_btn.pack(side="left")
        buttons.pack(anchor="w")
        self._progress = ProgressRow(self, label="当前文件")
        self._progress.pack(fill="x", pady=4)
        self._log = LogBox(self, height=9)
        self._log.pack(fill="both", expand=True)
        self._success = False

    def on_enter(self) -> None:
        # Reflect the RAM recommendation the first time the page is shown.
        if not self._success and self.state.llm_tier == "3b":
            recommended = systeminfo.recommended_tier(systeminfo.total_ram_gb())
            self._tier.set(recommended)
            self.state.llm_tier = recommended

    def _start(self) -> None:
        self.state.llm_tier = self._tier.get()
        self.state.include_streaming_asr = self._streaming.get()
        self._success = False
        self._download_btn.configure(state="disabled")
        self.set_nav(busy=True)
        self._log.clear()
        self._progress.reset("当前文件")

        def on_model(key: str, note: str) -> None:
            self.post(lambda: self._log.append(f"→ {key}（{note}）"))

        def on_progress(key: str, done: int, total: int) -> None:
            self.post(lambda: self._progress.set_fraction(done, total))

        def on_result(key: str, ok: bool, detail: str) -> None:
            mark = "✓" if ok else "✗"
            self.post(lambda: self._log.append(f"   {mark} {key} {detail}"))

        def work() -> None:
            try:
                flow.run_model_download(
                    self.state.install_dir, self.state,
                    on_line=lambda line: self.post(lambda l=line: self._log.append(l)),
                    on_model=on_model, on_progress=on_progress, on_result=on_result,
                )
                self._success = True
                self.post(lambda: self._log.append("全部模型就绪，并已生成完整性封存。"))
                self.post(lambda: self.set_nav(busy=False, next_=True))
                self.post(lambda: self._download_btn.configure(
                    state="normal", text="重新下载"))
            except flow.FlowError as error:
                self.post(lambda: self._log.append(f"[失败] {error}"))
                self.post(lambda: self._errorbox("下载失败", str(error)))
                self.post(lambda: self.set_nav(
                    busy=False, next_=False,
                    hint="下载失败 — 断点已保留，点「重试」继续"))
                self.post(lambda: self._download_btn.configure(
                    state="normal", text="重试"))

        self.run_bg(work)

    def can_proceed(self) -> bool:
        if not self._success:
            messagebox.showinfo("先下载模型", "请先完成模型下载（或点「开始下载」）。",
                                parent=self.app.root)
            return False
        return True


class DevicesPage(Page):
    title = "音频设备"

    def _build(self) -> None:
        _heading(self, "选择麦克风与扬声器，播放测试音并录音验证。"
                       "「default」使用系统缺省设备。").pack(anchor="w", pady=(0, 10))
        self._input_var = tk.StringVar(value="default")
        self._output_var = tk.StringVar(value="default")
        self._host_api_var = tk.StringVar(value="auto")

        input_row = ttk.Frame(self)
        ttk.Label(input_row, text="麦克风：", width=10, anchor="w").pack(side="left")
        self._input_box = ttk.Combobox(input_row, textvariable=self._input_var,
                                       values=["default"], state="readonly", width=52)
        self._input_box.pack(side="left", fill="x", expand=True)
        input_row.pack(fill="x", pady=3)

        output_row = ttk.Frame(self)
        ttk.Label(output_row, text="扬声器：", width=10, anchor="w").pack(side="left")
        self._output_box = ttk.Combobox(output_row, textvariable=self._output_var,
                                        values=["default"], state="readonly", width=52)
        self._output_box.pack(side="left", fill="x", expand=True)
        output_row.pack(fill="x", pady=3)

        host_row = ttk.Frame(self)
        ttk.Label(host_row, text="输出路由：", width=10, anchor="w").pack(side="left")
        ttk.Combobox(host_row, textvariable=self._host_api_var, state="readonly",
                     values=["auto", "wasapi", "default"], width=12).pack(side="left")
        make_note(host_row, "auto = 优先 WASAPI 共享（推荐）").pack(side="left", padx=8)
        host_row.pack(fill="x", pady=3)

        buttons = ttk.Frame(self)
        ttk.Button(buttons, text="播放测试音", command=self._play_test).pack(side="left")
        ttk.Button(buttons, text="录音测试（4 秒）", command=self._record_test).pack(
            side="left", padx=8)
        buttons.pack(anchor="w", pady=8)
        self._result = make_note(self, "正在枚举设备…")
        self._result.pack(anchor="w")

    def on_enter(self) -> None:
        self.run_bg(self._load_devices)

    def _load_devices(self) -> None:
        try:
            inventory = flow.probe_devices(
                self.state.install_dir, self.state, on_line=lambda line: None)
        except flow.FlowError as error:
            self.post(lambda: self._result.configure(
                text=f"设备枚举失败：{error}", foreground="#b3261e"))
            return
        self.post(lambda: self._fill(inventory))

    def _fill(self, inventory: dict) -> None:
        inputs = ["default"] + [d["name"] for d in inventory.get("inputs", [])]
        outputs = ["default"] + [d["name"] for d in inventory.get("outputs", [])]
        self._input_box.configure(values=inputs)
        self._output_box.configure(values=outputs)
        self._result.configure(
            text=f"找到 {len(inputs) - 1} 个输入设备、{len(outputs) - 1} 个输出设备。",
            foreground="#333333")

    def _play_test(self) -> None:
        self._result.configure(text="正在播放测试音，请留意音量…")
        self.set_nav(busy=True)

        def work():
            result = flow.probe_play_test(
                self.state.install_dir, self.state, self._output_var.get(),
                self._host_api_var.get(), 0, on_line=lambda line: None)
            self.post(lambda: self._show_probe(result))

        self.run_bg(work)

    def _record_test(self) -> None:
        self._result.configure(text="正在录音 4 秒，请对着麦克风说几句话…")
        self.set_nav(busy=True)

        def work():
            result = flow.probe_record_level(
                self.state.install_dir, self.state, self._input_var.get(), 4.0,
                on_line=lambda line: None)
            self.post(lambda: self._show_record(result))

        self.run_bg(work)

    def _show_probe(self, result: dict) -> None:
        self.set_nav(busy=False)
        if result.get("ok"):
            self._result.configure(
                text=f"✓ 播放成功（实际采样率 {result.get('sample_rate')} Hz）。"
                     "没听到声音请换扬声器或检查系统音量。",
                foreground="#0a7d32")
        else:
            self._result.configure(text=f"✗ 播放失败：{result.get('error')}",
                                   foreground="#b3261e")

    def _show_record(self, result: dict) -> None:
        self.set_nav(busy=False)
        if not result.get("ok"):
            self._result.configure(text=f"✗ 录音失败：{result.get('error')}",
                                   foreground="#b3261e")
            return
        if result.get("speech_like"):
            self._result.configure(
                text=f"✓ 录到声音（峰值 {result.get('peak')}，"
                     f"电平 {result.get('rms_dbfs')} dBFS）。",
                foreground="#0a7d32")
        else:
            self._result.configure(
                text="⚠ 几乎没有录到声音：检查麦克风隐私设置（设置 → 隐私 → 麦克风）或换设备。",
                foreground="#b3261e")

    def on_proceed(self) -> bool:
        self.state.audio_input = self._input_var.get() or "default"
        self.state.audio_output = self._output_var.get() or "default"
        self.state.output_host_api = self._host_api_var.get() or "auto"
        return True


class ConfigPage(Page):
    title = "个性化配置"

    def _build(self) -> None:
        _heading(self, "最后几项个人设置，随后生成 config.yaml。").pack(anchor="w", pady=(0, 8))
        self._city_row = EntryRow(self, "默认城市：", self.state.city, width=20).pack_full()

        make_note(self, "唤醒词（说这些词唤醒助手；中文走拼音匹配，英文走音素；"
                        "2–12 字，最多 8 个）").pack(anchor="w", pady=(10, 4))
        editor = ttk.Frame(self)
        self._word_list = tk.Listbox(editor, height=4, exportselection=False)
        self._word_list.pack(side="left", fill="both", expand=True)
        ttk.Button(editor, text="删除选中", command=self._remove_word).pack(
            side="left", padx=6, anchor="n")
        editor.pack(fill="x")
        add_row = ttk.Frame(self)
        self._new_word = tk.StringVar()
        ttk.Entry(add_row, textvariable=self._new_word, width=24).pack(side="left")
        ttk.Button(add_row, text="添加唤醒词", command=self._add_word).pack(
            side="left", padx=6)
        add_row.pack(anchor="w", pady=4)
        for word in self.state.wake_words:
            self._word_list.insert("end", word)
        self._word_warning = make_note(self, "")
        self._word_warning.pack(anchor="w")

        self._dsh = tk.BooleanVar(value=self.state.dsh_enabled)
        ttk.Checkbutton(self, text="安装 DSH Agent 层（本地大模型可调用工具，约多占 400 MB）",
                        variable=self._dsh).pack(anchor="w", pady=(12, 0))

        make_note(self, "云端密钥（可选）：下面两项留空即纯本地运行。").pack(
            anchor="w", pady=(12, 4))
        self._remote_row = EntryRow(self, "OPENAI 兼容 Key：", "", width=34)
        self._remote_base_row = EntryRow(self, "云端端点：", self.state.remote_base_url,
                                         width=34)
        self._deepseek_row = EntryRow(self, "DeepSeek Key：", "", width=34)
        make_note(self, "第一项写入 REMOTE_API_KEY 并启用 llm.remote 兜底；"
                        "第三项写入 DEEPSEEK_API_KEY，供 DSH 本地失败时升级云端。"
                  ).pack(anchor="w", pady=(2, 0))

    def _add_word(self) -> None:
        word = self._new_word.get().strip()
        if not word:
            return
        self._new_word.set("")
        accepted, warnings = wakewords.validate(self._words() + [word])
        self._refresh(accepted)
        self._word_warning.configure(
            text="；".join(warnings), foreground="#b3261e" if warnings else "#333333")

    def _remove_word(self) -> None:
        selection = self._word_list.curselection()
        if selection:
            self._word_list.delete(selection[0])

    def _words(self) -> List[str]:
        return [self._word_list.get(i) for i in range(self._word_list.size())]

    def _refresh(self, words: List[str]) -> None:
        self._word_list.delete(0, "end")
        for word in words:
            self._word_list.insert("end", word)

    def can_proceed(self) -> bool:
        if not wakewords.validate(self._words())[0]:
            messagebox.showwarning("唤醒词", "至少保留一个唤醒词。",
                                   parent=self.app.root)
            return False
        if not self._city_row.var.get().strip():
            messagebox.showwarning("默认城市", "请填写默认城市（天气查询用）。",
                                   parent=self.app.root)
            return False
        return True

    def on_proceed(self) -> bool:
        accepted, _ = wakewords.validate(self._words())
        self.state.wake_words = accepted
        self.state.city = self._city_row.var.get().strip()
        self.state.dsh_enabled = self._dsh.get()
        self.state.remote_key = self._remote_row.var.get().strip()
        self.state.remote_base_url = (
            self._remote_base_row.var.get().strip() or self.state.remote_base_url)
        self.state.deepseek_key = self._deepseek_row.var.get().strip()
        try:
            flow.render_user_config(self.state.install_dir, self.state)
        except Exception as error:  # noqa: BLE001 - shown verbatim
            self._errorbox("生成配置失败", f"{type(error).__name__}: {error}")
            return False
        written = flow.persist_keys(self.state)
        if written:
            messagebox.showinfo(
                "云端密钥已保存",
                "已写入用户环境变量：" + "、".join(written) + "。后续新进程自动继承。",
                parent=self.app.root)
        return True


class DshPage(Page):
    title = "DSH Agent 层"

    def _build(self) -> None:
        _heading(self, "安装 DSH 桥接：本地大模型将能通过受控工具执行操作"
                       "（权限分级、快照与验证器全部保持生效）。").pack(anchor="w", pady=(0, 8))
        self._log = LogBox(self, height=13)
        self._log.pack(fill="both", expand=True)
        self._status = make_note(self, "")
        self._status.pack(anchor="w", pady=4)
        self._disable_btn = ttk.Button(self, text="禁用 DSH 并继续",
                                       command=self._disable_and_continue)
        self._failed = False

    def on_enter(self) -> None:
        if not self.state.dsh_enabled:
            # User unchecked DSH on the previous page: skip silently.
            self.post(lambda: self.app.goto(self.index + 1))
            return
        self.set_nav(busy=True)
        self.run_bg(self._work)

    def _work(self) -> None:
        code = flow.install_dsh(
            self.state.install_dir, self.state,
            on_line=lambda line: self.post(lambda l=line: self._log.append(l)))
        if code == 0:
            self.post(lambda: self._status.configure(
                text="✓ DSH 桥接安装完成。", foreground="#0a7d32"))
            self.post(lambda: self.set_nav(busy=False, next_=True))
        else:
            self._failed = True
            self.post(lambda: self._status.configure(
                text="✗ 安装失败。可以「上一步」重试，或禁用 DSH 继续（其余功能不受影响）。",
                foreground="#b3261e"))
            self.post(lambda: self._disable_btn.pack(anchor="w"))
            self.post(lambda: self.set_nav(
                busy=False, next_=False,
                hint="安装失败 — 可返回重试，或点下方「禁用 DSH 并继续」"))

    def _disable_and_continue(self) -> None:
        self._disable_btn.pack_forget()
        self.state.dsh_enabled = False
        try:
            flow.render_user_config(self.state.install_dir, self.state)
        except Exception:  # noqa: BLE001 - config step already succeeded once
            pass
        self.app.goto(self.index + 1)


class EnrollPage(Page):
    title = "声纹注册"

    def _build(self) -> None:
        _heading(self, "注册主人的声音（8 遍引导朗读）。没有声纹档案时，"
                       "所有人都是访客权限——读文件、写文件等操作不可用。").pack(
            anchor="w", pady=(0, 8))
        make_note(self, "点击「立即注册」会打开一个控制台窗口，按它的提示朗读即可。"
                        "也可以以后补做：WinVoice 目录下运行 "
                        "python\\python.exe -m winvoice.enroll").pack(anchor="w", pady=(0, 8))
        ttk.Button(self, text="立即注册", command=self._start).pack(anchor="w")
        self._status = make_note(self, "")
        self._status.pack(anchor="w", pady=6)

    def _start(self) -> None:
        try:
            flow.start_enroll(self.state.install_dir)
        except Exception as error:  # noqa: BLE001
            self._errorbox("无法打开注册窗口", str(error))
            return
        self._status.configure(text="已打开注册窗口，请按提示朗读…", foreground="#333333")
        self._poll_profile()

    def _poll_profile(self) -> None:
        if flow.enroll_profile_path(self.state.install_dir).is_file():
            self._status.configure(text="✓ 声纹档案已生成。", foreground="#0a7d32")
            return
        self.app.root.after(1000, self._poll_profile)


class CheckPage(Page):
    title = "安装自检"

    def _build(self) -> None:
        _heading(self, "运行 python -m winvoice --check：加载全部模型、核对完整性封存、"
                       "拉起 llama-server。全部通过即安装成功。").pack(anchor="w", pady=(0, 8))
        make_note(self, "首次运行需要加载模型，可能需要 1–2 分钟。").pack(anchor="w", pady=(0, 6))
        self._log = LogBox(self, height=15)
        self._log.pack(fill="both", expand=True)
        self._passed = False

    def on_enter(self) -> None:
        if self._passed:
            return
        self.set_nav(busy=True)
        self.run_bg(self._work)

    def _work(self) -> None:
        code = flow.run_check(
            self.state.install_dir, self.state,
            on_line=lambda line: self.post(lambda l=line: self._log.append(l)))
        self._passed = code == 0
        if self._passed:
            self.post(lambda: self.set_nav(busy=False, next_=True))
        else:
            self.post(lambda: self._errorbox(
                "自检未通过",
                f"--check 退出码 {code}。请阅读上方日志；常见原因：模型下载不完整"
                "（回到模型页重试）、音频设备不可用（回到设备页重测）。"))
            self.post(lambda: self.set_nav(
                busy=False, next_=False,
                hint="自检未通过 — 查看日志，或返回检查设备/模型"))

    def can_proceed(self) -> bool:
        if not self._passed:
            messagebox.showinfo("自检", "自检通过后才能继续。", parent=self.app.root)
        return self._passed


class FinishPage(Page):
    title = "完成"
    next_label = "关闭向导"

    def _build(self) -> None:
        _heading(self, "安装完成！").pack(anchor="w", pady=(0, 8))
        self._desktop = tk.BooleanVar(value=True)
        self._startmenu = tk.BooleanVar(value=True)
        self._autostart = tk.BooleanVar(value=self.state.autostart)
        ttk.Checkbutton(self, text="创建桌面快捷方式", variable=self._desktop).pack(anchor="w")
        ttk.Checkbutton(self, text="创建开始菜单快捷方式", variable=self._startmenu).pack(anchor="w")
        ttk.Checkbutton(self, text="开机自启（登录后自动运行助手）",
                        variable=self._autostart).pack(anchor="w")
        self._status = make_note(self, "")
        self._status.pack(anchor="w", pady=(8, 4))
        buttons = ttk.Frame(self)
        ttk.Button(buttons, text="创建 / 更新快捷方式", command=self._create).pack(side="left")
        ttk.Button(buttons, text="立即启动助手", command=self._launch).pack(
            side="left", padx=8)
        buttons.pack(anchor="w", pady=6)
        make_note(self, "助手以控制台窗口运行：窗口里的日志就是它的工作记录，关闭窗口即退出。"
                        "装好后说一句唤醒词试试吧。").pack(anchor="w", pady=(8, 0))

    def on_enter(self) -> None:
        marker.write_marker(self.state.install_dir, buildinfo.VERSION,
                            check_updates=True)
        self._create()

    def _create(self) -> None:
        target = str(flow.embedded_python(self.state.install_dir))
        workdir = str(self.state.install_dir)
        created = 0
        errors: List[str] = []
        for folder, enabled in (
            (FOLDER_DESKTOP, self._desktop.get()),
            (FOLDER_STARTMENU, self._startmenu.get()),
            (FOLDER_STARTUP, self._autostart.get()),
        ):
            if not enabled:
                continue
            try:
                create_shortcut(special_folder(folder) / SHORTCUT_NAME, target,
                                arguments="-m winvoice", workdir=workdir,
                                icon_path=target, description="WinVoice 语音助手")
                created += 1
            except Exception as error:  # noqa: BLE001 - collect and show all
                errors.append(f"{folder}: {error}")
        # Unticking autostart removes an existing startup shortcut, so the
        # checkbox is the single source of truth.
        if not self._autostart.get():
            try:
                (special_folder(FOLDER_STARTUP) / SHORTCUT_NAME).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        self.state.autostart = self._autostart.get()
        if errors:
            self._status.configure(text="；".join(errors), foreground="#b3261e")
        else:
            self._status.configure(
                text=(f"✓ 已创建 {created} 个快捷方式。" if created
                      else "未选择任何快捷方式。"),
                foreground="#0a7d32" if created else "#333333")

    def _launch(self) -> None:
        flow.launch_assistant(self.state.install_dir)


PAGE_CLASSES = [
    WelcomePage,
    InstallDirPage,
    NetworkPage,
    ExtractPage,
    ModelsPage,
    DevicesPage,
    ConfigPage,
    DshPage,
    EnrollPage,
    CheckPage,
    FinishPage,
]
