"""HTTP API задач."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.api.deps import get_app_settings, get_store, require_auth
from app.config import Settings
from app.db import JobStore
from app.errors import (
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    RateLimitError,
    UnprocessableError,
    UnsupportedMediaError,
)
from app.jobs import make_docx_filename, remove_job_files, to_job_out
from app.media import (
    FfmpegUnavailableError,
    get_extension,
    probe_media,
    sanitize_filename,
    write_fileobj_limited,
)
from app.schemas import JobCreatedOut, JobListOut, JobOut
from app.security import client_ip

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["jobs"], dependencies=[Depends(require_auth)])

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TERMINAL = {"done", "failed", "canceled"}


def _sse(event: str, data: Any, *, event_id: str | None = None) -> str:
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    payload = json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))
    for raw in payload.splitlines() or [""]:
        lines.append(f"data: {raw}")
    return "\n".join(lines) + "\n\n"


@router.post("", response_model=JobCreatedOut, status_code=201, summary="Загрузить файл")
async def create_job(
    request: Request,
    file: Annotated[UploadFile, File(description="Видео или аудио файл")],
    timestamps: Annotated[bool | None, Form()] = None,
    model: Annotated[str | None, Form()] = None,
    settings: Settings = Depends(get_app_settings),
    store: JobStore = Depends(get_store),
) -> JSONResponse:
    ip = client_ip(request, settings)
    allowed, retry_after = request.app.state.upload_limiter.check(ip)
    if not allowed:
        raise RateLimitError(
            "Слишком много загрузок. Подождите и попробуйте снова.",
            headers={"Retry-After": str(retry_after)},
        )
    if store.count_queued() >= settings.queue_max:
        raise RateLimitError(
            f"Очередь заполнена ({settings.queue_max} задач). Дождитесь обработки текущих."
        )

    raw_name = file.filename or "audio"
    filename = sanitize_filename(raw_name)
    extension = get_extension(filename)
    if extension not in settings.extensions:
        raise UnsupportedMediaError(
            f"Расширение {extension or '(нет)'} не поддерживается. "
            f"Допустимо: {', '.join(settings.extensions)}."
        )
    selected_model = (model or settings.asr_model).strip()
    if selected_model not in settings.models:
        raise UnprocessableError(
            f"Модель {selected_model} недоступна. Доступно: {', '.join(settings.models)}.",
            code="unsupported_model",
        )
    use_timestamps = settings.docx_timestamps if timestamps is None else timestamps

    job_id = uuid.uuid4().hex
    job_dir = settings.uploads_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    destination = job_dir / filename
    try:
        await file.seek(0)
        try:
            size = await run_in_threadpool(
                write_fileobj_limited,
                file.file,
                destination,
                settings.max_upload_bytes,
            )
        except OverflowError as exc:
            raise PayloadTooLargeError(
                f"Файл больше {settings.max_upload_mb} МБ. Загрузите файл меньшего размера."
            ) from exc
        if size == 0:
            raise UnsupportedMediaError("Загружен пустой файл.")
        try:
            info = await run_in_threadpool(probe_media, destination, settings)
        except FfmpegUnavailableError as exc:
            raise exc
        if info.duration_s > settings.max_duration_s + 0.5:
            raise UnprocessableError(
                f"Длительность {info.duration_s / 60:.1f} мин превышает лимит "
                f"{settings.max_duration_min} мин.",
                code="duration_exceeded",
            )
        row = await run_in_threadpool(
            store.create_job,
            job_id=job_id,
            filename=filename,
            source_path=destination,
            model=selected_model,
            timestamps=use_timestamps,
            duration_s=info.duration_s,
        )
    except BaseException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise

    logger.info(
        "job_created",
        extra={
            "job_id": job_id,
            "model": selected_model,
            "duration_s": round(info.duration_s, 1),
            "event": f"{size} bytes",
        },
    )
    payload = JobCreatedOut(
        job_id=row["id"],
        status=row["status"],
        location=f"/api/jobs/{row['id']}",
    )
    return JSONResponse(
        status_code=201,
        content=payload.model_dump(mode="json"),
        headers={"Location": payload.location},
    )


@router.get("", response_model=JobListOut, summary="Список задач")
def list_jobs(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    settings: Settings = Depends(get_app_settings),
    store: JobStore = Depends(get_store),
) -> JobListOut:
    rows = store.list_jobs(limit)
    return JobListOut(
        items=[to_job_out(row, store, settings) for row in rows],
        total=store.count_jobs(),
    )


@router.get("/{job_id}", response_model=JobOut, summary="Статус задачи")
def get_job(
    job_id: str,
    settings: Settings = Depends(get_app_settings),
    store: JobStore = Depends(get_store),
) -> JobOut:
    row = store.get_job(job_id)
    if row is None:
        raise NotFoundError("Задача не найдена.")
    return to_job_out(row, store, settings)


@router.get("/{job_id}/events", summary="SSE-прогресс задачи")
async def job_events(
    request: Request,
    job_id: str,
    settings: Settings = Depends(get_app_settings),
    store: JobStore = Depends(get_store),
) -> StreamingResponse:
    row = store.get_job(job_id)
    if row is None:
        raise NotFoundError("Задача не найдена.")

    async def generator() -> AsyncIterator[str]:
        last_status: str | None = None
        last_progress: float | None = None
        last_job_exists = True
        last_heartbeat = asyncio.get_running_loop().time()
        yield "retry: 3000\n\n"
        while True:
            if await request.is_disconnected():
                return
            current = store.get_job(job_id)
            if current is None:
                if last_job_exists:
                    yield _sse(
                        "error",
                        {"error": {"code": "job_deleted", "message": "Задача удалена."}},
                    )
                return
            last_job_exists = True
            job = to_job_out(current, store, settings)
            payload = job.model_dump(mode="json")
            status = str(job.status)
            if status != last_status:
                yield _sse("status", payload, event_id=current.get("updated_at"))
                last_status = status
            if job.progress != last_progress:
                yield _sse("progress", payload, event_id=current.get("updated_at"))
                last_progress = job.progress
            if status in TERMINAL:
                if status == "done":
                    yield _sse("done", payload)
                else:
                    yield _sse("error", payload)
                return
            now = asyncio.get_running_loop().time()
            if now - last_heartbeat >= 15:
                yield ": heartbeat\n\n"
                last_heartbeat = now
            await asyncio.sleep(0.75)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{job_id}/result.docx", summary="Скачать DOCX")
def download_result(
    job_id: str,
    settings: Settings = Depends(get_app_settings),
    store: JobStore = Depends(get_store),
) -> FileResponse:
    row = store.get_job(job_id)
    if row is None:
        raise NotFoundError("Задача не найдена.")
    if row["status"] != "done":
        raise ConflictError("Результат ещё не готов.")
    raw_path = row.get("result_path")
    if not raw_path:
        raise ConflictError("Результат недоступен.")
    path = Path(raw_path)
    try:
        path.resolve().relative_to(settings.results_dir.resolve())
    except ValueError as exc:
        raise ConflictError("Некорректный путь результата.") from exc
    if not path.is_file():
        raise ConflictError("Файл результата удалён по TTL.")
    result_filename = row.get("result_filename") or make_docx_filename(row["filename"])
    return FileResponse(
        path,
        media_type=DOCX_MEDIA_TYPE,
        filename=result_filename,
        content_disposition_type="attachment",
        headers={"Cache-Control": "private, no-store"},
    )


@router.delete("/{job_id}", summary="Отменить или удалить задачу")
def delete_job(
    job_id: str,
    settings: Settings = Depends(get_app_settings),
    store: JobStore = Depends(get_store),
) -> JSONResponse:
    mode, row = store.request_cancel_or_delete(job_id)
    if mode == "absent" or row is None:
        raise NotFoundError("Задача не найдена.")
    if mode == "deleted":
        remove_job_files(row, settings)
        logger.info("job_deleted", extra={"job_id": job_id})
        return JSONResponse(status_code=200, content={"job_id": job_id, "status": "deleted"})
    logger.info("job_cancel_requested", extra={"job_id": job_id})
    return JSONResponse(status_code=202, content={"job_id": job_id, "status": "canceling"})
