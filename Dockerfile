# syntax=docker/dockerfile:1.7
# Stage 1 тащит статический ffmpeg (его apt-версия тянет ~300МБ зависимостей —
# на маленьком корневом диске мини-ПК это дорого). Финальный образ — slim + бинарь.

# ──────────────────────────────────────────────────────────────────────────
# Stage 1: ffmpeg (статическая сборка)
# ──────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS ffmpeg
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl xz-utils ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz -o /tmp/ff.tar.xz \
    && mkdir -p /ff \
    && tar -xJf /tmp/ff.tar.xz -C /ff --strip-components=1 \
    && cp /ff/ffmpeg /ff/ffprobe /usr/local/bin/

# ──────────────────────────────────────────────────────────────────────────
# Stage 2: runtime
# ──────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ffmpeg /usr/local/bin/ffmpeg /usr/local/bin/ffprobe /usr/local/bin/

# uid/gid 101 — СПЕЦИАЛЬНО совпадает с telegram-bot-api (он работает под uid 101),
# чтобы общий каталог на HDD читался/писался обоими контейнерами без правок прав.
RUN groupadd --system --gid 101 app \
    && useradd --system --uid 101 --gid app --home-dir /srv/app --shell /usr/sbin/nologin app

RUN python -m venv "$VIRTUAL_ENV"

WORKDIR /srv/app
COPY requirements.txt .
# chown venv в ОДНОМ слое с pip install: кешируется, пока не менялся requirements.txt.
# venv отдаём app, чтобы entrypoint мог обновлять yt-dlp на старте (pip -U).
# Дорогой recursive chown больше НЕ зависит от правок кода → быстрые пересборки.
RUN pip install --no-cache-dir -r requirements.txt && chown -R app:app "$VIRTUAL_ENV"

# Правки кода инвалидируют только этот дешёвый слой.
COPY --chown=app:app . .
RUN chmod +x docker/entrypoint.sh

USER app

# Маячок живости: бот трогает /tmp/heartbeat раз в 30с; если завис — Docker рестартит.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,time,sys; sys.exit(0 if os.path.exists('/tmp/heartbeat') and time.time()-os.path.getmtime('/tmp/heartbeat')<120 else 1)"

ENTRYPOINT ["docker/entrypoint.sh"]
