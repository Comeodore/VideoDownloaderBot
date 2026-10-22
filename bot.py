"""Телеграм-бот: присылаешь ссылку — получаешь видео без вотермарки.

С локальным Bot API server работает в path-режиме: файл скачивается в общий
с сервером каталог, и серверу передаётся путь, а не байты (без двойной передачи,
лимит до 2 ГБ).
"""
from __future__ import annotations

import asyncio
import contextlib
import glob
import logging
import os
import re
import shutil
import signal
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from aiogram import Bot, Dispatcher, F, html
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
    "• Instagram Reels\n"
    "• Threads\n\n"
    "I'll download it and send it back here — Instagram and Threads photos "
    "and carousels too.\n\n"
    "✂️ To cut clips, add timecodes after the link:\n"
    "<code>&lt;link&gt; 1:00-1:10, 2:10-2:55</code>\n"
    "Each range comes back as a separate video, in order."
)

# Чтобы не спамить владельцу про cookies — не чаще раза в 6 часов. Начинаем с
# -inf, а не с 0: monotonic() отсчитывает от загрузки хоста, и после свежей
# перезагрузки первое уведомление молчало бы до 6 часов аптайма.
_last_cookie_notify = {"t": float("-inf")}


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
        await message.answer("That doesn't look like cookies.txt — send a .txt cookies file.")
        return
    if not config.cookies_file:
        await message.answer("COOKIES_FILE isn't set in .env — nowhere to save it.")
        return
    if (doc.file_size or 0) > 1_000_000:
        await message.answer("That file is too big for a cookies.txt (limit 1 MB).")
        return
    # Экспорт из браузера обычно по одному сайту, а файл один на все площадки —
    # поэтому вливаем поверх (см. merge_cookies_file), а не перезаписываем.
    upload = config.cookies_file + ".upload"
    try:
        await message.bot.download(doc, destination=upload)
        os.chmod(upload, 0o600)
        domains = downloader.merge_cookies_file(upload, config.cookies_file)
        if not domains:
            await message.answer(
                "No cookie lines found in that file — nothing changed. "
                "Export in <b>Netscape</b> (cookies.txt) format, not JSON."
            )
            return
        os.chmod(config.cookies_file, 0o600)
        # yt-dlp читает cookiefile при каждом скачивании — перезагрузка не нужна.
        await message.answer("✅ Cookies updated for: " + html.quote(", ".join(domains)))
        logger.info("Cookies обновлены владельцем %s: %s",
                    message.from_user.id, ", ".join(domains))
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось сохранить cookies")
        await message.answer("❌ Couldn't save the cookies — check the bot logs.")
    finally:
        try:
            os.remove(upload)
        except OSError:
            pass


# Таймкод-диапазоны в сообщении: «1:00-1:10», «2:10-2:55», «1:02:03-1:02:30», «5-30».
# Границы по краям — чтобы «2024-2025» в тексте не читалось как клип 0:24–3:22.
_RANGE_RE = re.compile(
    r"(?<![\d:])(\d{1,3}(?::\d{2}){0,2})\s*[-–—]\s*(\d{1,3}(?::\d{2}){0,2})(?![\d:])")
MAX_CLIPS = 20
# Сколько секунд клипов можно заказать одним сообщением. Куски кодируются в
# H.264 на CPU мини-ПК, а бот открыт всем — без потолка один запрос
# «0:00-10:00:00» занимал бы его часами.
MAX_CUT_SECONDS = 30 * 60


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
            "This link isn't supported. Send a link to a single video or post "
            "from TikTok, YouTube, Instagram or Threads."
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


# Как часто правим сообщение с прогрессом. Telegram душит частые edit'ы
# (~20/мин на чат), поэтому раз в 5 с — 12 правок в минуту, с запасом.
_PROGRESS_INTERVAL = 5.0


def _make_progress_hook(status: Message, prefix: str = "⏳ Downloading…"):
    """Хук yt-dlp → правки статусного сообщения (для 2-гигабайтных файлов).

    Хук зовётся из рабочего потока (asyncio.to_thread), поэтому корутину правки
    отправляем в основной loop через run_coroutine_threadsafe. Первую правку
    делаем только через интервал — мелкие файлы уходят без лишнего мигания.
    """
    loop = asyncio.get_running_loop()
    state = {"t": time.monotonic()}

    def hook(d: dict) -> None:
        if d.get("status") != "downloading":
            return
        now = time.monotonic()
        if now - state["t"] < _PROGRESS_INTERVAL:
            return
        state["t"] = now
        done = d.get("downloaded_bytes") or 0
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if total:
            text = f"{prefix} {done * 100 // total}% of {total / 1024 / 1024:.0f} MB"
        else:
            text = f"{prefix} {done / 1024 / 1024:.0f} MB"
        try:
            asyncio.run_coroutine_threadsafe(_safe_edit(status, text), loop)
        except RuntimeError:
            pass  # loop уже закрыт (бот гасится) — прогресс не важен

    return hook


# Осиротевшие каталоги запросов: если процесс убили посреди скачивания
# (плановый рестарт, OOM), rmtree в finally не выполнился и каталог остаётся
# на диске навсегда. Подметаем на старте — там же лежат гигабайтные файлы.
STALE_WORK_DIR_AGE = 6 * 3600


def _sweep_stale_work_dirs(config: Config) -> None:
    now = time.time()
    removed = 0
    for prefix in ("vdl_", "cut_"):
        for path in glob.glob(os.path.join(config.download_dir, prefix + "*")):
            if not os.path.isdir(path):
                continue
            try:
                # Свежие не трогаем: вдруг рядом работает второй экземпляр.
                if now - os.path.getmtime(path) < STALE_WORK_DIR_AGE:
                    continue
            except OSError:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    if removed:
        logger.info("Убрано осиротевших каталогов загрузок: %d", removed)


# Сколько запросов обрабатываем одновременно. Каждый — поток yt-dlp, а
# перекодирование и нарезка грузят CPU мини-ПК (4 ядра); бот открыт всем,
# поэтому остальные запросы ждут в очереди — и видят это в статусе.
MAX_PARALLEL_JOBS = 3
_job_slots = asyncio.Semaphore(MAX_PARALLEL_JOBS)
# Запросы в работе, включая ждущие в очереди: плановый рестарт ждёт, пока их не станет.
_jobs_in_flight = 0


@contextlib.asynccontextmanager
async def _job(status: Message, working_text: str):
    """Место в очереди запросов; пока ждём — статус об этом говорит."""
    global _jobs_in_flight
    _jobs_in_flight += 1
    try:
        queued = _job_slots.locked()
        if queued:
            await _safe_edit(status, "⏳ Waiting in line — other downloads are running…")
        async with _job_slots:
            if queued:
                await _safe_edit(status, working_text)
            yield
    finally:
        _jobs_in_flight -= 1


def _who(message: Message) -> str:
    return str(message.from_user.id) if message.from_user else "?"


async def _report_download_error(
    message: Message, status: Message, config: Config, url: str,
    e: downloader.DownloadError,
) -> None:
    """Ошибку скачивания — в лог и пользователю (текст экранируем: HTML-режим)."""
    logger.info("Не скачалось для %s: %s — %s", _who(message), url, e)
    await status.edit_text(f"❌ {html.quote(str(e))}")
    if e.auth and _needs_cookies(url):
        await _notify_owner_cookies(message.bot, config)


async def _handle_full(message: Message, config: Config, url: str) -> None:
    status = await message.answer("⏳ Downloading…")

    async with _job(status, "⏳ Downloading…"):
        os.makedirs(config.download_dir, exist_ok=True)
        work_dir = tempfile.mkdtemp(prefix="vdl_", dir=config.download_dir)
        try:
            try:
                result = await downloader.download(
                    url, config, work_dir, _make_progress_hook(status))
            except downloader.DownloadError as e:
                await _report_download_error(message, status, config, url, e)
                return
            except Exception:  # noqa: BLE001
                logger.exception("Неожиданная ошибка при скачивании %s", url)
                await status.edit_text(
                    "❌ Something went wrong while handling this link."
                )
                return
            logger.info("Скачано для %s: %s — %d файл(ов), %.1f МБ", _who(message), url,
                        len(result.items), result.total_size / 1024 / 1024)

            # Скачалось только без cookies — сессия негодна: владельцу стоит знать,
            # иначе закрытые посты будут молча недоступны (см. _extract_info).
            if result.cookies_stale:
                await _notify_owner_cookies(message.bot, config, stale_session=True)

            # «Отправляю…» показываем только для крупных файлов (мелкие уходят мгновенно).
            # Эмодзи тот же, чтобы не было «прыжка» глифа.
            if result.total_size > 25 * 1024 * 1024:
                await _safe_edit(status, "⏳ Uploading…")
            try:
                await _send_media(message, result, url)
                await status.delete()
            except Exception:  # noqa: BLE001
                logger.exception("Не удалось отправить медиа")
                await status.edit_text(
                    "❌ Downloaded it, but Telegram didn't accept the file."
                )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


async def _handle_cut(
    message: Message, config: Config, url: str,
    ranges: list[tuple[int, int]], rejected: list[str] | None = None,
) -> None:
    ranges = ranges[:MAX_CLIPS]
    if rejected:
        await message.answer(
            "⚠️ Skipped these — the start isn't before the end: " + ", ".join(rejected)
        )
    total = sum(end - start for start, end in ranges)
    if total > MAX_CUT_SECONDS:
        await message.answer(
            f"⚠️ That's {_fmt_ts(total)} of clips in total — I cut up to "
            f"{_fmt_ts(MAX_CUT_SECONDS)} per message."
        )
        return
    working_text = f"✂️ Cutting {len(ranges)} clip(s)…"
    status = await message.answer(working_text)

    async with _job(status, working_text):
        os.makedirs(config.download_dir, exist_ok=True)
        work_dir = tempfile.mkdtemp(prefix="cut_", dir=config.download_dir)
        try:
            try:
                result = await downloader.download_clips(
                    url, config, work_dir, ranges, _make_progress_hook(status, working_text))
            except downloader.DownloadError as e:
                await _report_download_error(message, status, config, url, e)
                return
            except Exception:  # noqa: BLE001
                logger.exception("cut: не удалось скачать %s", url)
                await status.edit_text(
                    "❌ Something went wrong while handling this link."
                )
                return
            logger.info("Нарезано для %s: %s — %d из %d клип(ов)", _who(message), url,
                        len(result.clips), len(ranges))
            if result.cookies_stale:
                await _notify_owner_cookies(message.bot, config, stale_session=True)

            vdur = result.duration
            sent = 0
            for i, (start, end) in enumerate(ranges):  # по порядку → и приходят по порядку
                label = f"{_fmt_ts(start)}–{_fmt_ts(end)}"
                if vdur and start >= vdur:
                    await message.answer(
                        f"⏭ {label}: skipped, the video is only {_fmt_ts(vdur)} long"
                    )
                    continue
                clip = result.clips.get(i)
                if clip is None:
                    error = result.errors.get(i, "couldn't process this piece")
                    await message.answer(f"❌ {label}: {html.quote(error)}")
                    continue
                try:
                    await message.answer_video(
                        video=FSInputFile(clip.path, filename="clip.mp4"),
                        caption=label,
                        duration=clip.duration,
                        width=clip.width,
                        height=clip.height,
                        supports_streaming=True,
                    )
                    sent += 1
                except Exception:  # noqa: BLE001
                    logger.exception("cut: не удалось отправить клип %s", label)
                    await message.answer(f"❌ {label}: Telegram didn't accept this clip")

            if sent:
                await status.delete()
            else:
                await _safe_edit(status, "❌ Couldn't produce any clips.")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


def _chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i : i + n]


# Лимит Telegram на caption. Считается в UTF-16 code units (эмодзи = 2 юнита),
# поэтому режем не по len(), а по юнитам.
CAPTION_LIMIT = 1024


def _truncate_utf16(text: str, limit: int = CAPTION_LIMIT) -> str:
    units = 0
    for i, ch in enumerate(text):
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > limit - 1:  # -1: запас под «…»
            return text[:i] + "…"
    return text


def _caption_for(result: downloader.DownloadResult) -> str | None:
    """Текст поста для caption: description (полный текст), иначе title."""
    text = (result.description or "").strip() or (result.title or "").strip()
    if not text or text == "video":
        return None
    # Пост может содержать <, > и & — экранируем, т.к. бот шлёт в HTML parse mode.
    return html.quote(_truncate_utf16(text))


async def _send_media(message: Message, result: downloader.DownloadResult, url: str) -> None:
    items = result.items
    # Текст поста добавляем только для Threads — у остальных источников
    # description обычно мусорный (хэштеги, авто-заголовки).
    caption = _caption_for(result) if downloader.is_threads(url) else None

    if len(items) == 1:
        await _send_single(message, items[0], caption)
        return

    # Слайдшоу / карусель — отправляем альбомами по 10 (лимит Telegram).
    # Caption вешаем только на первый элемент первого альбома — Telegram
    # показывает его под всем альбомом.
    for chunk in _chunks(items, 10):
        if len(chunk) == 1:
            await _send_single(message, chunk[0], caption)
            caption = None
            continue
        media = []
        for it in chunk:
            f = FSInputFile(it.path)
            media.append(
                InputMediaVideo(media=f, caption=caption, supports_streaming=True)
                if it.is_video
                else InputMediaPhoto(media=f, caption=caption)
            )
            caption = None
        await message.answer_media_group(media=media)


async def _send_single(
    message: Message, it: downloader.MediaItem, caption: str | None = None
) -> None:
    if it.is_video:
        await message.answer_video(
            video=FSInputFile(it.path, filename="video.mp4"),
            caption=caption,
            duration=it.duration,
            width=it.width,
            height=it.height,
            supports_streaming=True,
        )
    else:
        await message.answer_photo(photo=FSInputFile(it.path), caption=caption)


async def _safe_edit(status: Message, text: str) -> None:
    try:
        await status.edit_text(text)
    except Exception:  # noqa: BLE001  (message not modified / rate-limit — не страшно)
        pass


def _needs_cookies(url: str) -> bool:
    """Площадки, где логин-ошибка означает протухшие cookies, а не битую ссылку."""
    return downloader.site_of(url) in ("instagram", "threads")


async def _notify_owner_cookies(
    bot: Bot, config: Config, stale_session: bool = False,
) -> None:
    """Сообщает владельцу о протухших cookies (не чаще раза в 6 часов).

    stale_session=True — частный случай Threads: пост скачался только после
    того, как cookies отбросили. Значит сессия не просто «не хватило прав», а
    активно мешает: с ней Threads не отдаёт даже публичные посты.
    """
    if not config.owner_id:
        return
    now = time.monotonic()
    if now - _last_cookie_notify["t"] < 6 * 3600:
        return
    _last_cookie_notify["t"] = now
    text = (
        "⚠️ Looks like the cookies expired (login required / rate limit).\n"
        "Export cookies.txt for the site that failed — <b>instagram.com</b> or "
        "<b>threads.com</b> — and send it here as a document. I merge it into "
        "the existing file, so the other sites keep working."
    )
    if stale_session:
        text = (
            "⚠️ The saved <b>threads.com</b> session is being refused: this post "
            "only came through after I dropped the cookies. While that session "
            "sits in cookies.txt, Threads hides posts that need a login.\n"
            "Export cookies.txt for <b>threads.com</b> (logged in, with httpOnly "
            "cookies) and send it here as a document — I merge it into the "
            "existing file, so the other sites keep working."
        )
    try:
        await bot.send_message(config.owner_id, text)
    except Exception:  # noqa: BLE001
        logger.exception("Не удалось уведомить владельца про cookies")


# Отметка «event loop жив»: heartbeat ставит её на каждом ходе, watchdog-поток
# проверяет, не застыла ли она.
_loop_tick = {"t": time.monotonic()}
# Столько секунд без отметки = loop завис (ход heartbeat — раз в 30 с).
LOOP_STALL_LIMIT = 180
# Свой поток для пушей в health-monitor: общий пул asyncio занят скачиваниями
# (to_thread), и при загруженном пуле heartbeat ждал бы там своей очереди.
_beat_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="heartbeat")


async def _heartbeat() -> None:
    """Раз в 30 с: отметка для watchdog, файл-маячок для HEALTHCHECK, пуш в монитор.

    Всё идёт с event loop: если он завис, маячок стареет, пуши прекращаются,
    а watchdog перезапускает процесс (см. _start_watchdog).
    """
    loop = asyncio.get_running_loop()
    while True:
        _loop_tick["t"] = time.monotonic()
        try:
            with open(HEARTBEAT_FILE, "w") as f:
                f.write(str(int(time.time())))
        except OSError:
            pass
        # Ответа не ждём: медленный или лежащий монитор не должен тормозить этот
        # цикл. beat() сам глотает свои ошибки.
        loop.run_in_executor(_beat_pool, lambda: beat("polling alive", service_id="videodl-bot"))
        await asyncio.sleep(30)


def _start_watchdog() -> None:
    """Поток, который перезапускает процесс, если event loop завис.

    Docker сам unhealthy-контейнер не рестартит: restart-политика срабатывает
    только на выход процесса. Поэтому выходим сами — `restart: unless-stopped`
    поднимет бота заново.
    """
    def watch() -> None:
        while True:
            time.sleep(30)
            stalled = time.monotonic() - _loop_tick["t"]
            if stalled > LOOP_STALL_LIMIT:
                logger.critical("Event loop не отвечает %.0f с — перезапускаю процесс", stalled)
                os._exit(1)

    threading.Thread(target=watch, name="watchdog", daemon=True).start()


# Сколько плановый рестарт ждёт простоя, прежде чем перезапуститься всё равно.
RESTART_IDLE_WAIT = 3600


async def _scheduled_restart(hours: int) -> None:
    """Раз в `hours` часов мягко гасим процесс через SIGTERM — дождавшись простоя.

    Docker с `restart: unless-stopped` поднимет контейнер заново, а entrypoint
    при старте обновит yt-dlp. Так библиотека всегда свежая без пересборки образа.
    Рестарт посреди скачивания оставил бы пользователя с вечным «Downloading…»,
    поэтому ждём, пока запросов в работе не станет (но не дольше RESTART_IDLE_WAIT).
    """
    if hours <= 0:
        return
    await asyncio.sleep(hours * 3600)
    deadline = time.monotonic() + RESTART_IDLE_WAIT
    while _jobs_in_flight and time.monotonic() < deadline:
        await asyncio.sleep(10)
    if _jobs_in_flight:
        logger.warning("Плановый рестарт: простоя не дождался, прерываю запросов: %d",
                       _jobs_in_flight)
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

    os.makedirs(config.download_dir, exist_ok=True)
    _sweep_stale_work_dirs(config)

    logger.info("Бот запущен. Каталог загрузок: %s | owner: %s", config.download_dir, config.owner_id)

    bg = [
        asyncio.create_task(_heartbeat()),
        asyncio.create_task(_scheduled_restart(config.auto_update_hours)),
    ]
    _start_watchdog()
    # Апдейты, пришедшие, пока бот лежал (плановый рестарт, деплой), не
    # выбрасываем: иначе ссылки, присланные в эти секунды, молча терялись бы.
    await bot.delete_webhook(drop_pending_updates=False)
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
