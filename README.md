# GigaAM Transcriber

Локальный веб-сервис транскрибации русской речи моделью **GigaAM v3 E2E (Сбер)** с экспортом в **.docx**. Разворачивается одной командой `docker compose up -d --build` на машине с NVIDIA GPU 16 ГБ. Аудио не покидает сервер, после первой загрузки весов интернет не нужен.

## Возможности

- Загрузка видео и аудио через drag & drop или выбор файла.
- `ffmpeg` → WAV 16 кГц моно PCM16 → Silero VAD → GigaAM v3 E2E (RNNT или CTC).
- Длинные записи до 3 часов, очередь до 20 задач, один GPU-воркер.
- Честный прогресс: 0–10% — поиск речи VAD, 10–99% — распознавание по секундам обработанных сегментов; SSE с авто-переподключением, восстановление после F5.
- Валидный OOXML `.docx`: метаданные, кириллица, `w:lang=ru-RU`, нумерация страниц, опциональные таймкоды.
- Тёмный адаптивный интерфейс на русском, без npm, сборщиков и CDN.
- Полностью локальная работа, опциональный общий токен, rate limit, security-заголовки.
- GPU пробрасывается только воркеру; web и worker — один и тот же образ.

## Архитектура

```
browser ──multipart──> web (FastAPI) ──> SQLite на volume ──> worker (GPU)
   ▲                        │                                  │
   └─────SSE /events────────┘              ffmpeg → VAD → GigaAM → python-docx
                                                               │
                                            volumes: data (uploads/results), models
```

- `web` — API, статика, валидация загрузки, SSE. GPU не нужен.
- `worker` — один процесс, сериализует доступ к GPU, держит модели в памяти, ловит `SIGTERM`.
- `models-init` — одноразовый сервис предзагрузки весов в volume `models`.
- `data` — SQLite, исходники (временно), WAV (временно), готовые DOCX.
- Обмен событиями между web и worker — через SQLite (WAL) и опрос состояния; Redis/Celery не нужны.

## Быстрый старт (GPU)

Требования: Linux с NVIDIA GPU, Docker Engine ≥ 24, Docker Compose v2, NVIDIA Container Toolkit. Для RTX 50xx нужен драйвер 570+, для RTX 30xx/40xx достаточно 550+. База по умолчанию — CUDA 12.8.1 + cuDNN 9; для 30xx/40xx можно задать в `.env` более лёгкую `BASE_IMAGE=nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04`.

```bash
cp .env.example .env
# при необходимости отредактируйте .env (порт, AUTH_TOKEN, лимиты)

make init     # скачать веса GigaAM v3 E2E RNNT/CTC и Silero VAD в volume models
make up       # собрать образ и запустить web + worker с GPU
```

Откройте <http://localhost:8000>. Swagger — <http://localhost:8000/docs> (Swagger UI поставляется локально, без CDN).

Без GNU make:

```bash
docker compose --profile init run --rm --build models-init
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

### CPU-режим

```bash
make init
make cpu      # docker compose up -d --build, ASR_DEVICE=cpu, ASR_QUANT=int8
```

CPU-режим полностью работоспособен, но медленный. На CPU автоматически используется квантованная int8-модель и батч 1. GPU в этом режиме не требуется, но образ всё равно основан на CUDA-базе (один образ для обоих режимов).

## Проверка GPU и установка NVIDIA Container Toolkit

### Ubuntu / Debian

```bash
# официальный репозиторий NVIDIA Container Toolkit
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Проверка до запуска сервиса:

```bash
docker run --rm --gpus all nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04 nvidia-smi
```

Проверка провайдера внутри воркера:

```bash
docker compose exec worker python -c "import onnxruntime as o; print(o.get_available_providers())"
# ожидается список с CUDAExecutionProvider
```

Изоляция: в `web` GPU нет.

```bash
docker compose exec web nvidia-smi   # должно завершиться ошибкой
```

### Windows + WSL2

1. Установите актуальный драйвер NVIDIA для Windows (570+ для RTX 50xx).
2. Установите Docker Desktop и включите WSL2 backend: Settings → General → Use the WSL 2 based engine.
3. Обновите WSL: `wsl --update`.
4. Проверьте GPU в контейнере из PowerShell:
   ```powershell
   docker run --rm --gpus all nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04 nvidia-smi
   ```
5. Запускайте `make` из Git Bash/WSL или используйте raw-команды выше. Пути проекта лучше держать внутри WSL (`~/...`) — так быстрее bind-mount и нет проблем с правами.

Если `nvidia-smi` в контейнере не работает: обновите драйвер, Docker Desktop и WSL; проверьте `wsl --status`; перезапустите Docker Desktop.

## Модели и офлайн-режим

`make init` скачивает в named volume `models`:

- `gigaam-v3-e2e-rnnt` (fp32 и int8);
- `gigaam-v3-e2e-ctc` (fp32 и int8);
- Silero VAD.

Объём — около 6.2 ГБ (обе модели в fp32 и int8). Файл-маркер: `/models/.models_ready.json`. Загрузка защищена file-lock, повторный запуск использует кеш Hugging Face.

После прогрева можно работать без интернета:

```bash
HF_HUB_OFFLINE=1 make up
```

### Air-gapped / «толстый» образ

```bash
docker compose build --build-arg PRELOAD_MODELS=1
```

Веса окажутся в `/opt/models` внутри образа. Чтобы использовать их вместо volume, задайте в `.env`:

```dotenv
HF_HOME=/opt/models
HF_HUB_OFFLINE=1
```

В air-gapped режиме `make init` не нужен: веса уже внутри образа, `make up` сразу использует `/opt/models`.

Либо перенесите образ и volume:

```bash
docker save gigaam-transcriber:latest -o gigaam-image.tar
docker save $(docker volume inspect gigaam_models -f '{{.Mountpoint}}')  # не работает напрямую
# проще: docker run --rm -v gigaam_models:/models -v "$PWD:/backup" alpine \
#   tar czf /backup/models.tar.gz -C /models .
```

Для закрытого контура удобнее заранее выполнить `make init` на машине с интернетом, сохранить volume `models` архивом и восстановить его на целевой машине.

## Конфигурация

Все переменные читаются из `.env` (см. `.env.example`) и имеют безопасные дефолты. Основные:

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `APP_BIND` | `0.0.0.0` | адрес публикации порта |
| `APP_PORT` | `8000` | внешний порт |
| `DATA_DIR` | `/data` | SQLite, uploads, results, work |
| `HF_HOME` | `/models` | кеш весов Hugging Face |
| `ASR_MODEL` | `gigaam-v3-e2e-rnnt` | модель по умолчанию |
| `ASR_AVAILABLE_MODELS` | `gigaam-v3-e2e-rnnt,gigaam-v3-e2e-ctc` | модели в UI/API |
| `ASR_DEVICE` | `cuda` | `cuda` или `cpu` |
| `ASR_QUANT` | пусто / `int8` на CPU | квантование ONNX |
| `ASR_BATCH` | `8` | батч VAD-сегментов (на CPU принудительно 1) |
| `ASR_BACKEND` | `onnx` | `onnx` или `fake` (тесты) |
| `ASR_VAD_*` | 0.5 / 250 / 20 / 300 / 100 | threshold, min_speech_ms, max_speech_s, min_silence_ms, speech_pad_ms |
| `ASR_STREAM_CHUNK_S` | `60` | блок чтения WAV для VAD |
| `MAX_UPLOAD_MB` | `2048` | лимит размера файла |
| `MAX_DURATION_MIN` | `180` | лимит длительности |
| `QUEUE_MAX` | `20` | максимум задач в очереди |
| `RESULT_TTL_HOURS` | `24` | автоочистка результатов |
| `DOCX_TIMESTAMPS` | `false` | таймкоды по умолчанию |
| `UPLOAD_RATE_LIMIT_PER_MIN` | `5` | загрузок в минуту с одного IP |
| `AUTH_TOKEN` | пусто | общий токен; пусто = авторизация выключена |
| `WORKER_STALE_SECONDS` | `90` | когда считать воркер мёртвым |
| `TRUST_PROXY` | `false` | доверять `X-Forwarded-For` |
| `ENABLE_HSTS` | `false` | добавить HSTS (только за HTTPS) |
| `PRELOAD_MODELS` | `0` | запечь веса в образ |
| `TZ` | `Europe/Moscow` | часовой пояс дат в DOCX |

## HTTP API

| Метод | Путь | Назначение |
|---|---|---|
| POST | `/api/jobs` | multipart `file` + `timestamps` + `model` → 201 `{job_id}` |
| GET | `/api/jobs` | список задач (`?limit=50`) |
| GET | `/api/jobs/{id}` | статус, прогресс, ETA, ошибка |
| GET | `/api/jobs/{id}/events` | SSE: `status`, `progress`, `done`, `error`, heartbeat |
| GET | `/api/jobs/{id}/result.docx` | скачивание DOCX |
| DELETE | `/api/jobs/{id}` | отмена/удаление задачи и файлов |
| GET | `/api/health` | liveness |
| GET | `/api/ready` | readiness: модель загружена, воркер жив, GPU-статус |
| GET | `/api/limits` | лимиты, расширения, модели |

Единый формат ошибок:

```json
{"error": {"code": "unsupported_media", "message": "Формат файла не поддерживается."}}
```

Коды: `file_too_large` (413), `unsupported_media` (415), `validation_error` / `duration_exceeded` / `unsupported_model` (422), `rate_limited` (429), `unauthorized` (401), `not_found` (404), `conflict` (409), `service_unavailable` (503), `internal_error` (500).

Пример загрузки:

```bash
curl -X POST http://localhost:8000/api/jobs \
  -F "file=@lecture.mp4" \
  -F "timestamps=true" \
  -F "model=gigaam-v3-e2e-rnnt"
```

Если задан `AUTH_TOKEN`, передавайте `X-Auth-Token: <token>` или `Authorization: Bearer <token>`. Для SSE и скачивания по прямой ссылке допускается `?token=<token>` (используется только когда нельзя задать заголовок).

### SSE

```
event: status
data: { ...JobOut... }

event: progress
data: { ...JobOut... }

event: done
data: { ...JobOut... }

event: error
data: { ...JobOut... }        // failed/canceled
data: {"error":{"code":"job_deleted","message":"Задача удалена."}}
```

Сервер отправляет комментарий `: heartbeat` каждые 15 секунд. При обрыве браузер переподключается через 3 секунды; UI дополнительно опрашивает `/api/jobs/{id}`.

## DOCX

- Только `.docx` (OOXML), не `.doc` и не переименованный HTML.
- Заголовок «Транскрибация: {имя файла}».
- Таблица метаданных: файл, длительность, дата обработки, модель, язык, время обработки, RTF, версия образа.
- Абзацы: новая строка при паузе > 1.5 с или каждые 6 сегментов.
- Опция таймкодов: `[ЧЧ:ММ:СС]` полужирным перед абзацем.
- Calibri 11, интерлиньяж 1.15, поля 2 см, A4, `w:lang=ru-RU` в стилях и docDefaults.
- Футер: «Страница X из Y» через поля `PAGE`/`NUMPAGES`.
- Тишина/музыка → валидный документ с пометкой «Речь не обнаружена».
- Имя файла: `{stem}__{YYYY-MM-DD_HH-mm}.docx`, санитизация и защита от path traversal.
- MIME: `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, `Content-Disposition: attachment`.

## Хранение и обслуживание

- `/data/uploads` — исходники (удаляются после успешной обработки).
- `/data/work` — WAV и логи ffmpeg (удаляются после обработки).
- `/data/results/{job_id}` — готовые DOCX (живут `RESULT_TTL_HOURS`).
- `/data/gigaam.sqlite3` — очередь и история.
- `/models` — веса.

Результаты и история сохраняются при `docker compose down` и `make up`, пока живы volume `data` и `models`.

```bash
make logs      # логи web + worker (JSON)
make smoke     # реальная модель: пример GigaAM или 2 с тишины
make test      # pytest в контейнере с FakeTranscriber, без GPU
make lint
make typecheck
make backup    # архив volume data в ./backups
make down      # остановить; volume сохраняются
make update    # пересобрать с --pull и перезапустить
```

Обновление: новый тег `IMAGE_TAG` + `make up`; откат: старый тег + `make up`. Внутри контейнера руками ничего не правится.

## Reverse proxy (опционально)

Профиль `proxy` поднимает Caddy с TLS, basic auth, лимитом тела 2 ГБ и отключённой буферизацией для SSE.

```bash
# хеш пароля:
docker run --rm caddy:2.10.2-alpine caddy hash-password --plaintext 'ваш-пароль'

# .env:
# PROXY_DOMAIN=transcribe.example.com
# PROXY_USER=admin
# PROXY_PASSWORD_HASH=$2a$14$...
make proxy        # GPU
# make proxy-cpu  # без GPU
```

Caddy автоматически получает TLS-сертификат для домена (для `localhost` — внутренний CA). `client_max_body_size` аналог — `request_body { max_size 2GB }`; `flush_interval -1` отключает буферизацию SSE; `X-Forwarded-*` передаются автоматически, `web` запускается с `--proxy-headers` и `TRUST_PROXY=true`.

Прямой порт 8000 в профиле proxy закрывается через `ports: !reset []`.

## Наблюдаемость

JSON-логи содержат `job_id`, стадию, длительность, `processing_s`, `rtf`, `peak_vram_mb` (если доступен NVML), коды ошибок. Пример:

```json
{"ts":"2026-11-12T16:44:46.019+00:00","level":"info","logger":"app.worker","message":"job_done","job_id":"...","model":"gigaam-v3-e2e-rnnt","stage":"done","duration_s":3600.0,"processing_s":240.5,"rtf":15.0,"peak_vram_mb":4312.0}
```

`/api/health` — liveness для Docker healthcheck. `/api/ready` — 503, пока модель не загружена или воркер не отвечает; UI показывает «Модель загружается…».

## Тесты

```bash
# локально, без GPU (нужны ffmpeg/ffprobe)
python -m venv .venv
.venv/bin/pip install -e ".[dev,cpu]"
pytest -q -m "not gpu"

# в контейнере
make test
make lint
make typecheck
```

Покрыто: DOCX (OOXML, кириллица, таймкоды, пустой результат, футер), склейка абзацев, API-контракты и коды ошибок, санитизация имён, SQLite-очередь, отмена, восстановление после рестарта, полный цикл с `FakeTranscriber`, статика без CDN.

Smoke с реальной моделью (вне CI):

```bash
RUN_GPU_TESTS=1 pytest -m gpu -q
# или
make smoke
```

## Структура проекта

```
app/
  api/            HTTP-роутеры (jobs, meta)
  asr/            Transcriber, OnnxGigaAM, FakeTranscriber
  static/         index.html, app.css, app.js, favicon.svg
  config.py       pydantic-settings
  db.py           SQLite, очередь, состояние воркера
  docx_render.py  OOXML
  jobs.py         сериализация, файлы, имена
  main.py         FastAPI, статика, уборка
  media.py        ffmpeg/ffprobe, WAV, санитизация
  security.py     auth, rate limit, заголовки
  worker.py       GPU-воркер
scripts/
  download_models.py
  smoke_test.py
tests/
samples/
  example.docx    # реальный пример результата (не входит в образ)
Dockerfile
docker-compose.yml
docker-compose.gpu.yml
docker-compose.proxy.yml
proxy/Caddyfile
Makefile
```

## Диагностика

- **`could not select device driver "" with capabilities: [[gpu]]`** — не установлен/не настроен NVIDIA Container Toolkit. Выполните `nvidia-ctk runtime configure --runtime=docker`, перезапустите Docker и проверьте `docker run --rm --gpus all nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04 nvidia-smi`.
- **В воркере нет `CUDAExecutionProvider`** — проверьте `docker compose exec worker python -c "import onnxruntime as o; print(o.get_available_providers())"`. Если списка CUDA нет, проверьте драйвер/тулкит; если есть, но модель упала на cuDNN — смотрите `docker compose logs worker`. Сервис откатится на CPU с предупреждением, но GPU-скорости не будет.
- **`libcudnn.so.9: cannot open shared object file`** — образ собран не на той базе. Используйте `nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04` или `12.6.3-cudnn-runtime-ubuntu24.04`; не подмешивайте cuDNN 8.
- **`/api/ready` возвращает 503** — воркер ещё загружает модель или не может скачать веса. Проверьте `docker compose logs worker`; выполните `make init`. UI в это время показывает «Модель загружается».
- **`ffmpeg not found` / ошибка извлечения аудио** — проверьте `docker compose exec worker ffmpeg -version`. Если ffmpeg нет, образ собран не из этого Dockerfile; пересоберите `make up`.
- **413 / 429** — сработали лимиты `MAX_UPLOAD_MB`/`MAX_DURATION_MIN`/`QUEUE_MAX`/`UPLOAD_RATE_LIMIT_PER_MIN`. Измените `.env` и перезапустите.
- **SSE не обновляется за прокси** — включите `proxy_buffering off` (nginx) или `flush_interval -1` (Caddy); готовый профиль `make proxy` уже настроен.
- **DOCX не открывается** — файл должен быть OOXML; проверьте, что скачивание идёт с `/api/jobs/{id}/result.docx`, а не из HTML-страницы. Логи рендера — `docker compose logs worker` с `job_id`.
- **Модели не скачиваются** — проверьте DNS/прокси на хосте и свободное место; `make init` можно безопасно повторять, file-lock и кеш HF не дадут скачать лишнее.

## Проверено в этой сборке

- `pytest`: 56 passed, 1 skipped (GPU-тест вне CI).
- `ruff` и `mypy`: без замечаний.
- GPU: RTX 4070 Ti Super 16 ГБ, NVIDIA 610.x, Docker Desktop/WSL2, CUDA 12.8.1.
- GigaAM v3 E2E RNNT **fp32**, часовой MP4 (3600 с, 926 сегментов): обработка 39.5 с, **91× реального времени**, пик VRAM процесса 4.0 ГБ. Это полный путь через API/worker.
- GigaAM v3 E2E RNNT int8, часовой MP4: 215.5 с (**16.7×**), пик VRAM 2.8 ГБ. Даже квантованный вариант проходит критерий ≥10×.
- 3-часовой WAV (тишина): полный путь VAD → DOCX за 26.6 с, RSS worker ~1.1–1.9 ГБ, без OOM; результат — валидный DOCX «Речь не обнаружена».
- CPU int8: 16.3-секундный WAV обработан за 6.3 с (включая загрузку модели); CPU-режим рабочий, но заметно медленнее GPU.
- `docker compose config` проходит для base, GPU и proxy-профилей; Caddyfile валидируется `caddy validate`.
- Образ без весов: 9.05 ГБ в распакованном виде; из них ~7.3 ГБ — база `nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04`. В registry/pull передаются сжатые слои, поэтому сетевой размер меньше; веса в образ не запечены.

## Известные ограничения

- Диаризация, SRT/VTT/TXT/PDF, суммаризация LLM, мультиязычность и OAuth не входят в MVP. Архитектура позволяет добавить их без переписывания: `Transcriber` — протокол, рендеры результатов — отдельный слой.
- Один GPU-воркер: параллельные задачи ждут в очереди. Это осознанный выбор для 16 ГБ GPU.
- При смене модели между задачами новая модель загружается один раз и кешируется (до 2 моделей). Первый запрос CTC может быть дольше.
- CPU-режим значительно медленнее GPU; на 3-часовом файле время может измеряться часами.
- `?token=` для SSE может попасть в access-логи сторонних прокси; сервис запускает uvicorn с `--no-access-log`, а `Referrer-Policy: no-referrer` не даёт утечь токену через referer.
- Автоочистка по TTL удаляет результаты безвозвратно; для архива делайте `make backup`.

Подробный прогон приёмки — в `VALIDATION.md`.

## Лицензии

Код сервиса — MIT (см. `LICENSE`). GigaAM v3, onnx-asr, Silero VAD, ONNX Runtime, python-docx — MIT/BSD; атрибуция в `NOTICE`. Веса моделей не входят в образ по умолчанию и скачиваются с Hugging Face.
