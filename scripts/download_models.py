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
import re
import sys
import tarfile
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

    # ── TTS: Chinese VITS (icefall aishell3) ──────────────────
    # Piper zh voices are not published as a single GitHub asset; this VITS
    # model is the supported alternative for Chinese TTS in sherpa-onnx.
    "tts/vits-zh": {
        "url": (
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/"
            "vits-icefall-zh-aishell3.tar.bz2"
        ),
        "dest": "tts/vits-icefall-zh-aishell3.tar.bz2",
        "extract": True,
        "sha256": None,
        "note": "VITS Chinese (icefall aishell3), 31.6 MB",
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
    "tts": ["tts/vits-zh"],
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
    "tts/vits-zh",
    "sv/campplus",
]

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


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
                  resume: bool = True) -> bool:
    """Stream a URL to `dest` atomically, with resume and optional SHA256 check."""
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
                return download_file(url, dest, expected_sha256, resume=False)

            r.raise_for_status()

            total = int(r.headers.get("Content-Length") or 0)
            if r.status_code == 206:
                total += resume_from

            with tqdm(total=total or None, unit="B", unit_scale=True,
                      desc=f"  {dest.name}", leave=False) as bar:
                if resume_from and total:
                    bar.update(resume_from)
                with open(part, mode) as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            f.write(chunk)
                            bar.update(len(chunk))

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


def download_model(key: str, models_dir: Path, force: bool = False) -> bool:
    """Download (and extract) one manifest entry."""
    info = MANIFEST[key]
    dest = models_dir / info["dest"]
    dest.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n>> {key} - {info['note']}")
    print(f"  -> {dest}")

    # Already present?
    if dest.exists() and not force:
        if is_real_hash(info.get("sha256")) and dest.is_file():
            if sha256_of(dest).lower() == info["sha256"].lower():
                print("  [OK] Already present and SHA256-verified")
                return True
            print("  ! Existing file failed SHA256 - re-downloading")
        else:
            print("  [OK] Already present (use --force to re-download)")
            return True

    ok = download_file(info["url"], dest, info.get("sha256"))
    if not ok:
        return False

    if info.get("extract"):
        if not extract_archive(dest, dest.parent):
            return False

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
    p.add_argument("--force", action="store_true", help="re-download even if present")
    p.add_argument("--list", action="store_true", help="list manifest entries and exit")
    return p


def main() -> int:
    args = build_parser().parse_args()
    models_dir = Path(args.models_dir).expanduser().resolve()

    if args.list:
        print(f"Manifest ({len(MANIFEST)} entries):\n")
        for key, info in MANIFEST.items():
            flag = "sha256" if is_real_hash(info.get("sha256")) else "no-hash"
            kind = "archive" if info.get("extract") else "file"
            print(f"  {key}")
            print(f"      [{flag}, {kind}] {info['note']}")
            print(f"      -> {models_dir / info['dest']}")
        print("\nFlags: " + ", ".join(f"--{k}" for k in GROUPS))
        print("       --all, --all-with-llm, --force, --list")
        return 0

    # Resolve selection
    selected: list[str] = []
    for flag, keys in GROUPS.items():
        if getattr(args, flag.replace("-", "_").replace(".", "_"), False):
            selected.extend(keys)

    if args.all_with_llm:
        selected = CORE_KEYS + GROUPS["llm"]
    elif args.all:
        selected = list(CORE_KEYS)
    elif not selected:
        print("No selection given - downloading all core models (no LLM).")
        print("Use --list to see options, --llm to add the local model.\n")
        selected = list(CORE_KEYS)

    # De-dupe, keep manifest order
    selected = [k for k in MANIFEST if k in set(selected)]

    print("=" * 68)
    print(f"Windows Voice Assistant - model download")
    print(f"Target : {models_dir}")
    print(f"Models : {len(selected)}")
    print("=" * 68)

    failed: list[str] = []
    for key in selected:
        try:
            if not download_model(key, models_dir, force=args.force):
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
