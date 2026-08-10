"""Обёртка над yt-dlp: скачивание видео/фото из TikTok / Instagram / YouTube Shorts / Threads.

Поддерживает одиночные видео, фото и слайдшоу/карусели (несколько медиа в посте).
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable

import yt_dlp

from config import Config

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


class DownloadError(Exception):
    """Пользовательская ошибка скачивания (текст показываем пользователю).

    auth=True — проблема с доступом (login required / rate-limit / приватный),
    обычно означает протухшие cookies → повод уведомить владельца.
    """

    def __init__(self, message: str, auth: bool = False) -> None:
        super().__init__(message)
        self.auth = auth


def extract_url(text: str) -> str | None:
    match = _URL_RE.search(text or "")
    return match.group(0) if match else None


def is_supported(url: str) -> bool:
    return any(re.search(p, url, re.IGNORECASE) for p in SUPPORTED_PATTERNS)


def _to_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _build_opts(config: Config, out_template: str, url: str) -> dict:
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
    if config.cookies_file and os.path.isfile(config.cookies_file):
        opts["cookiefile"] = config.cookies_file
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
    opts = _build_opts(config, out_template, url)
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        low = msg.lower()
        if "login" in low or "rate-limit" in low or "private" in low:
            raise DownloadError(
                "Couldn't download: the content is private, requires login, or hit a "
                "rate limit (common with Instagram). Fresh cookies are needed.",
                auth=True,
            ) from e
        raise DownloadError(f"yt-dlp couldn't download it: {msg.splitlines()[-1]}") from e

    files = _collect_media(work_dir)
    if not files:
        raise DownloadError(
            "Nothing was downloaded — the file may have exceeded the size limit. "
            "Try a shorter video."
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
            f"The media is {total / 1024 / 1024:.1f} MB — over the "
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
    opts = _build_opts(config, out_template, url)
    opts.pop("max_filesize", None)     # целиком качаем без лимита — лимит применим к клипам
    opts["noplaylist"] = True
    opts.pop("playlistend", None)
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        low = msg.lower()
        if "login" in low or "rate-limit" in low or "private" in low:
            raise DownloadError(
                "Couldn't download: the content is private, requires login, or hit a "
                "rate limit (common with Instagram). Fresh cookies are needed.",
                auth=True,
            ) from e
        raise DownloadError(f"yt-dlp couldn't download it: {msg.splitlines()[-1]}") from e

    videos = [f for f in _collect_media(work_dir) if os.path.splitext(f)[1].lower() in VIDEO_EXTS]
    if not videos:
        raise DownloadError("Couldn't download the video to cut from.")
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
        raise DownloadError("couldn't cut this segment") from e
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise DownloadError("empty segment (timecodes out of range?)")
    if os.path.getsize(out) > config.max_file_size_bytes:
        os.remove(out)
        raise DownloadError(f"segment over the {config.max_file_size_mb} MB limit")
    meta = _probe_video(out)
    return MediaItem(path=out, is_video=True, width=meta.get("w"),
                     height=meta.get("h"), duration=meta.get("dur"))


async def cut_segment(src: str, work_dir: str, start: int, end: int, config: Config) -> MediaItem:
    return await asyncio.to_thread(_cut_blocking, src, work_dir, start, end, config)
