"""Предзагрузка весов GigaAM и Silero VAD в HF_HOME (/models).

Запускается сервисом models-init. Берёт file-lock, чтобы web/worker не качали
модели одновременно. По умолчанию скачивает оба варианта квантования, чтобы
volume работал и в CPU-, и в GPU-режиме, и обе модели из ASR_AVAILABLE_MODELS.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import numpy as np
from filelock import FileLock

from app import __version__
from app.config import DEFAULT_MODELS, Settings


def _parse_models(values: list[str] | None) -> list[str]:
    if values:
        return values
    raw = os.getenv("ASR_AVAILABLE_MODELS", DEFAULT_MODELS)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _with_retries[T](label: str, loader: Callable[[], T], *, attempts: int = 4) -> T:
    """Повторить загрузку при временной ошибке сети/Hugging Face."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return loader()
        except Exception as exc:
            last_error = exc
            if attempt == attempts:
                break
            delay = min(30, 5 * attempt)
            print(
                f"  ! {label}: попытка {attempt}/{attempts} не удалась ({exc}); пауза {delay} с",
                flush=True,
            )
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Загрузка весов GigaAM v3 E2E и Silero VAD")
    parser.add_argument("--model", action="append", help="Имя модели (можно несколько)")
    parser.add_argument(
        "--quantization",
        action="append",
        choices=["none", "int8"],
        help="Квантование: none (fp32, GPU) и/или int8 (CPU). По умолчанию оба.",
    )
    parser.add_argument("--skip-vad", action="store_true", help="Не загружать Silero VAD")
    parser.add_argument("--offline", action="store_true", help="Только проверить кеш, без сети")
    args = parser.parse_args(argv)

    settings = Settings()
    settings.ensure_directories()
    os.environ["HF_HOME"] = str(settings.hf_home)
    os.environ["HF_HUB_OFFLINE"] = "1" if args.offline else "0"

    models = _parse_models(args.model)
    quantizations: list[str | None] = []
    for value in args.quantization or ["none", "int8"]:
        quantizations.append(None if value == "none" else value)

    print(f"HF_HOME={settings.hf_home}", flush=True)
    print(f"Модели: {', '.join(models)}", flush=True)
    print(f"Квантования: {[q or 'fp32' for q in quantizations]}", flush=True)

    lock = FileLock(str(settings.preload_lock_file))
    with lock:
        import onnx_asr
        import onnxruntime as ort

        print(f"onnx-asr {getattr(onnx_asr, '__version__', '?')}, onnxruntime {ort.__version__}", flush=True)
        print(f"Провайдеры: {ort.get_available_providers()}", flush=True)
        loaded: dict[str, list[str]] = {}
        for model_name in models:
            loaded[model_name] = []
            for quantization in quantizations:
                label = quantization or "fp32"
                print(f"-> {model_name} [{label}]", flush=True)

                def load_model(
                    name: str = model_name,
                    quant: str | None = quantization,
                ) -> Any:
                    return onnx_asr.load_model(
                        name,
                        providers=["CPUExecutionProvider"],
                        quantization=quant,
                    )

                adapter = _with_retries(f"{model_name} [{label}]", load_model)
                # Прогрев на 1 секунде тишины подтверждает, что файлы читаются.
                adapter.recognize(np.zeros(16_000, dtype="float32"), sample_rate=16_000)
                loaded[model_name].append(label)
                del adapter
                gc.collect()
        if not args.skip_vad:
            print("-> Silero VAD", flush=True)
            vad: Any = _with_retries(
                "Silero VAD",
                lambda: onnx_asr.load_vad("silero", providers=["CPUExecutionProvider"]),
            )
            segments = vad.segment_batch(
                np.zeros((1, 16_000), dtype=np.float32),
                np.array([16_000], dtype=np.int64),
                16_000,
            )
            list(next(iter(segments)))
            del vad
            gc.collect()

    metadata = {
        "schema": 1,
        "app_version": __version__,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "hf_home": str(settings.hf_home),
        "models": loaded,
        "vad": "silero",
        "onnxruntime": ort.__version__,
        "providers": ort.get_available_providers(),
    }
    settings.models_ready_file.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    total_size = sum(path.stat().st_size for path in settings.hf_home.rglob("*") if path.is_file())
    print(f"Готово. Объём каталога моделей: {total_size / 1024**3:.2f} ГБ", flush=True)
    print(f"Маркер: {settings.models_ready_file}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        print(f"ОШИБКА загрузки моделей: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
