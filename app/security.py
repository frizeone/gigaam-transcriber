"""Авторизация, rate limit и security-заголовки."""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from typing import ClassVar

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings
from app.errors import AppError, error_payload


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"
    default_message = "Требуется токен доступа."


class SlidingWindowLimiter:
    """Простой потокобезопасный лимитер по ключу (IP)."""

    def __init__(self, limit: int, window_s: float = 60.0) -> None:
        self.limit = limit
        self.window_s = window_s
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._last_cleanup = time.monotonic()

    def check(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            queue = self._events.setdefault(key, deque())
            cutoff = now - self.window_s
            while queue and queue[0] < cutoff:
                queue.popleft()
            if len(queue) >= self.limit:
                retry_after = max(1, int(self.window_s - (now - queue[0])))
                return False, retry_after
            queue.append(now)
            if now - self._last_cleanup > self.window_s * 5:
                self._cleanup(now)
            return True, 0

    def _cleanup(self, now: float) -> None:
        cutoff = now - self.window_s
        for key in list(self._events):
            queue = self._events[key]
            while queue and queue[0] < cutoff:
                queue.popleft()
            if not queue:
                del self._events[key]
        self._last_cleanup = now


def extract_token(request: Request) -> str | None:
    header = request.headers.get("x-auth-token")
    if header:
        return header.strip()
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    query_token = request.query_params.get("token")
    if query_token:
        return query_token.strip()
    return None


def require_auth(request: Request) -> None:
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return
    provided = extract_token(request)
    if not provided or not secrets.compare_digest(provided, settings.auth_token):
        raise UnauthorizedError(headers={"WWW-Authenticate": "Bearer"})


def client_ip(request: Request, settings: Settings) -> str:
    if settings.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


class BodySizeLimitMiddleware:
    """Ранний отказ по Content-Length; потоковая проверка — в обработчике upload."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings
        self.limit = settings.max_upload_bytes + 2 * 1024 * 1024

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] == "/api/jobs" and scope.get("method") == "POST":
            headers = MutableHeaders(scope=scope)
            raw_length = headers.get("content-length")
            if raw_length:
                try:
                    content_length = int(raw_length)
                except ValueError:
                    content_length = 0
                if content_length > self.limit:
                    response = JSONResponse(
                        status_code=413,
                        content=error_payload(
                            "file_too_large",
                            f"Файл больше лимита {self.settings.max_upload_mb} МБ.",
                        ),
                        headers={"Connection": "close"},
                    )
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    """Добавить security-заголовки ко всем ответам, включая SSE."""

    _HEADERS: ClassVar[dict[str, str]] = {
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "no-referrer",
        "permissions-policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
        "cross-origin-opener-policy": "same-origin",
        "cross-origin-resource-policy": "same-origin",
        "content-security-policy": (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'"
        ),
        "x-accel-buffering": "no",
    }

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        headers_to_add = dict(self._HEADERS)
        if path.startswith("/docs"):
            # Swagger UI генерирует inline-скрипт инициализации; внешние источники
            # всё равно запрещены, поэтому ослабляем только inline на /docs.
            headers_to_add["content-security-policy"] = (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "font-src 'self' data:; connect-src 'self'; object-src 'none'; "
                "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
            )

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in headers_to_add.items():
                    if name not in headers:
                        headers[name] = value
                if path.startswith("/api/") and "cache-control" not in headers:
                    headers["cache-control"] = "no-store"
                if self.settings.enable_hsts:
                    headers.setdefault("strict-transport-security", "max-age=31536000; includeSubDomains")
            await send(message)

        await self.app(scope, receive, send_with_headers)


