"""Smoke-тест реальной модели внутри контейнера воркера.

make smoke
  docker compose exec worker python -m scripts.smoke_test
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path

from app.asr.onnx_gigaam import OnnxGigaAM
from app.config import Settings
from app.media import convert_to_wav, probe_media, write_wav_silence

EXAMPLE_URL = "https://cdn.chatwm.opensmodel.sberdevices.ru/GigaAM/example.wav"


def _prepare_audio(settings: Settings, explicit: Path | None) -> tuple[Path, bool]:
    target = explicit or (settings.work_dir / "smoke_example.wav")
    if target.is_file() and target.stat().st_size > 44:
        return target, True
    if explicit is not None:
        raise FileNotFoundError(f"Файл не найден: {explicit}")
    try:
        print(f"Скачивание тестового файла: {EXAMPLE_URL}", flush=True)
        with urllib.request.urlopen(EXAMPLE_URL, timeout=30) as response, target.open("wb") as output:
            shutil.copyfileobj(response, output)
        return target, True
    except Exception as exc:
        target.unlink(missing_ok=True)
        print(f"Не удалось скачать пример ({exc}); использую 2 секунды тишины.", flush=True)
        write_wav_silence(target, 2.0)
        return target, False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-тест GigaAM v3 E2E")
    parser.add_argument("--audio", type=Path, help="Свой WAV/медиафайл")
    parser.add_argument("--model", default=None, help="Имя модели")
    args = parser.parse_args(argv)

    settings = Settings()
    settings.ensure_directories()
    model_name = args.model or settings.asr_model
    print(f"Модель: {model_name}; устройство: {settings.asr_device}", flush=True)
    source, is_speech = _prepare_audio(settings, args.audio)
    info = probe_media(source, settings)
    wav_path = settings.work_dir / "smoke_input.wav"
    if (
        source.suffix.lower() != ".wav"
        or info.sample_rate != 16_000
        or info.channels != 1
        or info.audio_codec != "pcm_s16le"
    ):
        print("Конвертация в WAV 16 кГц моно PCM16…", flush=True)
        convert_to_wav(source, wav_path, settings, duration_s=info.duration_s)
    else:
        wav_path = source
    transcriber = OnnxGigaAM(settings, model_name=model_name)
    started = time.monotonic()
    transcriber.load()
    print(f"Провайдеры: {transcriber.providers}; устройство: {transcriber.device}", flush=True)
    try:
        result = transcriber.transcribe(wav_path, info.duration_s)
    finally:
        transcriber.close()
    elapsed = time.monotonic() - started
    summary = {
        "model": result.model,
        "device": result.device,
        "duration_s": round(info.duration_s, 2),
        "processing_s": round(elapsed, 2),
        "rtf": round(elapsed / info.duration_s, 2) if info.duration_s else None,
        "peak_vram_mb": result.peak_vram_mb,
        "segments": [
            {"start": round(segment.start, 2), "end": round(segment.end, 2), "text": segment.text}
            for segment in result.segments[:10]
        ],
        "segment_count": len(result.segments),
        "speech_test_file": is_speech,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print("SMOKE OK", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"SMOKE FAILED: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
