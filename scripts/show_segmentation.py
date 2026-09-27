#!/usr/bin/env python3
"""
Show how a reply will be spoken: normalization, cut points, and pauses.

This exists because the two decisions that decide how the assistant *sounds* are
made by pure functions (`winvoice/text/`) and can be inspected without loading a
single model — no microphone, no synthesis, no listening:

    python scripts/show_segmentation.py "我已经把文件保存好了，接下来告诉你结果。"
    python scripts/show_segmentation.py --stdin < reply.txt
    python scripts/show_segmentation.py --json "好的。"     # for diffing rules

Measured context for the numbers it prints (see `scripts/calibrate_tts_pauses.py`
for the instrument that produced them, on the shipped 8 kHz model):

* the TTS lexicon holds no punctuation at all and sherpa-onnx's `silence_scale`
  changed nothing, so **every pause below is written by the playback layer**, not
  by the model;
* speech runs at 176–239 ms per character with the default 22 kHz voice (the 8 kHz
  fallback is 262–315) — the `est. audio` column is that estimate, and it is what
  turns a 240-character reply into 45–60 seconds of talking;
* each synthesis call also carries 80–250 ms of its own near-silence at both
  ends, which the engine trims before the pause in this table is inserted.

Usage notes: run it from the repo root (or pass `--config`), and remember the
pause table is read from `config/config.yaml` — editing `tts.pause_*` there and
re-running this script is the intended way to tune prosody.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parents[1]
# Same bootstrap as the other scripts: running `python scripts/x.py` puts
# `scripts/` on sys.path, not the repo root, so `import winvoice` would fail.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Speech rate at speed 1.0, measured with `scripts/calibrate_tts_pauses.py`:
# Matcha (the default) runs at 176–239 ms/char, the 8 kHz VITS fallback at
# 262–315. Only used for the "est. audio" column — and it is the reason the reply
# budget is a duration decision: 240 characters is 45–60 seconds of talking.
DEFAULT_MS_PER_CHAR = 200.0


def _load_speech_config(config_path: Path):
    from winvoice.config import get_config, reset_config
    from winvoice.text import speech_text_config

    reset_config()
    return speech_text_config(get_config(config_path))


def _normalize(text: str, config, max_chars: int) -> str:
    from winvoice.text import normalize_for_speech

    return normalize_for_speech(text, max_chars=max_chars)


def _pause_table(config) -> List[tuple[str, int]]:
    return [
        ("。", config.pause_sentence_ms),
        ("？", config.pause_question_ms),
        ("！", config.pause_exclaim_ms),
        ("……", config.pause_ellipsis_ms),
        ("；", config.pause_semicolon_ms),
        ("，", config.pause_comma_ms),
        ("、", config.pause_enumeration_ms),
        ("：", config.pause_colon_ms),
        ("换行", config.pause_paragraph_ms),
        ("连词前", config.pause_conjunction_ms),
        ("强制切分", config.pause_forced_ms),
        ("整段结束", config.tail_silence_ms),
    ]


def _report(text: str, args) -> int:
    from winvoice.text import segment_for_speech

    config = _load_speech_config(Path(args.config).resolve())
    max_chars = args.max_chars
    normalized = _normalize(text, config, max_chars)
    segments = segment_for_speech(normalized, config)

    total_chars = sum(segment.chars for segment in segments)
    total_pause = sum(segment.pause_after_ms for segment in segments)
    total_audio = total_chars * args.ms_per_char + total_pause

    if args.json:
        print(
            json.dumps(
                {
                    "raw": text,
                    "normalized": normalized,
                    "segments": [
                        {
                            "index": index,
                            "chars": segment.chars,
                            "kind": segment.kind,
                            "pause_after_ms": segment.pause_after_ms,
                            "text": segment.text,
                        }
                        for index, segment in enumerate(segments)
                    ],
                    "totals": {
                        "segments": len(segments),
                        "chars": total_chars,
                        "pause_ms": total_pause,
                        "estimated_audio_ms": int(total_audio),
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    print("=" * 72)
    print("raw        :", text.replace("\n", "\\n"))
    print("normalized :", normalized.replace("\n", "\\n"))
    print("=" * 72)
    print(
        f"budget={max_chars}  segments={len(segments)}  chars={total_chars}  "
        f"pause_total={total_pause} ms  est_audio={int(total_audio)} ms"
    )
    print("-" * 72)
    print(f"{'#':>2}  {'chars':>5}  {'kind':<9} {'pause':>6}  text")
    for index, segment in enumerate(segments):
        print(
            f"{index:>2}  {segment.chars:>5}  {segment.kind:<9} "
            f"{segment.pause_after_ms:>5}ms  {segment.text}"
        )
    print("-" * 72)

    if args.table:
        print("pause table (config/config.yaml → tts.pause_*):")
        for mark, value in _pause_table(config):
            print(f"  {mark:<8} {value:>4} ms")

    if not segments:
        print("nothing pronounceable survives — the caller says an honest fallback")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Show normalization, cut points and pauses for a reply",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python scripts/show_segmentation.py "好的，我马上办。"\n'
            "  python scripts/show_segmentation.py --table --stdin < reply.txt\n"
            "  python scripts/show_segmentation.py --json \"好的。\"\n"
        ),
    )
    parser.add_argument("text", nargs="?", help="reply text (or use --stdin)")
    parser.add_argument("--stdin", action="store_true", help="read the reply from stdin")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--table", action="store_true", help="also print the pause table in force"
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=240,
        help="speech budget: 240 for an agent reply, 80 for a tool one-liner",
    )
    parser.add_argument(
        "--ms-per-char",
        type=float,
        default=DEFAULT_MS_PER_CHAR,
        help=f"speech-rate estimate for the duration column (default {DEFAULT_MS_PER_CHAR:.0f})",
    )
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "config" / "config.yaml"),
        help="config file the pause table is read from",
    )
    return parser


def main() -> int:
    try:  # a cp936 console cannot encode every mark; degrade instead of dying
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # pragma: no cover - older/odd stdout objects
        pass

    args = build_parser().parse_args()
    text = sys.stdin.read() if args.stdin else (args.text or "")
    if not text.strip():
        print("nothing to segment: pass text or --stdin", file=sys.stderr)
        return 2
    return _report(text, args)


if __name__ == "__main__":
    sys.exit(main())
