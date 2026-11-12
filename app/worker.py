"""GPU-воркер: один процесс, одна очередь, модель в памяти между задачами."""

from __future__ import annotations

import contextlib
import gc
import json
import logging
import os
import signal
import socket
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.asr.base import (
    Transcriber,
    TranscriberCanceled,
    TranscriberError,
    TranscriptionResult,
)
from app.asr.fake import FakeTranscriber
from app.asr.onnx_gigaam import OnnxGigaAM
from app.config import Settings, get_settings
from app.db import JobStore, utcnow_iso
from app.docx_render import render_docx
from app.errors import AppError
from app.jobs import (
    build_preview,
    make_docx_filename,
    remove_intermediate_files,
    remove_job_files,
)
from app.logging_conf import setup_logging
from app.media import (
    MediaCanceledError,
    convert_to_wav,
    format_duration_hms,
    probe_media,
)

logger = logging.getLogger(__name__)

TranscriberFactory = Callable[[Settings, str], Transcriber]


def default_transcriber_factory(settings: Settings, model_name: str) -> Transcriber:
    if settings.asr_backend == "fake":
        return FakeTranscriber(model_name=model_name, device="cpu")
    return OnnxGigaAM(settings, model_name=model_name)


class Worker:
    """Обрабатывает задачи по одной, обновляет SQLite и держит модели в кеше."""

    def __init__(
        self,
        settings: Settings,
        *,
        store: JobStore | None = None,
        transcriber_factory: TranscriberFactory | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or JobStore(settings.db_path)
        self._factory = transcriber_factory or default_transcriber_factory
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._stop_event = threading.Event()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._current_job_id: str | None = None
        self._transcribers: OrderedDict[str, Transcriber] = OrderedDict()
        self._started = False
        self._last_cleanup = 0.0

    # ── Жизненный цикл ──────────────────────────────────────────────────────

    def startup(self) -> None:
        if self._started:
            return
        self.settings.ensure_directories()
        self.store.initialize()
        self.store.register_worker(self.worker_id, version=self.settings.app_version)
        failed = self.store.fail_active_jobs_of_other_workers(self.worker_id)
        if failed:
            logger.warning(
                "stale_jobs_failed_on_start",
                extra={"worker_id": self.worker_id, "event": f"count={failed}"},
            )
        self._started = True
        self._start_heartbeat()
        logger.info(
            "worker_started",
            extra={"worker_id": self.worker_id, "status": "starting"},
        )
        self._cleanup_expired()
        # Первичная загрузка модели; ошибка не убивает воркер — он повторит попытку.
        self._try_load_default()

    def shutdown(self) -> None:
        self._stop_event.set()
        self._heartbeat_stop.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=3)
        for transcriber in self._transcribers.values():
            try:
                transcriber.close()
            except Exception:
                logger.exception("transcriber_close_failed")
        self._transcribers.clear()
        gc.collect()
        if self._started:
            try:
                self.store.update_worker_state(
                    status="stopped",
                    message="Воркер остановлен",
                    heartbeat_at=utcnow_iso(),
                )
            except Exception:
                logger.exception("worker_state_stop_failed")
        self._started = False
        logger.info("worker_stopped", extra={"worker_id": self.worker_id, "status": "stopped"})

    def _start_heartbeat(self) -> None:
        self._heartbeat_stop.clear()

        def loop() -> None:
            while not self._heartbeat_stop.wait(self.settings.worker_heartbeat_s):
                try:
                    self.store.heartbeat(self.worker_id, self._current_job_id)
                except Exception:
                    logger.exception("heartbeat_failed")

        self._heartbeat_thread = threading.Thread(target=loop, name="worker-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    # ── Модели ──────────────────────────────────────────────────────────────

    def _loaded_models(self) -> list[str]:
        return list(self._transcribers.keys())

    def _publish_ready(self, active: Transcriber) -> None:
        message = "Модель загружена"
        if self.settings.asr_device == "cuda" and active.device != "cuda":
            message = "CUDA недоступна — работа на CPU (int8)"
        self.store.update_worker_state(
            status="ready",
            asr_model=self.settings.asr_model,
            device=active.device,
            cuda_available=int(active.device == "cuda"),
            providers=",".join(getattr(active, "providers", []) or []),
            message=message,
            loaded_models=json.dumps(self._loaded_models(), ensure_ascii=False),
            heartbeat_at=utcnow_iso(),
        )

    def _try_load_default(self) -> bool:
        if self.settings.asr_model in self._transcribers:
            return True
        self.store.update_worker_state(
            status="loading",
            message=f"Загрузка модели {self.settings.asr_model}…",
            heartbeat_at=utcnow_iso(),
        )
        try:
            transcriber = self._load_model(self.settings.asr_model)
        except Exception as exc:
            message = str(exc)
            logger.exception("default_model_load_failed", extra={"model": self.settings.asr_model})
            self.store.update_worker_state(
                status="error",
                asr_model=self.settings.asr_model,
                message=(
                    "Не удалось загрузить модель. Проверьте интернет/volume models и выполните make init. "
                    f"Детали: {message[:300]}"
                ),
                heartbeat_at=utcnow_iso(),
            )
            return False
        self._publish_ready(transcriber)
        return True

    def _load_model(self, model_name: str) -> Transcriber:
        if model_name in self._transcribers:
            self._transcribers.move_to_end(model_name)
            return self._transcribers[model_name]
        while len(self._transcribers) >= self.settings.max_cached_models:
            evicted_name, evicted = next(
                (
                    (name, model)
                    for name, model in self._transcribers.items()
                    if name != self.settings.asr_model
                ),
                (None, None),
            )
            if evicted_name is None or evicted is None:
                break
            logger.info("model_evicted", extra={"model": evicted_name})
            self._transcribers.pop(evicted_name, None)
            evicted.close()
            gc.collect()
        logger.info("model_loading", extra={"model": model_name})
        transcriber = self._factory(self.settings, model_name)
        transcriber.load()
        self._transcribers[model_name] = transcriber
        if model_name != self.settings.asr_model:
            self.store.update_worker_state(
                message=f"Дополнительная модель {model_name} загружена",
                loaded_models=json.dumps(self._loaded_models(), ensure_ascii=False),
            )
        return transcriber

    def _get_transcriber(self, model_name: str) -> Transcriber:
        if model_name not in self._transcribers:
            self.store.update_worker_state(
                message=f"Загрузка модели {model_name}…",
                heartbeat_at=utcnow_iso(),
            )
            self._load_model(model_name)
            active = self._transcribers.get(self.settings.asr_model)
            if active is not None:
                self._publish_ready(active)
        transcriber = self._transcribers[model_name]
        self._transcribers.move_to_end(model_name)
        return transcriber

    # ── Основной цикл ───────────────────────────────────────────────────────

    def run_forever(self) -> None:
        self._install_signal_handlers()
        self.startup()
        try:
            while not self._stop_event.is_set():
                if not self._try_load_default():
                    self._stop_event.wait(15)
                    continue
                row = self.store.claim_next(self.worker_id)
                if row is None:
                    self._periodic_cleanup()
                    self._stop_event.wait(self.settings.worker_poll_interval_s)
                    continue
                try:
                    self.process_job(row)
                except Exception:
                    logger.exception("job_processing_crashed", extra={"job_id": row.get("id")})
                    try:
                        self._fail_job(
                            row["id"],
                            "internal_error",
                            "Внутренняя ошибка обработки. Подробности в журнале.",
                        )
                    except Exception:
                        logger.exception("job_failure_marking_failed", extra={"job_id": row.get("id")})
        finally:
            self.shutdown()

    def run_once(self) -> bool:
        """Обработать одну задачу (для тестов). True — если задача была."""
        if not self._started:
            self.startup()
        if not self._try_load_default():
            return False
        row = self.store.claim_next(self.worker_id)
        if row is None:
            return False
        self.process_job(row)
        return True

    def _install_signal_handlers(self) -> None:
        def handler(signum: int, frame: object) -> None:
            if not self._stop_event.is_set():
                logger.info(
                    "worker_signal_received",
                    extra={"event": f"signal={signum}", "status": "stopping"},
                )
            self._stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)

    # ── Обработка одной задачи ──────────────────────────────────────────────

    def _should_abort(self, job_id: str) -> bool:
        if self._stop_event.is_set():
            return True
        row = self.store.get_job(job_id)
        return row is None or bool(row.get("cancel_requested"))

    def _abort_reason(self, job_id: str) -> tuple[str, str]:
        if self._stop_event.is_set():
            return (
                "worker_shutdown",
                "Обработка прервана остановкой сервиса. Запустите задачу повторно.",
            )
        row = self.store.get_job(job_id)
        if row is None:
            return "job_deleted", "Задача удалена."
        if row.get("delete_after_finish"):
            return "job_deleted", "Задача удалена."
        return "canceled", "Задача отменена пользователем."

    def _mark_aborted(self, job_id: str) -> None:
        code, message = self._abort_reason(job_id)
        row = self.store.get_job(job_id)
        if row is None:
            return
        if code in {"job_deleted", "canceled"} and not self._stop_event.is_set():
            if row.get("delete_after_finish"):
                try:
                    remove_job_files(row, self.settings)
                except Exception:
                    logger.exception("job_files_cleanup_failed", extra={"job_id": job_id})
                self.store.delete_job(job_id)
                logger.info("job_canceled_deleted", extra={"job_id": job_id})
            else:
                self.store.set_canceled(job_id)
                try:
                    remove_job_files(row, self.settings)
                    self.store.clear_paths(job_id)
                except Exception:
                    logger.exception("job_files_cleanup_failed", extra={"job_id": job_id})
                logger.info("job_canceled", extra={"job_id": job_id})
            return
        self.store.set_failed(job_id, code=code, message=message, stage_message="Прервано")
        try:
            remove_job_files(row, self.settings)
            self.store.clear_paths(job_id)
        except Exception:
            logger.exception("job_files_cleanup_failed", extra={"job_id": job_id})
        logger.warning("job_aborted", extra={"job_id": job_id, "code": code})

    def _fail_job(self, job_id: str, code: str, message: str) -> None:
        row = self.store.get_job(job_id)
        self.store.set_failed(job_id, code=code, message=message, stage_message="Ошибка")
        if row is not None:
            try:
                remove_job_files(row, self.settings)
                self.store.clear_paths(job_id)
            except Exception:
                logger.exception("job_files_cleanup_failed", extra={"job_id": job_id})
        logger.error("job_failed", extra={"job_id": job_id, "code": code, "event": message[:500]})

    def process_job(self, row: dict[str, Any]) -> None:
        job_id = row["id"]
        started = time.monotonic()
        self._current_job_id = job_id
        logger.info(
            "job_started",
            extra={
                "job_id": job_id,
                "model": row["model"],
                "stage": "extracting",
                "duration_s": round(float(row.get("duration_s") or 0), 1),
            },
        )
        try:
            if self._should_abort(job_id):
                self._mark_aborted(job_id)
                return
            source_raw = row.get("source_path")
            if not source_raw or not Path(source_raw).is_file():
                self._fail_job(job_id, "source_missing", "Исходный файл не найден. Загрузите его повторно.")
                return
            source = Path(source_raw)
            self.store.update_job(
                job_id,
                status="extracting",
                progress=0,
                processed_seconds=0,
                stage_message="Проверка файла",
                heartbeat_at=utcnow_iso(),
            )
            try:
                info = probe_media(source, self.settings)
            except AppError as exc:
                self._fail_job(job_id, exc.code, exc.message)
                return
            except Exception as exc:
                logger.exception("probe_failed", extra={"job_id": job_id})
                self._fail_job(job_id, "probe_failed", f"Не удалось прочитать файл: {str(exc)[:200]}")
                return
            if info.duration_s > self.settings.max_duration_s + 0.5:
                self._fail_job(
                    job_id,
                    "duration_exceeded",
                    f"Длительность {info.duration_s / 60:.1f} мин превышает лимит "
                    f"{self.settings.max_duration_min} мин.",
                )
                return
            self.store.update_job(job_id, duration_s=info.duration_s)

            wav_path = self.settings.work_dir / f"{job_id}.wav"

            def on_extract_progress(seconds: float) -> None:
                percent = int(min(100, seconds / max(info.duration_s, 0.001) * 100))
                self.store.update_job(
                    job_id,
                    stage_message=f"Извлечение аудио: {percent}%",
                    heartbeat_at=utcnow_iso(),
                )

            try:
                convert_to_wav(
                    source,
                    wav_path,
                    self.settings,
                    duration_s=info.duration_s,
                    on_progress=on_extract_progress,
                    should_stop=lambda: self._should_abort(job_id),
                )
            except MediaCanceledError:
                self._mark_aborted(job_id)
                return
            except AppError as exc:
                self._fail_job(job_id, exc.code, exc.message)
                return
            except Exception as exc:
                logger.exception("conversion_failed", extra={"job_id": job_id})
                self._fail_job(
                    job_id,
                    "audio_extraction_failed",
                    f"Ошибка извлечения аудио: {str(exc)[:200]}",
                )
                return
            self.store.update_job(
                job_id,
                wav_path=str(wav_path),
                status="transcribing",
                progress=0,
                processed_seconds=0,
                stage_message="Поиск речи и распознавание",
                transcribing_started_at=utcnow_iso(),
                heartbeat_at=utcnow_iso(),
            )
            try:
                transcriber = self._get_transcriber(row["model"])
            except Exception as exc:
                logger.exception("job_model_load_failed", extra={"job_id": job_id, "model": row["model"]})
                self._fail_job(
                    job_id,
                    "model_load_failed",
                    f"Не удалось загрузить модель {row['model']}. Детали: {str(exc)[:200]}",
                )
                return
            last_update = 0.0
            last_scan_update = 0.0

            def on_progress(processed: float) -> None:
                nonlocal last_update
                now = time.monotonic()
                if now - last_update < 0.5 and processed < info.duration_s:
                    return
                last_update = now
                # 0–10% — поиск речи (VAD), 10–99% — распознавание сегментов.
                progress = 10.0 + min(89.0, 89.0 * processed / max(info.duration_s, 0.001))
                self.store.set_progress(
                    job_id,
                    progress=progress,
                    processed_seconds=min(processed, info.duration_s),
                    stage_message=(
                        f"Распознавание: {format_duration_hms(processed)} из "
                        f"{format_duration_hms(info.duration_s)}"
                    ),
                )

            def on_scan_progress(scanned: float) -> None:
                nonlocal last_scan_update
                now = time.monotonic()
                if now - last_scan_update < 0.5 and scanned < info.duration_s:
                    return
                last_scan_update = now
                progress = min(10.0, 10.0 * scanned / max(info.duration_s, 0.001))
                self.store.set_progress(
                    job_id,
                    progress=progress,
                    processed_seconds=0,
                    stage_message=(
                        f"Поиск речи: {format_duration_hms(scanned)} из "
                        f"{format_duration_hms(info.duration_s)}"
                    ),
                )

            try:
                result: TranscriptionResult = transcriber.transcribe(
                    wav_path,
                    info.duration_s,
                    on_progress=on_progress,
                    on_scan_progress=on_scan_progress,
                    should_stop=lambda: self._should_abort(job_id),
                )
            except TranscriberCanceled:
                self._mark_aborted(job_id)
                return
            except TranscriberError as exc:
                self._fail_job(job_id, exc.code, exc.message)
                return
            except Exception as exc:
                logger.exception("transcription_failed", extra={"job_id": job_id})
                self._fail_job(job_id, "asr_failed", f"Ошибка распознавания: {str(exc)[:200]}")
                return
            if self._stop_event.is_set():
                self._mark_aborted(job_id)
                return

            self.store.update_job(
                job_id,
                status="rendering",
                progress=99.0,
                processed_seconds=info.duration_s,
                stage_message="Формирование DOCX",
                heartbeat_at=utcnow_iso(),
            )
            processing_seconds = time.monotonic() - started
            rtf = processing_seconds / info.duration_s if info.duration_s > 0 else None
            result_dir = self.settings.results_dir / job_id
            result_dir.mkdir(parents=True, exist_ok=True)
            result_filename = make_docx_filename(row["filename"])
            result_path = result_dir / result_filename
            try:
                render_docx(
                    result_path,
                    filename=row["filename"],
                    segments=result.segments,
                    duration_s=info.duration_s,
                    model=self._model_display_name(row["model"]),
                    language="Русский (ru-RU)",
                    processed_at=datetime.now(UTC).astimezone(),
                    processing_seconds=processing_seconds,
                    rtf=rtf,
                    image_version=self.settings.image_version,
                    timestamps=bool(row.get("timestamps")),
                )
            except Exception as exc:
                logger.exception("docx_render_failed", extra={"job_id": job_id})
                self._fail_job(job_id, "docx_failed", f"Не удалось сформировать DOCX: {str(exc)[:200]}")
                return

            finished_at = utcnow_iso()
            preview = build_preview(result.segments, self.settings.max_preview_chars)
            peak_vram = result.peak_vram_mb
            if peak_vram is None:
                peak_vram = getattr(transcriber, "peak_vram_mb", None)
            self.store.update_job(
                job_id,
                status="done",
                progress=100.0,
                processed_seconds=info.duration_s,
                stage_message="Готово",
                preview=preview,
                result_path=str(result_path),
                result_filename=result_filename,
                finished_at=finished_at,
                processing_seconds=round(processing_seconds, 2),
                rtf=round(rtf, 3) if rtf is not None else None,
                peak_vram_mb=round(float(peak_vram), 1) if peak_vram is not None else None,
                error_code=None,
                error_message=None,
                heartbeat_at=finished_at,
            )
            updated = self.store.get_job(job_id)
            if updated:
                try:
                    remove_intermediate_files(updated, self.settings)
                    self.store.update_job(job_id, source_path=None, wav_path=None)
                except Exception:
                    logger.exception("intermediate_cleanup_failed", extra={"job_id": job_id})
            logger.info(
                "job_done",
                extra={
                    "job_id": job_id,
                    "model": row["model"],
                    "stage": "done",
                    "duration_s": round(info.duration_s, 1),
                    "processing_s": round(processing_seconds, 2),
                    "rtf": round(rtf, 2) if rtf else None,
                    "peak_vram_mb": round(float(peak_vram), 1) if peak_vram is not None else None,
                    "event": f"segments={len(result.segments)}",
                },
            )
        except Exception as exc:
            logger.exception("job_internal_error", extra={"job_id": job_id})
            self._fail_job(job_id, "internal_error", f"Внутренняя ошибка обработки: {str(exc)[:200]}")
        finally:
            self._current_job_id = None
            try:
                self.store.heartbeat(self.worker_id)
            except Exception:
                logger.exception("heartbeat_failed")

    def _model_display_name(self, model_name: str) -> str:
        mapping = {
            "gigaam-v3-e2e-rnnt": "GigaAM v3 E2E RNNT (gigaam-v3-e2e-rnnt)",
            "gigaam-v3-e2e-ctc": "GigaAM v3 E2E CTC (gigaam-v3-e2e-ctc)",
        }
        return mapping.get(model_name, model_name)

    def _periodic_cleanup(self) -> None:
        now = time.monotonic()
        if now - self._last_cleanup < 600:
            return
        self._last_cleanup = now
        self._cleanup_expired()

    def _cleanup_expired(self) -> None:
        try:
            rows = self.store.list_expired(self.settings.result_ttl_hours)
        except Exception:
            logger.exception("expired_jobs_query_failed")
            return
        for row in rows:
            try:
                remove_job_files(row, self.settings)
                self.store.delete_job(row["id"])
                logger.info("job_expired_removed", extra={"job_id": row["id"]})
            except Exception:
                logger.exception("job_expired_remove_failed", extra={"job_id": row["id"]})


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    worker = Worker(settings)
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        worker.shutdown()


if __name__ == "__main__":
    main()
