# VALIDATION — прогон приёмки

Дата: 2026-11-12. Хост: Windows 11, Docker Desktop 29.6.2 (WSL2), NVIDIA RTX 4070 Ti Super 16 ГБ, драйвер 610.88. GNU make на Windows-хосте не установлен, поэтому команды Makefile проверялись их прямыми аналогами `docker compose ...`; сами target'ы простые и используют те же команды.

## 1. Сборка и запуск

| Проверка | Результат |
|---|---|
| `docker compose config` base / GPU / proxy / test | OK |
| Сборка runtime-образа | OK, `gigaam-transcriber:latest` |
| `models-init` (RNNT/CTC fp32+int8 + Silero VAD) | OK, volume 6.24 ГБ, маркер `/models/.models_ready.json` |
| Повторный `models-init` | OK, без повторной загрузки |
| `docker compose up -d` (GPU) | web healthy, worker up |
| `/api/ready` | HTTP 200, `device=cuda`, `CUDAExecutionProvider` |
| `/api/health` | HTTP 200 |
| GPU только у worker | в web нет nvidia-smi/GPU; worker видит CUDA provider |
| Non-root | `uid=1000(app) gid=1000(app)` |
| В образе нет весов/тестов/.env/медиа | проверено `find`/`ls` в контейнере |

## 2. Реальные файлы

| Сценарий | Результат |
|---|---|
| 1-часовой MP4 (3600 с, 926 сегментов), GigaAM v3 E2E RNNT **fp32** | 39.49 с, RTF **0.011 (91×)**, пик VRAM 3995 МБ, DOCX 39 КБ |
| 3-часовой WAV (тишина) | 26.58 с, RSS worker 1.1–1.9 ГБ, DOCX «Речь не обнаружена» |
| 16-секундный русский WAV через API | 0.67 с, RTF 0.041, пик VRAM 3768 МБ, DOCX с таймкодами |
| DOCX открывается python-docx / zipfile | OK, OOXML, кириллица, `w:lang=ru-RU`, поля страниц |
| `samples/example.docx` | реальный результат 16-секундного файла |

## 3. Очередь, рестарт, персистентность

| Проверка | Результат |
|---|---|
| `docker compose restart worker` во время часовой задачи | задача `failed`, `worker_shutdown`, понятное сообщение |
| Следующая задача после рестарта | `done`, очередь не сломана |
| `docker compose down && up` | история и DOCX сохранились, скачивание HTTP 200 |
| Отмена/удаление queued/active | API-тесты + ручная проверка |
| TTL/очистка | unit-тесты `list_expired` / `_cleanup_expired` |

## 4. Ошибки и безопасность

- 413 (размер), 415 (не медиа/расширение), 422 (длительность/модель/валидация), 429 (очередь/rate limit), 401 (токен), 404/409 — покрыты `tests/test_api.py`.
- CORS не подключён; CSP без внешних источников; Swagger UI self-hosted; security-заголовки и `Cache-Control: no-store` для API.
- Санитизация имён, защита путей результата и удаления — unit-тесты.
- `AUTH_TOKEN` через `X-Auth-Token`, Bearer и `?token=` (SSE/скачивание) — тесты.

## 5. Тесты и статический анализ

```
pytest (в контейнере test): 56 passed, 1 skipped (GPU-тест вне CI)
ruff check app scripts: All checks passed
mypy app scripts: Success: no issues found in 24 source files
```

Локально на Windows с ffmpeg:
```
pytest -q -m "not gpu": 56 passed
```

## 6. Ограничения среды проверки

- Word/LibreOffice визуально не открывались (на хосте нет LibreOffice); OOXML проверен python-docx, zipfile и полями PAGE/NUMPAGES.
- `PRELOAD_MODELS=1` (толстый air-gapped образ) не собирался в этой сессии: код использует тот же `download_models.py`, что и проверенный `models-init`; образ с весами ожидается ~15 ГБ uncompressed.
- 2-ГБ файл не загружался целиком; лимит проверен unit-тестом, 3-часовой 345-МБ файл прошёл полный путь.
- Caddy-профиль проверен `caddy validate`, но TLS/basic-auth в браузере не прогонялись.
- Makefile-команды проверены эквивалентными compose-командами; GNU make на Windows-хосте отсутствует.
