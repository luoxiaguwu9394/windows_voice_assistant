"""
The speech text path: normalize → segment → (pause per segment) → TTS.

Two pure functions, deliberately free of models and I/O, because the two
questions they answer are the ones that decide how the assistant *sounds*:

* `normalize_for_speech` — what is actually said (units spoken, markdown and
  Latin gone, punctuation that means a pause kept);
* `segment_for_speech` — where it is cut, and how long the silence after each
  cut is.

Everything in between (synthesis, buffering, playback) is downstream and can
only reproduce those decisions, not improve them. `scripts/show_segmentation.py`
prints both, so the rules above can be tuned without listening to anything.

See `normalize.py` for the measurement (10 ms-RMS envelope over real synthesis)
that the pause table and the trim thresholds come from.
"""

from winvoice.text.normalize import (
    DEFAULT_CONFIG,
    SpeechTextConfig,
    normalize_for_speech,
    speech_text_config,
)
from winvoice.text.segment import (
    SpeechSegment,
    pause_for_mark,
    segment_for_speech,
)

__all__ = [
    "DEFAULT_CONFIG",
    "SpeechSegment",
    "SpeechTextConfig",
    "normalize_for_speech",
    "pause_for_mark",
    "segment_for_speech",
    "speech_text_config",
]
