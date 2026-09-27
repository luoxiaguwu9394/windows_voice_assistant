"""
The model seal: fingerprint once, verify at every startup, report honestly.

Enforcement is report-only by design — a startup check that renames files to
`.corrupt/` breaks a running assistant instead of informing its user. These
tests pin the seal/verify contract and the exclusion rules (enrollment
profiles and the KWS keyword cache are *supposed* to change and must never be
sealed).
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from winvoice.integrity import (
    IntegrityReport,
    failures_by_model,
    load_seal,
    seal_models,
    verify_sealed_models,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def models_tree():
    """
    A small stand-in for models/: two models, one enrollable profile, one KWS
    keyword cache — the last two are runtime data, not sealed artefacts.
    """
    base = PROJECT_ROOT / "runtime" / "_pytest_work"
    root = base / f"integrity_{uuid.uuid4().hex[:10]}"
    (root / "llm").mkdir(parents=True)
    (root / "tts" / "baker").mkdir(parents=True)
    (root / "sv" / "profiles").mkdir(parents=True)
    (root / "llm" / "qwen.gguf").write_bytes(b"qwen-bytes" * 100)
    (root / "tts" / "baker" / "model.onnx").write_bytes(b"baker-bytes" * 100)
    (root / "tts" / "baker" / "lexicon.txt").write_text("谁 shui2", encoding="utf-8")
    (root / "sv" / "profiles" / "me.json").write_text("{}", encoding="utf-8")
    (root / "winvoice_keywords_abc.txt").write_text("cache", encoding="utf-8")
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_seal_then_verify_is_clean(models_tree: Path):
    assert seal_models(models_tree) == models_tree / "integrity.json"

    report = verify_sealed_models(models_tree, deep=True)

    assert report.sealed is True and report.ok is True
    # 3 sealed files; the profile and the keyword cache are excluded.
    assert report.checked == 3


def test_runtime_data_is_never_sealed(models_tree: Path):
    seal_models(models_tree)

    records = load_seal(models_tree)
    assert records is not None
    assert "sv/profiles/me.json" not in records
    assert "winvoice_keywords_abc.txt" not in records
    assert "integrity.json" not in records


def test_truncation_is_caught_by_the_quick_check(models_tree: Path):
    seal_models(models_tree)
    gguf = models_tree / "llm" / "qwen.gguf"
    gguf.write_bytes(gguf.read_bytes()[:50])  # truncated: different size

    report = verify_sealed_models(models_tree, deep=False)

    assert report.ok is False
    assert report.failures == [
        {
            "path": "llm/qwen.gguf",
            "reason": "size",
            "expected": 1000,
            "actual": 50,
        }
    ]


def test_a_same_size_swap_only_deep_check_catches(models_tree: Path):
    seal_models(models_tree)
    lexicon = models_tree / "tts" / "baker" / "lexicon.txt"
    lexicon.write_text("谁 shei2", encoding="utf-8")  # same size, different bytes

    quick = verify_sealed_models(models_tree, deep=False)
    deep = verify_sealed_models(models_tree, deep=True)

    assert quick.ok is True, "sizes only: the quick path cannot see this"
    assert deep.ok is False and deep.failures[0]["reason"] == "sha256"


def test_a_deleted_model_is_reported_missing(models_tree: Path):
    seal_models(models_tree)
    (models_tree / "tts" / "baker" / "model.onnx").unlink()

    report = verify_sealed_models(models_tree, deep=True)

    assert report.failures[0]["reason"] == "missing"
    assert report.failures[0]["path"] == "tts/baker/model.onnx"


def test_an_unsealed_tree_is_skipped_not_failed(tmp_working):
    report = verify_sealed_models(tmp_working, deep=True)

    assert report.sealed is False
    assert report.ok is False
    assert report.checked == 0 and report.failures == []


@pytest.fixture
def tmp_working():
    base = PROJECT_ROOT / "runtime" / "_pytest_work"
    directory = base / f"integrity_empty_{uuid.uuid4().hex[:10]}"
    directory.mkdir(parents=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_rescaling_one_model_keeps_the_rest_of_the_seal(models_tree: Path):
    seal_models(models_tree)
    model = models_tree / "tts" / "baker" / "model.onnx"
    model.write_bytes(b"baker-updated" * 100)

    # Re-download flow: the tts model changed, so only it is resealed.
    assert seal_models(models_tree, sub_dir=Path("tts/baker")) is not None

    report = verify_sealed_models(models_tree, deep=True)
    assert report.ok is True, "the updated file matches its new record, the rest is intact"
    assert report.checked == 3, "the untouched llm seal record survived"


def test_failures_are_grouped_by_model_directory(models_tree: Path):
    seal_models(models_tree)
    (models_tree / "llm" / "qwen.gguf").unlink()
    lexicon = models_tree / "tts" / "baker" / "lexicon.txt"
    lexicon.write_text("x", encoding="utf-8")

    report = verify_sealed_models(models_tree, deep=True)
    grouped = failures_by_model(report)

    assert set(grouped) == {"llm", "tts/baker"}
    assert all(isinstance(group, list) for group in grouped.values())


def test_an_empty_directory_has_nothing_to_seal(tmp_working):
    assert seal_models(tmp_working) is None


def test_report_defaults_are_fail_closed():
    report = IntegrityReport()
    assert report.ok is False, "an unchecked report must never read as healthy"
