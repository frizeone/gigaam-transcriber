# Changelog

## v0.1.0 — 2026-11-12

Первая версия.

- FastAPI-сервис с загрузкой видео/аудио, очередью в SQLite и SSE-прогрессом.
- Потоковый VAD + батчевое распознавание GigaAM v3 E2E RNNT/CTC через onnx-asr 0.12.0 и onnxruntime-gpu 1.24.4.
- Экспорт в валидный DOCX: метаданные, кириллица, `w:lang=ru-RU`, таймкоды, нумерация страниц.
- Тёмный адаптивный интерфейс без сборщиков и CDN, drag & drop, история, восстановление после F5, токен доступа.
- Docker: multi-stage на `nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04`, non-root, tini, healthcheck, CPU base + GPU override, models-init, Caddy proxy-профиль.
- Makefile: init/up/cpu/logs/smoke/test/lint/typecheck/update/down/backup/proxy.
- Тесты pytest с FakeTranscriber, ruff и mypy.
- Документация: README, DECISIONS, NOTICE.
