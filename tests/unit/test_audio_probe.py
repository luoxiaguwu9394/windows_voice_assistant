"""
The wizard's play-test probe must feed the device stream buffers shaped like
the real player's.

The device stream is opened with 2 channels (`playback.OUT_CHANNELS` — mono
endpoints are what ruined the audio on three machines, see UNIMPLEMENTED.md
§0), and PortAudio rejects a flat mono array on a multi-channel stream with
"number of channels must match". v0.1.5 shipped exactly that breakage: the
chime went out flat, so every 「播放测试音」 click failed before a single
sample reached the device. These tests load `scripts/audio_probe.py` the way
the wizard runs it (against the real playback module) and check the buffers
it writes — no audio hardware involved.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

import winvoice.audio.playback as playback

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PROBE_PATH = PROJECT_ROOT / "scripts" / "audio_probe.py"


def _load_probe():
    # Unique module name: scripts/ has no package __init__, and a generic name
    # like "audio_probe" could shadow (or be shadowed by) another importer.
    spec = importlib.util.spec_from_file_location(
        "winvoice_audio_probe_under_test", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _FakeStream:
    """Stands in for the opened sd.OutputStream; records what was written."""

    def __init__(self, channels: int) -> None:
        self.channels = channels
        self.writes: List[np.ndarray] = []

    def write(self, data: Any) -> None:
        self.writes.append(np.asarray(data))


def _patch_open(monkeypatch, channels: int) -> _FakeStream:
    probe = _load_probe()
    stream = _FakeStream(channels)

    def fake_open(rate, blocksize_ms, device=None, host_api="auto"):
        return stream, 48000

    # play_test imports open_output_stream from the playback module at call
    # time, so patching the module attribute is what it will see.
    monkeypatch.setattr(playback, "open_output_stream", fake_open)
    return stream


def test_play_test_writes_two_dimensional_buffers_to_the_stereo_stream(monkeypatch):
    stream = _patch_open(monkeypatch, playback.OUT_CHANNELS)

    result = _load_probe().play_test("default", "auto", 48000)

    assert result["ok"] is True, result
    assert stream.writes, "the chime never reached the stream"
    for chunk in stream.writes:
        assert chunk.ndim == 2, "a flat array makes PortAudio refuse the write"
        assert chunk.shape[1] == playback.OUT_CHANNELS
    total = sum(chunk.shape[0] for chunk in stream.writes)
    assert total == int(48000 * 1.6), "channel replication must not stretch time"


def test_play_test_replicates_the_mono_chime_to_every_channel(monkeypatch):
    stream = _patch_open(monkeypatch, playback.OUT_CHANNELS)
    mono = _load_probe()._chime(48000)

    result = _load_probe().play_test("default", "auto", 48000)

    assert result["ok"] is True
    stereo = np.concatenate(stream.writes, axis=0)
    assert stereo.shape == (mono.size, playback.OUT_CHANNELS)
    assert np.array_equal(stereo[:, 0], mono), "left channel is not the chime"
    assert np.array_equal(stereo[:, 1], mono), "right channel is not the chime"


def test_play_test_keeps_flat_writes_for_a_single_channel_stream(monkeypatch):
    # A mono stream accepts flat arrays; the probe must not reshape for it
    # (mirrors the player's out_channels=1 byte-compat rule).
    stream = _patch_open(monkeypatch, 1)

    result = _load_probe().play_test("default", "auto", 48000)

    assert result["ok"] is True
    assert stream.writes
    for chunk in stream.writes:
        assert chunk.ndim == 1
