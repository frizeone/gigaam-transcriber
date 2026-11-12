"""JSON-логирование с контекстом задачи."""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import UTC, datetime
from typing import Any

_EXTRA_FIELDS = (
    "job_id",
    "worker_id",
    "model",
    "stage",
    "status",
    "duration_s",
    "processing_s",
    "rtf",
    "peak_vram_mb",
    "progress",
    "path",
    "code",
    "queue_position",
    "event",
)


class JsonFormatter(logging.Formatter):
    """Форматтер одной строки JSON на запись."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def setup_logging(level: str = "info") -> None:
    """Настроить root и uvicorn-логгеры на JSON."""
    if hasattr(time, "tzset"):
        time.tzset()
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    formatter = JsonFormatter()

    root = logging.getLogger()
    root.setLevel(numeric_level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root.addHandler(handler)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "app", "httpx", "huggingface_hub"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(numeric_level)
