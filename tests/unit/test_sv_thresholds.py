"""
Speaker-verification threshold validation.

Regression tests for an inverted-threshold bug: enrollment could write
`T_high < T_low`, which collapses the tier ladder so that any score above the
*lower* bar is promoted straight to "full" — a stranger scoring 0.48 would get
file-write and script-execution rights.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from winvoice.audio.sv import (
    DEFAULT_MIN_GAP,
    SpeakerProfile,
    SvEngine,
    thresholds_valid,
)


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def unit_vectors(n: int, dim: int = 16, cos_ij: float = 0.8) -> list[np.ndarray]:
    """
    Build `n` unit vectors whose pairwise cosine similarity is `cos_ij`.

    v_i = cos_ij * e0 + sqrt(1 - cos_ij^2) * e_i
    => cos(v_i, v_j) = cos_ij^2 for i != j
    """
    scale = np.sqrt(1.0 - cos_ij**2)
    out = []
    for i in range(n):
        v = np.zeros(dim, dtype=np.float32)
        v[0] = cos_ij
        v[i + 1] = scale
        out.append(v / np.linalg.norm(v))
    return out


def make_engine(tmp_path, **kwargs) -> SvEngine:
    """SvEngine wired to a throwaway profile directory."""
    kwargs.setdefault("profiles_dir", str(tmp_path / "profiles"))
    return SvEngine(**kwargs)


def enroll_fake(engine: SvEngine, speaker_id: str, embeddings: list[np.ndarray]) -> None:
    engine.enroll_start(speaker_id)
    engine._profiles[speaker_id].embeddings = list(embeddings)


# ──────────────────────────────────────────────────────────────
# The validity predicate
# ──────────────────────────────────────────────────────────────

class TestThresholdsValid:
    def test_healthy_gap_is_valid(self):
        assert thresholds_valid(0.60, 0.45)

    def test_equal_thresholds_are_invalid(self):
        assert not thresholds_valid(0.50, 0.50)

    def test_inverted_thresholds_are_invalid(self):
        # The exact shape produced by the reported enrollment.
        assert not thresholds_valid(0.474, 0.500)

    def test_gap_smaller_than_min_gap_is_invalid(self):
        assert not thresholds_valid(0.502, 0.500)
        assert thresholds_valid(0.500 + DEFAULT_MIN_GAP, 0.500)


# ──────────────────────────────────────────────────────────────
# Enrollment refuses to produce an unusable profile
# ──────────────────────────────────────────────────────────────

class TestEnrollFinalizeValidation:
    def test_writes_profile_when_separable(self, tmp_path):
        engine = make_engine(tmp_path, max_inter=0.45)
        # pairwise cosine 0.8^2 = 0.64 -> T_high 0.59, T_low 0.50
        enroll_fake(engine, "me", unit_vectors(8, cos_ij=0.8))

        profile = engine.enroll_finalize("me")

        assert profile.threshold_high == pytest.approx(0.59, abs=0.02)
        assert profile.threshold_low == pytest.approx(0.50, abs=0.01)
        assert thresholds_valid(profile.threshold_high, profile.threshold_low)
        assert (tmp_path / "profiles" / "me.json").exists()

    def test_refuses_inverted_thresholds(self, tmp_path):
        """max_inter too high for the enrollment quality -> refuse, do not save."""
        engine = make_engine(tmp_path, max_inter=0.62)
        enroll_fake(engine, "me", unit_vectors(8, cos_ij=0.8))

        with pytest.raises(ValueError) as exc:
            engine.enroll_finalize("me")

        msg = str(exc.value)
        assert "cannot separate" in msg
        assert "T_high" in msg and "T_low" in msg
        # The two concrete fixes must be spelled out.
        assert "--max-inter" in msg
        assert "Re-record" in msg

    def test_no_profile_written_on_refusal(self, tmp_path):
        engine = make_engine(tmp_path, max_inter=0.62)
        enroll_fake(engine, "me", unit_vectors(8, cos_ij=0.8))

        with pytest.raises(ValueError):
            engine.enroll_finalize("me")

        assert not (tmp_path / "profiles" / "me.json").exists()

    def test_rejects_too_few_samples(self, tmp_path):
        engine = make_engine(tmp_path)
        enroll_fake(engine, "me", unit_vectors(3))

        with pytest.raises(ValueError, match="at least 8"):
            engine.enroll_finalize("me")

    def test_lower_max_inter_makes_enrollment_succeed(self, tmp_path):
        """The suggested fix actually works."""
        engine = make_engine(tmp_path, max_inter=0.35)
        enroll_fake(engine, "me", unit_vectors(8, cos_ij=0.8))

        profile = engine.enroll_finalize("me")
        assert profile.threshold_low == pytest.approx(0.40, abs=0.01)
        assert thresholds_valid(profile.threshold_high, profile.threshold_low)


# ──────────────────────────────────────────────────────────────
# A bad profile must never grant access
# ──────────────────────────────────────────────────────────────

class TestInvalidProfileIsRefused:
    def test_inverted_profile_is_not_loaded(self, tmp_path):
        profiles = tmp_path / "profiles"
        profiles.mkdir()
        (profiles / "me.json").write_text(
            json.dumps(
                {
                    "speaker_id": "me",
                    "embeddings": [unit_vectors(1)[0].tolist()],
                    "threshold_high": 0.474,
                    "threshold_low": 0.500,
                    "created_at": 0.0,
                    "updated_at": 0.0,
                }
            ),
            encoding="utf-8",
        )

        engine = make_engine(tmp_path)

        assert engine.enrolled_speakers == [], "inverted profile must not be loaded"

    def test_healthy_profile_is_loaded(self, tmp_path):
        profiles = tmp_path / "profiles"
        profiles.mkdir()
        (profiles / "me.json").write_text(
            json.dumps(
                {
                    "speaker_id": "me",
                    "embeddings": [unit_vectors(1)[0].tolist()],
                    "threshold_high": 0.60,
                    "threshold_low": 0.45,
                    "created_at": 0.0,
                    "updated_at": 0.0,
                }
            ),
            encoding="utf-8",
        )

        engine = make_engine(tmp_path)

        assert engine.enrolled_speakers == ["me"]

    def test_verify_fails_closed_on_inverted_thresholds(self, tmp_path):
        """Defence in depth: even an in-memory bad profile must deny access."""
        engine = make_engine(tmp_path)
        engine._ready = True

        probe = unit_vectors(1)[0]
        engine._profiles["me"] = SpeakerProfile(
            speaker_id="me",
            embeddings=[probe],
            threshold_high=0.474,
            threshold_low=0.500,
        )
        # Any probe scores 1.0 against itself, i.e. far above BOTH thresholds —
        # which is exactly why the ladder must not be trusted when inverted.
        engine.compute_embedding = lambda _frames: probe  # type: ignore[assignment]

        result = engine.verify(probe, "me")

        assert result is not None
        assert result.tier == "rejected", "must deny, not grant 'full'"

    def test_verify_grants_full_when_thresholds_are_healthy(self, tmp_path):
        engine = make_engine(tmp_path)
        engine._ready = True

        probe = unit_vectors(1)[0]
        engine._profiles["me"] = SpeakerProfile(
            speaker_id="me",
            embeddings=[probe],
            threshold_high=0.60,
            threshold_low=0.45,
        )
        engine.compute_embedding = lambda _frames: probe  # type: ignore[assignment]

        result = engine.verify(probe, "me")

        assert result is not None
        assert result.tier == "full"
