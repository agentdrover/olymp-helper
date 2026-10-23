# Контейнер приложения (OH-T04, SPEC 1.7): FastAPI + ffmpeg + whisper.cpp с весами multilingual small.
# Сборка тянет whisper.cpp и веса (с проверкой sha256); во время работы ничего не скачивается.

# --- Стадия 1: сборка whisper.cpp и загрузка весов -----------------------------------------
FROM debian:12.8-slim AS whisper

ARG WHISPER_CPP_REPO=https://github.com/ggml-org/whisper.cpp
# v1.7.6
ARG WHISPER_CPP_COMMIT=a8d002cfd879315632a579e73f0148d06959de36
# Файл из репозитория ggerganov/whisper.cpp на Hugging Face, закреплён по коммиту репозитория весов.
ARG WHISPER_MODEL_URL=https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1/ggml-small.bin
ARG WHISPER_MODEL_SHA256=1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential cmake git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN git init /src/whisper.cpp \
    && git -C /src/whisper.cpp fetch --depth 1 "$WHISPER_CPP_REPO" "$WHISPER_CPP_COMMIT" \
    && git -C /src/whisper.cpp checkout --detach FETCH_HEAD \
    && test "$(git -C /src/whisper.cpp rev-parse HEAD)" = "$WHISPER_CPP_COMMIT"

# Статическая сборка без GPU: в рантайме нужна только libgomp.
RUN cmake -S /src/whisper.cpp -B /src/build -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=OFF \
        -DGGML_NATIVE=OFF \
    && cmake --build /src/build --config Release -j"$(nproc)" --target whisper-cli \
    && install -D /src/build/bin/whisper-cli /out/whisper-cli

RUN curl --fail --location --silent --show-error -o /out/ggml-small.bin "$WHISPER_MODEL_URL" \
    && echo "${WHISPER_MODEL_SHA256}  /out/ggml-small.bin" | sha256sum -c -

# --- Стадия 2: приложение ------------------------------------------------------------------
FROM python:3.12.8-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin olymp \
    && install -d -o olymp -g olymp /srv/olymp

COPY --from=whisper /out/whisper-cli /usr/local/bin/whisper-cli
COPY --from=whisper /out/ggml-small.bin /opt/whisper/ggml-small.bin

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY deploy/entrypoint.sh deploy/healthcheck.py /app/deploy/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OLYMP_DB=/srv/olymp/olymp.db \
    OLYMP_PORT=8443 \
    WHISPER_BIN=/usr/local/bin/whisper-cli \
    WHISPER_MODEL=/opt/whisper/ggml-small.bin

USER olymp
EXPOSE 8443
ENTRYPOINT ["/bin/sh", "/app/deploy/entrypoint.sh"]
