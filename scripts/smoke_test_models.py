#!/usr/bin/env python3
"""
Smoke test: run every audio engine against the installed models.

Unlike the stub tests, this loads the real sherpa-onnx models and the
bundled test WAVs, so it answers "do the engines actually run?" rather
than "is the plumbing wired?".

Usage:
    python scripts/smoke_test_models.py            # all engines
    python scripts/smoke_test_models.py --only asr
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import traceback
from pathlib import Path
from typing import Callable, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MODELS = ROOT / "models"


def hr(title: str) -> None:
    print()
    print("=" * 68)
    print(title)
    print("=" * 68)


class Results:
    def __init__(self) -> None:
        self.rows: List[Tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))

    def summary(self) -> int:
        hr("SUMMARY")
        passed = sum(1 for _, ok, _ in self.rows if ok)
        for name, ok, detail in self.rows:
            print(f"  {'PASS' if ok else 'FAIL'}  {name:<34} {detail}")
        print()
        print(f"  {passed}/{len(self.rows)} passed")
        return 0 if passed == len(self.rows) else 1


def first_wav(directory: Path, *names: str) -> Path | None:
    for name in names:
        p = directory / name
        if p.exists():
            return p
    wavs = sorted(directory.glob("*.wav"))
    return wavs[0] if wavs else None


# ──────────────────────────────────────────────────────────────
# Engine tests
# ──────────────────────────────────────────────────────────────

async def test_kws(res: Results) -> None:
    hr("KWS - keyword spotting (real model)")
    from winvoice.audio import create_kws_engine, read_wav
    from winvoice.config import reset_config

    reset_config()
    model_dir = MODELS / "kws" / "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
    if not model_dir.exists():
        res.add("kws: model present", False, f"missing {model_dir}")
        return

    # 1. Config keywords must tokenize into a usable keywords file.
    try:
        engine = create_kws_engine(use_stub=False)
        kf = engine.build_keywords_file()
        lines = [l for l in kf.read_text(encoding="utf-8").splitlines() if l.strip()]
        res.add("kws: tokenize config keywords", bool(lines),
                f"{len(lines)} keyword(s) -> {kf.name}")
        for line in lines:
            print(f"        {line}")
        await engine.initialize()
        res.add("kws: initialize (config keywords)", True)
    except Exception as e:
        res.add("kws: initialize (config keywords)", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()
        return

    # 2. Feed the model's own bundled test wavs, which contain the keywords
    #    listed in test_wavs/keywords.txt (en_0 = LIGHT UP, en_1 = LOVELY CHILD).
    test_dir = model_dir / "test_wavs"
    bundled_keywords = test_dir / "keywords.txt"
    if not bundled_keywords.exists():
        res.add("kws: bundled test assets", False, "no test_wavs/keywords.txt")
        return

    from winvoice.audio.kws import KwsEngine

    try:
        detector = KwsEngine(keywords_file=str(bundled_keywords))
        await detector.initialize()
    except Exception as e:
        res.add("kws: initialize (bundled keywords)", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()
        return

    cases = [("en_0.wav", "LIGHT_UP"), ("en_1.wav", "LOVELY_CHILD")]
    tested = 0
    expected_hits = 0

    for wav_name, expected in cases:
        wav = test_dir / wav_name
        if not wav.exists():
            continue
        tested += 1
        try:
            samples, _ = read_wav(wav)
            detector.reset()
            hits: list[str] = []
            window = 1600  # 100 ms
            for i in range(0, len(samples), window):
                detector.accept_waveform(samples[i : i + window])
                hit = detector.get_result()
                if hit:
                    hits.append(hit.keyword)
                    detector.reset()

            got_expected = expected in hits
            expected_hits += int(got_expected)
            res.add(
                f"kws: {wav_name} -> {expected}",
                got_expected,
                f"hits={hits}" if hits else "no detection",
            )
        except Exception as e:
            res.add(f"kws: {wav_name}", False, f"{type(e).__name__}: {e}")
            traceback.print_exc()

    if not tested:
        res.add("kws: detect in bundled wav", False, "no test wavs found")


async def test_vad(res: Results) -> None:
    hr("VAD - voice activity detection (real model)")
    from winvoice.audio import create_vad_engine, read_wav
    from winvoice.config import reset_config

    reset_config()
    model = MODELS / "vad" / "silero_vad_v5.onnx"
    if not model.exists():
        res.add("vad: model present", False, f"missing {model}")
        return
    res.add("vad: model present", True, f"{model.stat().st_size / 1e6:.1f} MB")

    engine = create_vad_engine(use_stub=False)
    try:
        await engine.initialize()
        res.add("vad: initialize", True)
    except Exception as e:
        res.add("vad: initialize", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()
        return

    wav_dir = MODELS / "kws" / "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20" / "test_wavs"
    wav = first_wav(wav_dir, "zh_0.wav", "en_0.wav")
    if not wav:
        # fall back to any model wav we can find
        candidates = list(MODELS.rglob("test_wavs/*.wav"))
        wav = candidates[0] if candidates else None
    if not wav:
        res.add("vad: segment a wav", False, "no test wav available")
        return

    try:
        samples, _ = read_wav(wav)
        segments = []
        window = 512
        for i in range(0, len(samples), window):
            segments.extend(engine.accept_waveform(samples[i : i + window]))
        segments.extend(engine.flush())

        total_ms = sum(s.duration_ms for s in segments)
        res.add("vad: segment a wav", len(segments) > 0,
                f"{wav.name} -> {len(segments)} segment(s), {total_ms} ms speech")
    except Exception as e:
        res.add("vad: segment a wav", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()


async def test_asr(res: Results) -> None:
    hr("ASR - speech recognition (real model)")
    from winvoice.audio import create_asr_engine, read_wav
    from winvoice.config import reset_config

    reset_config()
    engine = create_asr_engine(use_stub=False)
    try:
        await engine.initialize()
        res.add("asr: initialize", True, str(engine.model_path.name))
    except Exception as e:
        res.add("asr: initialize", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()
        return

    wav_dir = MODELS / "asr" / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17" / "test_wavs"
    cases = [("zh.wav", "zh"), ("en.wav", "en")]
    found = 0
    for name, label in cases:
        wav = wav_dir / name
        if not wav.exists():
            continue
        found += 1
        try:
            samples, _ = read_wav(wav)
            out = await engine.transcribe(samples)
            ok = bool(out.text.strip())
            res.add(f"asr: transcribe {name}", ok,
                    f"lang={out.language} len={len(out.text)} :: {out.text[:48]}")
        except Exception as e:
            res.add(f"asr: transcribe {name}", False, f"{type(e).__name__}: {e}")
            traceback.print_exc()

    if not found:
        res.add("asr: transcribe", False, f"no test wavs in {wav_dir}")


async def test_sv(res: Results) -> None:
    hr("SV - speaker verification (real model)")
    from winvoice.audio import create_sv_engine, read_wav
    from winvoice.config import reset_config

    reset_config()
    engine = create_sv_engine(use_stub=False)
    try:
        await engine.initialize()
        res.add("sv: initialize", True, f"dim={engine.embedding_dim}")
    except Exception as e:
        res.add("sv: initialize", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()
        return

    wav_dir = MODELS / "kws" / "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20" / "test_wavs"
    wavs = [wav_dir / "zh_0.wav", wav_dir / "zh_1.wav"]
    wavs = [w for w in wavs if w.exists()]
    if len(wavs) < 2:
        wavs = sorted(wav_dir.glob("*.wav"))[:2]
    if len(wavs) < 2:
        res.add("sv: embeddings", False, "need two test wavs")
        return

    try:
        s0, _ = read_wav(wavs[0])
        s1, _ = read_wav(wavs[1])
        e0a = engine.compute_embedding(s0)
        e0b = engine.compute_embedding(s0)
        if e0a is None or e0b is None:
            res.add("sv: embeddings", False, "compute_embedding returned None")
            return

        res.add("sv: embedding shape", e0a.shape == (engine.embedding_dim,), f"shape={e0a.shape}")

        from winvoice.audio.sv import cosine
        same = cosine(e0a, e0b)
        res.add("sv: same-audio similarity ~1.0", same > 0.999, f"cos={same:.6f}")

        e1 = engine.compute_embedding(s1)
        if e1 is not None:
            cross = cosine(e0a, e1)
            res.add("sv: cross-audio similarity", True,
                    f"cos={cross:.4f} (different speakers usually lower)")
    except Exception as e:
        res.add("sv: embeddings", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()


async def test_tts(res: Results) -> None:
    hr("TTS - speech synthesis (real model)")
    import numpy as np

    from winvoice.audio import create_tts_engine
    from winvoice.config import reset_config
    from winvoice.contracts import TtsRequest

    reset_config()
    engine = create_tts_engine(use_stub=False)
    try:
        await engine.initialize()
        res.add("tts: initialize", True,
                f"sr={engine.sample_rate} speakers={engine.num_speakers}")
    except Exception as e:
        res.add("tts: initialize", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()
        return

    text = "你好，我是语音助手。"
    try:
        chunks = []
        async for chunk in engine.synthesize(TtsRequest(text=text, voice="default")):
            chunks.append(chunk)
        total = sum(len(c.data) for c in chunks)
        duration_ms = int(total / 2 / engine.sample_rate * 1000)
        res.add("tts: synthesize", total > 0,
                f"{len(chunks)} chunk(s), {duration_ms} ms audio for {len(text)} chars")

        if chunks:
            pcm = np.frombuffer(b"".join(c.data for c in chunks), dtype=np.int16)
            peak = int(np.abs(pcm).max())
            res.add("tts: audio not silent", peak > 0, f"peak={peak}")
    except Exception as e:
        res.add("tts: synthesize", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()


async def test_pipeline_stub(res: Results) -> None:
    hr("Pipeline - stub mode (no models)")
    import os

    os.environ["WINVOICE_STUB_AUDIO"] = "1"
    from winvoice.config import reset_config

    reset_config()
    try:
        from winvoice.audio import AudioPipeline

        pipe = AudioPipeline(use_stub=True)
        await pipe.initialize()
        res.add("pipeline: stub initialize", True, "all 5 stub engines ready")
        pipe.stop()
    except Exception as e:
        res.add("pipeline: stub initialize", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        os.environ.pop("WINVOICE_STUB_AUDIO", None)


# ──────────────────────────────────────────────────────────────

TESTS: dict[str, Callable] = {
    "kws": test_kws,
    "vad": test_vad,
    "asr": test_asr,
    "sv": test_sv,
    "tts": test_tts,
    "pipeline": test_pipeline_stub,
}


async def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test the audio engines against real models")
    parser.add_argument("--only", choices=list(TESTS), help="run a single engine test")
    args = parser.parse_args()

    print("Windows Voice Assistant - real-model smoke test")
    print(f"workspace: {ROOT}")

    res = Results()
    names = [args.only] if args.only else list(TESTS)

    for name in names:
        try:
            await TESTS[name](res)
        except Exception as e:
            res.add(f"{name}: unexpected failure", False, f"{type(e).__name__}: {e}")
            traceback.print_exc()

    return res.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
