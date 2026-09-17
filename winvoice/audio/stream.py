"""
Audio I/O Stream Manager.

Handles microphone input and speaker output using sounddevice.
Provides callback-based streaming for the audio pipeline.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Optional

import numpy as np
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
    Manages audio input/output streams.

    - Input: Microphone → callback → AudioFrame queue
    - Output: Speaker playback from TTS chunks
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
        self._output_stream: Optional[sd.OutputStream] = None
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
        """Stop audio streams."""
        self._running = False
        if self._input_stream:
            self._input_stream.stop()
            self._input_stream.close()
            self._input_stream = None
        if self._output_stream:
            self._output_stream.stop()
            self._output_stream.close()
            self._output_stream = None
        logger.info("audio_streams_stopped")

    def get_frame(self) -> Optional[AudioFrame]:
        """Get next audio frame (non-blocking)."""
        if self._frame_queue:
            return self._frame_queue.popleft()
        return None

    async def play_audio(self, pcm_bytes: bytes, sample_rate: int = 16000) -> None:
        """Play PCM audio to output device."""
        if self._output_stream is None or self._output_stream.samplerate != sample_rate:
            if self._output_stream:
                self._output_stream.stop()
                self._output_stream.close()
            self._output_stream = sd.OutputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="int16",
                blocksize=1600,
                latency="low",
            )
            self._output_stream.start()

        audio = np.frombuffer(pcm_bytes, dtype=np.int16)
        self._output_stream.write(audio)

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