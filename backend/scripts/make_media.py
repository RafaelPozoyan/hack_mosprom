"""Генерирует брендированные шаблоны изображений и PDF, на которые ссылается сценарий (в ТЗ — «Изображение N»).

Это заглушки-шаблоны: HR заменяет их реальными макетами, просто положив файл с тем же именем в media/.
Запуск: uv run --group dev python scripts/make_media.py
"""

import random
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parents[1] / "media"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
W, H = 1200, 800
DARK, DARK2, LIME, WHITE = (11, 46, 58), (16, 78, 96), (166, 206, 57), (255, 255, 255)

CARDS = {
    "img_01.png": ("Корсик", "Твой гид по адаптации в Группе ПОЛИПЛАСТИК"),
    "img_02.png": ("Добро пожаловать в команду!", "Первые три месяца мы пройдём вместе"),
    "img_03.png": ("Документы для оформления", "Подготовь их к первому рабочему дню"),
    "img_04.png": ("С первым рабочим днём!", "Мы рады, что ты с нами"),
    "img_06.png": ("Справочник контактов", "8500 коллег на связи"),
    "img_07.png": ("Адаптационный план", "Шаг за шагом к уверенной работе"),
    "img_09.png": ("Финишная прямая", "Испытательный срок почти позади"),
    "img_10.png": ("Так держать!", "Ещё один важный этап пройден"),
    "img_11.png": ("Корпоративные мероприятия", "Спорт, праздники и большая команда"),
    "img_12.png": ("Небольшое напоминание", "Опрос займёт всего 1–2 минуты"),
    "img_13.png": ("Посоветуюсь с коллегами", "Передам вопрос в отдел подбора персонала"),
    "img_14.png": ("Как дела?", "Расскажи, чем помочь"),
    "img_15.png": ("Встреча по адаптационному плану", "Детали пришлёт закреплённый за тобой сотрудник HR"),
    "img_16.png": ("До встречи!", "Было здорово сопровождать тебя"),
    "postcard_1.png": ("Открытка", "С началом пути в ПОЛИПЛАСТИК!"),
    "postcard_2.png": ("Открытка", "Два месяца вместе — так держать!"),
    "postcard_3.png": ("Открытка", "Вдохновения и смелых идей!"),
    "events_photo.png": ("Фото с мероприятий", "Шаблон: HR загружает фотоархив корпоративных событий"),
    "map_gtp.png": ("Карта территории", "Шаблон: HR загружает актуальную карту площадки GTP"),
    "map_luzhniki.png": ("Карта этажей офиса", "Шаблон: HR загружает план этажей офиса «Лужники»"),
}

FOOD = {
    "food_grpp.pdf": "Группа ПОЛИПЛАСТИК — офис",
    "food_luzhniki.pdf": "Офис «Лужники»",
    "food_gtp.pdf": "Производственная площадка GTP",
}


def background(seed: int) -> Image.Image:
    img = Image.new("RGB", (W, H), DARK)
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(DARK, DARK2, strict=True)))
    rnd = random.Random(seed)
    for _ in range(26):  # «воксели» в стиле айдентики хакатона
        s = rnd.choice([24, 32, 48])
        x, y = rnd.randrange(int(W * 0.7), W - s), rnd.randrange(0, H - 100 - s)
        color = LIME if rnd.random() < 0.6 else (60, 160, 190)
        d.rectangle([x, y, x + s, y + s], fill=color)
        d.rectangle([x + s // 4, y + s // 4, x + s - 4, y + s - 4], outline=DARK, width=2)
    return img


def paw(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    d.ellipse([cx - r, cy - int(r * 0.8), cx + r, cy + int(r * 0.8)], fill=LIME)
    for dx, dy in ((-1.1, -1.25), (-0.4, -1.65), (0.4, -1.65), (1.1, -1.25)):
        tx, ty = cx + int(dx * r), cy + int(dy * r)
        d.ellipse([tx - r // 3, ty - r // 3, tx + r // 3, ty + r // 3], fill=LIME)


def card(name: str, title: str, subtitle: str, seed: int) -> None:
    img = background(seed)
    d = ImageDraw.Draw(img)
    paw(d, 140, 190, 46)
    d.text(
        (80, 300),
        "\n".join(textwrap.wrap(title, 20)),
        font=ImageFont.truetype(FONT_BOLD, 64),
        fill=WHITE,
        spacing=10,
    )
    lines = textwrap.wrap(subtitle, 34)
    d.text(
        (80, 300 + 80 * len(textwrap.wrap(title, 20)) + 20),
        "\n".join(lines),
        font=ImageFont.truetype(FONT, 34),
        fill=LIME,
        spacing=8,
    )
    d.text(
        (80, H - 60),
        "Корсик · Группа ПОЛИПЛАСТИК · шаблон изображения, сгенерирован автоматически",
        font=ImageFont.truetype(FONT, 18),
        fill=(150, 190, 200),
    )
    img.save(OUT / name)


def food_pdf(name: str, site: str) -> None:
    img = Image.new("RGB", (1240, 1754), WHITE)  # A4 @150 dpi
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 1240, 220], fill=DARK)
    paw(d, 130, 120, 40)
    d.text((220, 70), "Где пообедать", font=ImageFont.truetype(FONT_BOLD, 64), fill=WHITE)
    d.text((220, 150), site, font=ImageFont.truetype(FONT, 32), fill=LIME)
    body = (
        "Шаблон документа.\n\n"
        "Здесь HR размещает перечень столовых и кафе рядом с площадкой: адрес, часы работы, "
        "время в пути от проходной и способы оплаты.\n\n"
        "Чтобы заменить шаблон, загрузите PDF с тем же именем файла в папку media/ "
        "или добавьте документ в базу знаний через панель HR — тогда Корсик сможет "
        "отвечать на вопросы о питании своими словами."
    )
    y = 300
    for para in body.split("\n"):
        for line in textwrap.wrap(para, 52) or [""]:
            d.text((100, y), line, font=ImageFont.truetype(FONT, 34), fill=DARK)
            y += 48
    img.save(OUT / name, "PDF", resolution=150)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for i, (name, (title, subtitle)) in enumerate(CARDS.items()):
        card(name, title, subtitle, i)
    for name, site in FOOD.items():
        food_pdf(name, site)
    print(f"готово: {len(CARDS)} изображений, {len(FOOD)} PDF → {OUT}")


if __name__ == "__main__":
    main()
