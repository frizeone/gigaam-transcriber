"""GigaAM v3 E2E через onnx-asr: потоковый VAD + батчевое распознавание.

Полный файл не загружается в память: VAD идёт блоками по ASR_STREAM_CHUNK_S,
а для каждого речевого сегмента из WAV читается только его фрагмент.
"""

from __future__ import annotations

import gc
import logging
import os
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
from filelock import FileLock

from app.asr.base import (
    ProgressCallback,
    Segment,
    StopCallback,
    TranscriberCanceled,
    TranscriberError,
    TranscriptionResult,
    validate_segments,
)
from app.config import Settings
from app.gpu import GpuMonitor
from app.media import WavReader

logger = logging.getLogger(__name__)


class OnnxGigaAM:
    """Локальный ASR-движок GigaAM v3 E2E (ONNX Runtime)."""

    def __init__(self, settings: Settings, model_name: str | None = None) -> None:
        self._settings = settings
        self._model_name = model_name or settings.asr_model
        self._model: Any | None = None
        self._vad: Any | None = None
        self._loaded = False
        self._device = settings.asr_device
        self._providers: list[str] = []
        self._cuda_available = False
        self._monitor: GpuMonitor | None = GpuMonitor() if settings.asr_device == "cuda" else None
        self._batch_size = settings.effective_asr_batch
        self._load_error: str | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def device(self) -> str:
        return self._device

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def cuda_available(self) -> bool:
        return self._cuda_available

    @property
    def providers(self) -> list[str]:
        return list(self._providers)

    @property
    def peak_vram_mb(self) -> float | None:
        return self._monitor.peak_mb if self._monitor else None

    def _configure_environment(self) -> None:
        os.environ["HF_HOME"] = str(self._settings.hf_home)
        os.environ["HF_HUB_OFFLINE"] = "1" if self._settings.hf_hub_offline else "0"
        if self._settings.asr_backend == "onnx":
            os.environ.setdefault("OMP_NUM_THREADS", str(max(1, os.cpu_count() or 1)))

    def _load_onnx_model(self, *, use_cuda: bool, quantization: str | None) -> Any:
        import onnx_asr

        providers = (
            ["CUDAExecutionProvider", "CPUExecutionProvider"]
            if use_cuda
            else ["CPUExecutionProvider"]
        )
        logger.info(
            "asr_model_loading",
            extra={"model": self._model_name, "event": ",".join(providers), "status": "loading"},
        )
        return onnx_asr.load_model(
            self._model_name,
            providers=providers,
            quantization=quantization,
        )

    def _load_vad(self) -> Any:
        import onnx_asr

        return onnx_asr.load_vad("silero", providers=["CPUExecutionProvider"])

    def load(self) -> None:
        """Загрузить модель и VAD. При недоступной CUDA — откатиться на CPU."""
        self._configure_environment()
        # Тот же lock, что у models-init: не качаем гигабайты параллельно.
        lock = FileLock(str(self._settings.preload_lock_file))
        with lock:
            self._load_locked()

    def _load_locked(self) -> None:
        requested_cuda = self._settings.asr_device == "cuda"
        quantization = self._settings.asr_quantization
        try:
            self._model = self._load_onnx_model(use_cuda=requested_cuda, quantization=quantization)
            self._device = "cuda" if requested_cuda else "cpu"
            self._cuda_available = requested_cuda
            self._providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if requested_cuda else [
                "CPUExecutionProvider"
            ]
        except Exception as exc:
            if not requested_cuda:
                self._load_error = str(exc)
                raise TranscriberError(
                    "model_load_failed",
                    "Не удалось загрузить модель GigaAM. Проверьте volume models и выполните make init. "
                    f"Детали: {str(exc)[:300]}",
                ) from exc
            logger.warning(
                "cuda_unavailable_fallback_cpu",
                extra={"event": str(exc)[:500], "model": self._model_name},
            )
            self._load_error = str(exc)
            quantization = self._settings.asr_quantization or "int8"
            self._model = self._load_onnx_model(use_cuda=False, quantization=quantization)
            self._device = "cpu"
            self._cuda_available = False
            self._providers = ["CPUExecutionProvider"]

        self._vad = self._load_vad()
        self._batch_size = self._settings.asr_batch if self._device == "cuda" else 1
        self._warmup()
        self._refresh_active_providers()
        self._loaded = True
        logger.info(
            "asr_model_ready",
            extra={
                "model": self._model_name,
                "status": "ready",
                "event": ",".join(self._providers),
            },
        )

    def _refresh_active_providers(self) -> None:
        """Уточнить реальные провайдеры сессии (ORT может откатиться на CPU)."""
        asr = getattr(self._model, "asr", None)
        for attribute in ("_encoder", "_model"):
            session = getattr(asr, attribute, None)
            if session is not None and hasattr(session, "get_providers"):
                providers = list(session.get_providers())
                self._providers = providers
                if "CUDAExecutionProvider" not in providers:
                    self._device = "cpu"
                    self._cuda_available = False
                    self._batch_size = 1
                return

    def _warmup(self) -> None:
        """Прогреть CUDA-ядра и декодер на секунде тишины."""
        assert self._model is not None
        try:
            self._model.recognize(np.zeros(16_000, dtype=np.float32), sample_rate=16_000)
            if self._monitor:
                self._monitor.sample()
        except Exception as exc:
            if self._device == "cuda":
                logger.warning("cuda_warmup_failed_fallback_cpu", extra={"event": str(exc)[:500]})
                self._model = None
                gc.collect()
                quantization = self._settings.asr_quantization or "int8"
                self._model = self._load_onnx_model(use_cuda=False, quantization=quantization)
                self._device = "cpu"
                self._cuda_available = False
                self._providers = ["CPUExecutionProvider"]
                self._model.recognize(np.zeros(16_000, dtype=np.float32), sample_rate=16_000)
            else:
                logger.warning("asr_warmup_failed", extra={"event": str(exc)[:500]})

    def _check_stop(self, should_stop: StopCallback | None) -> None:
        if should_stop and should_stop():
            raise TranscriberCanceled()

    def _detect_speech(
        self,
        reader: WavReader,
        *,
        on_scan_progress: ProgressCallback | None,
        should_stop: StopCallback | None,
    ) -> list[tuple[float, float]]:
        assert self._vad is not None
        chunk_frames = max(1, self._settings.asr_stream_chunk_s * reader.sample_rate)
        max_speech = float(self._settings.asr_vad_max_speech_s)
        intervals: list[list[float]] = []
        start_frame = 0
        while start_frame < reader.frames:
            self._check_stop(should_stop)
            end_frame = min(reader.frames, start_frame + chunk_frames)
            block_start = start_frame / reader.sample_rate
            block_end = end_frame / reader.sample_rate
            waveform = reader.read_seconds(block_start, block_end)
            if waveform.size == 0:
                break
            batch = np.asarray(waveform[np.newaxis, :], dtype=np.float32)
            lengths = np.array([batch.shape[1]], dtype=np.int64)
            iterator = self._vad.segment_batch(
                batch,
                lengths,
                16_000,
                batch_size=1,
                threshold=self._settings.asr_vad_threshold,
                min_speech_duration_ms=self._settings.asr_vad_min_speech_ms,
                max_speech_duration_s=self._settings.asr_vad_max_speech_s,
                min_silence_duration_ms=self._settings.asr_vad_min_silence_ms,
                speech_pad_ms=self._settings.asr_vad_speech_pad_ms,
            )
            raw_segments = list(next(iter(iterator)))
            for raw_start, raw_end in raw_segments:
                abs_start = block_start + raw_start / reader.sample_rate
                abs_end = block_start + raw_end / reader.sample_rate
                if (
                    intervals
                    and abs_start <= intervals[-1][1] + 0.05
                    and intervals[-1][1] >= block_end - 0.1
                    and abs_end - intervals[-1][0] <= max_speech + 0.01
                ):
                    intervals[-1][1] = max(intervals[-1][1], abs_end)
                else:
                    intervals.append([abs_start, abs_end])
            start_frame = end_frame
            if on_scan_progress:
                on_scan_progress(block_end)
        self._check_stop(should_stop)
        return [(start, end) for start, end in intervals if end - start >= 0.1]

    def transcribe(
        self,
        wav_path: Path,
        duration_s: float,
        *,
        on_progress: ProgressCallback | None = None,
        on_scan_progress: ProgressCallback | None = None,
        should_stop: StopCallback | None = None,
    ) -> TranscriptionResult:
        if not self._loaded or self._model is None or self._vad is None:
            raise TranscriberError("model_not_loaded", "Модель ещё не загружена.")
        if on_progress:
            on_progress(0.0)
        if self._monitor:
            self._monitor.sample()
        try:
            with WavReader(wav_path) as reader:
                intervals = self._detect_speech(
                    reader,
                    on_scan_progress=on_scan_progress,
                    should_stop=should_stop,
                )
                segments: list[Segment] = []
                position = 0
                while position < len(intervals):
                    self._check_stop(should_stop)
                    batch_intervals = intervals[position : position + self._batch_size]
                    waveforms: list[npt.NDArray[np.float32]] = []
                    valid_intervals: list[tuple[float, float]] = []
                    for start, end in batch_intervals:
                        samples = reader.read_seconds(start, end)
                        if samples.size < 160:  # < 10 мс
                            continue
                        waveforms.append(samples)
                        valid_intervals.append((start, end))
                    if waveforms:
                        texts = self._model.recognize(waveforms, sample_rate=16_000)
                        if isinstance(texts, str):
                            texts = [texts]
                        for (start, end), text in zip(valid_intervals, texts, strict=False):
                            segments.append(Segment(start=start, end=end, text=str(text)))
                    position += len(batch_intervals)
                    processed = valid_intervals[-1][1] if valid_intervals else (
                        batch_intervals[-1][1] if batch_intervals else 0.0
                    )
                    if self._monitor:
                        self._monitor.sample()
                    if on_progress:
                        on_progress(processed)
                if on_progress:
                    on_progress(duration_s)
            cleaned = validate_segments(segments)
            return TranscriptionResult(
                segments=cleaned,
                model=self._model_name,
                device=self._device,
                duration_s=duration_s,
                peak_vram_mb=self._monitor.peak_mb if self._monitor else None,
            )
        except TranscriberCanceled:
            raise
        except TranscriberError:
            raise
        except Exception as exc:
            logger.exception(
                "asr_transcription_failed",
                extra={"model": self._model_name, "event": str(exc)[:500]},
            )
            hint = ""
            text = str(exc).lower()
            if "cudnn" in text or "cuda" in text:
                hint = (
                    " Похоже на проблему CUDA/cuDNN: проверьте NVIDIA Container Toolkit, "
                    "драйвер и вывод `docker run --rm --gpus all ... nvidia-smi`."
                )
            elif "onnx" in text or "model" in text:
                hint = " Проверьте, что веса загружены: make init."
            raise TranscriberError(
                "asr_failed",
                f"Ошибка распознавания речи.{hint}",
            ) from exc

    def close(self) -> None:
        self._model = None
        self._vad = None
        self._loaded = False
        if self._monitor:
            self._monitor.shutdown()
        gc.collect()
