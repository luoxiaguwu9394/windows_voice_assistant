#!/usr/bin/env python3
"""
Model Download Script.

Downloads all required models for the voice assistant.
Supports resume, SHA256 verification, and atomic writes.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path
from typing import Dict, Any

import requests
from tqdm import tqdm


# ──────────────────────────────────────────────────────────────
# Model Manifest
# ──────────────────────────────────────────────────────────────

MANIFEST: Dict[str, Dict[str, Any]] = {
    "kws/zipformer-zh-en": {
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-zh-en-2024-01-01.tar.bz2",
        "sha256": "a1b2c3d4e5f6...",  # Replace with actual
        "size": 45_000_000,
        "extract": True,
    },
    "vad/silero": {
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/vad-models/silero_vad.onnx",
        "sha256": "f6e5d4c3b2a1...",
        "size": 2_500_000,
        "extract": False,
    },
    "asr/sense-voice": {
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2",
        "sha256": "1a2b3c4d5e6f...",
        "size": 380_000_000,
        "extract": True,
    },
    "asr/zipformer": {
        "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-16.tar.bz2",
        "sha256": "6f5e4d3c2b1a...",
        "size": 120_000_000,
        "extract": True,
    },
    "tts/piper-zh": {
        "url": "https://github.com/rhasspy/piper/releases/download/v1.2.0/zh_CN-huayan-medium.onnx",
        "sha256": "abcdef123456...",
        "size": 55_000_000,
        "extract": False,
    },
    "sv/campplus": {
        "url": "https://github.com/modelscope/3D-Speaker/releases/download/v1.0.0/campplus.onnx",
        "sha256": "123456abcdef...",
        "size": 30_000_000,
        "extract": False,
    },
}


# ──────────────────────────────────────────────────────────────
# Download Logic
# ──────────────────────────────────────────────────────────────

def download_file(
    url: str,
    dest: Path,
    expected_sha256: str = None,
    expected_size: int = None,
    resume: bool = True,
) -> bool:
    """Download file with resume, progress bar, and verification."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp_dest = dest.with_suffix(dest.suffix + ".part")

    # Check existing partial download
    headers = {}
    mode = "wb"
    if resume and temp_dest.exists():
        existing_size = temp_dest.stat().st_size
        headers["Range"] = f"bytes={existing_size}-"
        mode = "ab"
        print(f"Resuming download from {existing_size} bytes")

    try:
        response = requests.get(url, headers=headers, stream=True, timeout=30)
        response.raise_for_status()

        total_size = int(response.headers.get("Content-Length", 0))
        if resume and "Range" in headers:
            total_size += existing_size

        # Progress bar
        with tqdm(total=total_size, unit="B", unit_scale=True, desc=dest.name) as pbar:
            if resume and temp_dest.exists():
                pbar.update(existing_size)

            with open(temp_dest, mode) as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        pbar.update(len(chunk))

        # Verify SHA256
        if expected_sha256:
            print("Verifying SHA256...")
            sha256 = hashlib.sha256()
            with open(temp_dest, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    sha256.update(chunk)
            actual = sha256.hexdigest()
            if actual != expected_sha256:
                print(f"SHA256 mismatch! Expected: {expected_sha256}, Got: {actual}")
                temp_dest.unlink(missing_ok=True)
                return False

        # Verify size
        if expected_size and temp_dest.stat().st_size != expected_size:
            print(f"Size mismatch! Expected: {expected_size}, Got: {temp_dest.stat().st_size}")
            temp_dest.unlink(missing_ok=True)
            return False

        # Atomic rename
        temp_dest.replace(dest)
        print(f"Downloaded: {dest}")
        return True

    except Exception as e:
        print(f"Download failed: {e}")
        temp_dest.unlink(missing_ok=True)
        return False


def extract_archive(archive_path: Path, dest_dir: Path) -> bool:
    """Extract tar.bz2 archive."""
    import tarfile
    try:
        with tarfile.open(archive_path, "r:bz2") as tar:
            tar.extractall(dest_dir)
        print(f"Extracted: {archive_path} -> {dest_dir}")
        return True
    except Exception as e:
        print(f"Extraction failed: {e}")
        return False


def download_model(name: str, models_dir: Path, force: bool = False) -> bool:
    """Download a single model by name."""
    if name not in MANIFEST:
        print(f"Unknown model: {name}")
        print(f"Available: {list(MANIFEST.keys())}")
        return False

    info = MANIFEST[name]
    dest = models_dir / name
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Check if already exists and valid
    if dest.exists() and not force:
        if info.get("sha256"):
            sha256 = hashlib.sha256()
            with open(dest, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    sha256.update(chunk)
            if sha256.hexdigest() == info["sha256"]:
                print(f"Already exists (verified): {dest}")
                return True

    print(f"Downloading {name}...")
    success = download_file(
        info["url"],
        dest,
        expected_sha256=info.get("sha256"),
        expected_size=info.get("size"),
    )

    if success and info.get("extract"):
        extract_dir = dest.parent
        if not extract_archive(dest, extract_dir):
            return False
        # Optionally remove archive after extraction
        # dest.unlink()

    return success


def main():
    parser = argparse.ArgumentParser(description="Download models for Windows Voice Assistant")
    parser.add_argument("--models-dir", default="models", help="Models directory")
    parser.add_argument("--kws", action="store_true", help="Download KWS model")
    parser.add_argument("--vad", action="store_true", help="Download VAD model")
    parser.add_argument("--asr", action="store_true", help="Download ASR model")
    parser.add_argument("--tts", action="store_true", help="Download TTS model")
    parser.add_argument("--sv", action="store_true", help="Download SV model")
    parser.add_argument("--all", action="store_true", help="Download all models")
    parser.add_argument("--force", action="store_true", help="Force re-download")
    parser.add_argument("--list", action="store_true", help="List available models")

    args = parser.parse_args()

    models_dir = Path(args.models_dir).resolve()

    if args.list:
        print("Available models:")
        for name, info in MANIFEST.items():
            print(f"  {name}: {info['size']/1024/1024:.1f} MB")
        return

    # Determine which models to download
    to_download = []
    if args.all or (not any([args.kws, args.vad, args.asr, args.tts, args.sv])):
        to_download = list(MANIFEST.keys())
    else:
        if args.kws:
            to_download.append("kws/zipformer-zh-en")
        if args.vad:
            to_download.append("vad/silero")
        if args.asr:
            to_download.extend(["asr/sense-voice", "asr/zipformer"])
        if args.tts:
            to_download.append("tts/piper-zh")
        if args.sv:
            to_download.append("sv/campplus")

    print(f"Downloading {len(to_download)} model(s) to {models_dir}")
    print("-" * 60)

    failed = []
    for name in to_download:
        if not download_model(name, models_dir, force=args.force):
            failed.append(name)
        print("-" * 60)

    if failed:
        print(f"\nFailed downloads: {failed}")
        sys.exit(1)
    else:
        print("\nAll models downloaded successfully!")


if __name__ == "__main__":
    main()