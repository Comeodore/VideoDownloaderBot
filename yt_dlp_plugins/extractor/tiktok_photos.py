"""Фото-посты TikTok (слайдшоу) — плагин-экстрактор для yt-dlp.

Экстрактор TikTok в yt-dlp берёт только /video/<id>: ссылки /photo/<id> он не
узнаёт вовсе («Unsupported URL»). Сама страница /photo/ данных поста не несёт —
её рисует JS в браузере. Но тот же id, открытый как /video/<id>, TikTok отдаёт
целиком, вместе с imagePost — проверено на фото-постах из 1, 2 и 16 картинок.
Поэтому данные берём через _extract_web_data_and_status экстрактора TikTok (с
его обходом WAF и JS-челленджа), а картинки отдаём плейлистом — бот пришлёт их
альбомом. Фоновая музыка слайдшоу — последним элементом плейлиста (MP3): бот
пришлёт её следом отдельным треком.

По /photo/-ссылке TikTok отдаёт иногда и обычное видео — его передаём
стандартному экстрактору.
"""
import unicodedata

from yt_dlp.extractor.tiktok import TikTokIE
from yt_dlp.utils import (
    determine_ext,
    mimetype2ext,
    parse_qs,
    strip_or_none,
    traverse_obj,
    url_or_none,
)


def _printable(text):
    """Текст без неназначенных/служебных символов Unicode (None, если не осталось).

    В именах авторов TikTok встречаются «стилизованные» символы вроде U+FFF4,
    которые клиенты Telegram рисуют квадратиками.
    """
    text = ''.join(ch for ch in text or '' if unicodedata.category(ch) not in ('Cn', 'Co', 'Cs', 'Cc'))
    return text.strip() or None


class TikTokPhotoIE(TikTokIE):
    IE_NAME = 'tiktok:photo'
    _VALID_URL = r'https?://(?:www\.)?tiktok\.com/@(?P<user_id>[\w.-]*)/photo/(?P<id>\d+)'

    _TESTS = [{
        'url': 'https://www.tiktok.com/@hullcity/photo/7553302113757990166',
        'info_dict': {'id': '7553302113757990166'},
        'playlist_count': 2,
    }, {
        # Видео, отданное по /photo/-ссылке
        'url': 'https://www.tiktok.com/@memezar/photo/7449708266168274208',
        'only_matching': True,
    }]

    def _real_extract(self, url):
        video_id, user_id = self._match_valid_url(url).group('id', 'user_id')
        post_url = self._create_url(user_id, video_id)
        data, _ = self._extract_web_data_and_status(post_url, video_id)

        images = traverse_obj(data, (
            'imagePost', 'images', lambda _, v: url_or_none(v['imageURL']['urlList'][0])))
        if not images:
            return self.url_result(post_url, TikTokIE, video_id)

        description = strip_or_none(data.get('desc'))
        title = strip_or_none(traverse_obj(data, ('imagePost', 'title'))) or description
        metadata = traverse_obj(data, {
            'uploader': ('author', 'uniqueId', {str}),
            'uploader_id': ('author', 'id', {str}),
            'channel': ('author', 'nickname', {str}),
            'timestamp': ('createTime', {int}),
        })
        entries = []
        for index, image in enumerate(images, start=1):
            # urlList — зеркала одной и той же картинки (JPEG); берём первое.
            image_url = image['imageURL']['urlList'][0]
            entries.append({
                **metadata,
                'id': f'{video_id}-{index}',
                'title': title,
                'description': description,
                'formats': [{
                    'format_id': 'photo',
                    'url': image_url,
                    'ext': determine_ext(image_url, 'jpg'),
                    'width': image.get('imageWidth'),
                    'height': image.get('imageHeight'),
                }],
                'http_headers': {'Referer': 'https://www.tiktok.com/'},
            })

        if music_url := traverse_obj(data, ('music', 'playUrl', {url_or_none})):
            # Как в экстракторе TikTok: тип — из mime_type в ссылке. Без него
            # CDN отдаёт MP3 (audio/mpeg, с ID3); с mime_type=video_mp4 — AAC в
            # MP4 без видео, то есть M4A (проверено на обоих). Кодеки не
            # указываем: формат «без видео» не прошёл бы фильтр формата бота.
            ext = traverse_obj(parse_qs(music_url), (
                'mime_type', -1, {lambda x: x.replace('_', '/')}, {mimetype2ext})) or 'mp3'
            ext = 'm4a' if ext == 'mp4' else ext
            track = _printable(traverse_obj(data, ('music', 'title', {str})))
            entries.append({
                **metadata,
                'id': f'{video_id}-music',
                'title': track or title,
                'track': track,
                'artist': _printable(traverse_obj(data, ('music', 'authorName', {str}))),
                'duration': traverse_obj(data, ('music', 'duration', {int})),
                'formats': [{'format_id': 'music', 'url': music_url, 'ext': ext}],
                'http_headers': {'Referer': 'https://www.tiktok.com/'},
            })
        return self.playlist_result(
            entries, video_id, title, description, **metadata)
