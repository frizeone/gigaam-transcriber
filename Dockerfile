# syntax=docker/dockerfile:1.7

ARG BASE_IMAGE=nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

# ─────────────────────────────────────────────────────────────────────────────
# Builder: Python 3.12 venv + колёса. В финальный образ компиляторы не попадают.
# ─────────────────────────────────────────────────────────────────────────────
FROM ${BASE_IMAGE} AS builder

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 \
        python3.12-venv \
        python3.12-dev \
        build-essential \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python3.12 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /build
COPY requirements.txt ./
RUN python -m pip install --upgrade pip==26.3 \
    && python -m pip install -r requirements.txt

COPY app ./app
COPY scripts ./scripts

ARG PRELOAD_MODELS=0
RUN mkdir -p /opt/models \
    && if [ "${PRELOAD_MODELS}" = "1" ]; then \
         HF_HOME=/opt/models DATA_DIR=/tmp/build-data python scripts/download_models.py; \
       else \
         touch /opt/models/.not_preloaded; \
       fi

# ─────────────────────────────────────────────────────────────────────────────
# Test: dev-зависимости + ffmpeg. Тесты монтируются compose-профилем test,
# поэтому в build-context они не попадают и в runtime-образ не утекают.
# ─────────────────────────────────────────────────────────────────────────────
FROM builder AS test

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-dev.txt pyproject.toml ./
RUN python -m pip install -r requirements-dev.txt

WORKDIR /app
COPY app ./app
COPY scripts ./scripts
ENV ASR_BACKEND=fake \
    DATA_DIR=/tmp/data \
    HF_HOME=/tmp/models \
    PYTHONPATH=/app
CMD ["python", "-m", "pytest", "-q"]

# ─────────────────────────────────────────────────────────────────────────────
# Runtime: тот же CUDA 12.8 + cuDNN 9, Python 3.12, ffmpeg, tini, non-root.
# ─────────────────────────────────────────────────────────────────────────────
FROM ${BASE_IMAGE} AS runtime

ARG DEBIAN_FRONTEND=noninteractive
ARG GIT_SHA=dev
ARG BUILD_DATE=unknown
ARG VERSION=0.1.0

LABEL org.opencontainers.image.title="gigaam-transcriber" \
      org.opencontainers.image.description="Локальная транскрибация русской речи GigaAM v3 E2E с экспортом в DOCX" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.base.name="nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04"

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 \
        ffmpeg \
        tini \
        ca-certificates \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && usermod --login app --home /home/app --move-home ubuntu \
    && groupmod --new-name app ubuntu \
    && usermod --groups "" --shell /usr/sbin/nologin app \
    && mkdir -p /app /data /models \
    && chown -R app:app /data /models

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=random \
    HF_HOME=/models \
    HF_HUB_OFFLINE=0 \
    APP_VERSION=${VERSION} \
    GIT_SHA=${GIT_SHA} \
    IMAGE_TAG=${VERSION}

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/models /opt/models

WORKDIR /app
COPY --chown=app:app app ./app
COPY --chown=app:app scripts ./scripts
COPY --chown=app:app NOTICE LICENSE ./

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
