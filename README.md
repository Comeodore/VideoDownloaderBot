# 🎬 Video Downloader Bot

Телеграм-бот: присылаешь ссылку — получаешь видео.
Поддержка **TikTok (без вотермарки)**, **YouTube Shorts**, **Instagram Reels**.

Стек: [aiogram 3](https://aiogram.dev/) (async) + [yt-dlp](https://github.com/yt-dlp/yt-dlp) + ffmpeg.

## Как работает без вотермарки на TikTok

TikTok отдаёт **оригинал без вотермарки** на часть CDN-эндпоинтов — watermark
накладывается позже, при выдаче в приложение/веб-плеер. yt-dlp забирает поток
до этого шага, поэтому TikTok качается чистым по умолчанию, без доп. настроек.

## Установка

```bash
# 1. ffmpeg (нужен для склейки видео+аудио)
brew install ffmpeg          # macOS
# sudo apt install ffmpeg    # Debian/Ubuntu

# 2. зависимости
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. конфиг
cp .env.example .env
# впиши BOT_TOKEN от @BotFather
```

## Запуск

```bash
source .venv/bin/activate
python bot.py
```

Открой бота в Telegram, нажми `/start`, кинь ссылку.

## Конфигурация (`.env`)

| Переменная | Назначение |
|---|---|
| `BOT_TOKEN` | токен от [@BotFather](https://t.me/BotFather) (обязательно) |
| `MAX_FILE_SIZE_MB` | лимит размера файла (`49` без локального сервера, до `1900` с ним) |
| `COOKIES_FILE` | путь к `cookies.txt` для Instagram / приватного контента |
| `YTDLP_AUTO_UPDATE_HOURS` | период самоперезапуска ради обновления yt-dlp (`0` = выкл) |
| `TELEGRAM_API_BASE` | URL локального Bot API server для файлов до 2 ГБ |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | креды с my.telegram.org — **только для локального сервера** |

## 🐳 Деплой

Три сервиса: бот, локальный Bot API server (файлы до 2 ГБ), Watchtower.

Все данные (скачанные видео + данные Bot API server) живут в одном каталоге —
`VIDEODL_DATA_DIR` из `.env` (по умолчанию `./data`). Он монтируется в **оба**
контейнера по одному пути `/var/lib/telegram-bot-api`, иначе path-режим сервера
не найдёт файлы бота. Если системный диск маленький — укажи каталог на большом.
ffmpeg в образе — **статический бинарь** (не apt), чтобы не раздувать образ.

### Первый деплой

```bash
# на хосте: каталог данных с владельцем uid 101 (= юзер telegram-bot-api)
ssh root@YOUR_SERVER 'mkdir -p /path/to/videodl-data/downloads && chown -R 101:101 /path/to/videodl-data'

# .env на хосте: BOT_TOKEN, TELEGRAM_API_ID/HASH, MAX_FILE_SIZE_MB=1900,
#                VIDEODL_DATA_DIR=/path/to/videodl-data
# залить код (если на хосте нет rsync — scp/tar):
tar czf - --exclude=.git --exclude=.venv --exclude=.env . | ssh root@YOUR_SERVER 'tar xzf - -C /path/to/videodl-bot'

ssh root@YOUR_SERVER 'cd /path/to/videodl-bot && docker compose up -d --build'
```

### Передеплой после правок кода

```bash
scp bot.py downloader.py config.py root@YOUR_SERVER:/path/to/videodl-bot/
ssh root@YOUR_SERVER 'cd /path/to/videodl-bot && docker compose up -d --build bot'
```

### Полезное

```bash
ssh root@YOUR_SERVER 'cd /path/to/videodl-bot && docker compose logs -f bot'   # логи
ssh root@YOUR_SERVER 'cd /path/to/videodl-bot && docker compose ps'            # статус
```

## 🔄 Авто-обновление (двухуровневое)

| Что | Механизм |
|---|---|
| **yt-dlp** (ломается чаще всего) | `entrypoint.sh` делает `pip install -U --pre yt-dlp` при каждом старте + бот сам перезапускается раз в `YTDLP_AUTO_UPDATE_HOURS` (по умолч. 24 ч) через `SIGTERM` → Docker поднимает заново → свежий yt-dlp |
| **telegram-bot-api server** | Watchtower раз в сутки тянет новый образ из registry |
| **Watchtower** | обновляет сам себя |

> Образ бота собирается локально, поэтому Watchtower его не трогает — yt-dlp
> внутри обновляется сам (см. выше), а базовый образ/код — пересборкой.

## Лимит размера файла

Официальный Telegram Bot API **не даёт боту отправлять файлы больше 50 МБ**.
Shorts / TikTok / Reels почти всегда меньше, поэтому без локального сервера хватает.

**Локальный Bot API server** (поднимается в `docker-compose.yml`) снимает лимит
до **2 ГБ**. Как это работает:

- сервис `telegram-bot-api` (образ [`aiogram/telegram-bot-api`](https://github.com/aiogram/telegram-bot-api))
  запускается в `--local` режиме рядом с ботом;
- боту передаётся `TELEGRAM_API_BASE=http://telegram-bot-api:8081`, и aiogram
  шлёт запросы туда вместо `api.telegram.org`;
- серверу нужны **api_id / api_hash** с [my.telegram.org](https://my.telegram.org)
  (это креды Telegram-приложения, не путать с токеном бота);
- после этого `MAX_FILE_SIZE_MB` можно поднять до `1900`.

Без Docker можно запустить сервер вручную и указать его URL в `TELEGRAM_API_BASE`.

## Instagram требует cookies

Instagram часто отвечает `login required` / rate-limit даже на публичных Reels.
Решение — cookies своего аккаунта:

1. Поставь в браузер расширение **«Get cookies.txt LOCALLY»**.
2. Залогинься в Instagram, экспортируй `cookies.txt`.
3. Положи рядом с ботом и укажи путь в `COOKIES_FILE`.

> ⚠️ Используй второстепенный аккаунт — частые скачивания теоретически могут
> привести к ограничениям.

## Структура

```
bot.py                — хендлеры aiogram, polling, плановый self-restart
downloader.py         — обёртка yt-dlp (детекция ссылок, скачивание, лимиты)
config.py             — загрузка .env
Dockerfile            — образ бота (python + ffmpeg)
docker/entrypoint.sh  — апдейт yt-dlp при старте → запуск бота
docker-compose.yml    — бот + локальный Bot API server + Watchtower
```
