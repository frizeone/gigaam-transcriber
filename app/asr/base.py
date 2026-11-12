"""Абстракция ASR-движка."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class Segment:
    """Распознанный фрагмент речи."""

    start: float
    end: float
    text: str


@dataclass(slots=True)
class TranscriptionResult:
    """Результат распознавания файла."""

    segments: list[Segment] = field(default_factory=list)
    model: str = ""
    device: str = ""
    language: str = "ru"
    peak_vram_mb: float | None = None
    duration_s: float = 0.0


class TranscriberError(Exception):
    """Ошибка ASR с машинным кодом и понятным сообщением."""

    def __init__(self, code: str, message: str, *, transient: bool = False) -> None:
        self.code = code
        self.message = message
        self.transient = transient
        super().__init__(message)


class TranscriberCanceled(Exception):
    """Распознавание прервано отменой или остановкой воркера."""

    def __init__(self, code: str = "canceled", message: str = "Задача отменена.") -> None:
        self.code = code
        self.message = message
        super().__init__(message)


ProgressCallback = Callable[[float], None]
StopCallback = Callable[[], bool]


@runtime_checkable
class Transcriber(Protocol):
    """Интерфейс движка распознавания."""

    @property
    def model_name(self) -> str: ...

    @property
    def device(self) -> str: ...

    @property
    def is_loaded(self) -> bool: ...

    def load(self) -> None: ...

    def transcribe(
        self,
        wav_path: Path,
        duration_s: float,
        *,
        on_progress: ProgressCallback | None = None,
        on_scan_progress: ProgressCallback | None = None,
        should_stop: StopCallback | None = None,
    ) -> TranscriptionResult: ...

    def close(self) -> None: ...


def validate_segments(segments: list[Segment]) -> list[Segment]:
    """Отфильтровать пустые и пересекающиеся сегменты, сохранить порядок."""
    cleaned: list[Segment] = []
    last_end = 0.0
    for segment in sorted(segments, key=lambda item: (item.start, item.end)):
        text = " ".join(segment.text.split())
        if not text:
            continue
        start = max(0.0, min(segment.start, segment.end))
        end = max(start, segment.end)
        if end - start < 0.05:
            continue
        if cleaned and start < last_end - 0.05:
            start = last_end
        if end <= start:
            continue
        cleaned.append(Segment(start=start, end=end, text=text))
        last_end = end
    return cleaned
