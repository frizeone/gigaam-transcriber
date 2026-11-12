"""Smoke с реальной моделью. Запускается вне CI:

RUN_GPU_TESTS=1 pytest -m gpu -q
"""

from __future__ import annotations

import os
import shutil
import urllib.request
from pathlib import Path

import pytest

from app.asr.onnx_gigaam import OnnxGigaAM
from app.config import Settings
from app.media import probe_media

EXAMPLE_URL = "https://cdn.chatwm.opensmodel.sberdevices.ru/GigaAM/example.wav"


@pytest.mark.gpu
@pytest.mark.skipif(os.getenv("RUN_GPU_TESTS") != "1", reason="нужен RUN_GPU_TESTS=1 и загруженные веса")
def test_real_model_transcribes_example(tmp_path: Path) -> None:
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg/ffprobe не установлены")
    settings = Settings(
        data_dir=tmp_path / "data",
        hf_home=Path(os.getenv("HF_HOME", "/models")),
        asr_backend="onnx",
        asr_device=os.getenv("ASR_DEVICE", "cuda"),  # type: ignore[arg-type]
        asr_quant=os.getenv("ASR_QUANT", ""),
        log_level="info",
    )
    settings.ensure_directories()
    audio = tmp_path / "example.wav"
    urllib.request.urlretrieve(EXAMPLE_URL, audio)
    info = probe_media(audio, settings)
    transcriber = OnnxGigaAM(settings, model_name="gigaam-v3-e2e-rnnt")
    try:
        transcriber.load()
        result = transcriber.transcribe(audio, info.duration_s)
    finally:
        transcriber.close()
    assert result.segments, "GigaAM не вернул ни одного сегмента на тестовом примере"
    assert any(segment.text.strip() for segment in result.segments)
    print(f"device={result.device} segments={len(result.segments)}")
    for segment in result.segments[:3]:
        print(f"[{segment.start:.2f}-{segment.end:.2f}] {segment.text}")
