"""Работа с медиа: ffprobe, ffmpeg, WAV и безопасные имена файлов."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import subprocess
import threading
import time
import unicodedata
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np
import numpy.typing as npt

from app.config import Settings
from app.errors import ServiceUnavailableError, UnprocessableError, UnsupportedMediaError

logger = logging.getLogger(__name__)

_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *{f"com{i}" for i in range(1, 10)},
    *{f"lpt{i}" for i in range(1, 10)},
}
_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_MULTISPACE = re.compile(r"\s+")


class FfmpegUnavailableError(ServiceUnavailableError):
    code = "ffmpeg_unavailable"
    default_message = "ffmpeg/ffprobe не найдены в контейнере. Пересоберите образ."


class AudioExtractionError(UnprocessableError):
    code = "audio_extraction_failed"
    default_message = "Не удалось извлечь аудиодорожку из файла."


class MediaCanceledError(Exception):
    """Извлечение аудио прервано (отмена задачи или SIGTERM)."""


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration_s: float
    has_video: bool
    audio_codec: str
    sample_rate: int | None
    channels: int | None
    format_name: str
    size_bytes: int


def _run_ffprobe(path: Path, settings: Settings) -> dict[str, Any]:
    cmd = [
        settings.ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path.resolve()),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except FileNotFoundError as exc:
        raise FfmpegUnavailableError("ffprobe не найден. Проверьте установку ffmpeg в образе.") from exc
    except subprocess.TimeoutExpired as exc:
        raise UnprocessableError("ffprobe не успел прочитать файл за 120 секунд.") from exc
    if result.returncode != 0:
        details = (result.stderr or "").strip().splitlines()
        hint = details[-1][:300] if details else "неизвестная ошибка"
        raise UnsupportedMediaError(f"Файл не похож на видео или аудио: {hint}")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise UnsupportedMediaError("ffprobe вернул некорректный ответ.") from exc
    if not isinstance(payload, dict):
        raise UnsupportedMediaError("ffprobe вернул некорректный ответ.")
    return payload


def probe_media(path: Path, settings: Settings) -> MediaInfo:
    """Проверить файл через ffprobe и вернуть метаданные."""
    payload = _run_ffprobe(path, settings)
    streams = payload.get("streams") or []
    if not isinstance(streams, list):
        streams = []
    audio_streams = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"]
    if not audio_streams:
        raise UnsupportedMediaError("В файле не найдена аудиодорожка.")
    video_streams = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"]
    audio = audio_streams[0]
    format_info = payload.get("format") or {}
    duration_raw = format_info.get("duration") if isinstance(format_info, dict) else None
    if duration_raw in (None, "N/A"):
        duration_raw = audio.get("duration")
    if duration_raw is None:
        duration = 0.0
    else:
        try:
            duration = float(duration_raw)
        except (TypeError, ValueError):
            duration = 0.0
    if duration <= 0:
        raise UnsupportedMediaError("Не удалось определить длительность аудио.")
    try:
        sample_rate = int(audio["sample_rate"]) if audio.get("sample_rate") else None
    except (TypeError, ValueError):
        sample_rate = None
    try:
        channels = int(audio["channels"]) if audio.get("channels") else None
    except (TypeError, ValueError):
        channels = None
    try:
        size_bytes = int(format_info.get("size", path.stat().st_size))
    except (TypeError, ValueError, OSError):
        size_bytes = path.stat().st_size if path.exists() else 0
    return MediaInfo(
        path=path,
        duration_s=duration,
        has_video=bool(video_streams),
        audio_codec=str(audio.get("codec_name") or "unknown"),
        sample_rate=sample_rate,
        channels=channels,
        format_name=str(format_info.get("format_name") or "unknown"),
        size_bytes=size_bytes,
    )


def _read_ffmpeg_progress(
    stream: Any,
    progress: dict[str, float],
    stop_reader: threading.Event,
) -> None:
    try:
        for raw_line in stream:
            line = raw_line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in {"out_time_us", "out_time_ms"}:
                try:
                    progress["seconds"] = max(progress["seconds"], int(value) / 1_000_000)
                except ValueError:
                    continue
            elif key == "progress" and value == "end":
                progress["ended"] = 1.0
    except (ValueError, OSError):
        pass
    finally:
        stop_reader.set()


def convert_to_wav(
    source: Path,
    destination: Path,
    settings: Settings,
    *,
    duration_s: float,
    on_progress: Callable[[float], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Path:
    """Конвертировать вход в WAV PCM16 16 кГц моно, не блокируя отмену."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    log_path = destination.with_suffix(".ffmpeg.log")
    cmd = [
        settings.ffmpeg_bin,
        "-hide_banner",
        "-nostdin",
        "-progress",
        "pipe:1",
        "-nostats",
        "-y",
        "-i",
        str(source.resolve()),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(destination.resolve()),
    ]
    progress = {"seconds": 0.0, "ended": 0.0}
    stop_reader = threading.Event()
    with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
        try:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=log_file,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise FfmpegUnavailableError("ffmpeg не найден. Проверьте установку ffmpeg в образе.") from exc
        assert process.stdout is not None
        reader = threading.Thread(
            target=_read_ffmpeg_progress,
            args=(process.stdout, progress, stop_reader),
            daemon=True,
        )
        reader.start()
        canceled = False
        last_report = 0.0
        try:
            while process.poll() is None:
                if should_stop and should_stop():
                    canceled = True
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    break
                now = time.monotonic()
                if on_progress and now - last_report >= 0.4:
                    on_progress(min(duration_s, progress["seconds"]))
                    last_report = now
                time.sleep(0.15)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            reader.join(timeout=2)
            stop_reader.set()
            process.stdout.close()
    if canceled:
        destination.unlink(missing_ok=True)
        raise MediaCanceledError
    if process.returncode != 0:
        tail = ""
        with contextlib.suppress(OSError):
            tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-6:])
        logger.error(
            "ffmpeg_failed",
            extra={"code": "ffmpeg_failed", "path": str(source), "event": tail[-1000:]},
        )
        destination.unlink(missing_ok=True)
        raise AudioExtractionError(
            "ffmpeg не смог извлечь аудио. Файл повреждён или использует неподдерживаемый кодек."
            + (f" Детали: {tail[-300:]}" if tail else "")
        )
    if not destination.exists() or destination.stat().st_size < 44:
        raise AudioExtractionError("ffmpeg создал пустой WAV. Проверьте исходный файл.")
    with wave.open(str(destination), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != 16_000:
            raise AudioExtractionError("Внутренняя ошибка: WAV имеет неверный формат.")
    if on_progress:
        on_progress(duration_s)
    return destination


class WavReader:
    """Потоковое чтение WAV PCM16 16 кГц моно без загрузки файла целиком."""

    def __init__(self, path: Path) -> None:
        self.path = path
        # WavReader сам владеет файловым дескриптором; закрывается через close()/with.
        self._wav: wave.Wave_read = wave.open(str(path), "rb")  # noqa: SIM115
        if self._wav.getnchannels() != 1:
            self.close()
            raise AudioExtractionError("Ожидается моно WAV.")
        if self._wav.getsampwidth() != 2:
            self.close()
            raise AudioExtractionError("Ожидается WAV PCM16.")
        if self._wav.getframerate() != 16_000:
            self.close()
            raise AudioExtractionError("Ожидается частота 16 кГц.")
        self.frames = self._wav.getnframes()
        self.sample_rate = self._wav.getframerate()
        self.duration_s = self.frames / float(self.sample_rate)

    def read_seconds(self, start_s: float, end_s: float) -> npt.NDArray[np.float32]:
        start_frame = max(0, min(self.frames, round(start_s * self.sample_rate)))
        end_frame = max(start_frame, min(self.frames, round(end_s * self.sample_rate)))
        self._wav.setpos(start_frame)
        raw = self._wav.readframes(end_frame - start_frame)
        if not raw:
            return np.zeros(0, dtype=np.float32)
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        return samples

    def close(self) -> None:
        self._wav.close()

    def __enter__(self) -> WavReader:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


def sanitize_filename(filename: str | None, *, fallback: str = "audio") -> str:
    """Безопасное имя файла: без путей, traversal и управляющих символов."""
    if not filename:
        return fallback
    name = unicodedata.normalize("NFC", filename.replace("\\", "/").split("/")[-1])
    name = _INVALID_CHARS.sub("_", name).strip().strip(".")
    name = _MULTISPACE.sub(" ", name)
    if not name:
        return fallback
    stem = Path(name).stem.strip().strip(".")
    suffix = Path(name).suffix.lower()
    if not stem:
        stem = fallback
    if stem.lower() in _WINDOWS_RESERVED:
        stem = f"_{stem}"
    max_stem = 140
    if len(stem) > max_stem:
        stem = stem[:max_stem].rstrip(" .")
    if suffix and not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = ""
    return f"{stem}{suffix}"


def sanitize_stem(filename: str, *, fallback: str = "transcript") -> str:
    stem = Path(sanitize_filename(filename, fallback=fallback)).stem.strip().strip(".")
    if not stem or stem in {".", ".."}:
        stem = fallback
    return stem[:140].rstrip(" .")


def get_extension(filename: str) -> str:
    return Path(filename).suffix.lower()


def format_duration_hms(seconds: float, *, with_ms: bool = False) -> str:
    seconds = max(0.0, seconds)
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if with_ms:
        millis = round((seconds - total) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_timestamp(seconds: float) -> str:
    return format_duration_hms(seconds, with_ms=False)


def format_hr_min(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{int(seconds)} с"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes} мин {secs:02d} с"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes:02d} мин"


def tail_text(path: Path, *, lines: int = 5, max_chars: int = 800) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return ""
    return "\n".join(content)[-max_chars:]


def write_wav_silence(path: Path, duration_s: float, sample_rate: int = 16_000) -> None:
    """Создать WAV с тишиной (для smoke-теста и тестов)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(duration_s * sample_rate)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * frames)


def write_fileobj_limited(source: BinaryIO, destination: Path, limit_bytes: int) -> int:
    """Скопировать upload в файл, не превышая лимит. Вернуть число байт."""
    written = 0
    with destination.open("wb") as output:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > limit_bytes:
                output.close()
                destination.unlink(missing_ok=True)
                raise OverflowError
            output.write(chunk)
    return written
