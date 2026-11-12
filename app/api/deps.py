"""Зависимости FastAPI."""

from __future__ import annotations

from fastapi import Request

from app.config import Settings
from app.db import JobStore
from app.security import require_auth


def get_store(request: Request) -> JobStore:
    store: JobStore = request.app.state.store
    return store


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


__all__ = ["get_app_settings", "get_store", "require_auth"]
