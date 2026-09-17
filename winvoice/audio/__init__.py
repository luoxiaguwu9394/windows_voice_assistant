"""
Audio pipeline: KWS, VAD, ASR, Speaker Verification, TTS.

All classes provide a consistent async interface for the single-process prototype.
Real implementations use sherpa-onnx; stubs provided for testing without models.
"""

from .kws import KwsEngine, KwsResult, create_kws_engine
from .vad import VadEngine, VadSegment, create_vad_engine
from .asr import AsrEngine, AsrResult, create_asr_engine
from .sv import SvEngine, SvResult, create_sv_engine
from .tts import TtsEngine, TtsChunk, create_tts_engine
from .pipeline import AudioPipeline, PipelineState, PipelineContext
from .stream import AudioStreamManager, StreamConfig, create_audio_stream

__all__ = [
    "KwsEngine", "KwsResult", "create_kws_engine",
    "VadEngine", "VadSegment", "create_vad_engine",
    "AsrEngine", "AsrResult", "create_asr_engine",
    "SvEngine", "SvResult", "create_sv_engine",
    "TtsEngine", "TtsChunk", "create_tts_engine",
    "AudioPipeline", "PipelineState", "PipelineContext",
    "AudioStreamManager", "StreamConfig", "create_audio_stream",
]