"""
Unit tests for winvoice.tools.appdiscovery — the settings UI's scanner.

The enumerators are injected, so these tests never touch the real Start Menu
or registry; the default collectors are thin on purpose and get one test each
that only checks they *exist and swallow errors*, not what this machine has
installed.
"""

from __future__ import annotations

from winvoice.tools import appdiscovery


def test_shortcut_candidates_are_offered_with_their_labels(tmp_path):
    exe = tmp_path / "Weixin.exe"
    exe.write_bytes(b"MZ")

    result = appdiscovery.scan_installed_apps(
        shortcuts=lambda: [("微信", str(exe))],
        app_paths=lambda: [],
    )

    assert result == [{"label": "微信", "path": str(exe), "image": "Weixin.exe"}]


def test_app_paths_candidates_use_the_registered_stem_as_label(tmp_path):
    exe = tmp_path / "Code.exe"
    exe.write_bytes(b"MZ")

    result = appdiscovery.scan_installed_apps(
        shortcuts=lambda: [],
        app_paths=lambda: [("Code.exe", str(exe))],
    )

    assert result[0]["label"] == "Code"
    assert result[0]["image"] == "Code.exe"


def test_duplicates_by_path_are_merged(tmp_path):
    exe = tmp_path / "App.exe"
    exe.write_bytes(b"MZ")

    result = appdiscovery.scan_installed_apps(
        shortcuts=lambda: [("应用", str(exe)), ("App", str(exe).upper())],
        app_paths=lambda: [("App.exe", str(exe))],
    )

    assert len(result) == 1


def test_uninstallers_and_missing_targets_are_filtered(tmp_path):
    uninstaller = tmp_path / "unins000.exe"
    uninstaller.write_bytes(b"MZ")
    real = tmp_path / "WeChat.exe"
    real.write_bytes(b"MZ")

    result = appdiscovery.scan_installed_apps(
        shortcuts=lambda: [
            ("卸载", str(uninstaller)),
            ("微信", str(tmp_path / "gone.exe")),  # vanished install
            ("微信", str(real)),
        ],
        app_paths=lambda: [],
    )

    assert result == [{"label": "微信", "path": str(real), "image": "WeChat.exe"}]


def test_relative_and_non_exe_targets_are_filtered(tmp_path):
    result = appdiscovery.scan_installed_apps(
        shortcuts=lambda: [
            ("相对", "Weixin.exe"),
            ("文档", str(tmp_path / "readme.txt")),
        ],
        app_paths=lambda: [],
    )

    assert result == []


def test_an_exploding_enumerator_is_swallowed():
    def boom():
        raise RuntimeError("shell unavailable")

    assert appdiscovery.scan_installed_apps(shortcuts=boom, app_paths=lambda: []) == []


def test_results_are_sorted_by_label(tmp_path):
    a = tmp_path / "A.exe"
    b = tmp_path / "B.exe"
    for exe in (a, b):
        exe.write_bytes(b"MZ")

    result = appdiscovery.scan_installed_apps(
        shortcuts=lambda: [("乙工具", str(b)), ("甲工具", str(a))],
        app_paths=lambda: [],
    )

    assert [entry["label"] for entry in result] == ["乙工具", "甲工具"]
