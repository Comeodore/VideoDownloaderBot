#!/usr/bin/env sh
set -e

# Каталог загрузок лежит на общем с Bot API server volume (HDD).
mkdir -p "${DOWNLOAD_DIR:-/var/lib/telegram-bot-api/downloads}" 2>/dev/null || true

# Обновляем yt-dlp при каждом старте (включая плановые рестарты бота).
# --pre берёт nightly-канал — рекомендованный, т.к. площадки часто меняют API.
echo "[entrypoint] Обновляю yt-dlp до последней nightly..."
pip install --no-cache-dir -U --pre "yt-dlp[default]" || \
    echo "[entrypoint] Обновление не удалось (нет сети?) — запускаюсь на текущей версии."

echo "[entrypoint] yt-dlp: $(yt-dlp --version 2>/dev/null || echo '?') | ffmpeg: $(ffmpeg -version 2>/dev/null | head -1 || echo '?')"

exec python bot.py
