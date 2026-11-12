"""Служебные endpoint'ы: health, ready, limits."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse

from app.api.deps import get_app_settings, get_store
from app.config import Settings
from app.db import JobStore
from app.schemas import ErrorInfo, HealthOut, LimitsOut, ReadyOut

router = APIRouter(prefix="/api", tags=["service"])


@router.get("/health", response_model=HealthOut, summary="Liveness")
def health(settings: Settings = Depends(get_app_settings)) -> HealthOut:
    return HealthOut(status="ok", version=settings.app_version)


@router.get("/ready", response_model=ReadyOut, summary="Readiness: модель загружена")
def ready(
    response: Response,
    settings: Settings = Depends(get_app_settings),
    store: JobStore = Depends(get_store),
) -> ReadyOut | JSONResponse:
    state = store.get_runtime_state()
    if not state:
        starting_message = "Воркер ещё не запускался. Выполните make init && make up."
        payload = ReadyOut(
            ready=False,
            status="starting",
            message=starting_message,
            error=ErrorInfo(code="not_ready", message=starting_message),
        )
        return JSONResponse(status_code=503, content=payload.model_dump(mode="json"))
    heartbeat_raw = state.get("heartbeat_at")
    heartbeat_age: float | None = None
    heartbeat_fresh = False
    if heartbeat_raw:
        try:
            heartbeat = datetime.fromisoformat(heartbeat_raw)
            heartbeat_age = max(0.0, (datetime.now(UTC) - heartbeat).total_seconds())
            heartbeat_fresh = heartbeat_age <= settings.worker_stale_seconds
        except ValueError:
            heartbeat_age = None
    loaded_models: list[str] = []
    if state.get("loaded_models"):
        try:
            parsed = json.loads(state["loaded_models"])
            if isinstance(parsed, list):
                loaded_models = [str(item) for item in parsed]
        except json.JSONDecodeError:
            loaded_models = []
    ready_flag = bool(heartbeat_fresh and state.get("status") == "ready")
    providers = [item for item in str(state.get("providers") or "").split(",") if item]
    message: str | None = state.get("message")
    if not heartbeat_fresh:
        ready_flag = False
        message = "Воркер не отвечает. Проверьте docker compose logs worker."
    elif state.get("status") != "ready":
        ready_flag = False
    error: ErrorInfo | None = None
    if not ready_flag:
        error = ErrorInfo(
            code="worker_unavailable" if not heartbeat_fresh else "not_ready",
            message=message or "Сервис не готов.",
        )
    payload = ReadyOut(
        ready=ready_flag,
        status=str(state.get("status") or "unknown"),
        model=state.get("asr_model"),
        device=state.get("device"),
        cuda_available=bool(state.get("cuda_available")),
        providers=providers,
        heartbeat_age_s=round(heartbeat_age, 1) if heartbeat_age is not None else None,
        message=message,
        models_loaded=loaded_models,
        error=error,
    )
    if ready_flag:
        return payload
    return JSONResponse(status_code=503, content=payload.model_dump(mode="json"))


@router.get("/limits", response_model=LimitsOut, summary="Лимиты и доступные модели")
def limits(settings: Settings = Depends(get_app_settings)) -> LimitsOut:
    return LimitsOut(
        max_upload_mb=settings.max_upload_mb,
        max_duration_min=settings.max_duration_min,
        queue_max=settings.queue_max,
        result_ttl_hours=settings.result_ttl_hours,
        allowed_extensions=settings.extensions,
        models=settings.models,
        default_model=settings.asr_model,
        default_timestamps=settings.docx_timestamps,
        auth_required=settings.auth_enabled,
        app_version=settings.app_version,
        image_tag=settings.image_tag,
    )
