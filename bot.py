"""Телеграм-бот: присылаешь ссылку — получаешь видео без вотермарки.

С локальным Bot API server работает в path-режиме: файл скачивается в общий
с сервером каталог, и серверу передаётся путь, а не байты (без двойной передачи,
лимит до 2 ГБ).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import tempfile
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    FSInputFile,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

import downloader
from config import Config, load_config
from heartbeat import beat

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("videobot")

HEARTBEAT_FILE = "/tmp/heartbeat"

dp = Dispatcher()

WELCOME = (
    "👋 Hi! Send me a link to a video from:\n\n"
    "• TikTok\n"
    "• YouTube Shorts\n"
    "• Instagram Reels\n\n"
    "I'll download it and send it back here.\n\n"
    "✂️ To cut clips, add timecodes after the link:\n"
    "<code>&lt;link&gt; 1:00-1:10, 2:10-2:55</code>\n"
    "Each range comes back as a separate video, in order."
)

# Чтобы не спамить владельцу про cookies — не чаще раза в 6 часов.
_last_cookie_notify = {"t": 0.0}


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(WELCOME)


@dp.message(F.document)
async def handle_cookies_upload(message: Message, config: Config) -> None:
    """Owner-flow: владелец присылает cookies.txt документом → горячая замена."""
    if not config.owner_id or not message.from_user or message.from_user.id != config.owner_id:
        return  # тихо игнорируем чужие документы
    doc = message.document
    name = (doc.file_name or "").lower()
    if not (name.endswith(".txt") or "cookie" in name):
        await message.answer("That doesn't look like cookies.txt — send a cookies text file.")
        return
    if not config.cookies_file:
        await message.answer("COOKIES_FILE isn't set in .env — nowhere to save it.")
        return
    if (doc.file_size or 0) > 1_000_000:
        await message.answer("That file is too large for cookies.")
        return
    try:
        await message.bot.download(doc, destination=config.cookies_file)
        os.chmod(config.cookies_file, 0o600)
        # yt-dlp читает cookiefile при каждом скачивании — перезагрузка не нужна.
        await message.answer("✅ Cookies updated — Instagram should work again.")
        logger.info("Cookies обновлены владельцем %s", message.from_user.id)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось сохранить cookies")
        await message.answer("❌ Couldn't save the cookies.")


# Таймкод-диапазоны в сообщении: «1:00-1:10», «2:10-2:55», «1:02:03-1:02:30», «5-30».
_RANGE_RE = re.compile(r"(\d{1,3}(?::\d{2}){0,2})\s*[-–—]\s*(\d{1,3}(?::\d{2}){0,2})")
MAX_CLIPS = 20


def _ts_to_sec(ts: str) -> int:
    sec = 0
    for part in ts.split(":"):
        sec = sec * 60 + int(part)
    return sec


def _fmt_ts(sec: int) -> str:
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _parse_ranges(text: str, url: str) -> tuple[list[tuple[int, int]], list[str]]:
    """Возвращает (валидные диапазоны, отвергнутые строки с start>=end)."""
    # Убираем URL, чтобы цифры внутри него (напр. дефис в YouTube-ID) не ловились как диапазон.
    cleaned = (text or "").replace(url, " ") if url else (text or "")
    ranges, rejected = [], []
    for m in _RANGE_RE.finditer(cleaned):
        start, end = _ts_to_sec(m.group(1)), _ts_to_sec(m.group(2))
        if end > start:
            ranges.append((start, end))
        else:
            rejected.append(m.group(0).strip())
    return ranges, rejected


def _looks_like_timecodes(text: str) -> bool:
    """Похоже ли, что пользователь ХОТЕЛ таймкоды (есть mm:ss), но распарсить не вышло."""
    return bool(re.search(r"\d{1,2}:\d{2}", text or ""))


@dp.message(F.text)
async def handle_link(message: Message, config: Config) -> None:
    url = downloader.extract_url(message.text)
    if not url:
        await message.answer("That doesn't look like a link 🤔 Send me a video link.")
        return

    if not downloader.is_supported(url):
        await message.answer(
            "This link isn't supported. I can do TikTok, YouTube Shorts and Instagram."
        )
        return

    ranges, rejected = _parse_ranges(message.text, url)
    cleaned = (message.text or "").replace(url, " ")
    if ranges:
        await _handle_cut(message, config, url, ranges, rejected)
    elif rejected or _looks_like_timecodes(cleaned):
        # Таймкоды явно задуманы, но ни один не распарсился — не качаем молча всё видео.
        await message.answer(
            "⚠️ I couldn't read those timecodes. Use e.g. "
            "<code>1:00-1:10, 2:10-2:55</code> — format mm:ss, start before end."
        )
    else:
        await _handle_full(message, config, url)


async def _handle_full(message: Message, config: Config, url: str) -> None:
    status = await message.answer("⏳ Downloading…")

    os.makedirs(config.download_dir, exist_ok=True)
    work_dir = tempfile.mkdtemp(prefix="vdl_", dir=config.download_dir)
    try:
        try:
            result = await downloader.download(url, config, work_dir)
        except downloader.DownloadError as e:
            await status.edit_text(f"❌ {e}")
            if e.auth and "instagram" in url.lower():
                await _notify_owner_cookies(message.bot, config)
            return
        except Exception:  # noqa: BLE001
            logger.exception("Неожиданная ошибка при скачивании %s", url)
            await status.edit_text("❌ Something went wrong. Try another link.")
            return

        # «Отправляю…» показываем только для крупных файлов (мелкие уходят мгновенно).
        # Эмодзи тот же, чтобы не было «прыжка» глифа.
        if result.total_size > 25 * 1024 * 1024:
            await _safe_edit(status, "⏳ Uploading…")
        try:
            await _send_media(message, result)
            await status.delete()
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось отправить медиа")
            await status.edit_text(
                "❌ Couldn't send it to Telegram (the file may be too large)."
            )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


async def _handle_cut(
    message: Message, config: Config, url: str,
    ranges: list[tuple[int, int]], rejected: list[str] | None = None,
) -> None:
    ranges = ranges[:MAX_CLIPS]
    if rejected:
        await message.answer("⚠️ Skipped (start ≥ end): " + ", ".join(rejected))
    status = await message.answer(f"⏳ Downloading… (then cutting {len(ranges)} clip(s))")

    os.makedirs(config.download_dir, exist_ok=True)
    work_dir = tempfile.mkdtemp(prefix="cut_", dir=config.download_dir)
    try:
        try:
            src, vdur = await downloader.download_full(url, config, work_dir)
        except downloader.DownloadError as e:
            await status.edit_text(f"❌ {e}")
            if e.auth and "instagram" in url.lower():
                await _notify_owner_cookies(message.bot, config)
            return
        except Exception:  # noqa: BLE001
            logger.exception("cut: не удалось скачать %s", url)
            await status.edit_text("❌ Something went wrong. Try another link.")
            return

        await _safe_edit(status, f"✂️ Cutting {len(ranges)} clip(s)…")
        sent = 0
        for start, end in ranges:  # по порядку → и приходят по порядку
            label = f"{_fmt_ts(start)}–{_fmt_ts(end)}"
            if vdur and start >= vdur:
                await message.answer(f"⏭ {label}: beyond video length ({_fmt_ts(vdur)})")
                continue
            clip_end = min(end, vdur) if vdur else end
            try:
                clip = await downloader.cut_segment(src, work_dir, start, clip_end, config)
                await message.answer_video(
                    video=FSInputFile(clip.path, filename="clip.mp4"),
                    caption=label,
                    duration=clip.duration,
                    width=clip.width,
                    height=clip.height,
                    supports_streaming=True,
                )
                sent += 1
            except downloader.DownloadError as e:
                await message.answer(f"❌ {label}: {e}")
            except Exception:  # noqa: BLE001
                logger.exception("cut: сегмент %s не удался", label)
                await message.answer(f"❌ {label}: couldn't process")

        if sent:
            await status.delete()
        else:
            await _safe_edit(status, "❌ Couldn't produce any clips.")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i : i + n]


async def _send_media(message: Message, result: downloader.DownloadResult) -> None:
    items = result.items

    if len(items) == 1:
        await _send_single(message, items[0])
        return

    # Слайдшоу / карусель — отправляем альбомами по 10 (лимит Telegram).
    for chunk in _chunks(items, 10):
        if len(chunk) == 1:
            await _send_single(message, chunk[0])
            continue
        media = []
        for it in chunk:
            f = FSInputFile(it.path)
            media.append(
                InputMediaVideo(media=f, supports_streaming=True)
                if it.is_video
                else InputMediaPhoto(media=f)
            )
        await message.answer_media_group(media=media)


async def _send_single(message: Message, it: downloader.MediaItem) -> None:
    if it.is_video:
        await message.answer_video(
            video=FSInputFile(it.path, filename="video.mp4"),
            duration=it.duration,
            width=it.width,
            height=it.height,
            supports_streaming=True,
        )
    else:
        await message.answer_photo(photo=FSInputFile(it.path))


async def _safe_edit(status: Message, text: str) -> None:
    try:
        await status.edit_text(text)
    except Exception:  # noqa: BLE001  (message not modified / rate-limit — не страшно)
        pass


async def _notify_owner_cookies(bot: Bot, config: Config) -> None:
    """Сообщает владельцу о протухших cookies (не чаще раза в 6 часов)."""
    if not config.owner_id:
        return
    now = time.monotonic()
    if now - _last_cookie_notify["t"] < 6 * 3600:
        return
    _last_cookie_notify["t"] = now
    try:
        await bot.send_message(
            config.owner_id,
            "⚠️ Looks like the Instagram cookies expired (login/rate-limit).\n"
            "Send me a fresh <b>cookies.txt</b> as a document — I'll pick it up automatically.",
        )
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось уведомить владельца про cookies")


async def _heartbeat() -> None:
    """Трогает файл-маячок — по нему Docker HEALTHCHECK видит живость polling'а."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            with open(HEARTBEAT_FILE, "w") as f:
                f.write(str(int(time.time())))
        except OSError:
            pass
        # also report to the services health monitor (offloaded so a slow/down
        # monitor never stalls polling)
        await loop.run_in_executor(None, lambda: beat("polling alive", service_id="videodl-bot"))
        await asyncio.sleep(30)


async def _scheduled_restart(hours: int) -> None:
    """Раз в `hours` часов мягко гасим процесс через SIGTERM.

    Docker с `restart: unless-stopped` поднимет контейнер заново, а entrypoint
    при старте обновит yt-dlp. Так библиотека всегда свежая без пересборки образа.
    """
    if hours <= 0:
        return
    await asyncio.sleep(hours * 3600)
    logger.info("Плановый рестарт (%d ч) для обновления yt-dlp — шлю SIGTERM.", hours)
    os.kill(os.getpid(), signal.SIGTERM)


async def main() -> None:
    config = load_config()

    session = None
    if config.is_local_server:
        # is_local=True → aiogram шлёт серверу путь к файлу вместо multipart-аплоада.
        api_server = TelegramAPIServer.from_base(config.telegram_api_base, is_local=True)
        session = AiohttpSession(api=api_server)
        logger.info("Локальный Bot API server (path-режим): %s", config.telegram_api_base)

    bot = Bot(
        token=config.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp["config"] = config

    logger.info("Бот запущен. Каталог загрузок: %s | owner: %s", config.download_dir, config.owner_id)
    await bot.delete_webhook(drop_pending_updates=True)

    bg = [
        asyncio.create_task(_heartbeat()),
        asyncio.create_task(_scheduled_restart(config.auto_update_hours)),
    ]
    try:
        await dp.start_polling(bot)
    finally:
        for t in bg:
            t.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановлен.")
