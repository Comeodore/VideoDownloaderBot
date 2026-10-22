"""Обёртка над yt-dlp: скачивание видео/фото из TikTok / Instagram / YouTube Shorts / Threads.

Поддерживает одиночные видео, фото и слайдшоу/карусели (несколько медиа в посте).
"""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable

import yt_dlp

from config import Config

logger = logging.getLogger("videobot.downloader")

# Плеер Telegram (особенно iOS) надёжно играет только H.264 8-bit yuv420p.
# HEVC → лагает, VP9/AV1 → застывает картинка (звук идёт). Всё не-H.264 перекодируем.
_TELEGRAM_VCODEC = "h264"
_TELEGRAM_PIX = {"yuv420p", "yuvj420p"}

SUPPORTED_PATTERNS = (
    r"tiktok\.com",
    r"vm\.tiktok\.com",
    r"instagram\.com",
    r"instagr\.am",
    r"youtube\.com/shorts",
    r"youtube\.com/watch",
    r"youtu\.be/",
    r"threads\.com",
    r"threads\.net",
)
_URL_RE = re.compile(r"https?://[^\s]+")

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_SKIP_EXTS = {".part", ".ytdl", ".temp", ".m4a", ".f4v"}


@dataclass
class MediaItem:
    path: str
    is_video: bool
    width: int | None = None
    height: int | None = None
    duration: int | None = None


@dataclass
class DownloadResult:
    items: list[MediaItem] = field(default_factory=list)
    title: str = "video"
    description: str = ""
    total_size: int = 0
    # True — скачалось только без cookies: сохранённая сессия негодна (см. _extract_info).
    cookies_stale: bool = False


class DownloadError(Exception):
    """Пользовательская ошибка скачивания (текст показываем пользователю).

    auth=True — проблема с доступом (login required / rate-limit / приватный),
    обычно означает протухшие cookies → повод уведомить владельца.
    no_post=True — страницу поста вообще не отдали (редирект на главную,
    заготовка без данных, 404). Признак негодной сессии → есть смысл
    переспросить анонимно, см. _extract_info.
    no_media=True — пост отдали, но медиа в нём не нашлось. Под сессией это
    может быть урезанный ответ API → тоже переспрашиваем анонимно.
    """

    def __init__(self, message: str, auth: bool = False, no_post: bool = False,
                 no_media: bool = False) -> None:
        super().__init__(message)
        self.auth = auth
        self.no_post = no_post
        self.no_media = no_media


def extract_url(text: str) -> str | None:
    match = _URL_RE.search(text or "")
    return match.group(0) if match else None


def is_supported(url: str) -> bool:
    return any(re.search(p, url, re.IGNORECASE) for p in SUPPORTED_PATTERNS)


def is_threads(url: str) -> bool:
    return bool(re.search(r"threads\.(com|net)", url, re.IGNORECASE))


# ── Подмена impersonate-таргета curl_cffi ───────────────────────────────────
# TikTok-экстрактор просит «любой» таргет, и yt-dlp берёт первый из своей карты
# (самый свежий Chrome). Площадки периодически банят конкретный TLS-отпечаток:
# в ответ прилетает WAF-заглушка «Site Maintenance», и экстрактор падает с
# «Unexpected response from webpage request». Поэтому перебираем браузеры сами.
_IMPERSONATE_CLIENTS = ("firefox", "safari", "chrome")
# Скачивания идут в разных потоках (asyncio.to_thread) — таргет попытки храним
# в thread-local, иначе параллельные запросы перетирали бы выбор друг друга.
_pinned = threading.local()


def _install_impersonate_pinning() -> bool:
    """Учит yt-dlp спрашивать наш таргет вместо «первого попавшегося».

    Публичного API нет: параметр impersonate экстрактор перекрывает своим
    wildcard-таргетом — поэтому патчим внутренний _resolve_target. yt-dlp
    обновляется nightly на каждом старте, так что при любой неожиданности
    молча остаёмся на дефолтном поведении.
    """
    try:
        from yt_dlp.networking.impersonate import ImpersonateRequestHandler
    except ImportError:
        return False  # curl_cffi не установлен — impersonation не используется

    original = ImpersonateRequestHandler._resolve_target

    def _resolve_target(self, target):
        pinned = getattr(_pinned, "target", None)
        # Подменяем только wildcard (client=None); явно запрошенный таргет не трогаем.
        if pinned is not None and target is not None and target.client is None:
            return original(self, pinned) or original(self, target)
        return original(self, target)

    try:
        ImpersonateRequestHandler._resolve_target = _resolve_target
    except Exception:  # noqa: BLE001
        return False
    return True


_IMPERSONATE_PINNING = _install_impersonate_pinning()


def _impersonate_candidates() -> list:
    """Таргеты для попыток: по самому свежему на браузер + дефолт yt-dlp в конце."""
    if not _IMPERSONATE_PINNING:
        return [None]
    try:
        from yt_dlp.networking._curlcffi import CurlCFFIRH
        from yt_dlp.networking.impersonate import ImpersonateTarget
        available = {t.client for t in CurlCFFIRH.supported_targets}
        return [ImpersonateTarget(c) for c in _IMPERSONATE_CLIENTS if c in available] + [None]
    except Exception:  # noqa: BLE001
        return [None]


# ── Ошибки: с языка yt-dlp на человеческий ──────────────────────────────────
# yt-dlp пишет для того, кто запускает его руками: «ERROR: [Instagram] C1zz:
# Instagram sent an empty media response. Check if this post is accessible in
# your browser without being logged-in. If it is...». Пользователю бота нужен
# короткий понятный ответ, поэтому режем служебный префикс «ERROR: [Экстрактор]
# <id>:» и переводим известные случаи.
_YTDLP_PREFIX_RE = re.compile(r"^ERROR:\s*(?:\[[^\]]+\]\s*)?(?:[\w.-]+:\s+)?")

# Признаки того, что нужен логин/свежие cookies (auth=True → повод сказать
# владельцу, что пора обновить cookies.txt).
_AUTH_MARKERS = (
    "login", "log in", "sign in", "rate-limit", "rate limit", "private",
    "empty media response", "requires authentication", "not a bot",
)

# Симптомы «страницу поста нам не отдали»: редирект на главную, страница-
# заготовка без данных поста, 404 на весь домен. Так выглядит и закрытый пост,
# и — что важнее — негодная сессия из cookies.txt (Threads на неё отвечает так
# даже для публичных постов). Отсюда флаг no_post и откат в _extract_info.
_NO_POST_MARKERS = (
    "hidden from logged-out", "could not find post data", "http error 404",
)

# «Пост есть, медиа нет» — текст плагина Threads. Отсюда флаг no_media.
_NO_MEDIA_MARKERS = ("has no photo or video",)

# (маркеры в тексте yt-dlp) → что показать. Первое совпадение выигрывает,
# поэтому конкретные случаи идут раньше общих.
_ERROR_MESSAGES = (
    # Свои сообщения из плагина Threads уже человеческие — не портим их общими.
    (("quoted post holding the media",),
     "This post quotes another one, and that post is unavailable — "
     "deleted, or not shown to logged-out visitors."),
    (("hidden from logged-out", "could not find post data"),
     "The site didn't hand over this post — it's deleted, "
     "or visible only to logged-in users."),
    (("has no photo or video",),
     "This post is text only — no photo or video in it."),
    (("unsupported url",),
     "This link doesn't point to a video or a post."),
    (("blocked from accessing", "ip address is blocked"),
     "The site is blocking this server's IP address."),
    (("not available in your country", "geo restriction", "geo-restricted"),
     "This video isn't available in the country where the bot runs."),
    (("is unavailable", "has been removed", "no longer available",
      "video not found", "does not exist"),
     "This video is no longer available — deleted or private."),
    (("requested format", "no video formats", "no formats found"),
     "No downloadable video or photo in this post."),
    (("http error 4", "http error 5", "unable to download video data",
      "forbidden"),
     "The site refused to hand over the file."),
    (("timed out", "timeout", "unable to connect", "connection reset",
      "temporary failure in name resolution"),
     "The site didn't respond in time."),
)


def _as_download_error(e: Exception) -> DownloadError:
    # Берём последнюю строку: у yt-dlp там суть, выше — трассировка.
    raw = str(e).splitlines()[-1].strip()
    clean = _YTDLP_PREFIX_RE.sub("", raw)
    low = clean.lower()

    # Признак «страницы поста не было» считаем по тексту yt-dlp, а не по нашему
    # переводу: перевод человеческий и нужных слов в нём может не остаться.
    no_post = any(marker in low for marker in _NO_POST_MARKERS)
    no_media = any(marker in low for marker in _NO_MEDIA_MARKERS)

    if any(marker in low for marker in _AUTH_MARKERS):
        return DownloadError(
            "This needs a login: the post is private, or the saved cookies "
            "have expired.",
            auth=True, no_post=no_post,
        )
    for markers, text in _ERROR_MESSAGES:
        if any(marker in low for marker in markers):
            return DownloadError(text, no_post=no_post, no_media=no_media)

    # Неизвестный случай — отдаём текст yt-dlp, но без служебного префикса и
    # без простыни на пол-экрана.
    if len(clean) > 200:
        clean = clean[:199].rstrip() + "…"
    return DownloadError(f"Couldn't download it: {clean}", no_post=no_post)


# ── cookies.txt: слияние по домену ──────────────────────────────────────────
# Экспорт из браузера делается по одному сайту, а файл у бота один на все
# площадки. Перезапись целиком убивала бы cookies остальных сайтов, поэтому
# новые записи вливаем поверх старых по ключу (домен, путь, имя).
_COOKIE_HEADER = "# Netscape HTTP Cookie File\n"
_HTTPONLY_PREFIX = "#HttpOnly_"


def _cookie_key(line: str) -> tuple[str, str, str] | None:
    """(домен, путь, имя) для строки Netscape-формата; None — если не запись."""
    parts = line.rstrip("\n").split("\t")
    if len(parts) != 7:
        return None
    domain = parts[0]
    if domain.startswith(_HTTPONLY_PREFIX):
        domain = domain[len(_HTTPONLY_PREFIX):]
    if not domain or domain.startswith("#"):
        return None
    return domain.lower(), parts[2], parts[5]


def merge_cookies_file(upload_path: str, target_path: str) -> list[str]:
    """Вливает записи из upload_path в target_path; возвращает затронутые домены.

    Пустой список = в загруженном файле нет ни одной валидной записи (прислали
    не Netscape-формат). Рабочий файл при этом не тронут — вызывающий обязан
    проверить результат, иначе кривой экспорт стирал бы живые cookies.
    """
    with open(upload_path, encoding="utf-8", errors="replace") as f:
        incoming = [(key, line) for line in f if (key := _cookie_key(line))]
    if not incoming:
        return []

    fresh = dict(incoming)
    lines: list[str] = []
    if os.path.isfile(target_path):
        with open(target_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                key = _cookie_key(line)
                if key is None:
                    continue  # шапку и комментарии пересобираем сами
                lines.append(fresh.pop(key, line))  # своё поверх старого, порядок цел
    lines.extend(fresh.values())

    # Пишем НА МЕСТО (не через rename): файл лежит на общем с Bot API server
    # volume, и подмена инода сменила бы владельца — бот потерял бы доступ.
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(_COOKIE_HEADER)
        for line in lines:
            f.write(line if line.endswith("\n") else line + "\n")
    return sorted({key[0] for key, _ in incoming})


# Метка в info-словаре: скачалось только анонимно, сессия в cookies.txt негодна.
# Кладём в сам словарь, а не в глобал — скачивания идут в разных потоках.
_STALE_COOKIES_KEY = "__videobot_threads_cookies_stale"


def _extract_info(opts: dict, url: str) -> dict:
    """extract_info; для Threads с cookies — с откатом на анонимную попытку."""
    try:
        return _extract_info_attempts(opts, url)
    except DownloadError as e:
        # Threads жёстко валидирует сессию: на негодную cookie он отдаёт 404 или
        # редирект на главную для ЛЮБОЙ страницы — даже для публичного поста,
        # который открывается анонимно (проверено: поддельный sessionid на домене
        # threads.com роняет заведомо рабочий пост, а без него тот же пост
        # качается). Поэтому если с cookies не вышло — пробуем ещё раз без них,
        # иначе протухшая запись ломала бы Threads целиком.
        #
        # Повод — no_post (страницу поста не отдали): это и есть симптом
        # негодной сессии. И no_media: под сессией плагин берёт пост из
        # приватного API, а там цитата может прийти без медиа процитированного
        # поста, хотя гостевой HTML его отдаёт (так было с
        # @margarita_oliverivna/post/Dev_Rq2Dva9: бот ответил «text only»,
        # анонимно — видео из цитаты).
        if (e.auth or not (e.no_post or e.no_media)
                or not (is_threads(url) and opts.get("cookiefile"))):
            raise
        logger.info("Threads: не вышло с cookies (%s) — пробую анонимно", e)
        try:
            info = _extract_info_attempts({**opts, "cookiefile": None}, url)
        except DownloadError as anon:
            # Сессия пост отдала, и он правда без медиа — её ответ и показываем:
            # что гостю пост не виден, ничего к нему не добавляет.
            if e.no_media:
                raise e from anon
            if not anon.no_post:
                raise
            # Ни с сессией, ни без неё страницы поста нет. Пост может быть правда
            # закрыт/удалён, но сессия у нас есть и она не помогла — владельцу
            # стоит проверить cookies, поэтому auth=True.
            raise DownloadError(
                "The site didn't hand over this post, with the saved cookies or "
                "without them — it's deleted, or shown only to a narrower "
                "audience than the saved session belongs to.",
                auth=True, no_post=True,
            ) from anon
        if e.no_media:
            # Сессия рабочая — пост она отдала, просто без медиа. Не протухла.
            return info
        # Анонимно получилось, а с cookies — нет. С рабочей сессией так не
        # бывает: значит запись для threads.com протухла и режет доступ.
        logger.warning("Threads: cookies протухли — без них пост открылся, с ними нет")
        info[_STALE_COOKIES_KEY] = True
        return info


def _extract_info_attempts(opts: dict, url: str) -> dict:
    """extract_info с перебором impersonate-таргетов (обычно хватает первого)."""
    # Экстрактор Threads impersonation не просит, так что перебор таргетов там
    # ничего не меняет — только множит запросы вчетверо (а Threads на частые
    # запросы отвечает страницей без данных поста).
    candidates = [None] if is_threads(url) else _impersonate_candidates()
    for attempt, target in enumerate(candidates, start=1):
        _pinned.target = target
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as e:
            err = _as_download_error(e)
            # Приватное/логин/лимит — отпечаток ни при чём, перебирать нечего.
            if err.auth or attempt == len(candidates):
                raise err from e
        finally:
            _pinned.target = None
    raise DownloadError("Couldn't download it.")  # сюда не попадаем


def _to_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _cookies_for_run(config: Config, work_dir: str) -> str | None:
    """Одноразовая копия cookies.txt на время скачивания (её и скармливаем yt-dlp).

    yt-dlp по завершении пишет cookie-jar ОБРАТНО в файл, и ответ площадки с
    Set-Cookie правит рабочий файл. Для Threads это опасно: отвергнутый запрос
    он сопровождает разлогином — и строка `sessionid` навсегда исчезает из
    cookies.txt бота (проверено). Оригинал меняет только владелец загрузкой.
    """
    src = config.cookies_file
    if not src or not os.path.isfile(src):
        return None
    dst = os.path.join(work_dir, "cookies.txt")
    try:
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o600)
    except OSError:
        logger.exception("Не удалось скопировать cookies — качаю без них")
        return None
    return dst


def _build_opts(config: Config, out_template: str, url: str, work_dir: str) -> dict:
    opts: dict = {
        # У вертикальных видео «1080p» = 1080x1920, т.е. height=1920 — поэтому
        # ограничиваем ДЛИННУЮ сторону (<=1920), иначе height<=1080 резал их до 540p.
        "format": "bv*[height<=1920][width<=1920]+ba/b[height<=1920][width<=1920]/b",
        # Предпочитаем H.264 (Telegram играет только его; иначе перекодируем),
        # затем 1080p. Нативный H.264 не потребует перекодирования.
        "format_sort": ["vcodec:h264", "res:1080"],
        "outtmpl": out_template,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "nocheckcertificate": True,
        "retries": 3,
        "concurrent_fragment_downloads": 4,
        "max_filesize": config.max_file_size_bytes,
    }
    # Карусели Instagram/Threads и фото-слайдшоу TikTok — это «плейлист» из
    # нескольких медиа. Для них разрешаем до 10 элементов (лимит media group в Telegram).
    if re.search(r"instagram|tiktok|threads\.", url, re.IGNORECASE) and "youtube" not in url.lower():
        opts["noplaylist"] = False
        opts["playlistend"] = 10
    # Передаём cookies только если файл реально есть (его могли ещё не загрузить).
    if cookies := _cookies_for_run(config, work_dir):
        opts["cookiefile"] = cookies
    return opts


def _collect_media(work_dir: str) -> list[str]:
    """Собирает финальные медиафайлы из папки запроса (после merge/скачивания)."""
    files = []
    for path in sorted(glob.glob(os.path.join(work_dir, "*"))):
        if not os.path.isfile(path):
            continue
        ext = os.path.splitext(path)[1].lower()
        if ext in _SKIP_EXTS:
            continue
        if ext in VIDEO_EXTS or ext in PHOTO_EXTS:
            files.append(path)
    return files


def _probe_video(path: str) -> dict:
    """Достаёт codec/pix_fmt/размеры/длительность видеопотока (ffprobe)."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,pix_fmt,width,height,duration",
                "-of", "json", path,
            ],
            capture_output=True, text=True, timeout=30,
        ).stdout
        s = json.loads(out)["streams"][0]
        dur = s.get("duration")
        return {
            "codec": s.get("codec_name"),
            "pix": s.get("pix_fmt"),
            "w": _to_int(s.get("width")),
            "h": _to_int(s.get("height")),
            "dur": int(float(dur)) if dur else None,
        }
    except Exception:  # noqa: BLE001
        return {}


def _ensure_telegram_playable(path: str) -> str:
    """Если видео не H.264/8-bit — перекодируем в него (иначе Telegram не играет).

    Нативный H.264 не трогаем (нет нагрузки на CPU). Перекодирование — только для
    VP9/HEVC/AV1/10-bit источников, которых на телефоне не видно.
    """
    meta = _probe_video(path)
    if meta.get("codec") == _TELEGRAM_VCODEC and meta.get("pix") in _TELEGRAM_PIX:
        return path

    out = path + ".h264.mp4"
    # Качество задаёт CRF (18 ≈ визуально без потерь). preset влияет только на
    # размер/скорость при том же качестве — берём veryfast ради слабого CPU мини-ПК.
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", path,
        "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-preset", "veryfast", "-crf", "18",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        out,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=600, capture_output=True)
    except Exception:  # noqa: BLE001  — не смогли перекодировать, отдаём как есть
        if os.path.exists(out):
            os.remove(out)
        return path
    os.replace(out, path)  # сохраняем то же имя/расширение
    return path


def _download_blocking(
    url: str,
    config: Config,
    work_dir: str,
    progress_hook: Callable[[dict], None] | None = None,
) -> DownloadResult:
    # autonumber сохраняет порядок элементов карусели/слайдшоу
    out_template = os.path.join(work_dir, "%(autonumber)03d-%(id)s.%(ext)s")
    opts = _build_opts(config, out_template, url, work_dir)
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]

    info = _extract_info(opts, url)

    files = _collect_media(work_dir)
    if not files:
        raise DownloadError(
            f"Nothing came through — the video is probably over the "
            f"{config.max_file_size_mb} MB limit."
        )

    items = []
    for f in files:
        is_video = os.path.splitext(f)[1].lower() in VIDEO_EXTS
        item = MediaItem(path=f, is_video=is_video)
        if is_video:
            # Гарантируем H.264 (перекодируем VP9/HEVC/AV1), берём размеры из файла.
            item.path = _ensure_telegram_playable(f)
            meta = _probe_video(item.path)
            item.width = meta.get("w")
            item.height = meta.get("h")
            item.duration = meta.get("dur")
        items.append(item)

    total = sum(os.path.getsize(i.path) for i in items)
    if total > config.max_file_size_bytes:
        for i in items:
            try:
                os.remove(i.path)
            except OSError:
                pass
        raise DownloadError(
            f"This is {total / 1024 / 1024:.0f} MB — over the "
            f"{config.max_file_size_mb} MB limit."
        )

    # Текст поста: у одиночных видео лежит в description; у плейлистов (карусели)
    # yt-dlp может держать его только в элементах — берём из первого непустого.
    description = info.get("description") or ""
    if not description:
        for entry in info.get("entries") or []:
            if entry and entry.get("description"):
                description = entry["description"]
                break

    return DownloadResult(
        items=items,
        title=info.get("title") or "video",
        description=description,
        total_size=total,
        cookies_stale=bool(info.get(_STALE_COOKIES_KEY)),
    )


async def download(
    url: str,
    config: Config,
    work_dir: str,
    progress_hook: Callable[[dict], None] | None = None,
) -> DownloadResult:
    """Асинхронная обёртка — yt-dlp блокирующий, гоняем в thread pool."""
    return await asyncio.to_thread(_download_blocking, url, config, work_dir, progress_hook)


# ── Нарезка: скачать длинное видео целиком и вырезать сегменты по таймкодам ──


def _download_full_blocking(
    url: str,
    config: Config,
    work_dir: str,
    progress_hook: Callable[[dict], None] | None = None,
) -> tuple[str, int | None]:
    """Качает видео ЦЕЛИКОМ (без нормализации и лимита размера) для последующей нарезки.

    Возвращает (путь_к_файлу, длительность_сек). Нормализация не нужна — итоговые
    клипы всё равно перекодируются в H.264 при нарезке.
    """
    out_template = os.path.join(work_dir, "full-%(id)s.%(ext)s")
    opts = _build_opts(config, out_template, url, work_dir)
    opts.pop("max_filesize", None)     # целиком качаем без лимита — лимит применим к клипам
    opts["noplaylist"] = True
    opts.pop("playlistend", None)
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]

    info = _extract_info(opts, url)

    videos = [f for f in _collect_media(work_dir) if os.path.splitext(f)[1].lower() in VIDEO_EXTS]
    if not videos:
        raise DownloadError("Couldn't download the video to cut clips from.")
    src = max(videos, key=os.path.getsize)
    return src, _probe_video(src).get("dur") or _to_int(info.get("duration"))


async def download_full(
    url: str, config: Config, work_dir: str,
    progress_hook: Callable[[dict], None] | None = None,
) -> tuple[str, int | None]:
    return await asyncio.to_thread(_download_full_blocking, url, config, work_dir, progress_hook)


def _cut_blocking(src: str, work_dir: str, start: int, end: int, config: Config) -> MediaItem:
    dur = end - start
    out = os.path.join(work_dir, f"clip_{start}_{end}.mp4")
    # -ss перед -i = быстрый seek; при перекодировании нарезка точная. Всегда H.264.
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", str(start), "-i", src, "-t", str(dur),
        "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-preset", "veryfast", "-crf", "18",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", out,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=1800, capture_output=True)
    except Exception as e:  # noqa: BLE001
        raise DownloadError("couldn't cut this piece") from e
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise DownloadError("came out empty, the timecodes are outside the video")
    if os.path.getsize(out) > config.max_file_size_bytes:
        os.remove(out)
        raise DownloadError(f"over the {config.max_file_size_mb} MB limit")
    meta = _probe_video(out)
    return MediaItem(path=out, is_video=True, width=meta.get("w"),
                     height=meta.get("h"), duration=meta.get("dur"))


async def cut_segment(src: str, work_dir: str, start: int, end: int, config: Config) -> MediaItem:
    return await asyncio.to_thread(_cut_blocking, src, work_dir, start, end, config)
