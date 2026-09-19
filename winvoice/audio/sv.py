"""
Speaker Verification — sherpa-onnx SpeakerEmbeddingExtractor (3D-Speaker CAM++).

Enrollment collects N samples, computes embeddings, and derives two
thresholds from the intra-speaker similarity distribution:

    T_high = min_intra - offset_high
    T_low  = max_inter + offset_low

Runtime verification compares a fresh embedding against the enrolled set
by max cosine similarity and maps it to a permission tier.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np

from winvoice.config import get_config
from winvoice.logging import get_logger, inc_request
from ._common import ModelNotFoundError, frames_to_float32, resolve_path

logger = get_logger(__name__)

SAMPLE_RATE = 16000
MIN_ENROLL_SAMPLES = 8
MIN_INTRA_FLOOR = 0.40

# T_high must exceed T_low by at least this much, otherwise the "guest" band
# collapses and the tier logic cannot separate speakers from impostors.
DEFAULT_MIN_GAP = 0.05

# Estimated similarity to the most similar non-target speaker. It cannot be
# measured from a single enrolled speaker, so it is an estimate from the
# AISHELL-3 / CN-Celeb reference range and is configurable.
DEFAULT_MAX_INTER = 0.45


@dataclass
class SvResult:
    speaker_id: str
    score: float
    tier: str  # "full" | "guest" | "rejected"
    threshold_high: float
    threshold_low: float


@dataclass
class SpeakerProfile:
    speaker_id: str
    embeddings: List[np.ndarray] = field(default_factory=list)
    threshold_high: float = 0.60
    threshold_low: float = 0.40
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


def _load_sherpa():
    try:
        import sherpa_onnx
    except ImportError as e:  # pragma: no cover
        raise ModelNotFoundError(
            "sherpa-onnx is not installed. Install it with:\n"
            "  pip install sherpa-onnx==1.13.8"
        ) from e
    return sherpa_onnx


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity, safe against zero vectors."""
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def thresholds_valid(high: float, low: float, min_gap: float = DEFAULT_MIN_GAP) -> bool:
    """
    A usable profile needs `high` strictly above `low` by at least `min_gap`.

    When `high <= low` the tier ladder collapses: `score >= high` is evaluated
    first, so anything at or above the *lower* bar is promoted straight to
    "full" and the guest band becomes unreachable. That is a security hole,
    not a cosmetic issue, so both enrollment and verification reject it.
    """
    return (high - low) >= min_gap


class SvEngine:
    """CAM++ speaker embedding extractor with threshold calibration."""

    def __init__(
        self,
        model_path: Optional[str] = None,
        enabled: Optional[bool] = None,
        threshold_high: Optional[float] = None,
        threshold_low: Optional[float] = None,
        adaptive_update: Optional[bool] = None,
        update_weight: Optional[float] = None,
        profiles_dir: Optional[str] = None,
        offset_high: Optional[float] = None,
        offset_low: Optional[float] = None,
        max_inter: Optional[float] = None,
        min_gap: Optional[float] = None,
    ):
        cfg = get_config()
        self.model_path = resolve_path(
            model_path
            or cfg.get(
                "sv.model",
                "models/sv/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
            ),
            kind="file",
        )
        self.enabled = bool(enabled if enabled is not None else cfg.get("sv.enabled", True))
        self.threshold_high = float(
            threshold_high if threshold_high is not None else cfg.get("sv.threshold_high", 0.60)
        )
        self.threshold_low = float(
            threshold_low if threshold_low is not None else cfg.get("sv.threshold_low", 0.40)
        )
        self.adaptive_update = bool(
            adaptive_update if adaptive_update is not None else cfg.get("sv.adaptive_update", True)
        )
        self.update_weight = float(
            update_weight if update_weight is not None else cfg.get("sv.update_weight", 0.05)
        )
        self.offset_high = float(
            offset_high if offset_high is not None else cfg.get("sv.offset_high", 0.05)
        )
        self.offset_low = float(
            offset_low if offset_low is not None else cfg.get("sv.offset_low", 0.05)
        )
        self.max_inter = float(
            max_inter if max_inter is not None else cfg.get("sv.max_inter", DEFAULT_MAX_INTER)
        )
        self.min_gap = float(
            min_gap if min_gap is not None else cfg.get("sv.min_gap", DEFAULT_MIN_GAP)
        )

        self.profiles_dir = Path(
            profiles_dir or cfg.get("sv.profiles_dir", "models/sv/profiles")
        )
        self.profiles_dir.mkdir(parents=True, exist_ok=True)

        self._extractor = None
        self._ready = False
        self._profiles: Dict[str, SpeakerProfile] = {}
        self._load_profiles()

    # ── lifecycle ──────────────────────────────────────────────

    async def initialize(self) -> None:
        if not self.enabled:
            logger.info("sv_disabled")
            return

        sherpa_onnx = _load_sherpa()
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(self.model_path),
            num_threads=1,
            provider="cpu",
            debug=False,
        )
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        self._ready = True
        logger.info(
            "sv_initialized",
            model=str(self.model_path),
            dim=self.embedding_dim,
            enrolled=list(self._profiles),
        )

    @property
    def embedding_dim(self) -> int:
        return self._extractor.dim if self._ready else 0

    # ── embeddings ─────────────────────────────────────────────

    def compute_embedding(self, frames) -> Optional[np.ndarray]:
        """Compute a unit-normalised embedding from audio frames or an array."""
        if not self._ready:
            return None

        samples = (
            np.asarray(frames, dtype=np.float32)
            if isinstance(frames, np.ndarray)
            else frames_to_float32(frames)
        )
        if samples.size == 0:
            return None

        stream = self._extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        stream.input_finished()

        if not self._extractor.is_ready(stream):
            logger.warning("sv_embedding_not_ready", samples=int(samples.size))
            return None

        emb = np.asarray(self._extractor.compute(stream), dtype=np.float32)
        norm = np.linalg.norm(emb)
        return emb / norm if norm > 0 else emb

    # ── enrollment ─────────────────────────────────────────────

    def enroll_start(self, speaker_id: str, force: bool = False) -> None:
        if speaker_id in self._profiles and not force:
            raise ValueError(f"Speaker '{speaker_id}' is already enrolled (use force=True to redo)")
        self._profiles[speaker_id] = SpeakerProfile(speaker_id=speaker_id)
        logger.info("sv_enroll_started", speaker_id=speaker_id)

    def enroll_sample(self, speaker_id: str, frames) -> bool:
        if speaker_id not in self._profiles:
            raise ValueError(f"Enrollment not started for '{speaker_id}'")

        emb = self.compute_embedding(frames)
        if emb is None:
            logger.warning("sv_enroll_sample_failed", speaker_id=speaker_id)
            return False

        self._profiles[speaker_id].embeddings.append(emb)
        logger.info(
            "sv_enroll_sample_added",
            speaker_id=speaker_id,
            count=len(self._profiles[speaker_id].embeddings),
        )
        return True

    def enroll_finalize(self, speaker_id: str, max_inter: Optional[float] = None) -> SpeakerProfile:
        """
        Derive thresholds from the intra-speaker similarity distribution.

            T_high = min_intra - offset_high
            T_low  = max_inter + offset_low

        `max_inter` is the estimated similarity to the most similar non-target
        speaker. It cannot be measured from one enrolled speaker, so it is an
        estimate (AISHELL-3 / CN-Celeb reference range) and is tunable via
        `sv.max_inter` or this argument.

        The derivation is only meaningful when the genuine and impostor score
        ranges are separable, i.e. `T_high > T_low`. If they are not, no
        profile is written and the caller gets a message naming the two
        concrete fixes — silently saving inverted thresholds would hand the
        "full" tier to anyone scoring just above the lower bar.
        """
        profile = self._profiles[speaker_id]
        embs = profile.embeddings

        if len(embs) < MIN_ENROLL_SAMPLES:
            raise ValueError(f"Need at least {MIN_ENROLL_SAMPLES} samples, got {len(embs)}")

        sims = [cosine(embs[i], embs[j])
                for i in range(len(embs)) for j in range(i + 1, len(embs))]
        if not sims:
            raise ValueError("Not enough distinct samples to compute similarities")

        min_intra = float(min(sims))
        mean_intra = float(np.mean(sims))
        p10_intra = float(np.percentile(sims, 10))
        effective_max_inter = self.max_inter if max_inter is None else float(max_inter)

        logger.info(
            "sv_enroll_stats",
            speaker_id=speaker_id,
            samples=len(embs),
            min_intra=round(min_intra, 4),
            p10_intra=round(p10_intra, 4),
            mean_intra=round(mean_intra, 4),
            max_inter=effective_max_inter,
        )

        if min_intra < MIN_INTRA_FLOOR:
            raise ValueError(
                f"Intra-speaker similarity too low (min_intra={min_intra:.3f} < {MIN_INTRA_FLOOR}).\n"
                "  The enrolment samples do not sound consistently like the same person.\n"
                "  Re-record in a quieter room, closer to the microphone, with varied wording."
            )

        proposed_high = min_intra - self.offset_high
        proposed_low = effective_max_inter + self.offset_low

        if not thresholds_valid(proposed_high, proposed_low, self.min_gap):
            gap = proposed_high - proposed_low
            raise ValueError(
                "Enrolment cannot separate you from other speakers with these settings.\n"
                f"  min_intra        = {min_intra:.3f}  (your worst self-similarity)\n"
                f"  max_inter        = {effective_max_inter:.3f}  (estimated impostor similarity)\n"
                f"  T_high would be  = {proposed_high:.3f}\n"
                f"  T_low  would be  = {proposed_low:.3f}\n"
                f"  gap              = {gap:+.3f}  (need >= {self.min_gap:.2f})\n"
                "\n"
                "  Saving these would put T_high BELOW T_low, which promotes anyone\n"
                "  scoring above the lower bar straight to the 'full' tier.\n"
                "\n"
                "  Two fixes, easiest first:\n"
                f"    1) Lower the impostor estimate (it is only a guess):\n"
                f"         python -m winvoice.enroll --speaker {speaker_id} --samples 8 --force "
                f"--max-inter {max(0.0, min_intra - self.min_gap - self.offset_high - self.offset_low):.2f}\n"
                "    2) Re-record for a higher min_intra: quiet room, closer mic,\n"
                "       varied wording, 8+ samples."
            )

        profile.threshold_high = proposed_high
        profile.threshold_low = proposed_low
        self._save_profile(profile)

        gap = profile.threshold_high - profile.threshold_low
        logger.info(
            "sv_enroll_finalized",
            speaker_id=speaker_id,
            threshold_high=round(profile.threshold_high, 4),
            threshold_low=round(profile.threshold_low, 4),
            gap=round(gap, 4),
        )

        # A technically-valid but narrow gap means the profile is fragile: the
        # genuine and impostor ranges are nearly touching, so a slightly noisy
        # utterance can land on the wrong side. Warn rather than fail, since a
        # working-but-marginal profile still beats no protection at all.
        if gap < 2 * self.min_gap:
            logger.warning(
                "sv_profile_margin_thin",
                speaker_id=speaker_id,
                gap=round(gap, 4),
                comfortable_gap=round(2 * self.min_gap, 4),
                min_intra=round(min_intra, 4),
                detail=(
                    "genuine and impostor score ranges are close together; "
                    "T_high is low enough that a similar voice may reach the "
                    "'full' tier"
                ),
                suggestion="re-record in a quieter room for a higher min_intra",
            )

        return profile

    # ── verification ───────────────────────────────────────────

    def verify(self, frames, speaker_id: str = "me") -> Optional[SvResult]:
        """
        Verify audio against an enrolled speaker and return the tier.

        Fails closed: a profile whose thresholds are inverted or too close
        together yields `rejected` rather than granting access, because the
        ladder would otherwise collapse and promote low scores to "full".
        """
        if not self._ready or speaker_id not in self._profiles:
            return None

        profile = self._profiles[speaker_id]
        if not profile.embeddings:
            return None

        emb = self.compute_embedding(frames)
        if emb is None:
            return None

        score = max(cosine(emb, e) for e in profile.embeddings)

        if not thresholds_valid(profile.threshold_high, profile.threshold_low, self.min_gap):
            logger.error(
                "sv_profile_thresholds_invalid",
                speaker_id=speaker_id,
                threshold_high=profile.threshold_high,
                threshold_low=profile.threshold_low,
                min_gap=self.min_gap,
                action="failing closed -> rejected; re-run enrollment",
            )
            tier = "rejected"
        elif score >= profile.threshold_high:
            tier = "full"
        elif score >= profile.threshold_low:
            tier = "guest"
        else:
            tier = "rejected"

        inc_request("sv", tier)
        if self.adaptive_update and tier in ("full", "guest"):
            self._adaptive_update(profile, emb)

        result = SvResult(
            speaker_id=speaker_id,
            score=float(score),
            tier=tier,
            threshold_high=profile.threshold_high,
            threshold_low=profile.threshold_low,
        )
        logger.info(
            "sv_result",
            speaker_id=speaker_id,
            score=round(result.score, 4),
            tier=tier,
        )
        return result

    def _adaptive_update(self, profile: SpeakerProfile, emb: np.ndarray) -> None:
        """EMA-update the newest enrolled embedding with a fresh one."""
        if not profile.embeddings:
            return
        latest = profile.embeddings[-1]
        updated = (1 - self.update_weight) * latest + self.update_weight * emb
        norm = np.linalg.norm(updated)
        if norm > 0:
            profile.embeddings[-1] = updated / norm
            self._save_profile(profile)

    # ── persistence ────────────────────────────────────────────

    def _profile_path(self, speaker_id: str) -> Path:
        return self.profiles_dir / f"{speaker_id}.json"

    def _save_profile(self, profile: SpeakerProfile) -> None:
        profile.updated_at = time.time()
        payload = {
            "speaker_id": profile.speaker_id,
            "embeddings": [e.tolist() for e in profile.embeddings],
            "threshold_high": profile.threshold_high,
            "threshold_low": profile.threshold_low,
            "created_at": profile.created_at,
            "updated_at": profile.updated_at,
        }
        self._profile_path(profile.speaker_id).write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def _load_profiles(self) -> None:
        """
        Load enrolled profiles from disk.

        A profile whose thresholds are inverted is refused rather than loaded:
        keeping it would leave the "full" tier reachable by a low score, which
        is exactly the state the enrollment-time validation now prevents.
        """
        for file in self.profiles_dir.glob("*.json"):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
                threshold_high = float(data["threshold_high"])
                threshold_low = float(data["threshold_low"])
                speaker_id = str(data["speaker_id"])

                if not thresholds_valid(threshold_high, threshold_low, self.min_gap):
                    logger.error(
                        "sv_profile_rejected_inverted_thresholds",
                        speaker_id=speaker_id,
                        file=str(file),
                        threshold_high=threshold_high,
                        threshold_low=threshold_low,
                        detail=(
                            "T_high must exceed T_low; this profile would promote "
                            "low scores to the 'full' tier"
                        ),
                        action=(
                            f"re-enroll: python -m winvoice.enroll --speaker {speaker_id} "
                            "--samples 8 --force"
                        ),
                    )
                    continue

                profile = SpeakerProfile(
                    speaker_id=speaker_id,
                    embeddings=[np.asarray(e, dtype=np.float32) for e in data["embeddings"]],
                    threshold_high=threshold_high,
                    threshold_low=threshold_low,
                    created_at=float(data.get("created_at", time.time())),
                    updated_at=float(data.get("updated_at", time.time())),
                )
                self._profiles[profile.speaker_id] = profile
                logger.info(
                    "sv_profile_loaded",
                    speaker_id=profile.speaker_id,
                    embeddings=len(profile.embeddings),
                    threshold_high=round(threshold_high, 4),
                    threshold_low=round(threshold_low, 4),
                )
            except Exception as e:
                logger.error("sv_profile_load_failed", file=str(file), error=str(e))

    @property
    def enrolled_speakers(self) -> List[str]:
        return list(self._profiles)


class StubSvEngine(SvEngine):
    """Model-free stand-in for `--stub-audio` runs (always 'full' tier)."""

    def __init__(self, *args, **kwargs):
        self.model_path = Path(".")
        self.enabled = True
        self.threshold_high = 0.60
        self.threshold_low = 0.40
        self.adaptive_update = False
        self.update_weight = 0.05
        self.offset_high = 0.05
        self.offset_low = 0.05
        self.profiles_dir = Path(".")
        self._extractor = None
        self._ready = False
        self._profiles = {}

    async def initialize(self) -> None:
        logger.info("stub_sv_initialized")

    def verify(self, frames, speaker_id: str = "me") -> Optional[SvResult]:
        return SvResult(
            speaker_id=speaker_id,
            score=0.95,
            tier="full",
            threshold_high=self.threshold_high,
            threshold_low=self.threshold_low,
        )


def create_sv_engine(use_stub: bool = False) -> SvEngine:
    """Factory honouring the WINVOICE_STUB_AUDIO env var."""
    if use_stub or os.getenv("WINVOICE_STUB_AUDIO") == "1":
        return StubSvEngine()
    return SvEngine()
