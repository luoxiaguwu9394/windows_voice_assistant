"""
Audio I/O Stream Manager — microphone input only.

Playback deliberately does **not** live here. It used to: `play_audio()` opened an
`OutputStream` per sample rate and wrote TTS chunks straight into it, which meant
no buffer, no pause between sentences, and an 8 kHz MME stream on a 44.1 kHz
device (see `winvoice/audio/playback.py`, which replaced it). Keeping a second
way to write PCM to a device is how that defect would come back.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Optional

import sounddevice as sd

from winvoice.config import get_config
from winvoice.logging import get_logger
from winvoice.contracts import AudioFrame

logger = get_logger(__name__)


@dataclass
class StreamConfig:
    sample_rate: int = 16000
    channels: int = 1
    dtype: str = "int16"
    blocksize: int = 1600  # 100ms at 16kHz
    latency: str = "low"


class AudioStreamManager:
    """
    Manages the microphone input stream.

    - Input: Microphone → callback → AudioFrame queue
    - Output: not here — see `winvoice.audio.playback.SpeechPlayer`
    """

    def __init__(
        self,
        config: Optional[StreamConfig] = None,
        on_audio_frame: Optional[Callable[[AudioFrame], None]] = None,
    ):
        cfg = get_config()
        self.config = config or StreamConfig(
            sample_rate=cfg.get("audio.sample_rate", 16000),
            blocksize=cfg.get("audio.blocksize", 1600),
        )
        self.on_audio_frame = on_audio_frame

        self._input_stream: Optional[sd.InputStream] = None
        self._running = False
        self._frame_queue: Deque[AudioFrame] = deque(maxlen=1000)
        self._trace_id = ""

    def start(self, trace_id: str = "") -> None:
        """Start audio input stream."""
        self._trace_id = trace_id
        self._running = True

        def input_callback(indata, frames, time_info, status):
            if status:
                logger.warning("input_stream_status", status=str(status))
            if not self._running:
                return

            # Convert to bytes
            pcm_bytes = indata.tobytes()
            timestamp_ms = int(time.time() * 1000)

            frame = AudioFrame(
                trace_id=self._trace_id,
                timestamp_ms=timestamp_ms,
                data=pcm_bytes,
                sample_rate=self.config.sample_rate,
                channels=self.config.channels,
                frame_ms=int(self.config.blocksize / self.config.sample_rate * 1000),
            )

            self._frame_queue.append(frame)
            if self.on_audio_frame:
                self.on_audio_frame(frame)

        self._input_stream = sd.InputStream(
            samplerate=self.config.sample_rate,
            channels=self.config.channels,
            dtype=self.config.dtype,
            blocksize=self.config.blocksize,
            callback=input_callback,
            latency=self.config.latency,
        )
        self._input_stream.start()
        logger.info("audio_input_started", sample_rate=self.config.sample_rate, blocksize=self.config.blocksize)

    def stop(self) -> None:
        """Stop the microphone stream."""
        self._running = False
        if self._input_stream:
            self._input_stream.stop()
            self._input_stream.close()
            self._input_stream = None
        logger.info("audio_stream_stopped")

    def get_frame(self) -> Optional[AudioFrame]:
        """Get next audio frame (non-blocking)."""
        if self._frame_queue:
            return self._frame_queue.popleft()
        return None

    @property
    def is_running(self) -> bool:
        return self._running


async def create_audio_stream(
    on_audio_frame: Optional[Callable[[AudioFrame], None]] = None,
) -> AudioStreamManager:
    """Factory to create and start audio stream."""
    stream = AudioStreamManager(on_audio_frame=on_audio_frame)
    stream.start()
    return stream