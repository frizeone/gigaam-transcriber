"""Конфигурация приложения (12-factor, переменные окружения)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_EXTENSIONS = (
    "mp4,mkv,mov,avi,webm,m4v,mpeg,mpg,ts,m2ts,mts,wav,mp3,m4a,m4b,aac,flac,"
    "ogg,oga,opus,wma,amr,3gp"
)
DEFAULT_MODELS = "gigaam-v3-e2e-rnnt,gigaam-v3-e2e-ctc"


class Settings(BaseSettings):
    """Настройки сервиса. Все значения имеют безопасные дефолты."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Приложение
    app_name: str = "GigaAM Transcriber"
    app_version: str = "0.1.0"
    app_bind: str = "0.0.0.0"  # noqa: S104 - сервис по умолчанию доступен в локальной сети
    app_port: int = Field(default=8000, ge=1, le=65535)
    data_dir: Path = Path("/data")
    hf_home: Path = Path("/models")
    log_level: str = "info"
    image_tag: str = "latest"
    git_sha: str = ""

    # ASR
    asr_model: str = "gigaam-v3-e2e-rnnt"
    asr_available_models: str = DEFAULT_MODELS
    asr_backend: Literal["onnx", "fake"] = "onnx"
    asr_device: Literal["cuda", "cpu"] = "cuda"
    asr_quant: str = ""
    asr_batch: int = Field(default=8, ge=1, le=32)
    hf_hub_offline: bool = False

    # VAD и потоковая обработка
    asr_vad_threshold: float = Field(default=0.5, gt=0.0, lt=1.0)
    asr_vad_min_speech_ms: int = Field(default=250, ge=0, le=10_000)
    asr_vad_max_speech_s: float = Field(default=20.0, gt=1.0, le=25.0)
    asr_vad_min_silence_ms: int = Field(default=300, ge=0, le=10_000)
    asr_vad_speech_pad_ms: int = Field(default=100, ge=0, le=2_000)
    asr_stream_chunk_s: int = Field(default=60, ge=10, le=300)
    max_cached_models: int = Field(default=2, ge=1, le=4)

    # Лимиты, хранение
    max_upload_mb: int = Field(default=2048, ge=1, le=10240)
    max_duration_min: int = Field(default=180, ge=1, le=1440)
    queue_max: int = Field(default=20, ge=1, le=1000)
    result_ttl_hours: int = Field(default=24, ge=1, le=24 * 365)
    docx_timestamps: bool = False
    upload_rate_limit_per_min: int = Field(default=5, ge=1, le=1000)

    # Воркер
    worker_poll_interval_s: float = Field(default=0.5, ge=0.1, le=10.0)
    worker_heartbeat_s: float = Field(default=5.0, ge=1.0, le=60.0)
    worker_stale_seconds: int = Field(default=90, ge=15, le=3600)

    # Безопасность
    auth_token: str = ""
    trust_proxy: bool = False
    enable_hsts: bool = False
    allowed_extensions: str = DEFAULT_EXTENSIONS

    # Docker / интеграция
    preload_models: bool = False
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    # Прочее
    max_preview_chars: int = Field(default=500, ge=0, le=10_000)
    allowed_hosts: str = "*"

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        return value.lower().strip()

    @field_validator("asr_quant")
    @classmethod
    def _normalize_quant(cls, value: str) -> str:
        value = value.strip().lower()
        if value in {"", "none", "null", "false", "0"}:
            return ""
        return value

    def model_post_init(self, __context: object) -> None:
        """Дополнительная нормализация после чтения окружения."""
        if self.asr_device == "cpu" and not self.asr_quant:
            object.__setattr__(self, "asr_quant", "int8")
        if self.asr_device == "cuda" and self.asr_quant in {"none", "null"}:
            object.__setattr__(self, "asr_quant", "")

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def asr_quantization(self) -> str | None:
        return self.asr_quant or None

    @property
    def effective_asr_batch(self) -> int:
        # На CPU батч 1 уменьшает задержку реакции на SIGTERM и пиковую память.
        return 1 if self.asr_device == "cpu" else self.asr_batch

    @property
    def models(self) -> list[str]:
        values = [item.strip() for item in self.asr_available_models.split(",") if item.strip()]
        if self.asr_model not in values:
            values.insert(0, self.asr_model)
        return list(dict.fromkeys(values))

    @property
    def extensions(self) -> list[str]:
        return [
            f".{item.strip().lower().lstrip('.')}"
            for item in self.allowed_extensions.split(",")
            if item.strip()
        ]

    @property
    def max_duration_s(self) -> float:
        return float(self.max_duration_min * 60)

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_token)

    @property
    def image_version(self) -> str:
        sha = f", sha {self.git_sha[:8]}" if self.git_sha else ""
        return f"{self.image_tag} (v{self.app_version}{sha})"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def results_dir(self) -> Path:
        return self.data_dir / "results"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "gigaam.sqlite3"

    @property
    def models_ready_file(self) -> Path:
        return self.hf_home / ".models_ready.json"

    @property
    def preload_lock_file(self) -> Path:
        return self.hf_home / ".models-init.lock"

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.uploads_dir, self.results_dir, self.work_dir, self.hf_home):
            path.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Вернуть настройки (кешируются в пределах процесса)."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_cache() -> None:
    """Сбросить кеш настроек (используется в тестах)."""
    global _settings
    _settings = None
