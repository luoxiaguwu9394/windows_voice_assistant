"""
Speaker Verification (SV) using sherpa-onnx CAM++ (3D-Speaker).

Computes embeddings and cosine similarity for enrolled speakers.
"""

from __future__ import annotations

import os
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from winvoice.config import get_config
from winvoice.logging import get_logger, observe_latency, inc_request

logger = get_logger(__name__)


@dataclass
class SvResult:
    speaker_id: str
    score: float
    tier: str  # "full", "guest", "rejected"
    threshold_high: float
    threshold_low: float


@dataclass
class SpeakerProfile:
    """Enrolled speaker with embeddings and thresholds."""
    speaker_id: str
    embeddings: List[np.ndarray] = field(default_factory=list)
    threshold_high: float = 0.60
    threshold_low: float = 0.40
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class SvEngine:
    """
    Speaker verification engine.

    - Enroll: collect 8 samples, compute embeddings, derive thresholds
    - Verify: compute embedding, cosine similarity vs enrolled, decide tier
    - Adaptive update: on successful verify, update embedding with weight
    """

    def __init__(
        self,
        model_path: Optional[str] = None,
        enabled: bool = True,
        threshold_high: Optional[float] = None,
        threshold_low: Optional[float] = None,
        adaptive_update: bool = True,
        update_weight: float = 0.05,
        profiles_dir: Optional[str] = None,
    ):
        cfg = get_config()
        self.model_path = model_path or cfg.get("sv.model", "models/sv/campplus")
        self.enabled = enabled and cfg.get("sv.enabled", True)
        self.threshold_high = threshold_high or cfg.get("sv.threshold_high", 0.60)
        self.threshold_low = threshold_low or cfg.get("sv.threshold_low", 0.40)
        self.adaptive_update = adaptive_update and cfg.get("sv.adaptive_update", True)
        self.update_weight = update_weight or cfg.get("sv.update_weight", 0.05)
        self.profiles_dir = Path(profiles_dir or cfg.get("snapshot.path", "snapshots")) / "sv_profiles"
        self.profiles_dir.mkdir(parents=True, exist_ok=True)

        self._extractor = None
        self._profiles: Dict[str, SpeakerProfile] = {}
        self._load_profiles()

    async def initialize(self) -> None:
        if not self.enabled:
            logger.info("sv_disabled")
            return

        try:
            import sherpa_onnx
        except ImportError:
            logger.warning("sherpa_onnx not installed, using stub SV")
            return

        if not Path(self.model_path).exists():
            raise FileNotFoundError(f"SV model not found: {self.model_path}")

        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(self.model_path)
        logger.info("sv_initialized", model=self.model_path)

    def _load_profiles(self) -> None:
        """Load enrolled speaker profiles from disk."""
        for profile_file in self.profiles_dir.glob("*.json"):
            try:
                with open(profile_file, "r") as f:
                    data = json.load(f)
                profile = SpeakerProfile(
                    speaker_id=data["speaker_id"],
                    embeddings=[np.array(e, dtype=np.float32) for e in data["embeddings"]],
                    threshold_high=data["threshold_high"],
                    threshold_low=data["threshold_low"],
                    created_at=data["created_at"],
                    updated_at=data["updated_at"],
                )
                self._profiles[profile.speaker_id] = profile
            except Exception as e:
                logger.error("sv_profile_load_failed", file=str(profile_file), error=str(e))

    def _save_profile(self, profile: SpeakerProfile) -> None:
        profile.updated_at = time.time()
        path = self.profiles_dir / f"{profile.speaker_id}.json"
        data = {
            "speaker_id": profile.speaker_id,
            "embeddings": [e.tolist() for e in profile.embeddings],
            "threshold_high": profile.threshold_high,
            "threshold_low": profile.threshold_low,
            "created_at": profile.created_at,
            "updated_at": profile.updated_at,
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def enroll_start(self, speaker_id: str) -> None:
        """Begin enrollment for a new speaker."""
        if speaker_id in self._profiles:
            raise ValueError(f"Speaker {speaker_id} already enrolled")
        self._profiles[speaker_id] = SpeakerProfile(speaker_id=speaker_id)
        logger.info("sv_enroll_started", speaker_id=speaker_id)

    def enroll_sample(self, speaker_id: str, audio_frames: List[AudioFrame]) -> bool:
        """Add one enrollment sample (3-5 seconds)."""
        if speaker_id not in self._profiles:
            raise ValueError(f"Enrollment not started for {speaker_id}")

        embedding = self._compute_embedding(audio_frames)
        if embedding is None:
            return False

        self._profiles[speaker_id].embeddings.append(embedding)
        logger.info("sv_enroll_sample_added", speaker_id=speaker_id, count=len(self._profiles[speaker_id].embeddings))
        return True

    def enroll_finalize(self, speaker_id: str, max_inter: float = 0.45) -> SpeakerProfile:
        """
        Finalize enrollment: compute thresholds from intra-speaker similarities.
        max_inter: estimated max similarity to other speakers (from AISHELL-3/CN-Celeb)
        """
        profile = self._profiles[speaker_id]
        embeddings = profile.embeddings

        if len(embeddings) < 8:
            raise ValueError(f"Need at least 8 samples, got {len(embeddings)}")

        # Compute pairwise cosine similarities
        sims = []
        for i in range(len(embeddings)):
            for j in range(i + 1, len(embeddings)):
                sim = np.dot(embeddings[i], embeddings[j]) / (
                    np.linalg.norm(embeddings[i]) * np.linalg.norm(embeddings[j])
                )
                sims.append(sim)

        min_intra = min(sims) if sims else 0.0
        logger.info("sv_enroll_stats", speaker_id=speaker_id, min_intra=min_intra, max_inter=max_inter)

        if min_intra < 0.4:
            raise ValueError(f"Intra-speaker similarity too low (min_intra={min_intra:.3f}), please re-record")

        # Configurable offsets (default 0.05)
        profile.threshold_high = min_intra - 0.05
        profile.threshold_low = max_inter + 0.05

        self._save_profile(profile)
        logger.info("sv_enroll_finalized", speaker_id=speaker_id,
                   threshold_high=profile.threshold_high, threshold_low=profile.threshold_low)
        return profile

    def verify(self, audio_frames: List[AudioFrame], speaker_id: str = "me") -> Optional[SvResult]:
        """Verify audio against enrolled speaker."""
        if not self.enabled or speaker_id not in self._profiles:
            return None

        embedding = self._compute_embedding(audio_frames)
        if embedding is None:
            return None

        profile = self._profiles[speaker_id]
        if not profile.embeddings:
            return None

        # Compute max cosine similarity to enrolled embeddings
        max_sim = max(
            np.dot(embedding, e) / (np.linalg.norm(embedding) * np.linalg.norm(e))
            for e in profile.embeddings
        )

        # Determine tier
        if max_sim >= profile.threshold_high:
            tier = "full"
        elif max_sim >= profile.threshold_low:
            tier = "guest"
        else:
            tier = "rejected"

        # Adaptive update on successful verification (full or guest)
        if self.adaptive_update and tier in ("full", "guest"):
            self._adaptive_update(profile, embedding)

        return SvResult(
            speaker_id=speaker_id,
            score=float(max_sim),
            tier=tier,
            threshold_high=profile.threshold_high,
            threshold_low=profile.threshold_low,
        )

    def _compute_embedding(self, frames: List[AudioFrame]) -> Optional[np.ndarray]:
        if self._extractor is None:
            return self._stub_embedding(frames)

        import sherpa_onnx
        stream = self._extractor.create_stream()

        for frame in frames:
            audio = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32) / 32768.0
            stream.accept_waveform(frame.sample_rate, audio)

        embedding = stream.compute()
        return np.array(embedding, dtype=np.float32)

    def _stub_embedding(self, frames: List[AudioFrame]) -> np.ndarray:
        """Deterministic stub embedding based on audio hash."""
        import hashlib
        data = b"".join(f.data for f in frames)
        hash_bytes = hashlib.md5(data).digest()
        # Convert to pseudo-embedding on unit sphere
        vec = np.frombuffer(hash_bytes, dtype=np.uint8).astype(np.float32) / 255.0
        vec = vec[:192]  # CAM++ embedding dim
        if len(vec) < 192:
            vec = np.pad(vec, (0, 192 - len(vec)))
        return vec / np.linalg.norm(vec)

    def _adaptive_update(self, profile: SpeakerProfile, new_embedding: np.ndarray) -> None:
        """Exponential moving average update of the latest embedding."""
        if not profile.embeddings:
            return
        # Update the most recent embedding
        latest = profile.embeddings[-1]
        updated = (1 - self.update_weight) * latest + self.update_weight * new_embedding
        updated = updated / np.linalg.norm(updated)
        profile.embeddings[-1] = updated
        self._save_profile(profile)
        logger.debug("sv_adaptive_update", speaker_id=profile.speaker_id)


class StubSvEngine(SvEngine):
    async def initialize(self) -> None:
        logger.info("stub_sv_initialized")


def create_sv_engine(use_stub: bool = False) -> SvEngine:
    if use_stub or os.getenv("WINVOICE_STUB_AUDIO") == "1":
        return StubSvEngine()
    return SvEngine()