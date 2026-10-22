"""Плагин-экстрактор Threads (threads.com / threads.net) для yt-dlp.

В самом yt-dlp поддержки Threads пока нет (PR yt-dlp/yt-dlp#9852 не смержен).
Это адаптация того PR под механизм плагинов: абсолютные импорты, домен
threads.com (threads.net теперь редиректит на него), карусель — как плейлист,
чтобы бот отправлял её альбомом. Сверх PR добавлены фото (в самом PR только
видео), медиа из процитированного/репостнутого поста и распознавание
недоступного поста. Когда экстрактор появится в апстриме, этот файл можно
удалить — но фото проверь, их там может не быть.

yt-dlp находит плагин автоматически: пакет `yt_dlp_plugins` лежит в корне
проекта, а бот запускается из него (корень попадает в sys.path).
"""
import re

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import (
    ExtractorError,
    decode_base_n,
    determine_ext,
    parse_qs,
    remove_end,
    strip_or_none,
    traverse_obj,
)

_BASE_URL = 'https://www.threads.com/'
# X-IG-App-ID веб-клиента Threads: без него приватный API отвечает 302 на логин.
# Значение лежит в любой странице threads.com (RelayAPIConfigDefaults.customHeaders).
_WEB_APP_ID = '238260118697367'
# Алфавит шорткодов Instagram/Threads: код поста — это его numeric pk в base64url.
_CODE_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'


# Пост может быть цитатой или репостом чужого: своих медиа у него нет
# (media_type 19 — «текст»), а видео лежит во вложенном посте-источнике.
# Ключи разные для цитаты-вложения, обычной цитаты и репоста — смотрим все.
_SHARED_POST_KEYS = ('quoted_attachment_post', 'quoted_post', 'reposted_post')


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
        """Формирует запись для одного медиа поста/карусели: видео или фото."""
        images = [
            t for t in traverse_obj(media, ('image_versions2', 'candidates')) or []
            if t.get('url')
        ]
        formats = [{
            'format_id': '{}-{}'.format(media.get('pk'), video.get('type')),
            'url': video['url'],
            'width': media.get('original_width'),
            'height': media.get('original_height'),
        } for video in media.get('video_versions') or []]

        if formats:
            thumbnails = [
                {'url': t['url'], 'width': t.get('width'), 'height': t.get('height')}
                for t in images
            ]
            return {
                'id': str(media.get('pk') or post_code),
                'formats': formats,
                'thumbnails': thumbnails,
            }

        # Фото-пост или фото внутри карусели: video_versions нет, зато есть
        # кандидаты картинки разных размеров.
        if not images:
            return None  # текстовый пост (media_type 19) — качать нечего
        best = self._best_image(media, images)
        return {
            'id': str(media.get('pk') or post_code),
            'formats': [{
                'format_id': 'photo',
                'url': best['url'],
                'ext': determine_ext(best['url'], 'jpg'),
                'width': best.get('width'),
                'height': best.get('height'),
            }],
        }

    @staticmethod
    def _best_image(media, images):
        """Крупнейший кандидат В ОРИГИНАЛЬНОМ соотношении сторон.

        Кандидаты идут парами: оригинал и квадратный кроп того же размера
        (972x1296 и 972x972). Сортировка yt-dlp по res их не различает и берёт
        кроп, обрезая картинку, — поэтому фильтруем по original_width/height
        сами и отдаём один формат, а не весь список.
        """
        def area(t):
            return (t.get('width') or 0) * (t.get('height') or 0)

        width, height = media.get('original_width'), media.get('original_height')
        if width and height:
            ratio = width / height
            original = [
                t for t in images if t.get('width') and t.get('height')
                and abs(t['width'] / t['height'] - ratio) < 0.01
            ]
            if original:
                return max(original, key=area)
        return max(images, key=area)

    def _entries(self, post, post_code):
        """Медиа поста: карусель — по элементам, одиночный — сам объект."""
        return [
            entry for media in post.get('carousel_media') or [post]
            if (entry := self._media_entry(media, post_code))
        ]

    @staticmethod
    def _shared_post(post):
        """Вложенный пост-источник цитаты/репоста и признак его недоступности.

        Гостю Threads такой пост не отдаёт вовсе: ставит *_unavailable и режет
        медиа — снаружи это выглядит как обычный текстовый пост. Проверено: тот
        же URL под валидной сессией отдаёт quoted_attachment_post целиком, с
        video_versions, а анонимно — media_type 19 и пустые кандидаты.
        """
        share_info = traverse_obj(post, ('text_post_app_info', 'share_info')) or {}
        for key in _SHARED_POST_KEYS:
            if shared := share_info.get(key):
                return shared, False
        return None, any(share_info.get(f'{key}_unavailable') for key in _SHARED_POST_KEYS)

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

    def _has_session(self):
        """Есть ли в cookies живая сессия Threads (только с ней работает API)."""
        return bool(self._get_cookies(_BASE_URL).get('sessionid'))

    @staticmethod
    def _pk_from_code(code):
        """Числовой pk медиа из шорткода поста (он же — pk в base64url)."""
        try:
            return decode_base_n(code, table=_CODE_CHARS)
        except ValueError:
            return None

    def _api_post(self, video_id, pk=None):
        """Объект поста из приватного API Threads — нужна сессия в cookies.

        Форма ответа та же, что у объекта из HTML (code / media_type /
        carousel_media / video_versions / user / caption), так что дальше пост
        обрабатывается общим кодом. Без сессии API отвечает 302 на логин.
        """
        pk = pk or self._pk_from_code(video_id)
        if not pk:
            return None
        data = self._download_json(
            f'{_BASE_URL}api/v1/media/{pk}/info/', video_id,
            note='Downloading post from API', errnote='Post API request failed',
            fatal=False, headers={'X-IG-App-ID': _WEB_APP_ID, 'Referer': _BASE_URL})
        post = traverse_obj(data, ('items', 0))
        # Сверяем code: по чужому pk (например из мусорного редиректа) API
        # молча отдал бы другой пост.
        return post if post and post.get('code') == video_id else None

    def _webpage_post(self, url, video_id):
        """Объект поста из HTML страницы + сама страница (для метаданных)."""
        # Threads изредка отдаёт страницу без данных поста — одна повторная
        # загрузка обычно решает.
        for attempt in range(2):
            webpage, urlh = self._download_webpage_handle(
                url, video_id,
                note='Downloading webpage' if not attempt else 'Retrying webpage (no post data)')
            # Недоступный пост (удалён / скрыт от гостей) Threads не отдаёт 404:
            # молча редиректит на главную (/?error=invalid_post) и присылает
            # HTTP 200 с чужой лентой. Поэтому смотрим финальный URL, не статус —
            # иначе поиск данных поста упирался бы в невнятное «no post data».
            if video_id not in urlh.url:
                # Но у залогиненного клиента редирект в ленту — это норма, а не
                # «поста нет»: pk нужного медиа Threads кладёт в injected_media_ids.
                injected = traverse_obj(parse_qs(urlh.url), ('injected_media_ids', 0)) or ''
                pk = self._search_regex(r'\d{6,}', injected, 'media id', default=None)
                post = self._api_post(video_id, pk) if pk else None
                if post:
                    return post, None
                raise ExtractorError(
                    'This post is unavailable — it may have been deleted, '
                    'or it is hidden from logged-out visitors', expected=True)
            post = self._find_post(webpage, video_id)
            if post:
                return post, webpage
        return None, webpage

    def _real_extract(self, url):
        video_id = self._match_id(url)

        # Залогиненному клиенту Threads страницу поста не рендерит вовсе: отвечает
        # 302 в ленту (/?injected_media_ids=["<pk>"]) и отдаёт медиа только через
        # приватный API. Поэтому при живой сессии сразу идём в API — иначе
        # обычный редирект выглядел бы как «пост удалён», а закрытые посты
        # («This content isn't available to everyone») не достать вообще.
        webpage = None
        post = self._api_post(video_id) if self._has_session() else None
        if not post:
            post, webpage = self._webpage_post(url, video_id)
        if not post:
            raise ExtractorError('Could not find post data', expected=True)

        username = traverse_obj(post, ('user', 'username'))
        # Текст поста берём из caption: он полный и есть на обоих путях, а
        # <title>/og:description — только у страницы поста (в API-пути её нет).
        caption = strip_or_none(traverse_obj(post, ('caption', 'text')))
        page_title = strip_or_none(
            remove_end(self._html_extract_title(webpage), '• Threads')) if webpage else None
        metadata = {
            'title': caption or page_title,
            'description': caption or (
                self._og_search_description(webpage, default=None) if webpage else None),
            'uploader': traverse_obj(post, ('user', 'full_name')) or username,
            'uploader_id': username,
            'uploader_url': f'https://www.threads.com/@{username}' if username else None,
            'channel': username,
            'channel_is_verified': traverse_obj(post, ('user', 'is_verified')),
            'timestamp': post.get('taken_at'),
            'like_count': post.get('like_count'),
        }

        entries = self._entries(post, video_id)
        if not entries:
            # Своих медиа нет — значит либо пост правда текстовый, либо он
            # цитирует/репостит чужой и видео лежит там.
            shared, unavailable = self._shared_post(post)
            if shared:
                entries = self._entries(shared, video_id)
            elif unavailable:
                raise ExtractorError(
                    'The quoted post holding the media is unavailable — it may have '
                    'been deleted, or it is not shown to logged-out visitors',
                    expected=True)
        if not entries:
            raise ExtractorError('This Threads post has no photo or video', expected=True)

        if len(entries) == 1:
            return {**metadata, **entries[0], 'id': video_id}
        return self.playlist_result(
            [{**metadata, **e} for e in entries], playlist_id=video_id,
            playlist_title=metadata['title'])
