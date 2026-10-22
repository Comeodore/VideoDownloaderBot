"""Обёртка над yt-dlp: скачивание видео/фото из TikTok / Instagram / YouTube Shorts / Threads.

Поддерживает одиночные видео, фото и карусели/слайдшоу (несколько медиа в
посте), у слайдшоу TikTok — и фоновую музыку. Фото Instagram, сторис, Threads и
фото-посты TikTok — свои плагины в yt_dlp_plugins: сам yt-dlp у этих площадок
берёт только видео.
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
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qs, urlsplit, urlunsplit

import yt_dlp

from config import Config

logger = logging.getLogger("videobot.downloader")

# Плеер Telegram (особенно iOS) надёжно играет только H.264 8-bit yuv420p.
# HEVC → лагает, VP9/AV1 → застывает картинка (звук идёт). Всё не-H.264 перекодируем.
_TELEGRAM_VCODEC = "h264"
_TELEGRAM_PIX = {"yuv420p", "yuvj420p"}
# Звук: Opus/Vorbis, которые YouTube отдаёт и yt-dlp кладёт в mp4, Telegram-клиенты
# играют не везде — их перекодируем в AAC (дёшево, видео при этом не трогаем).
_TELEGRAM_ACODECS = {"aac", "mp3"}

# Настройки кодирования для Telegram. CRF 18 ≈ визуально без потерь; preset
# влияет только на размер/скорость при том же качестве — veryfast ради CPU мини-ПК.
_H264_ARGS = [
    "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
    "-preset", "veryfast", "-crf", "18",
]
_AAC_ARGS = ["-c:a", "aac", "-b:a", "160k"]

# Площадки бота. Хост ссылки должен совпасть с одним из доменов или быть его
# поддоменом — сверяем разобранный хост, а не подстроку: раньше «tiktok.com»
# где угодно в ссылке (хоть в query) пропускал её в yt-dlp, а generic-экстрактор
# качал что угодно, включая адреса домашней сети, и бот пересылал это в чат.
_SITES = {
    "tiktok": ("tiktok.com",),
    "instagram": ("instagram.com", "instagr.am"),
    "youtube": ("youtube.com", "youtu.be"),
    "threads": ("threads.com", "threads.net"),
}

# Только экстракторы постов наших площадок (yt-dlp сверяет имена целиком).
# Без этого списка ссылку, которую не узнал ни один из них, подбирает generic —
# а он идёт по любому адресу, в том числе по редиректу с площадки куда-то ещё.
# Профили, теги и подборки (tiktok:user, instagram:user, …) тоже не берём:
# ссылка на профиль выкачивала бы чужой аккаунт целиком.
_ALLOWED_EXTRACTORS = [
    r"tiktok", r"vm\.tiktok",                # ролик и короткие vm./t/ ссылки
    r"tiktok:photo",                         # наш плагин: фото-посты
    # Пост/рилс и сторис. «+videobot» — наши надстройки (фото, сторис по
    # номеру): yt-dlp подменяет ими свои экстракторы, и суффикс в имени есть.
    r"instagram(?:\+videobot)?", r"instagram:story(?:\+videobot)?",
    r"youtube", r"youtube:tab",              # tab — ролик, открытый из плейлиста
    r"threads",                              # наш плагин
]

# Сколько медиа поста берём: фото-пост TikTok — до 35 картинок и музыка,
# карусель Instagram — до 20. Бот шлёт их альбомами по 10 (лимит media group).
MAX_POST_ITEMS = 36

_URL_RE = re.compile(r"https?://\S+")
# Знаки, которые на конце ссылки в тексте почти всегда пунктуация, а не её часть.
_URL_TAIL = ".,;:!?…»\"'>]"

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
# Отдельным треком приходит музыка слайдшоу TikTok (MP3 или M4A).
AUDIO_EXTS = {".mp3", ".m4a"}
_SKIP_EXTS = {".part", ".ytdl", ".temp", ".f4v"}
# Промежуточные дорожки yt-dlp до склейки видео со звуком: «001-id.f140.m4a».
_INTERMEDIATE_RE = re.compile(r"\.f[^.]+\.[^.]+$")


@dataclass
class MediaItem:
    path: str
    is_video: bool
    width: int | None = None
    height: int | None = None
    duration: int | None = None
    # Аудио (музыка слайдшоу): бот шлёт его отдельным треком с этими подписями.
    is_audio: bool = False
    title: str | None = None
    performer: str | None = None


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
    waf=True — WAF площадки отверг TLS-отпечаток (TikTok): есть смысл повторить
    с другим impersonate-таргетом, см. _extract_info_attempts.
    """

    def __init__(self, message: str, auth: bool = False, no_post: bool = False,
                 no_media: bool = False, waf: bool = False) -> None:
        super().__init__(message)
        self.auth = auth
        self.no_post = no_post
        self.no_media = no_media
        self.waf = waf


def extract_url(text: str) -> str | None:
    match = _URL_RE.search(text or "")
    if not match:
        return None
    url = match.group(0)
    # «(https://youtu.be/abc).» — хвост из текста, не из ссылки. Закрывающую
    # скобку режем, только если у неё нет пары внутри ссылки.
    while url:
        if url[-1] in _URL_TAIL:
            url = url[:-1]
        elif url[-1] == ")" and url.count("(") < url.count(")"):
            url = url[:-1]
        else:
            break
    return url or None


def site_of(url: str) -> str | None:
    """Площадка бота по ссылке ("tiktok", "instagram", "youtube", "threads") или None."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    # userinfo («tiktok.com@192.168.0.1») и обратный слэш — классика расхождения
    # парсеров: urlsplit видит один хост, а HTTP-клиент может пойти на другой.
    if (parts.scheme not in ("http", "https") or "@" in parts.netloc
            or "\\" in url or port not in (None, 80, 443)):
        return None
    host = (parts.hostname or "").rstrip(".")
    for site, domains in _SITES.items():
        for domain in domains:
            if host != domain and not host.endswith("." + domain):
                continue
            # С youtube.com берём только ролики (/shorts/<id>, /watch?v=<id>):
            # канал или плейлист без v= выкачивался бы целиком.
            if domain == "youtube.com" and not (
                    parts.path.startswith("/shorts/")
                    or (parts.path == "/watch" and parse_qs(parts.query).get("v"))):
                return None
            return site
    return None


def is_supported(url: str) -> bool:
    return site_of(url) is not None


def _normalize_url(url: str) -> str:
    """Ссылка TikTok в форме, которую узнаёт экстрактор yt-dlp; прочие — как есть.

    Экстрактор TikTok требует хост www.tiktok.com: ссылки без www
    (tiktok.com/@u/video/1) и старые мобильные m.tiktok.com/v/<id>.html он не
    узнаёт («Unsupported URL»). Короткие vm./vt. не трогаем — у них свой экстрактор.
    """
    if site_of(url) != "tiktok":
        return url
    parts = urlsplit(url)
    if parts.hostname in ("www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"):
        return url
    path = parts.path
    if old_mobile := re.fullmatch(r"/v/(\d+)\.html", path):
        path = f"/share/video/{old_mobile.group(1)}"
    return urlunsplit(("https", "www.tiktok.com", path, parts.query, ""))


def is_threads(url: str) -> bool:
    return site_of(url) == "threads"


class _InstagramOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Редиректы, которые не уводят с Instagram (иначе — ошибка)."""

    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if site_of(newurl) != "instagram":
            raise urllib.error.HTTPError(newurl, code, "redirect off Instagram", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _resolve_share_link(url: str) -> str:
    """instagram.com/share/… → ссылка на сам пост; остальные ссылки как есть.

    Экстрактор Instagram share-ссылки не берёт (в его _VALID_URL они исключены):
    это редирект на /reel/<id> или /p/<id>. Раньше их разворачивал generic, но
    он теперь под запретом (_ALLOWED_EXTRACTORS) — разворачиваем сами, сверяя
    каждый шаг: увести запрос с Instagram такая ссылка не может.
    """
    if site_of(url) != "instagram" or not urlsplit(url).path.startswith("/share/"):
        return url
    opener = urllib.request.build_opener(_InstagramOnlyRedirects)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with opener.open(request, timeout=15) as resp:
            final = resp.geturl()
    except (OSError, ValueError) as e:
        logger.info("Instagram share-ссылку не развернуть (%s): %s", url, e)
        return url
    return final if site_of(final) == "instagram" else url


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

# Так TikTok-экстрактор падает, когда WAF ответил заглушкой на TLS-отпечаток
# («Site Maintenance» или JS-челлендж вместо страницы). Отсюда флаг waf.
_WAF_MARKERS = ("unexpected response from webpage request", "challenge")

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
    (("unsupported url", "no suitable extractor"),
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


def _raw_error(e: Exception) -> str:
    # Последняя строка: у yt-dlp там суть, выше — трассировка.
    lines = str(e).splitlines()
    return lines[-1].strip() if lines else ""


def _as_download_error(e: Exception) -> DownloadError:
    clean = _YTDLP_PREFIX_RE.sub("", _raw_error(e))
    low = clean.lower()

    # Признак «страницы поста не было» считаем по тексту yt-dlp, а не по нашему
    # переводу: перевод человеческий и нужных слов в нём может не остаться.
    no_post = any(marker in low for marker in _NO_POST_MARKERS)
    no_media = any(marker in low for marker in _NO_MEDIA_MARKERS)
    waf = any(marker in low for marker in _WAF_MARKERS)

    if any(marker in low for marker in _AUTH_MARKERS):
        return DownloadError(
            "This needs a login: the post is private, or the saved cookies "
            "have expired.",
            auth=True, no_post=no_post,
        )
    for markers, text in _ERROR_MESSAGES:
        if any(marker in low for marker in markers):
            return DownloadError(text, no_post=no_post, no_media=no_media, waf=waf)

    # Неизвестный случай — отдаём текст yt-dlp, но без служебного префикса и
    # без простыни на пол-экрана.
    if len(clean) > 200:
        clean = clean[:199].rstrip() + "…"
    return DownloadError(f"Couldn't download it: {clean}", no_post=no_post, waf=waf)


# ── cookies.txt: замена по сайту ────────────────────────────────────────────
# Экспорт из браузера делается по одному сайту, а файл у бота один на все
# площадки. Перезапись целиком убивала бы cookies остальных сайтов, поэтому
# заменяем записи только тех сайтов, что есть в новом экспорте.
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


def _site_domain(domain: str) -> str:
    """Домен записи без ведущей точки и www.: «.www.threads.com» → «threads.com»."""
    domain = domain.lstrip(".")
    return domain[4:] if domain.startswith("www.") else domain


def merge_cookies_file(upload_path: str, target_path: str) -> list[str]:
    """Заменяет в target_path cookies сайтов из upload_path; возвращает эти сайты.

    Экспорт браузера — полный снимок cookies сайта, поэтому старые записи этих
    сайтов (и их поддоменов) выбрасываем целиком, а не сливаем по имени: иначе
    в файле оставался бы, например, старый sessionid, когда свежий экспорт
    сделан из разлогиненного браузера. Записи остальных сайтов не трогаем.

    Пустой список = в загруженном файле нет ни одной валидной записи (прислали
    не Netscape-формат). Рабочий файл при этом не тронут — вызывающий обязан
    проверить результат, иначе кривой экспорт стирал бы живые cookies.
    """
    with open(upload_path, encoding="utf-8", errors="replace") as f:
        incoming = [(key, line) for line in f if (key := _cookie_key(line))]
    if not incoming:
        return []

    sites = {_site_domain(key[0]) for key, _ in incoming}

    def replaced(domain: str) -> bool:
        d = _site_domain(domain)
        return any(d == s or d.endswith("." + s) for s in sites)

    lines: list[str] = []
    if os.path.isfile(target_path):
        with open(target_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                key = _cookie_key(line)
                # Шапку и комментарии пересобираем сами; записи сайтов из
                # экспорта заменяются его записями целиком.
                if key is None or replaced(key[0]):
                    continue
                lines.append(line)
    lines.extend(line for _, line in incoming)

    # Пишем НА МЕСТО (не через rename): файл лежит на общем с Bot API server
    # volume, и подмена инода сменила бы владельца — бот потерял бы доступ.
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(_COOKIE_HEADER)
        for line in lines:
            f.write(line if line.endswith("\n") else line + "\n")
    return sorted(sites)


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
        # приватного API, а там цитаты может не быть вовсе, хотя гостевой HTML
        # её отдаёт вместе с видео. Проверено на
        # @margarita_oliverivna/post/Dev_Rq2Dva9: API — media_type 19 и пустой
        # share_info, ни code, ни pk процитированного поста во всём ответе.
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
    """extract_info; для TikTok — с перебором impersonate-таргетов.

    Перебор нужен только TikTok и только когда его WAF отверг TLS-отпечаток
    (флаг waf). Остальные площадки impersonation не просят, и повтор лишь
    множит запросы: Instagram за это режет rate-limit'ом, Threads отвечает
    страницей без данных поста, а удалённое видео отвечало бы ошибкой вчетверо
    дольше.
    """
    candidates = _impersonate_candidates() if site_of(url) == "tiktok" else [None]
    for attempt, target in enumerate(candidates, start=1):
        _pinned.target = target
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as e:
            err = _as_download_error(e)
            # Сырой текст yt-dlp — в лог: пользователю уходит перевод, а по
            # переводу причину не восстановить.
            logger.warning(
                "yt-dlp: не вышло %s (попытка %d/%d, таргет %s): %s",
                url, attempt, len(candidates), target or "по умолчанию", _raw_error(e))
            if not err.waf or attempt == len(candidates):
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
        # затем 1080p, затем AAC: Opus, который YouTube отдаёт к H.264, пришлось
        # бы перекодировать. Нативные H.264+AAC не требуют перекодирования.
        "format_sort": ["vcodec:h264", "res:1080", "acodec:aac"],
        "outtmpl": out_template,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "allowed_extractors": _ALLOWED_EXTRACTORS,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "concurrent_fragment_downloads": 4,
        "max_filesize": config.max_file_size_bytes,
        # Медиа одного поста, а заодно страховка: любой «плейлист» — не больше
        # MAX_POST_ITEMS, даже если площадка неожиданно отдаст список.
        "playlistend": MAX_POST_ITEMS,
    }
    # Карусели Instagram/Threads — это «плейлист» из нескольких медиа.
    if site_of(url) in ("instagram", "threads"):
        opts["noplaylist"] = False
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
        if ext in _SKIP_EXTS or _INTERMEDIATE_RE.search(os.path.basename(path)):
            continue
        if ext in VIDEO_EXTS or ext in PHOTO_EXTS or ext in AUDIO_EXTS:
            files.append(path)
    return files


def _probe_video(path: str) -> dict:
    """codec/pix_fmt/размеры/длительность видеопотока и кодек звука (ffprobe)."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "stream=codec_type,codec_name,pix_fmt,width,height,duration",
                "-of", "json", path,
            ],
            capture_output=True, text=True, timeout=30,
        ).stdout
        streams = json.loads(out)["streams"]
        s = next(st for st in streams if st.get("codec_type") == "video")
        audio = next((st for st in streams if st.get("codec_type") == "audio"), {})
        dur = s.get("duration")
        return {
            "codec": s.get("codec_name"),
            "pix": s.get("pix_fmt"),
            "acodec": audio.get("codec_name"),
            "w": _to_int(s.get("width")),
            "h": _to_int(s.get("height")),
            "dur": int(float(dur)) if dur else None,
        }
    except Exception:  # noqa: BLE001
        return {}


def _ensure_telegram_playable(path: str) -> str:
    """Приводит видео к тому, что Telegram играет везде: H.264 8-bit + AAC/MP3 в MP4.

    Годное не трогаем (нет нагрузки на CPU). Видео перекодируем только для
    VP9/HEVC/AV1/10-bit — их на телефоне не видно; если не подходит лишь звук
    (Opus с YouTube) — только звук, это секунды. Возвращает путь к итоговому
    .mp4: у перекодированного WebM расширение тоже меняется на .mp4.
    """
    meta = _probe_video(path)
    video_ok = meta.get("codec") == _TELEGRAM_VCODEC and meta.get("pix") in _TELEGRAM_PIX
    audio_ok = meta.get("acodec") in (None, *_TELEGRAM_ACODECS)  # None — звука нет
    base, ext = os.path.splitext(path)
    if video_ok and audio_ok and ext.lower() == ".mp4":
        return path

    out = base + ".tg.mp4"
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", path,
        *(["-c:v", "copy"] if video_ok else _H264_ARGS),
        *(["-c:a", "copy"] if audio_ok else _AAC_ARGS),
        "-movflags", "+faststart",
        out,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=600, capture_output=True)
    except Exception:  # noqa: BLE001  — не смогли перекодировать, отдаём как есть
        logger.warning("Не удалось привести %s к H.264/AAC — отправляю как есть",
                       os.path.basename(path), exc_info=True)
        if os.path.exists(out):
            os.remove(out)
        return path
    final = base + ".mp4"
    os.replace(out, final)
    if final != path:
        os.remove(path)
    return final


def _download_blocking(
    url: str,
    config: Config,
    work_dir: str,
    progress_hook: Callable[[dict], None] | None = None,
) -> DownloadResult:
    url = _resolve_share_link(_normalize_url(url))
    # autonumber сохраняет порядок элементов карусели
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

    # Метаданные элементов по id — для подписей трека (имя файла: NNN-<id>.<ext>).
    entries = {e["id"]: e for e in info.get("entries") or [info] if e and e.get("id")}
    items = []
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        is_video = ext in VIDEO_EXTS
        item = MediaItem(path=f, is_video=is_video, is_audio=ext in AUDIO_EXTS)
        if item.is_audio:
            entry = entries.get(os.path.splitext(os.path.basename(f))[0].split("-", 1)[-1]) or {}
            item.title = entry.get("track") or entry.get("title")
            item.performer = entry.get("artist")
            item.duration = _to_int(entry.get("duration"))
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


# ── Нарезка: качаем с площадки только нужные куски ──────────────────────────


@dataclass
class ClipsResult:
    duration: int | None = None
    # Номер диапазона (как в запросе) → готовый клип либо текст ошибки.
    # Диапазонов, что начинаются за концом видео, нет ни там, ни там.
    clips: dict[int, MediaItem] = field(default_factory=dict)
    errors: dict[int, str] = field(default_factory=dict)
    cookies_stale: bool = False


def _clip_sections(ranges: list[tuple[int, int]]) -> Callable:
    """download_ranges для yt-dlp: наши диапазоны, кроме тех, что за концом видео.

    Конец диапазона yt-dlp сам обрезает по длительности; кусок, начинающийся
    за концом, сломал бы ffmpeg — поэтому его отсеиваем здесь, зная duration.
    """
    def sections(info: dict, ydl: yt_dlp.YoutubeDL) -> list[dict]:
        dur = info.get("duration")
        return [
            {"start_time": start, "end_time": end, "index": i}
            for i, (start, end) in enumerate(ranges)
            if not (dur and start >= dur)
        ]
    return sections


def _download_clips_blocking(
    url: str,
    config: Config,
    work_dir: str,
    ranges: list[tuple[int, int]],
    progress_hook: Callable[[dict], None] | None = None,
) -> ClipsResult:
    """Качает с площадки только куски ranges, а не всё видео.

    Режет сам yt-dlp (download_ranges): ffmpeg читает с площадки ровно нужный
    отрезок. force_keyframes_at_cuts отключает копирование потоков, и с нашими
    аргументами вывода ffmpeg сразу кодирует кусок в H.264/AAC: рез точный по
    кадру, клип готов для Telegram. Раньше видео качалось целиком и без лимита
    размера — «0:00-0:05» по многочасовому стриму стоило гигабайты.
    """
    url = _resolve_share_link(_normalize_url(url))
    out_template = os.path.join(work_dir, "clip-%(section_number)03d.%(ext)s")
    opts = _build_opts(config, out_template, url, work_dir)
    opts.pop("max_filesize", None)  # размер куска заранее не известен — проверяем после
    opts.update({
        "noplaylist": True,
        "download_ranges": _clip_sections(ranges),
        "force_keyframes_at_cuts": True,
        "external_downloader_args": {
            # -rw_timeout (мкс): зависшее соединение роняет ffmpeg, а не вешает задачу.
            "ffmpeg_i": ["-rw_timeout", "30000000"],
            "ffmpeg_o": [*_H264_ARGS, *_AAC_ARGS, "-movflags", "+faststart"],
        },
        # Сбой одного куска не роняет остальные; ошибки извлечения — как обычно.
        "ignoreerrors": "only_download",
    })
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]

    info = _extract_info(opts, url)

    result = ClipsResult(
        duration=_to_int(info.get("duration")),
        cookies_stale=bool(info.get(_STALE_COOKIES_KEY)),
    )
    for i, (start, _end) in enumerate(ranges):
        if result.duration and start >= result.duration:
            continue
        path = next((p for p in _collect_media(work_dir)
                     if os.path.basename(p).startswith(f"clip-{i:03d}.")), None)
        if not path:
            result.errors[i] = "couldn't cut this piece"
            continue
        if os.path.getsize(path) == 0:
            os.remove(path)
            result.errors[i] = "came out empty, the timecodes are outside the video"
            continue
        # Клип уже H.264/AAC — проверка почти бесплатна, но страхует от
        # нестандартного источника (например, WebM без mp4-форматов).
        path = _ensure_telegram_playable(path)
        if os.path.getsize(path) > config.max_file_size_bytes:
            os.remove(path)
            result.errors[i] = f"over the {config.max_file_size_mb} MB limit"
            continue
        meta = _probe_video(path)
        result.clips[i] = MediaItem(path=path, is_video=True, width=meta.get("w"),
                                    height=meta.get("h"), duration=meta.get("dur"))
    return result


async def download_clips(
    url: str, config: Config, work_dir: str, ranges: list[tuple[int, int]],
    progress_hook: Callable[[dict], None] | None = None,
) -> ClipsResult:
    return await asyncio.to_thread(
        _download_clips_blocking, url, config, work_dir, ranges, progress_hook)
