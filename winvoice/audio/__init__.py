"""
Audio pipeline: KWS, VAD, ASR, Speaker Verification, TTS.

Every engine wraps sherpa-onnx 1.13.x and exposes a consistent interface.
Model-free stubs are provided for `--stub-audio` development runs.
"""

from ._common import (
    ModelNotFoundError,
    float32_to_pcm,
    frames_to_float32,
    read_wav,
    to_float32,
    trim_edge_silence,
)
from .kws import KwsEngine, KwsResult, StubKwsEngine, create_kws_engine
from .vad import StubVadEngine, VadEngine, VadSegment, create_vad_engine
from .asr import AsrEngine, AsrResult, StubAsrEngine, create_asr_engine
from .sv import (
    SpeakerProfile,
    StubSvEngine,
    SvEngine,
    SvResult,
    create_sv_engine,
)
from .tts import (
    StubTtsEngine,
    TtsChunk,
    TtsEngine,
    TtsModel,
    build_offline_tts,
    create_tts_engine,
    resolve_tts_model,
)
from .pipeline import AudioPipeline, PipelineContext, PipelineState
from .playback import (
    AudioOutputUnavailable,
    NullSpeechPlayer,
    PlaybackStats,
    SpeechPlayer,
    create_speech_player,
)
from .stream import AudioStreamManager, StreamConfig, create_audio_stream

__all__ = [
    # helpers
    "ModelNotFoundError",
    "to_float32",
    "frames_to_float32",
    "float32_to_pcm",
    "trim_edge_silence",
    "read_wav",
    # KWS
    "KwsEngine", "KwsResult", "StubKwsEngine", "create_kws_engine",
    # VAD
    "VadEngine", "VadSegment", "StubVadEngine", "create_vad_engine",
    # ASR
    "AsrEngine", "AsrResult", "StubAsrEngine", "create_asr_engine",
    # SV
    "SvEngine", "SvResult", "SpeakerProfile", "StubSvEngine", "create_sv_engine",
    # TTS
    "TtsEngine", "TtsChunk", "TtsModel", "StubTtsEngine", "create_tts_engine",
    "resolve_tts_model", "build_offline_tts",
    # pipeline / stream / playback
    "AudioPipeline", "PipelineState", "PipelineContext",
    "AudioStreamManager", "StreamConfig", "create_audio_stream",
    "SpeechPlayer", "NullSpeechPlayer", "PlaybackStats",
    "AudioOutputUnavailable", "create_speech_player",
]
