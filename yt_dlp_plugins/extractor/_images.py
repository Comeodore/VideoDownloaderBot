"""Общее для плагинов Instagram и Threads: выбор картинки из кандидатов.

Модуль начинается с «_» — загрузчик плагинов yt-dlp его пропускает, сюда
ходят только явным импортом.
"""


def best_image(media, images):
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
