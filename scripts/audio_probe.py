#!/usr/bin/env python3
"""
Audio device probe for the WinVoice setup wizard.

Runs on the target machine with the bundled runtime interpreter, so the wizard
tests the exact stack the assistant will use (sounddevice/PortAudio + the
project's own playback route logic) rather than a parallel implementation.

Subcommands
-----------
    list                       device inventory, JSON
    play-test [options]        play a short chime through the real route logic
    record-level [options]     record N seconds from a microphone, report levels

Output contract: every run prints exactly one final line on stdout,

    RESULT {json}

(ASCII-escaped, so a GBK console cannot mangle it). The wizard parses that
prefix; anything else on the stream is human-readable diagnostics. Exit code
0 = the probe ran to completion (the JSON carries ok true/false), 2 = usage
error.

This script deliberately does NOT read config/config.yaml: the wizard probes
devices before it has generated the final config, and every parameter the app
would take from config arrives on the command line instead.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Dict, List, Optional

import numpy as np

try:
    import sounddevice as sd
except Exception as error:  # pragma: no cover - no audio stack at all
    print(f"RESULT {json.dumps({'ok': False, 'error': f'sounddevice: {error}'})}")
    raise SystemExit(1)


# ──────────────────────────────────────────────────────────────
# Inventory
# ──────────────────────────────────────────────────────────────

def device_inventory() -> Dict[str, Any]:
    """Everything the wizard needs to render device dropdowns."""
    hostapis = []
    for index, api in enumerate(sd.query_hostapis()):
        hostapis.append(
            {
                "index": index,
                "name": str(api.get("name", "")),
                "default_input_device": int(api.get("default_input_device", -1)),
                "default_output_device": int(api.get("default_output_device", -1)),
            }
        )
    api_names = {api["index"]: api["name"] for api in hostapis}

    inputs: List[Dict[str, Any]] = []
    outputs: List[Dict[str, Any]] = []
    default_input = sd.default.device[0]
    default_output = sd.default.device[1]
    for index, dev in enumerate(sd.query_devices()):
        entry = {
            "index": index,
            "name": str(dev.get("name", "")),
            "hostapi": api_names.get(int(dev.get("hostapi", -1)), ""),
            "default_samplerate": int(dev.get("default_samplerate", 0)),
            "max_input_channels": int(dev.get("max_input_channels", 0)),
            "max_output_channels": int(dev.get("max_output_channels", 0)),
            "is_default": index in (default_input, default_output),
        }
        if entry["max_input_channels"] > 0:
            inputs.append(entry)
        if entry["max_output_channels"] > 0:
            outputs.append(entry)

    return {
        "ok": True,
        "hostapis": hostapis,
        "inputs": inputs,
        "outputs": outputs,
        "default_input": int(default_input),
        "default_output": int(default_output),
    }


# ──────────────────────────────────────────────────────────────
# Playback test (the app's own route logic, not a parallel one)
# ──────────────────────────────────────────────────────────────

def _chime(sample_rate: int, seconds: float = 1.6) -> np.ndarray:
    """A soft two-tone chime with fades, so a broken route is audible."""
    t = np.linspace(0.0, seconds, int(sample_rate * seconds), endpoint=False)
    tone = 0.35 * np.sin(2 * np.pi * 440.0 * t) + 0.30 * np.sin(2 * np.pi * 659.0 * t)
    fade = int(sample_rate * 0.05)
    envelope = np.ones_like(tone)
    envelope[:fade] = np.linspace(0.0, 1.0, fade)
    envelope[-fade:] = np.linspace(1.0, 0.0, fade)
    return (tone * envelope * 32767.0 * 0.6).astype(np.int16)


def play_test(device: Optional[str], host_api: str, rate: int) -> Dict[str, Any]:
    # Imported here (not at module top) so `list` works even if the playback
    # module's heavier import chain changes.
    from winvoice.audio.playback import (
        AudioOutputUnavailable,
        open_output_stream,
        to_device_frames,
    )

    device_arg: Any = None if device in ("", "default", None) else device
    try:
        stream, actual_rate = open_output_stream(
            rate, 20, device=device_arg, host_api=host_api
        )
    except AudioOutputUnavailable as error:
        return {"ok": False, "error": str(error), "sample_rate": 0}

    try:
        # The device stream is opened with OUT_CHANNELS (2) — see playback.py
        # for why it must never be mono — and PortAudio rejects a flat mono
        # array on a multi-channel stream ("number of channels must match"),
        # so the chime has to be replicated per channel exactly like the
        # player does before any of it reaches `stream.write`.
        pcm = to_device_frames(_chime(actual_rate), getattr(stream, "channels", 1) or 1)
        block = max(64, int(actual_rate * 0.02))
        for start in range(0, pcm.shape[0], block):
            stream.write(pcm[start : start + block])
            time.sleep(0)  # stream.write blocks at device rate; yield to be safe
        # Let the device drain its internal buffer before closing, or the chime
        # is cut off mid-tail.
        time.sleep(0.15)
        return {"ok": True, "sample_rate": actual_rate, "error": ""}
    except Exception as error:
        return {"ok": False, "error": f"{type(error).__name__}: {error}", "sample_rate": 0}
    finally:
        try:
            stream.stop()
            stream.close()
        except Exception:  # pragma: no cover - teardown of a dying stream
            pass


# ──────────────────────────────────────────────────────────────
# Recording test
# ──────────────────────────────────────────────────────────────

def summarize_recording(chunks: List[np.ndarray], rate: int) -> Dict[str, Any]:
    """Level metrics for one recording — pure, so tests can feed synthetic audio."""
    if not chunks:
        return {"ok": False, "error": "no audio captured", "seconds": 0.0}
    samples = np.concatenate(chunks).astype(np.float32) / 32768.0
    if samples.size == 0:
        return {"ok": False, "error": "no audio captured", "seconds": 0.0}

    peak = float(np.abs(samples).max())
    rms = float(np.sqrt(np.mean(samples**2)))
    rms_dbfs = 20.0 * float(np.log10(max(rms, 1e-10)))
    clipped_ms = int(np.count_nonzero(np.abs(samples) >= 0.999) / rate * 1000)

    # "Hear something" heuristic: at least one 100 ms window clearly above the
    # noise floor of a muted/unplugged mic (which sits near digital silence).
    window = max(1, rate // 10)
    windows = [samples[i : i + window] for i in range(0, samples.size - window + 1, window)]
    window_rms = [float(np.sqrt(np.mean(w**2))) for w in windows] if windows else [rms]
    speech_like = max(window_rms) > 0.01  # ≈ -40 dBFS

    return {
        "ok": True,
        "error": "",
        "seconds": round(samples.size / rate, 2),
        "peak": round(peak, 4),
        "rms_dbfs": round(rms_dbfs, 1),
        "clipped_ms": clipped_ms,
        "speech_like": bool(speech_like),
    }


def record_level(device: Optional[str], seconds: float) -> Dict[str, Any]:
    device_arg: Any = None if device in ("", "default", None) else device
    chunks: List[np.ndarray] = []

    def _callback(indata, frames, time_info, status) -> None:  # noqa: ANN001
        chunks.append(indata.copy())

    try:
        with sd.InputStream(
            samplerate=16000,
            channels=1,
            dtype="int16",
            device=device_arg,
            callback=_callback,
        ):
            time.sleep(seconds)
    except Exception as error:
        return {"ok": False, "error": f"{type(error).__name__}: {error}", "seconds": 0.0}

    result = summarize_recording(chunks, 16000)
    return result


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Audio device probe (setup wizard helper)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="print the device inventory as RESULT JSON")

    play = sub.add_parser("play-test", help="play a short chime and report the route")
    play.add_argument("--device", default="default", help="output device name (default = system default)")
    play.add_argument("--host-api", default="auto", choices=("auto", "wasapi", "default"))
    play.add_argument("--rate", type=int, default=0, help="0 = the device's own rate")

    rec = sub.add_parser("record-level", help="record from a microphone and report levels")
    rec.add_argument("--device", default="default", help="input device name (default = system default)")
    rec.add_argument("--seconds", type=float, default=4.0)

    return p


def main() -> int:
    args = build_parser().parse_args()

    if args.command == "list":
        result = device_inventory()
    elif args.command == "play-test":
        result = play_test(args.device, args.host_api, args.rate)
    else:
        result = record_level(args.device, args.seconds)

    print(f"RESULT {json.dumps(result, ensure_ascii=True)}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
