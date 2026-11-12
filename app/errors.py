"""Единый формат ошибок приложения."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Ошибка с кодом и русским сообщением для пользователя."""

    status_code = 400
    code = "bad_request"
    default_message = "Некорректный запрос."

    def __init__(
        self,
        message: str | None = None,
        *,
        status_code: int | None = None,
        code: str | None = None,
        headers: dict[str, str] | None = None,
        details: list[dict[str, Any]] | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.status_code = status_code or self.status_code
        self.code = code or self.code
        self.headers = headers or {}
        self.details = details
        super().__init__(self.message)


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    default_message = "Объект не найден."


class ConflictError(AppError):
    status_code = 409
    code = "conflict"
    default_message = "Операция невозможна в текущем состоянии."


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "file_too_large"
    default_message = "Файл превышает допустимый размер."


class UnsupportedMediaError(AppError):
    status_code = 415
    code = "unsupported_media"
    default_message = "Формат файла не поддерживается."


class UnprocessableError(AppError):
    status_code = 422
    code = "unprocessable"
    default_message = "Не удалось обработать файл."


class RateLimitError(AppError):
    status_code = 429
    code = "rate_limited"
    default_message = "Слишком много запросов. Попробуйте позже."


class ServiceUnavailableError(AppError):
    status_code = 503
    code = "service_unavailable"
    default_message = "Сервис временно недоступен."


def error_payload(
    code: str,
    message: str,
    details: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {"error": error}


def _json_error(
    status_code: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
    details: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=error_payload(code, message, details),
        headers=headers,
    )


def register_error_handlers(app: FastAPI) -> None:
    """Зарегистрировать обработчики ошибок в приложении."""

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error(
                "app_error",
                extra={"code": exc.code, "status": exc.status_code, "path": request.url.path},
            )
        return _json_error(exc.status_code, exc.code, exc.message, headers=exc.headers, details=exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = []
        for item in exc.errors():
            location = ".".join(str(part) for part in item.get("loc", ()))
            details.append({"field": location, "message": item.get("msg", "Некорректное значение")})
        return _json_error(
            422,
            "validation_error",
            "Проверьте параметры запроса.",
            details=details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if request.url.path.startswith("/api/"):
            code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
            message = {
                404: "Запрашиваемый ресурс не найден.",
                405: "Метод не поддерживается.",
            }.get(exc.status_code, str(exc.detail))
            return _json_error(exc.status_code, code, message, headers=getattr(exc, "headers", None))
        return _json_error(
            exc.status_code,
            "http_error",
            str(exc.detail),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_error", extra={"path": request.url.path})
        return _json_error(
            500,
            "internal_error",
            "Внутренняя ошибка сервера. Подробности записаны в журнал.",
        )
