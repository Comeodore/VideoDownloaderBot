"""Надстройки над экстракторами Instagram из yt-dlp: фото и сторис.

Это override'ы, а не отдельные экстракторы: yt-dlp подменяет ими свои
InstagramIE и InstagramStoryIE (к имени добавляется «+videobot»), и всё
остальное — сессия, гостевой доступ, ошибки — остаётся его.

Фото. Экстракторы Instagram в yt-dlp берут только видео: у фото-поста форматов
нет («There is no video in this post»), фото в карусели становятся элементами
без форматов — и карусель с фото не скачивается целиком, а фото-сторис молча
выпадают. Все пути извлечения (API под сессией, GraphQL для гостя, сторис)
собирают медиа через _extract_product_media — здесь у медиа без видео
появляется формат-картинка: крупнейший кандидат в исходных пропорциях, как у
фото в плагине Threads.

Сторис. yt-dlp берёт сторис списком всех сторис автора (feed/reels_media), и
под живой сессией этот запрос отвечает HTTP 500 — сторис не качались вовсе
(проверено 2026-10-22: та же сессия отдаёт посты, гостю reels_media честно
отвечает пустым списком). Ссылка «Поделиться» ведёт на одну сторис, а её номер —
это pk медиа: media/<pk>/info/ под сессией отдаёт её целиком. Поэтому сторис
по ссылке с номером берём так, а остальное (хайлайты, «все сторис автора») —
по-старому.
"""
from yt_dlp.extractor.instagram import InstagramIE, InstagramStoryIE
from yt_dlp.utils import determine_ext, traverse_obj, url_or_none

from yt_dlp_plugins.extractor._images import best_image


def _with_photo_format(info, product_media):
    """Добавляет формат-картинку медиа без видео; видео не трогает."""
    if info.get('formats'):
        return info
    images = traverse_obj(product_media, (
        'image_versions2', 'candidates', lambda _, v: url_or_none(v['url'])))
    if not images:
        return info
    # У гостевого пути размеров у кандидатов нет, но первым идёт оригинал
    # (у остальных в stp= суффикс s480x480 и меньше) — а best_image среди
    # равных оставляет первый.
    best = best_image(product_media, images)
    info['formats'] = [{
        'format_id': 'photo',
        'url': best['url'],
        'ext': determine_ext(best['url'], 'jpg'),
        'width': best.get('width'),
        'height': best.get('height'),
    }]
    return info


class _InstagramIE(InstagramIE, plugin_name='videobot'):
    def _extract_product_media(self, product_media):
        return _with_photo_format(super()._extract_product_media(product_media), product_media)


class _InstagramStoryIE(InstagramStoryIE, plugin_name='videobot'):
    def _extract_product_media(self, product_media):
        return _with_photo_format(super()._extract_product_media(product_media), product_media)

    def _real_extract(self, url):
        username, story_id = self._match_valid_url(url).group('user', 'id')
        # Без сессии сторис не отдаются вовсе — пусть yt-dlp скажет это сам.
        if story_id and username != 'highlights' and self._is_logged_in:
            item = traverse_obj(self._download_json(
                f'{self._API_BASE_URL}/media/{story_id}/info/', story_id,
                'Downloading story info', 'Story info request failed', fatal=False,
                headers=self._api_headers,
                impersonate=self._can_impersonate and self._is_web_app), ('items', 0, {dict}))
            if item:
                return {
                    **self._extract_product(item, get_comments=False),
                    'title': f'Story by {username}',
                }
        return super()._real_extract(url)
