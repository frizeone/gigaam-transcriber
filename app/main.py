"""FastAPI-приложение: API, статика, фоновые уборки."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.api import jobs as jobs_api
from app.api import meta as meta_api
from app.config import Settings, get_settings
from app.db import JobStore
from app.errors import register_error_handlers
from app.jobs import remove_job_files
from app.logging_conf import setup_logging
from app.security import BodySizeLimitMiddleware, SecurityHeadersMiddleware, SlidingWindowLimiter

logger = logging.getLogger(__name__)
_STATIC_DIR = Path(__file__).parent / "static"


async def _cleanup_loop(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    store: JobStore = app.state.store
    while True:
        try:
            if not store.runtime_state_is_fresh(settings.worker_stale_seconds):
                stats = store.stats()
                if any(stats.get(status, 0) for status in ("extracting", "transcribing", "rendering")):
                    failed = store.fail_all_active_jobs(
                        "worker_unavailable",
                        "Воркер перестал отвечать. Задача прервана; запустите её повторно.",
                    )
                    if failed:
                        logger.warning("stale_jobs_failed", extra={"event": f"count={failed}"})
            for row in store.list_expired(settings.result_ttl_hours):
                remove_job_files(row, settings)
                store.delete_job(row["id"])
                logger.info("job_expired_removed", extra={"job_id": row["id"]})
        except Exception:
            logger.exception("cleanup_loop_failed")
        await asyncio.sleep(60)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Фабрика приложения (удобна для тестов)."""
    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging(app_settings.log_level)
        app_settings.ensure_directories()
        store = JobStore(app_settings.db_path)
        store.initialize()
        app.state.settings = app_settings
        app.state.store = store
        app.state.upload_limiter = SlidingWindowLimiter(app_settings.upload_rate_limit_per_min, 60.0)
        task = asyncio.create_task(_cleanup_loop(app))
        logger.info(
            "web_started",
            extra={"event": f"data={app_settings.data_dir}", "status": "ready"},
        )
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            logger.info("web_stopped", extra={"status": "stopped"})

    app = FastAPI(
        title="GigaAM Transcriber",
        description=(
            "Локальная транскрибация русской речи моделью GigaAM v3 E2E. "
            "Загрузка видео/аудио, очередь, SSE-прогресс и экспорт в DOCX."
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    app.state.settings = app_settings
    app.state.store = JobStore(app_settings.db_path)
    app.state.upload_limiter = SlidingWindowLimiter(app_settings.upload_rate_limit_per_min, 60.0)

    # Middleware: security-заголовки снаружи, ограничение тела внутри.
    if app_settings.allowed_hosts.strip() not in {"", "*"}:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=[item.strip() for item in app_settings.allowed_hosts.split(",") if item.strip()],
        )
    app.add_middleware(BodySizeLimitMiddleware, settings=app_settings)
    app.add_middleware(SecurityHeadersMiddleware, settings=app_settings)

    register_error_handlers(app)
    app.include_router(meta_api.router)
    app.include_router(jobs_api.router)

    @app.get("/docs", include_in_schema=False)
    async def custom_swagger_ui() -> HTMLResponse:
        assert app.openapi_url is not None
        return get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title=f"{app.title} — Swagger UI",
            swagger_js_url="/swagger/swagger-ui-bundle.js",
            swagger_css_url="/swagger/swagger-ui.css",
            swagger_favicon_url="/favicon.svg",
            swagger_ui_parameters={"persistAuthorization": True},
        )

    if _STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")

    return app


setup_logging(get_settings().log_level)
app = create_app()
