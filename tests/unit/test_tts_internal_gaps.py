"""
Internal-gap collapse: the model's own mid-sentence holes, shortened.

The measurement that made this necessary (gap analysis on real Matcha
synthesis, 2026-09-26): one `generate()` carries 1–7 *internal* silences of
40–240 ms, placed wherever the model pleases — its lexicon holds no
punctuation, so it cannot see the comma it is pausing for. Heard alongside the
pause table's rhythm, those holes are what the listener reports as
「卡顿、一句话很多断点」.

The rules pinned here:

* only gaps **longer** than `max_internal_gap_ms` (110 ms) are touched — the
  40–80 ms runs are the voice's natural phrasing (`trim_edge_silence`'s
  docstring already promised those would stay);
* a collapsed gap keeps its first 80 ms — a pause does not disappear, it
  shrinks to something a comma would earn;
* edges are never touched here: they are the trimmer's business.
"""

from __future__ import annotations

import numpy as np

from winvoice.audio._common import collapse_internal_gaps
from winvoice.text import DEFAULT_CONFIG

SR = 16000  # exact 10 ms windows: the detector is grid-quantised by design


def _segment(
    speech_ms: int = 300,
    gap_ms: int = 0,
    *,
    speech_amplitude: float = 0.3,
    gap_amplitude: float = 0.002,
) -> np.ndarray:
    """
    speech | gap | speech — a stand-in for one synthesis call's middle.

    The gap is a noise floor (like the real model's), not digital zeros, so the
    10 ms envelope sees it the way it sees real silence.
    """
    rng = np.random.default_rng(1234)
    total = 2 * speech_ms + gap_ms
    n = int(SR * total / 1000)
    signal = rng.normal(0.0, gap_amplitude, n).astype(np.float32)

    def tone(start_ms: int, length_ms: int) -> None:
        start = int(SR * start_ms / 1000)
        length = int(SR * length_ms / 1000)
        t = np.arange(length, dtype=np.float32) / SR
        signal[start : start + length] = speech_amplitude * np.sin(2 * np.pi * 220.0 * t)

    tone(0, speech_ms)
    tone(speech_ms + gap_ms, speech_ms)
    return signal


def _ms(samples: np.ndarray) -> float:
    return samples.size / SR * 1000


def test_a_long_internal_gap_is_shortened_to_the_keep_length() -> None:
    raw = _segment(speech_ms=300, gap_ms=180)

    collapsed = collapse_internal_gaps(raw, SR, max_gap_ms=110, keep_ms=80)

    # 780 ms in (300 + 180 + 300), 100 ms of the hole gone: 680 out.
    assert _ms(raw) - _ms(collapsed) == 100
    assert 670 <= _ms(collapsed) <= 690


def test_a_short_internal_gap_is_the_voice_s_own_phrasing_and_stays() -> None:
    raw = _segment(speech_ms=300, gap_ms=60)

    assert _ms(collapse_internal_gaps(raw, SR)) == _ms(raw)


def test_the_default_threshold_keeps_natural_pauses_and_cuts_word_holes() -> None:
    """≤60 ms runs are phrasing and stop closures; holes up to 240 ms get cut."""
    assert DEFAULT_CONFIG.max_internal_gap_ms == 60
    assert DEFAULT_CONFIG.internal_gap_keep_ms == 30

    for gap_ms in (40, 60):
        raw = _segment(gap_ms=gap_ms)
        assert _ms(collapse_internal_gaps(raw, SR)) == _ms(raw)


def test_several_gaps_are_collapsed_in_one_pass() -> None:
    raw = np.concatenate([_segment(speech_ms=200, gap_ms=200) for _ in range(2)])

    collapsed = collapse_internal_gaps(raw, SR, max_gap_ms=110, keep_ms=80)

    # Two 200 ms holes, each minus 120 ms.
    assert _ms(raw) - _ms(collapsed) == 240


def test_the_kept_audio_is_slices_of_the_original() -> None:
    """The result is the original with spans removed, not a re-render."""
    raw = _segment(speech_ms=300, gap_ms=200)
    collapsed = collapse_internal_gaps(raw, SR, max_gap_ms=110, keep_ms=80)

    head = collapsed[: int(SR * 0.3)].tobytes()
    tail = collapsed[-int(SR * 0.3) :].tobytes()

    assert raw.tobytes().find(head) == 0, "the first kept span is not the original onset"
    assert raw.tobytes().find(tail) != -1, "the second kept span is not a slice of the original"


def test_the_edges_are_never_touched() -> None:
    """A hole at the very start/end belongs to `trim_edge_silence`, not here."""
    # A segment that *starts* with 300 ms of floor: no internal voiced window
    # before it, so the floor is not an internal gap and must survive.
    rng = np.random.default_rng(7)
    lead = rng.normal(0.0, 0.002, int(SR * 0.3)).astype(np.float32)
    speech = (0.3 * np.sin(2 * np.pi * 220 * np.arange(int(SR * 0.3)) / SR)).astype(np.float32)
    raw = np.concatenate([lead, speech])

    assert _ms(collapse_internal_gaps(raw, SR)) == _ms(raw)


def test_zero_disables_the_collapse() -> None:
    raw = _segment(speech_ms=300, gap_ms=240)

    assert _ms(collapse_internal_gaps(raw, SR, max_gap_ms=0)) == _ms(raw)


def test_degenerate_inputs_are_passed_through() -> None:
    empty = np.zeros(0, dtype=np.float32)
    silence = np.zeros(SR // 2, dtype=np.float32)
    quiet = np.full(SR // 2, 0.001, dtype=np.float32)
    tiny = _segment(speech_ms=5)

    assert collapse_internal_gaps(empty, SR).size == 0
    assert collapse_internal_gaps(silence, SR).size == silence.size
    assert collapse_internal_gaps(quiet, SR).size == quiet.size
    assert collapse_internal_gaps(tiny, SR).size == tiny.size
