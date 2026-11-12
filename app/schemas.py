"""Pydantic-схемы HTTP API и сериализация задач."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

JobStatus = Literal["queued", "extracting", "transcribing", "rendering", "done", "failed", "canceled"]


class ErrorInfo(BaseModel):
    code: str
    message: str


class JobOut(BaseModel):
    id: str
    status: JobStatus
    filename: str
    model: str
    timestamps: bool
    duration_s: float | None = None
    progress: float = 0
    processed_seconds: float = 0
    stage_message: str | None = None
    queue_position: int | None = None
    eta_seconds: float | None = None
    error: ErrorInfo | None = None
    preview: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    processing_seconds: float | None = None
    rtf: float | None = None
    peak_vram_mb: float | None = None
    download_url: str | None = None
    events_url: str | None = None


class JobListOut(BaseModel):
    items: list[JobOut]
    total: int


class JobCreatedOut(BaseModel):
    job_id: str
    status: JobStatus
    location: str


class LimitsOut(BaseModel):
    max_upload_mb: int
    max_duration_min: int
    queue_max: int
    result_ttl_hours: int
    allowed_extensions: list[str]
    models: list[str]
    default_model: str
    default_timestamps: bool
    auth_required: bool
    app_version: str
    image_tag: str


class HealthOut(BaseModel):
    status: Literal["ok"]
    version: str


class ReadyOut(BaseModel):
    ready: bool
    status: str
    model: str | None = None
    device: str | None = None
    cuda_available: bool = False
    providers: list[str] = Field(default_factory=list)
    heartbeat_age_s: float | None = None
    message: str | None = None
    models_loaded: list[str] = Field(default_factory=list)
    error: ErrorInfo | None = None


class ErrorOut(BaseModel):
    error: ErrorInfo
