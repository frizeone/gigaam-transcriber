SHELL := /bin/sh
COMPOSE := docker compose
COMPOSE_UP := docker compose -f docker-compose.yml -f docker-compose.gpu.yml
DATE := $(shell date +%Y%m%d-%H%M%S)

.PHONY: help init up cpu logs smoke test lint typecheck update down backup ps proxy proxy-cpu

help:
	@echo "make init      — скачать веса GigaAM и Silero VAD в volume models"
	@echo "make up        — собрать и запустить с GPU  → http://localhost:8000"
	@echo "make cpu       — собрать и запустить без GPU"
	@echo "make logs      — логи web и worker"
	@echo "make smoke     — smoke-тест реальной модели в контейнере worker"
	@echo "make test      — pytest в контейнере (FakeTranscriber, без GPU)"
	@echo "make lint      — ruff"
	@echo "make typecheck — mypy"
	@echo "make update    — пересобрать и перезапустить"
	@echo "make down      — остановить (volume data/models сохраняются)"
	@echo "make backup    — архив volume data в ./backups"
	@echo "make proxy     — поднять Caddy-профиль с GPU (TLS, basic auth, SSE)"
	@echo "make proxy-cpu — то же без GPU"
	@echo "make ps        — статус контейнеров"

init:
	$(COMPOSE) --profile init run --rm --build models-init

up:
	$(COMPOSE_UP) up -d --build

cpu:
	ASR_DEVICE=cpu ASR_QUANT=int8 $(COMPOSE) up -d --build

logs:
	$(COMPOSE) logs -f web worker

smoke:
	$(COMPOSE) exec worker python -m scripts.smoke_test

test:
	$(COMPOSE) --profile test run --rm --build test

lint:
	$(COMPOSE) --profile test run --rm test ruff check app scripts

typecheck:
	$(COMPOSE) --profile test run --rm test mypy app scripts

update:
	$(COMPOSE) build --pull
	$(COMPOSE_UP) up -d

down:
	$(COMPOSE) down

backup:
	@mkdir -p backups
	docker run --rm -v gigaam_data:/data -v "$(CURDIR)/backups:/backup" alpine:3.20 \
		sh -c "cd /data && tar czf /backup/data-$(DATE).tar.gz ."
	@echo "Архив: backups/data-$(DATE).tar.gz"

ps:
	$(COMPOSE) ps

proxy:
	$(COMPOSE) -f docker-compose.yml -f docker-compose.gpu.yml -f docker-compose.proxy.yml --profile proxy up -d --build

proxy-cpu:
	ASR_DEVICE=cpu ASR_QUANT=int8 $(COMPOSE) -f docker-compose.yml -f docker-compose.proxy.yml --profile proxy up -d --build
