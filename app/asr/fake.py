"""FakeTranscriber: детерминированный движок для тестов и CI."""

from __future__ import annotations

import time
from pathlib import Path

from app.asr.base import ProgressCallback, Segment, StopCallback, TranscriberCanceled, TranscriptionResult
from app.media import WavReader

_FAKE_TEXTS = (
    "Это тестовая расшифровка аудиозаписи.",
    "Сервис распознаёт русскую речь полностью локально.",
    "Данные не отправляются во внешние облачные сервисы.",
    "Результат сохраняется в документ формата DOCX.",
    "Кириллица, пунктуация и заглавные буквы поддерживаются.",
)


class FakeTranscriber:
    """Не использует GPU и сеть; подходит для интеграционных тестов."""

    def __init__(
        self,
        *,
        model_name: str = "fake",
        device: str = "cpu",
        empty: bool = False,
        fail: bool = False,
        delay_s: float = 0.0,
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._empty = empty
        self._fail = fail
        self._delay_s = delay_s
        self._loaded = False

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def device(self) -> str:
        return self._device

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        self._loaded = True

    def transcribe(
        self,
        wav_path: Path,
        duration_s: float,
        *,
        on_progress: ProgressCallback | None = None,
        on_scan_progress: ProgressCallback | None = None,
        should_stop: StopCallback | None = None,
    ) -> TranscriptionResult:
        if not self._loaded:
            self.load()
        if self._fail:
            raise RuntimeError("FakeTranscriber: тестовая ошибка распознавания")
        with WavReader(wav_path) as reader:
            real_duration = min(duration_s, reader.duration_s)
        if on_scan_progress:
            on_scan_progress(real_duration)
        if self._empty or real_duration <= 0:
            if on_progress:
                on_progress(real_duration)
            return TranscriptionResult(
                segments=[],
                model=self._model_name,
                device=self._device,
                duration_s=real_duration,
            )

        step = max(4.0, real_duration / max(1, min(12, int(real_duration // 15) + 1)))
        segments: list[Segment] = []
        position = 0.0
        index = 0
        while position < real_duration:
            if should_stop and should_stop():
                raise TranscriberCanceled()
            end = min(real_duration, position + step)
            text = _FAKE_TEXTS[index % len(_FAKE_TEXTS)]
            segments.append(Segment(start=position, end=end, text=text))
            index += 1
            position = end
            if on_progress:
                on_progress(end)
            if self._delay_s:
                time.sleep(self._delay_s)
        return TranscriptionResult(
            segments=segments,
            model=self._model_name,
            device=self._device,
            duration_s=real_duration,
        )

    def close(self) -> None:
        self._loaded = False
