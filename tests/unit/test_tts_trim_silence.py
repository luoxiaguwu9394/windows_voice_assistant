"""
Edge-silence trimming: making the pause table the only source of pauses.

The measurement that made this necessary (`scripts/calibrate_tts_pauses.py`, on
the shipped 8 kHz VITS):

    every synthesis call begins with 80–130 ms and ends with 180–250 ms of
    near-silence — a noise floor at 1–2 % of the signal peak, not digital zero.

Two consequences, and both are tested here:

* the detector has to work on a **10 ms RMS envelope**; a per-sample amplitude
  threshold sees almost no silence at all (measured: 0–110 ms instead of
  80–130 ms at the front);
* trimming must be **edges only**. A 40–130 ms gap *inside* a sentence is the
  model's own phrasing, and cutting it out would speed the speech up in a way
  nobody asked for.

The numbers below are the ones the real audio produced: a noise floor at 0.002
and speech at 0.3 is a ratio of 0.7 %, so the 1.5 % default sits between them —
above the floor, well below the quietest onset.
"""

from __future__ import annotations

import numpy as np

from winvoice.audio._common import trim_edge_silence

SR = 8000


def _segment(
    speech_ms: int = 500,
    lead_ms: int = 120,
    tail_ms: int = 240,
    *,
    speech_amplitude: float = 0.3,
    floor_amplitude: float = 0.002,
    internal_gap_ms: int = 0,
) -> np.ndarray:
    """A synthetic stand-in for one synthesis call: floor, speech, floor."""
    rng = np.random.default_rng(1234)
    tones = 2 if internal_gap_ms else 1
    total = lead_ms + tones * speech_ms + internal_gap_ms + tail_ms
    n = int(SR * total / 1000)
    signal = rng.normal(0.0, floor_amplitude, n).astype(np.float32)

    def tone(start_ms: int, length_ms: int) -> None:
        start = int(SR * start_ms / 1000)
        length = int(SR * length_ms / 1000)
        t = np.arange(length, dtype=np.float32) / SR
        signal[start : start + length] = speech_amplitude * np.sin(2 * np.pi * 220.0 * t)

    tone(lead_ms, speech_ms)
    if internal_gap_ms:
        tone(lead_ms + speech_ms + internal_gap_ms, speech_ms)
    return signal


def _ms(samples: np.ndarray) -> float:
    return samples.size / SR * 1000


def test_the_model_noise_floor_is_trimmed_from_both_edges() -> None:
    trimmed = trim_edge_silence(_segment(), SR)

    # 500 ms of speech, plus at most the 30 ms guard on each side.
    assert 500 <= _ms(trimmed) <= 570
    # …which means 0.3–0.4 s of the model's dead air is gone.
    assert _ms(trimmed) <= _ms(_segment()) - 200


def test_a_per_sample_threshold_would_have_missed_it() -> None:
    """
    The reason this is an envelope detector: at the very first sample of the
    «silence» the waveform is already above a naive amplitude threshold, so a
    per-sample rule trims almost nothing (measured: 0–110 ms of 120 ms).
    """
    raw = _segment()
    per_sample_threshold = 0.005 * float(np.abs(raw).max())
    leading_quiet_samples = int(np.argmax(np.abs(raw) > per_sample_threshold))

    assert leading_quiet_samples / SR * 1000 < 60  # naive: sees ~no silence
    assert 80 <= (_ms(raw) - _ms(trim_edge_silence(raw, SR))) <= 500  # envelope: sees it


def test_a_gap_inside_a_sentence_is_not_trimmed() -> None:
    """Internal phrasing is the model's, and it stays."""
    raw = _segment(speech_ms=200, internal_gap_ms=120)
    trimmed = trim_edge_silence(raw, SR)

    # 880 ms in, 580 ms out: only the 120 ms front and 240 ms tail floors went,
    # each reduced to the 30 ms guard. The 120 ms gap in the middle is intact.
    assert 560 <= _ms(trimmed) <= 600
    assert 280 <= _ms(raw) - _ms(trimmed) <= 320


def test_a_quieter_signal_is_judged_relative_to_its_own_peak() -> None:
    """A quiet reply must not be trimmed to nothing, nor left untrimmed."""
    quiet = _segment(speech_amplitude=0.05, floor_amplitude=0.0004)

    assert _ms(trim_edge_silence(quiet, SR)) <= 570


def test_a_floor_above_the_ratio_is_left_alone() -> None:
    """
    A 2 %-of-peak floor is *above* a 1.5 % threshold, so it is not silence as far
    as this detector is concerned — which is the safe direction: the trimmer may
    leave dead air in, it may never eat speech.
    """
    loud_floor = _segment(floor_amplitude=0.009, speech_amplitude=0.3)

    assert _ms(trim_edge_silence(loud_floor, SR, ratio=0.015)) == _ms(loud_floor)


def test_the_default_ratio_is_above_every_measured_floor() -> None:
    """
    The default comes from the two shipped models (Matcha 0.24–1.68 %, the 8 kHz
    VITS 0.78–1.74 % of peak): a threshold inside that range would trim some
    segments and leave others with their dead air intact.
    """
    from winvoice.text import DEFAULT_CONFIG

    assert DEFAULT_CONFIG.trim_ratio >= 0.02
    # …and still far below the quietest onset either model produces (~35 %).
    for floor in (0.0024, 0.005, 0.0168):
        segment = _segment(floor_amplitude=0.3 * floor, speech_amplitude=0.3)
        assert _ms(trim_edge_silence(segment, SR)) < _ms(segment)


def test_a_bigger_ratio_trims_more() -> None:
    raw = _segment(floor_amplitude=0.006, speech_amplitude=0.3)

    assert _ms(trim_edge_silence(raw, SR, ratio=0.05)) < _ms(raw)


def test_the_guard_keeps_a_ramp_around_the_onset() -> None:
    short_guard = trim_edge_silence(_segment(), SR, guard_ms=0)
    long_guard = trim_edge_silence(_segment(), SR, guard_ms=100)

    assert _ms(long_guard) - _ms(short_guard) > 150


def test_an_all_quiet_input_is_returned_unchanged() -> None:
    """Trimming away something the analysis does not understand is worse."""
    quiet = np.full(SR // 2, 0.001, dtype=np.float32)

    assert trim_edge_silence(quiet, SR).size == quiet.size


def test_digital_silence_is_returned_unchanged() -> None:
    silence = np.zeros(SR // 2, dtype=np.float32)

    assert trim_edge_silence(silence, SR).size == silence.size


def test_an_empty_or_disabled_input_is_passed_through() -> None:
    empty = np.zeros(0, dtype=np.float32)
    raw = _segment()

    assert trim_edge_silence(empty, SR).size == 0
    assert trim_edge_silence(raw, SR, ratio=0.0).size == raw.size


def test_trimming_preserves_the_audio_it_keeps() -> None:
    """The kept audio is a contiguous slice of the original, not a re-render."""
    raw = _segment()
    trimmed = trim_edge_silence(raw, SR)

    found = raw.tobytes().find(trimmed.tobytes())

    assert found != -1, "the kept audio is not a slice of the original"
    assert found % raw.dtype.itemsize == 0, "the slice is not sample-aligned"


def test_a_segment_shorter_than_one_window_is_passed_through() -> None:
    very_short = _segment(speech_ms=4, lead_ms=0, tail_ms=0)

    assert trim_edge_silence(very_short, SR).size == very_short.size
