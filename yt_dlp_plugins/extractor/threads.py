"""Плагин-экстрактор Threads (threads.com / threads.net) для yt-dlp.

В самом yt-dlp поддержки Threads пока нет (PR yt-dlp/yt-dlp#9852 не смержен).
Это адаптация того PR под механизм плагинов: абсолютные импорты, домен
threads.com (threads.net теперь редиректит на него), карусель — как плейлист,
чтобы бот отправлял её альбомом. Когда экстрактор появится в апстриме,
этот файл можно удалить.

yt-dlp находит плагин автоматически: пакет `yt_dlp_plugins` лежит в корне
проекта, а бот запускается из него (корень попадает в sys.path).
"""
import re

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import (
    ExtractorError,
    remove_end,
    strip_or_none,
    traverse_obj,
)


class ThreadsIE(InfoExtractor):
    _VALID_URL = r'https?://(?:www\.)?threads\.(?:com|net)/(?P<uploader>[^/?#]+)/post/(?P<id>[^/?#&]+)'

    _TESTS = [{
        'url': 'https://www.threads.com/@tntsportsbr/post/C6cqebdCfBi',
        'info_dict': {
            'id': 'C6cqebdCfBi',
            'ext': 'mp4',
            'title': 'md5:062673d04195aa2d99b8d7a11798cb9d',
            'uploader_id': 'tntsportsbr',
        },
    }]

    def _media_entry(self, media, post_code):
        """Формирует запись для одного медиа поста/карусели (только видео)."""
        formats = []
        for video in media.get('video_versions') or []:
            formats.append({
                'format_id': '{}-{}'.format(media.get('pk'), video.get('type')),
                'url': video['url'],
                'width': media.get('original_width'),
                'height': media.get('original_height'),
            })
        if not formats:
            return None
        thumbnails = [
            {'url': t['url'], 'width': t.get('width'), 'height': t.get('height')}
            for t in traverse_obj(media, ('image_versions2', 'candidates')) or []
            if t.get('url')
        ]
        return {
            'id': str(media.get('pk') or post_code),
            'formats': formats,
            'thumbnails': thumbnails,
        }

    def _find_post(self, webpage, video_id):
        """Ищет объект поста в JSON внутри <script> страницы.

        Скриптов с "code":"<id>" несколько, и объект поста в них в разных местах:
        либо сразу в result.data.media, либо в
        result.data.data.edges[].node.thread_items[].post.
        Внутри JSON символ "<" экранирован (<), поэтому [^<]* не обрежет его.
        """
        for mobj in re.finditer(
                rf'<script[^>]+>([^<]*"code":"{re.escape(video_id)}"[^<]*)</script>', webpage):
            result = self._search_json(
                r'"result":', mobj.group(1), 'result data', video_id, default=None)
            if not result:
                continue
            candidates = [traverse_obj(result, ('data', 'media'))]
            for node in traverse_obj(result, ('data', 'data', 'edges')) or []:
                for item in traverse_obj(node, ('node', 'thread_items')) or []:
                    candidates.append(item.get('post'))
            post = next((c for c in candidates if c and c.get('code') == video_id), None)
            if post:
                return post
        return None

    def _real_extract(self, url):
        video_id = self._match_id(url)

        # Threads изредка отдаёт страницу без данных поста — одна повторная
        # загрузка обычно решает.
        post = None
        for attempt in range(2):
            webpage = self._download_webpage(
                url, video_id,
                note='Downloading webpage' if not attempt else 'Retrying webpage (no post data)')
            post = self._find_post(webpage, video_id)
            if post:
                break
        if not post:
            raise ExtractorError('Could not find post data', expected=True)

        username = traverse_obj(post, ('user', 'username'))
        metadata = {
            'title': strip_or_none(remove_end(self._html_extract_title(webpage), '• Threads')),
            'description': self._og_search_description(webpage, default=None),
            'uploader': traverse_obj(post, ('user', 'full_name')) or username,
            'uploader_id': username,
            'uploader_url': f'https://www.threads.com/@{username}' if username else None,
            'channel': username,
            'channel_is_verified': traverse_obj(post, ('user', 'is_verified')),
            'timestamp': post.get('taken_at'),
            'like_count': post.get('like_count'),
        }

        entries = [
            entry for media in post.get('carousel_media') or [post]
            if (entry := self._media_entry(media, video_id))
        ]
        if not entries:
            raise ExtractorError('No video found in this Threads post', expected=True)

        if len(entries) == 1:
            return {**metadata, **entries[0], 'id': video_id}
        return self.playlist_result(
            [{**metadata, **e} for e in entries], playlist_id=video_id,
            playlist_title=metadata['title'])
