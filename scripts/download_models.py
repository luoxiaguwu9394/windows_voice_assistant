#!/usr/bin/env python3
"""
Model Download Script for Windows Voice Assistant.

Downloads all required models with:
- Resume support (HTTP Range)
- Optional SHA256 verification (only when a real hash is recorded)
- Atomic writes (.part -> final)
- Auto-extraction of .tar.bz2 archives

IMPORTANT: All URLs below were verified against the upstream GitHub Releases /
Hugging Face / ModelScope APIs. Sizes are informational only - we never
hard-fail on a size mismatch, because upstream sometimes repacks a release.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import tarfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    print("Missing dependency: requests\n  pip install requests tqdm", file=sys.stderr)
    raise SystemExit(2)

# tqdm is optional - fall back to a no-op progress bar if unavailable
try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover
    class tqdm:  # type: ignore[no-redef]
        """Minimal stand-in so the script works without tqdm installed."""

        def __init__(self, *_, **__):
            self.n = 0

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def update(self, n=1):
            self.n += n


# ──────────────────────────────────────────────────────────────
# Model Manifest
#
# Each entry:
#   url      : download URL (verified 2026-09, all return HTTP 200)
#   dest     : path relative to --models-dir where the file is saved
#   extract  : extract .tar.bz2 into dest's parent directory
#   sha256   : real 64-hex digest, or None to skip verification
#   note     : human-readable description
#
# NOTE: `size` is deliberately NOT stored. Upstream repacks releases from time
# to time, so a hard size check produces false failures. Only a pinned SHA256
# is treated as authoritative.
# ──────────────────────────────────────────────────────────────

MANIFEST: Dict[str, Dict[str, Any]] = {
    # ── Wake Word Detection (KWS) ─────────────────────────────
    "kws/zipformer-zh-en": {
        "url": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
            "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2"
        ),
        "dest": "kws/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2",
        "extract": True,
        "sha256": "68447f4fbc67e70eee3a93961f36e81e98f47aef73ce7e7ca00885c6cd3616a6",
        "note": "Zipformer KWS zh-en 3M (2025-12-20), 32.9 MB",
    },

    # ── Voice Activity Detection ──────────────────────────────
    "vad/silero": {
        "url": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
            "silero_vad_v5.onnx"
        ),
        "dest": "vad/silero_vad_v5.onnx",
        "extract": False,
        "sha256": None,
        "note": "Silero VAD v5 (onnx), 2.3 MB",
    },

    # ── ASR: SenseVoice int8 (zh/en/ja/ko/yue + ITN) ──────────
    "asr/sense-voice": {
        "url": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
            "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2"
        ),
        "dest": "asr/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
        "extract": True,
        "sha256": None,
        "note": "SenseVoice int8, 5 languages (2024-07-17), 163 MB",
    },

    # ── ASR: streaming Zipformer bilingual (fallback) ─────────
    # The asset name contains "small" - omitting it yields a 404.
    "asr/zipformer": {
        "url": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
            "sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16.tar.bz2"
        ),
        "dest": "asr/sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16.tar.bz2",
        "extract": True,
        "sha256": None,
        "note": "Streaming Zipformer small bilingual zh-en (2023-02-16), 458 MB",
    },

    # ── TTS: Chinese Matcha (22.05 kHz) — the default ─────────
    # 8 000 Hz was the ceiling of the old VITS model; this one is 22 050 Hz and
    # faster to synthesise (upstream RTF 0.54 @ 2 threads). It needs its vocoder
    # from the separate `vocoder-models` release, hence the second entry below —
    # a Matcha acoustic model alone cannot make sound, and the engine falls back
    # to `vits-zh` (8 kHz) rather than refusing to start.
    "tts/matcha-zh-baker": {
        "url": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
            "matcha-icefall-zh-baker.tar.bz2"
        ),
        "dest": "tts/matcha-icefall-zh-baker.tar.bz2",
        "extract": True,
        "sha256": None,
        "note": "Matcha Chinese (icefall baker, 1 female speaker, 22.05 kHz), 72 MB",
    },
    "tts/vocos-22khz": {
        "url": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/vocoder-models/"
            "vocos-22khz-univ.onnx"
        ),
        "dest": "tts/matcha-icefall-zh-baker/vocos-22khz-univ.onnx",
        "extract": False,
        "sha256": None,
        "note": "Vocos 22.05 kHz universal vocoder (required by the Matcha model), 51 MB",
    },

    # ── TTS: Chinese VITS (icefall aishell3) — fallback ───────
    # Natively 8 000 Hz. Kept as `tts.fallback_models`, so a machine that has not
    # downloaded the Matcha model still speaks.
    "tts/vits-zh": {
        "url": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
            "vits-icefall-zh-aishell3.tar.bz2"
        ),
        "dest": "tts/vits-icefall-zh-aishell3.tar.bz2",
        "extract": True,
        "sha256": None,
        "note": "VITS Chinese (icefall aishell3, 174 speakers, 8 kHz) - fallback, 31.6 MB",
    },

    # ── Speaker Verification: CAM++ (3D-Speaker) ──────────────
    # Release tag "speaker-recongition-models" is upstream's own typo.
    "sv/campplus": {
        "url": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
            "speaker-recongition-models/"
            "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"
        ),
        "dest": "sv/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
        "extract": False,
        "sha256": None,
        "note": "3D-Speaker CAM++ zh-cn 16k speaker embedding, 28.3 MB",
    },

    # ── Local LLM (optional) ──────────────────────────────────
    "llm/qwen2.5-3b-instruct": {
        "url": (
            "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/"
            "qwen2.5-3b-instruct-q4_k_m.gguf"
        ),
        "dest": "llm/qwen2.5-3b-instruct-q4_k_m.gguf",
        "extract": False,
        "sha256": None,
        "note": "Qwen2.5-3B-Instruct Q4_K_M (~2.2 GB) - recommended",
    },
    "llm/qwen2.5-1.5b-instruct": {
        "url": (
            "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/"
            "qwen2.5-1.5b-instruct-q4_k_m.gguf"
        ),
        "dest": "llm/qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "extract": False,
        "sha256": None,
        "note": "Qwen2.5-1.5B-Instruct Q4_K_M (~1.2 GB) - fastest",
    },
    "llm/qwen2.5-0.5b-instruct": {
        "url": (
            "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/"
            "qwen2.5-0.5b-instruct-q4_k_m.gguf"
        ),
        "dest": "llm/qwen2.5-0.5b-instruct-q4_k_m.gguf",
        "extract": False,
        "sha256": None,
        "note": "Qwen2.5-0.5B-Instruct Q4_K_M (~0.5 GB) - minimal",
    },
}


# Groups for CLI flags
GROUPS = {
    "kws": ["kws/zipformer-zh-en"],
    "vad": ["vad/silero"],
    "asr": ["asr/sense-voice", "asr/zipformer"],
    # The default model plus its vocoder and the (8 kHz) fallback: `--tts` has to
    # leave the machine in a working state, not in a state that falls back.
    "tts": ["tts/matcha-zh-baker", "tts/vocos-22khz", "tts/vits-zh"],
    "tts-fallback": ["tts/vits-zh"],
    "sv": ["sv/campplus"],
    "llm": ["llm/qwen2.5-3b-instruct"],
    "llm-1.5b": ["llm/qwen2.5-1.5b-instruct"],
    "llm-0.5b": ["llm/qwen2.5-0.5b-instruct"],
}

CORE_KEYS = [
    "kws/zipformer-zh-en",
    "vad/silero",
    "asr/sense-voice",
    "asr/zipformer",
    "tts/matcha-zh-baker",
    "tts/vocos-22khz",
    "tts/vits-zh",
    "sv/campplus",
]

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")

# Hugging Face endpoints can be redirected to a mirror (e.g. hf-mirror.com for
# machines that cannot reach huggingface.co directly). Only the scheme+host is
# rewritten; GitHub release assets are untouched and need a proxy instead.
HF_URL_PREFIX = "https://huggingface.co"
HF_ENDPOINT_ENV = "WINVOICE_HF_ENDPOINT"


def rewrite_hf_url(url: str, endpoint: Optional[str] = None) -> str:
    """Point a Hugging Face URL at a mirror endpoint (env or explicit)."""
    endpoint = (endpoint if endpoint is not None else os.environ.get(HF_ENDPOINT_ENV, "")) or ""
    endpoint = endpoint.strip().rstrip("/")
    if not endpoint or not url.startswith(HF_URL_PREFIX):
        return url
    return endpoint + url[len(HF_URL_PREFIX):]


class ProgressReporter:
    """
    Progress output for humans or for a driving process (the setup wizard).

    Machine mode emits tab-separated one-liners on stdout, flushed immediately
    so the wizard can stream them:

        MODEL\t<key>\t<note>
        PROGRESS\t<key>\t<done_bytes>\t<total_bytes>
        RESULT\t<key>\tok|fail\t<detail>

    Human mode keeps the historical prints and adds nothing.
    """

    def __init__(self, machine: bool = False):
        self.machine = machine
        self._last_emit: dict[str, tuple[float, int]] = {}

    def model(self, key: str, note: str) -> None:
        if self.machine:
            print(f"MODEL\t{key}\t{note}", flush=True)

    def bytes(self, key: str, done: int, total: int) -> None:
        if not self.machine:
            return
        now = time.monotonic()
        last_time, last_done = self._last_emit.get(key, (0.0, -1))
        step = max(total // 100, 1 << 20) if total else 1 << 20
        if done < total and done - last_done < step and now - last_time < 1.0:
            return
        self._last_emit[key] = (now, done)
        print(f"PROGRESS\t{key}\t{done}\t{total}", flush=True)

    def result(self, key: str, ok: bool, detail: str = "") -> None:
        if self.machine:
            print(f"RESULT\t{key}\t{'ok' if ok else 'fail'}\t{detail}", flush=True)


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def is_real_hash(value: Optional[str]) -> bool:
    """True only for an actual 64-char hex digest (not a TODO placeholder)."""
    return bool(value) and bool(_HEX64.match(value.strip()))


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} GB"


# ──────────────────────────────────────────────────────────────
# Download
# ──────────────────────────────────────────────────────────────

def download_file(url: str, dest: Path, expected_sha256: Optional[str] = None,
                  resume: bool = True, reporter: Optional[ProgressReporter] = None,
                  key: str = "") -> bool:
    """Stream a URL to `dest` atomically, with resume and optional SHA256 check."""
    reporter = reporter or ProgressReporter()
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) winvoice-downloader"}
    mode = "wb"
    resume_from = 0
    if resume and part.exists():
        resume_from = part.stat().st_size
        if resume_from > 0:
            headers["Range"] = f"bytes={resume_from}-"
            mode = "ab"
            print(f"  Resuming from {human(resume_from)}")

    try:
        with requests.get(url, headers=headers, stream=True, timeout=60,
                          allow_redirects=True) as r:
            if r.status_code == 416 and resume_from > 0:
                # Range not satisfiable -> already complete, restart clean
                print("  Server rejected range; restarting download")
                part.unlink(missing_ok=True)
                return download_file(url, dest, expected_sha256, resume=False,
                                     reporter=reporter, key=key)

            r.raise_for_status()

            total = int(r.headers.get("Content-Length") or 0)
            if r.status_code == 206:
                total += resume_from

            with tqdm(total=total or None, unit="B", unit_scale=True,
                      desc=f"  {dest.name}", leave=False) as bar:
                if resume_from and total:
                    bar.update(resume_from)
                with open(part, mode) as f:
                    written = resume_from
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)
                            bar.update(len(chunk))
                            reporter.bytes(key, written, total)

        # Integrity
        actual = sha256_of(part)
        print(f"  SHA256: {actual}")
        if is_real_hash(expected_sha256):
            if actual.lower() != expected_sha256.lower():
                print(f"  [X] SHA256 mismatch (expected {expected_sha256})")
                part.unlink(missing_ok=True)
                return False
            print("  [OK] SHA256 verified")
        else:
            print("  (no pinned SHA256 - skipping verification)")
            print("  -> paste the SHA256 above into MANIFEST to enable future checks")

        part.replace(dest)
        print(f"  [OK] Saved: {dest} ({human(dest.stat().st_size)})")
        return True

    except requests.HTTPError as e:
        print(f"  [X] HTTP error: {e}")
        part.unlink(missing_ok=True)
        return False
    except Exception as e:
        print(f"  [X] Download failed: {type(e).__name__}: {e}")
        print("    (partial file kept for resume - re-run to continue)")
        return False


def extract_archive(archive: Path, out_dir: Path) -> bool:
    """Extract a .tar.bz2 archive into out_dir."""
    try:
        with tarfile.open(archive, "r:bz2") as tar:
            tar.extractall(out_dir)
        print(f"  [OK] Extracted to {out_dir}")
        return True
    except Exception as e:
        print(f"  [X] Extraction failed: {e}")
        return False


def download_model(key: str, models_dir: Path, force: bool = False,
                   reporter: Optional[ProgressReporter] = None) -> bool:
    """Download (and extract) one manifest entry."""
    reporter = reporter or ProgressReporter()
    info = MANIFEST[key]
    dest = models_dir / info["dest"]
    dest.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n>> {key} - {info['note']}")
    print(f"  -> {dest}")
    reporter.model(key, info["note"])

    # Already present?
    if dest.exists() and not force:
        if is_real_hash(info.get("sha256")) and dest.is_file():
            if sha256_of(dest).lower() == info["sha256"].lower():
                print("  [OK] Already present and SHA256-verified")
                reporter.result(key, True, "already present")
                return True
            print("  ! Existing file failed SHA256 - re-downloading")
        else:
            print("  [OK] Already present (use --force to re-download)")
            reporter.result(key, True, "already present")
            return True

    ok = download_file(rewrite_hf_url(info["url"]), dest, info.get("sha256"),
                       reporter=reporter, key=key)
    if not ok:
        reporter.result(key, False, "download failed")
        return False

    if info.get("extract"):
        if not extract_archive(dest, dest.parent):
            reporter.result(key, False, "extraction failed")
            return False

    reporter.result(key, True, "downloaded")
    return True


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Download sherpa-onnx models for Windows Voice Assistant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/download_models.py --all\n"
            "  python scripts/download_models.py --kws --vad --asr\n"
            "  python scripts/download_models.py --llm\n"
            "  python scripts/download_models.py --list\n"
        ),
    )
    p.add_argument("--models-dir", default="models", help="target directory (default: models)")
    for flag, keys in GROUPS.items():
        dest = flag.replace("-", "_").replace(".", "_")
        p.add_argument(f"--{flag}", action="store_true", dest=dest,
                       help=f"download {flag}: {', '.join(keys)}")
    p.add_argument("--all", action="store_true",
                   help="download all core models (no LLM)")
    p.add_argument("--all-with-llm", action="store_true",
                   help="download all core models + recommended 3B LLM")
    p.add_argument("--only", default=None, metavar="KEY[,KEY...]",
                   help="download exactly these comma-separated manifest keys "
                        "(the setup wizard uses this; implies no group flags)")
    p.add_argument("--force", action="store_true", help="re-download even if present")
    p.add_argument("--hf-endpoint", default=None, metavar="URL",
                   help=f"rewrite {HF_URL_PREFIX} URLs to this mirror (default: "
                        f"env {HF_ENDPOINT_ENV}; GitHub URLs are unaffected)")
    p.add_argument("--progress-fmt", choices=("human", "machine"), default="human",
                   help="machine = tab-separated MODEL/PROGRESS/RESULT lines for "
                        "a driving process (the setup wizard)")
    p.add_argument("--list", action="store_true", help="list manifest entries and exit")
    p.add_argument("--seal", action="store_true",
                   help="fingerprint models/ into models/integrity.json for startup "
                        "integrity checks (no download)")
    p.add_argument("--model", dest="seal_model", default=None, metavar="KEY",
                   help="with --seal: (re)seal only this manifest key's directory, "
                        "keeping the rest of an existing seal")
    return p


def _run_seal(args, models_dir: Path) -> int:
    """`--seal`: fingerprint the local models tree (optionally one entry)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from winvoice.integrity import seal_models

    sub_dir: Path | None = None
    if args.seal_model:
        info = MANIFEST.get(args.seal_model)
        if info is None:
            known = ", ".join(sorted(MANIFEST))
            print(f"[X] Unknown model key: {args.seal_model}\n    Known keys: {known}")
            return 2
        sub_dir = (models_dir / info["dest"]).parent.relative_to(models_dir)

    scope = f"models/{sub_dir.as_posix()}/" if sub_dir is not None else "models/ (all)"
    print("=" * 68)
    print("Windows Voice Assistant - seal model integrity")
    print(f"Scope  : {scope}")
    print("This fingerprints the files AS THEY ARE NOW (trust-on-first-use).")
    print("=" * 68)

    seal_path = seal_models(models_dir, sub_dir=sub_dir)
    if seal_path is None:
        print(f"\n[X] Nothing to seal under {models_dir}")
        return 1

    import json

    payload = json.loads(seal_path.read_text(encoding="utf-8"))
    print(f"\n[OK] Sealed {len(payload['files'])} files -> {seal_path}")
    print("     sealed_at: " + str(payload.get("sealed_at")))
    print("     Startup and `python -m winvoice --check` now verify against this.")
    return 0


def resolve_selection(args: argparse.Namespace) -> list[str]:
    """Turn the CLI selection flags into an ordered list of manifest keys."""
    only = (getattr(args, "only", None) or "").strip()
    if only:
        keys = [k.strip() for k in only.split(",") if k.strip()]
        unknown = [k for k in keys if k not in MANIFEST]
        if unknown:
            raise SystemExit(
                f"[X] Unknown model key(s): {', '.join(unknown)}\n"
                f"    Known keys: {', '.join(sorted(MANIFEST))}"
            )
        return [k for k in MANIFEST if k in set(keys)]

    selected: list[str] = []
    for flag, keys in GROUPS.items():
        if getattr(args, flag.replace("-", "_").replace(".", "_"), False):
            selected.extend(keys)

    if args.all_with_llm:
        return list(CORE_KEYS) + list(GROUPS["llm"])
    if args.all:
        return list(CORE_KEYS)
    if selected:
        return selected

    print("No selection given - downloading all core models (no LLM).")
    print("Use --list to see options, --llm to add the local model.\n")
    return list(CORE_KEYS)


def main() -> int:
    args = build_parser().parse_args()

    if args.hf_endpoint:
        os.environ[HF_ENDPOINT_ENV] = args.hf_endpoint
    reporter = ProgressReporter(machine=args.progress_fmt == "machine")
    models_dir = Path(args.models_dir).expanduser().resolve()

    if args.seal:
        return _run_seal(args, models_dir)

    if args.list:
        print(f"Manifest ({len(MANIFEST)} entries):\n")
        for key, info in MANIFEST.items():
            flag = "sha256" if is_real_hash(info.get("sha256")) else "no-hash"
            kind = "archive" if info.get("extract") else "file"
            print(f"  {key}")
            print(f"      [{flag}, {kind}] {info['note']}")
            print(f"      -> {models_dir / info['dest']}")
        print("\nFlags: " + ", ".join(f"--{k}" for k in GROUPS))
        print("       --all, --all-with-llm, --only, --force, --list")
        return 0

    # De-dupe, keep manifest order
    selected = [k for k in MANIFEST if k in set(resolve_selection(args))]

    print("=" * 68)
    print(f"Windows Voice Assistant - model download")
    print(f"Target : {models_dir}")
    print(f"Models : {len(selected)}")
    print("=" * 68)

    failed: list[str] = []
    for key in selected:
        try:
            if not download_model(key, models_dir, force=args.force, reporter=reporter):
                failed.append(key)
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
            failed.append(key)
            break

    print("\n" + "=" * 68)
    if failed:
        print(f"[X] Failed ({len(failed)}): {', '.join(failed)}")
        print("  Re-run the same command to resume partial downloads.")
        return 1

    print(f"[OK] All {len(selected)} model(s) ready in {models_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
