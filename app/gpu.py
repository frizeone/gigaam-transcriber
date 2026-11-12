"""Проверка GPU и мониторинг видеопамяти."""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def available_providers() -> list[str]:
    try:
        import onnxruntime as ort
    except Exception:  # pragma: no cover - зависит от окружения
        return []
    try:
        return list(ort.get_available_providers())
    except Exception:  # pragma: no cover
        return []


def cuda_provider_available() -> bool:
    return "CUDAExecutionProvider" in available_providers()


class GpuMonitor:
    """Пиковое потребление VRAM через NVML; молча отключается без GPU."""

    def __init__(self) -> None:
        self._nvml: Any | None = None
        self._handle: Any | None = None
        self._peak_mb: float | None = None
        self._enabled = False
        try:
            import pynvml

            pynvml.nvmlInit()
            self._nvml = pynvml
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self._enabled = True
        except Exception as exc:  # pragma: no cover - нет GPU/драйвера
            logger.debug("nvml_unavailable", extra={"event": str(exc)})

    @property
    def enabled(self) -> bool:
        return self._enabled

    def sample(self) -> float | None:
        if not self._enabled or self._handle is None or self._nvml is None:
            return None
        used_bytes: int | None = None
        try:
            processes = self._nvml.nvmlDeviceGetComputeRunningProcesses(self._handle)
            if processes:
                own_pid = os.getpid()
                own = [process for process in processes if process.pid == own_pid]
                used_bytes = sum(int(process.usedGpuMemory or 0) for process in own)
        except Exception:  # pragma: no cover - не все драйверы/WDDM поддерживают
            used_bytes = None
        if used_bytes is None:
            try:
                info = self._nvml.nvmlDeviceGetMemoryInfo(self._handle)
            except Exception:  # pragma: no cover
                return None
            used_bytes = int(info.used)
        used_mb = float(used_bytes) / (1024 * 1024)
        self._peak_mb = used_mb if self._peak_mb is None else max(self._peak_mb, used_mb)
        return used_mb

    @property
    def peak_mb(self) -> float | None:
        return self._peak_mb

    def shutdown(self) -> None:
        if self._enabled and self._nvml is not None:
            with contextlib.suppress(Exception):  # pragma: no cover
                self._nvml.nvmlShutdown()
            self._enabled = False
