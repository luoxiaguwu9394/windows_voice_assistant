#!/usr/bin/env python3
"""
Measure a TTS model's own silences, so the pause table can be set from data.

Run it after downloading or switching a TTS model:

    python scripts/calibrate_tts_pauses.py
    python scripts/calibrate_tts_pauses.py --model models/tts/matcha-icefall-zh-baker
    python scripts/calibrate_tts_pauses.py --json > calibration.json

What it reports, and why each number exists:

| Column | Why it matters |
|---|---|
| `ms/char` | how long a reply takes to say (a 240-character answer at 250 ms/char is a minute of talking) |
| `floor` | the model's noise floor at the edges, as a % of peak — **not** digital silence, which is why a per-sample threshold measures nothing |
| `lead` / `tail` | how much dead air a synthesis call carries at each end, at 1 / 1.5 / 2 / 3 % of peak |
| `internal` | gaps inside one sentence. They are the model's own phrasing and are *not* trimmed |
| `speed` | what `tts.speed` buys, for when the voice reads too slowly |
| `pause check` | the pause the listener would actually hear between two clauses: trimmed tail + the table's pause + trimmed lead |

The suggestions it prints are deliberately **not written to the config**: applying
them means editing `config/config.yaml` by hand, because the config is written
back with `yaml.safe_dump`, which would delete every comment in the file — and
those comments are where the reasoning for the current numbers lives.

This script needs the real model and no audio device: it reads samples, never
plays them.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from winvoice.audio._common import trim_edge_silence  # noqa: E402
from winvoice.audio.tts import (  # noqa: E402
    build_offline_tts,
    resolve_tts_model,
)
from winvoice.config import get_config  # noqa: E402
from winvoice.text import DEFAULT_CONFIG, segment_for_speech  # noqa: E402

# Probe sentences shaped like what the assistant actually says: a query answer, a
# short confirmation, a long comma-heavy sentence, and a two-clause pair whose
# pause is then verified end to end.
PROBES: List[str] = [
    "现在是下午3点25分。",
    "已经打开记事本了。",
    "北京今天晴，气温10到20度。",
    "我完成了操作，但是结果说不清楚，请看屏幕。",
    "我先把桌面上那份重要的文件保存好，接下来我会把处理结果告诉你，最后再帮你检查一遍所有的设置。",
]

CLAUSE_PAIR = ("我先把文件保存好了，", "接下来告诉你结果。")

EDGE_RATIOS = (0.01, 0.015, 0.02, 0.03)
WINDOW_MS = 10


def _windows(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    window = max(1, int(sample_rate * WINDOW_MS / 1000))
    count = samples.size // window
    if count == 0:
        return np.zeros(0)
    return np.sqrt((samples[: count * window].reshape(count, window) ** 2).mean(axis=1))


def _edge_ms(envelope: np.ndarray, threshold: float) -> tuple[int, int]:
    voiced = np.flatnonzero(envelope >= threshold)
    if voiced.size == 0:
        return envelope.size * WINDOW_MS, 0
    return int(voiced[0]) * WINDOW_MS, (envelope.size - int(voiced[-1]) - 1) * WINDOW_MS


def measure(tts: Any, text: str, sample_rate: int) -> Dict[str, Any]:
    audio = tts.generate(text, sid=0, speed=1.0)
    samples = np.asarray(audio.samples, dtype=np.float32)
    rate = int(getattr(audio, "sample_rate", 0) or sample_rate)
    envelope = _windows(samples, rate)
    peak = float(np.abs(samples).max()) if samples.size else 0.0
    floor = float(np.percentile(envelope, 10)) / peak if peak and envelope.size else 0.0

    edges = {ratio: _edge_ms(envelope, ratio * peak) for ratio in EDGE_RATIOS} if peak else {}

    # Gaps inside the sentence: the model's own phrasing.
    internal: List[tuple[int, int]] = []
    voiced = envelope >= 0.03 * peak if peak else np.zeros(0, dtype=bool)
    if voiced.any():
        first, last = int(np.flatnonzero(voiced)[0]), int(np.flatnonzero(voiced)[-1])
        index = first
        while index <= last:
            if not voiced[index]:
                start = index
                while index <= last and not voiced[index]:
                    index += 1
                if (index - start) * WINDOW_MS >= 40:
                    internal.append((start * WINDOW_MS, (index - start) * WINDOW_MS))
            else:
                index += 1

    total_ms = int(samples.size / rate * 1000) if rate else 0
    trimmed = (
        trim_edge_silence(samples, rate, ratio=DEFAULT_CONFIG.trim_ratio,
                          guard_ms=DEFAULT_CONFIG.trim_guard_ms)
        if peak
        else samples
    )
    return {
        "text": text,
        "chars": len(text),
        "sample_rate": rate,
        "total_ms": total_ms,
        "ms_per_char": round(total_ms / max(1, len(text)), 1),
        "peak": round(peak, 4),
        "floor_pct": round(floor * 100, 2),
        "onset_pct": round(float(envelope.max()) / peak * 100, 1) if peak and envelope.size else 0.0,
        "edges": {f"{ratio:.3f}": edges.get(ratio, (0, 0)) for ratio in EDGE_RATIOS},
        "internal_gaps": internal,
        "trimmed_ms": int(trimmed.size / rate * 1000) if rate else 0,
    }


def _print_row(row: Dict[str, Any]) -> None:
    edges = row["edges"]
    print(
        f"  chars={row['chars']:>3} total={row['total_ms']:>6}ms "
        f"({row['ms_per_char']:>6} ms/char) floor={row['floor_pct']:>5}% "
        f"onset={row['onset_pct']:>5}%"
    )
    print(
        "        edge silence  "
        + "  ".join(
            f"{ratio}%: {edges[ratio][0]:>4}/{edges[ratio][1]:<4}"
            for ratio in (f"{value:.3f}" for value in EDGE_RATIOS)
        )
        + "   (lead/tail ms)"
    )
    if row["internal_gaps"]:
        gaps = ", ".join(f"{start}ms+{length}" for start, length in row["internal_gaps"])
        print(f"        internal gaps: {gaps}")
    print(f"        trimmed at the default ratio: {row['trimmed_ms']}ms "
          f"(removed {row['total_ms'] - row['trimmed_ms']}ms)")


def calibrate(args: argparse.Namespace) -> int:
    cfg = get_config(str(REPO_ROOT / "config" / "config.yaml"))
    model = resolve_tts_model(
        args.model or cfg.get("tts.model"),
        [] if args.no_fallback else (cfg.get("tts.fallback_models") or None),
        backend=args.backend or cfg.get("tts.backend", "auto"),
        vocoder=cfg.get("tts.vocoder", ""),
    )
    tts = build_offline_tts(model, num_threads=args.num_threads)
    sample_rate = int(getattr(tts, "sample_rate", 0) or 0)

    rows = [measure(tts, text, sample_rate) for text in PROBES]

    # The pause the listener actually hears: trimmed tail + table pause + lead.
    first_clause = measure(tts, CLAUSE_PAIR[0], sample_rate)
    second_clause = measure(tts, CLAUSE_PAIR[1], sample_rate)
    config = DEFAULT_CONFIG
    tail_residue = config.trim_guard_ms
    lead_residue = config.trim_guard_ms
    heard_pause = tail_residue + config.pause_comma_ms + lead_residue

    speeds = {}
    for speed in (1.0, 1.15, 1.3):
        audio = tts.generate(PROBES[0], sid=0, speed=speed)
        rate = int(getattr(audio, "sample_rate", 0) or sample_rate)
        speeds[speed] = int(np.asarray(audio.samples).size / rate * 1000)

    if args.json:
        print(
            json.dumps(
                {
                    "model": str(model.model_file),
                    "backend": model.backend,
                    "sample_rate": sample_rate,
                    "rows": rows,
                    "speed_ms": speeds,
                    "pause_check": {
                        "tail_residue_ms": tail_residue,
                        "table_pause_ms": config.pause_comma_ms,
                        "lead_residue_ms": lead_residue,
                        "audible_pause_ms": heard_pause,
                        "clause_pair": [first_clause["chars"], second_clause["chars"]],
                    },
                    "suggested": {
                        "trim_ratio": round(max(rows[0]["floor_pct"] / 100 * 1.2, 0.005), 4),
                        "trim_guard_ms": config.trim_guard_ms,
                        "pause_comma_ms": config.pause_comma_ms,
                        "pause_sentence_ms": config.pause_sentence_ms,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print("=" * 74)
    print(f"model      : {model.model_file}")
    print(f"backend    : {model.backend}   sample_rate: {sample_rate} Hz")
    print("=" * 74)
    for row in rows:
        _print_row(row)
    print("-" * 74)

    print("speed effect (first probe):")
    for speed, total in speeds.items():
        base = speeds[1.0] or 1
        print(f"  speed={speed:<4} {total:>6}ms   ({total / base * 100:5.1f}% of speed 1.0)")

    print("-" * 74)
    print("pause check (two clauses, trimmed, one table pause between them):")
    print(f"  clause 1: {first_clause['chars']} chars, {first_clause['total_ms']}ms raw "
          f"→ {first_clause['trimmed_ms']}ms trimmed")
    print(f"  clause 2: {second_clause['chars']} chars, {second_clause['total_ms']}ms raw "
          f"→ {second_clause['trimmed_ms']}ms trimmed")
    print(f"  audible pause = tail residue {tail_residue} + table {config.pause_comma_ms} "
          f"+ lead residue {lead_residue} = {heard_pause}ms")
    print("  (a listener should hear a comma-length pause here, not 300–400ms of dead air)")

    floor = rows[0]["floor_pct"]
    onset = rows[0]["onset_pct"]
    suggested_ratio = round(max(floor / 100 * 1.2, 0.005), 4)
    print("-" * 74)
    print("suggested values (edit config/config.yaml by hand — this script never")
    print("rewrites it, because a YAML round-trip would delete its comments):")
    print(f"  tts.trim_ratio: {suggested_ratio}   "
          f"# above the {floor}% floor, well below the {onset}% onset")
    print(f"  tts.trim_guard_ms: {config.trim_guard_ms}")
    print(f"  tts.pause_comma_ms: {config.pause_comma_ms}")
    print(f"  tts.pause_sentence_ms: {config.pause_sentence_ms}")

    natural = [length for row in rows for _, length in row["internal_gaps"]]
    if natural:
        print(
            f"  measured natural pauses inside sentences: {min(natural)}–{max(natural)}ms "
            f"(a comma pause of {config.pause_comma_ms}ms should sit in or just above this band)"
        )
    print("-" * 74)
    print("segmentation of the longest probe (what actually gets synthesised):")
    for index, segment in enumerate(segment_for_speech(PROBES[-1], config)):
        print(f"  {index}: {segment.chars:>3} chars  pause {segment.pause_after_ms:>4}ms  "
              f"[{segment.kind}]  {segment.text}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure a TTS model's silences and check the pause table",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--model", help="model directory (default: tts.model from config)")
    parser.add_argument("--backend", help="force vits | matcha")
    parser.add_argument(
        "--no-fallback", action="store_true", help="do not fall back to tts.fallback_models"
    )
    parser.add_argument("--num-threads", type=int, default=2)
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # pragma: no cover
        pass
    args = build_parser().parse_args()
    try:
        return calibrate(args)
    except Exception as error:  # pragma: no cover - CLI surface
        print(f"[X] calibration failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
