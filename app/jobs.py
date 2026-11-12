"""Сериализация задач и безопасная работа с файлами результата."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.asr.base import Segment
from app.config import Settings
from app.db import JobStore
from app.media import format_duration_hms, sanitize_stem
from app.schemas import ErrorInfo, JobOut

TERMINAL = {"done", "failed", "canceled"}


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _eta_seconds(row: dict[str, Any]) -> float | None:
    if row.get("status") != "transcribing":
        return None
    duration = float(row.get("duration_s") or 0)
    processed = float(row.get("processed_seconds") or 0)
    started = parse_dt(row.get("transcribing_started_at"))
    if duration <= 0 or processed <= 1 or started is None:
        return None
    elapsed = (datetime.now(UTC) - started).total_seconds()
    if elapsed <= 0:
        return None
    speed = processed / elapsed
    if speed <= 0:
        return None
    return max(0.0, min(duration * 2, (duration - processed) / speed))


def to_job_out(row: dict[str, Any], store: JobStore, settings: Settings) -> JobOut:
    status = row["status"]
    queue_position = store.queue_position(row["id"]) if status == "queued" else None
    error: ErrorInfo | None = None
    if row.get("error_code"):
        error = ErrorInfo(
            code=row["error_code"],
            message=row.get("error_message") or "Неизвестная ошибка",
        )
    download_url = None
    result_path = row.get("result_path")
    if status == "done" and result_path and Path(result_path).is_file():
        download_url = f"/api/jobs/{row['id']}/result.docx"
    events_url = f"/api/jobs/{row['id']}/events" if status not in TERMINAL else None
    return JobOut(
        id=row["id"],
        status=status,
        filename=row["filename"],
        model=row["model"],
        timestamps=bool(row.get("timestamps")),
        duration_s=row.get("duration_s"),
        progress=round(float(row.get("progress") or 0), 1),
        processed_seconds=round(float(row.get("processed_seconds") or 0), 2),
        stage_message=row.get("stage_message"),
        queue_position=queue_position,
        eta_seconds=_eta_seconds(row),
        error=error,
        preview=row.get("preview"),
        created_at=parse_dt(row.get("created_at")) or datetime.now(UTC),
        started_at=parse_dt(row.get("started_at")),
        finished_at=parse_dt(row.get("finished_at")),
        processing_seconds=row.get("processing_seconds"),
        rtf=row.get("rtf"),
        peak_vram_mb=row.get("peak_vram_mb"),
        download_url=download_url,
        events_url=events_url,
    )


def make_docx_filename(original_filename: str, dt: datetime | None = None) -> str:
    stem = sanitize_stem(original_filename)
    moment = (dt or datetime.now()).astimezone()
    return f"{stem}__{moment:%Y-%m-%d_%H-%M}.docx"


def build_preview(segments: list[Segment], max_chars: int) -> str:
    if not segments:
        return "Речь не обнаружена."
    text = " ".join(" ".join(segment.text.split()) for segment in segments if segment.text.strip())
    text = " ".join(text.split())
    if max_chars > 0 and len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


def duration_label(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    return format_duration_hms(seconds)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def remove_intermediate_files(row: dict[str, Any], settings: Settings) -> None:
    """Удалить исходник и WAV, сохранив готовый DOCX."""
    job_id = row.get("id")
    for key in ("source_path", "wav_path"):
        raw = row.get(key)
        if raw:
            path = Path(raw)
            if _is_within(path, settings.data_dir):
                path.unlink(missing_ok=True)
    if job_id:
        upload_dir = settings.uploads_dir / job_id
        if _is_within(upload_dir, settings.data_dir):
            shutil.rmtree(upload_dir, ignore_errors=True)
        wav = settings.work_dir / f"{job_id}.wav"
        if _is_within(wav, settings.data_dir):
            wav.unlink(missing_ok=True)
        log = settings.work_dir / f"{job_id}.ffmpeg.log"
        if _is_within(log, settings.data_dir):
            log.unlink(missing_ok=True)


def remove_job_files(row: dict[str, Any], settings: Settings) -> None:
    """Удалить исходник, WAV и результат, не выходя за пределы data_dir."""
    job_id = row.get("id")
    candidates: list[Path] = []
    for key in ("source_path", "wav_path", "result_path"):
        raw = row.get(key)
        if raw:
            candidates.append(Path(raw))
    for path in candidates:
        if _is_within(path, settings.data_dir):
            path.unlink(missing_ok=True)
    if job_id:
        for root in (settings.uploads_dir, settings.results_dir, settings.work_dir):
            directory = root / job_id
            if _is_within(directory, settings.data_dir) and directory.is_dir():
                shutil.rmtree(directory, ignore_errors=True)
        # WAV может лежать в work/{job_id}.wav
        wav = settings.work_dir / f"{job_id}.wav"
        if _is_within(wav, settings.data_dir):
            wav.unlink(missing_ok=True)
        log = settings.work_dir / f"{job_id}.ffmpeg.log"
        if _is_within(log, settings.data_dir):
            log.unlink(missing_ok=True)
