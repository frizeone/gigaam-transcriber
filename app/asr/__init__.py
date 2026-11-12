"""ASR-движки."""

from app.asr.base import Segment, Transcriber, TranscriberCanceled, TranscriberError, TranscriptionResult

__all__ = [
    "Segment",
    "Transcriber",
    "TranscriberCanceled",
    "TranscriberError",
    "TranscriptionResult",
]
