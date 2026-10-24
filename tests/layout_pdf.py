"""A synthetic collection with the layout of a real olympiad book, built on the fly.

Answers stand inside table cells of the task's own page, a photo table runs over two pages,
there is a drawing task with «Элементы ответа» and a scored long answer, and a second section
repeats the task numbers. Nothing here is taken from a real book.
"""

import pymupdf

from tests.bank_helpers import FONT, doc_bytes, quad_png

W, H = 595, 842
IMG = quad_png(130, 60)

# Geometry the tests use as the oracle (PDF points, unrotated page space).
PHOTOS_P1 = [pymupdf.Rect(60, 135 + 70 * i, 190, 195 + 70 * i) for i in range(3)]
PHOTOS_P2 = [pymupdf.Rect(60, 55 + 70 * i, 190, 115 + 70 * i) for i in range(2)]
CAPTION_PHOTOS = [pymupdf.Rect(60, 270, 200, 335), pymupdf.Rect(60, 392, 200, 450)]
CAPTION_AT = (70, 366)

NAMES = ["минога", "улитка", "пиявка", "муравей", "осьминог"]
OPTIONS_1_1 = [{"label": c, "text": n} for c, n in zip("АБВГД", NAMES, strict=True)]
OPTIONS_1_2 = [
    {"label": "А", "text": "митохондрия"},
    {"label": "Б", "text": "рибосома"},
    {"label": "В", "text": "ядро"},
    {"label": "Г", "text": "лизосома"},
]
STATEMENTS = [
    "1) Все птицы умеют летать.",
    "2) Клетки растений имеют клеточную стенку.",
    "3) Грибы относятся к растениям.",
    "4) Хлоропласты содержат пигмент хлорофилл.",
    "5) Все насекомые имеют восемь ног.",
    "6) Рибосомы участвуют в синтезе белка.",
]
EVENTS = [f"№ {i}. Событие номер {i}" for i in range(1, 9)]


def _text(page, x, y, line, size=11):
    tw = pymupdf.TextWriter(page.rect)
    tw.append((x, y), line, font=FONT, fontsize=size)
    tw.write_text(page)


def _cell(page, rect, line=None):
    page.draw_rect(rect, color=(0, 0, 0), width=0.8)
    if line:
        _text(page, rect.x0 + 8, (rect.y0 + rect.y1) / 2 + 4, line)


def _lines(page, x, y, lines, size=11, step=16):
    for i, line in enumerate(lines):
        _text(page, x, y + i * step, line, size)


def _answer_row(page, y, answer, label="Ответ"):
    _cell(page, pymupdf.Rect(50, y, 150, y + 26), label)
    _cell(page, pymupdf.Rect(150, y, 400, y + 26), answer)


def _page_one(doc):
    page = doc.new_page(width=W, height=H)
    _text(page, 50, 60, "Вариант 1. Отборочный этап", 13)
    _lines(
        page,
        50,
        95,
        ["1.1 Рассмотрите фотографии в таблице и выберите", "животных, относящихся к моллюскам."],
    )
    for i, rect in enumerate(PHOTOS_P1):
        top = 130 + 70 * i
        _cell(page, pymupdf.Rect(50, top, 200, top + 70))
        _cell(page, pymupdf.Rect(200, top, 400, top + 70), NAMES[i])
        page.insert_image(rect, stream=IMG, keep_proportion=False)


def _page_two(doc, tiny_label=False):
    page = doc.new_page(width=W, height=H)
    if tiny_label:  # a label nobody can read at the display width, inside the first photo
        page.insert_text((70, 70), "1 2", fontsize=2, fontname="helv")
    for i, rect in enumerate(PHOTOS_P2):
        top = 50 + 70 * i
        _cell(page, pymupdf.Rect(50, top, 200, top + 70))
        _cell(page, pymupdf.Rect(200, top, 400, top + 70), NAMES[3 + i])
        page.insert_image(rect, stream=IMG, keep_proportion=False)
    _text(page, 50, 215, "1.2 Определите органоид, показанный на микрофотографии.")
    _cell(page, pymupdf.Rect(50, 262, 220, 340))
    _cell(page, pymupdf.Rect(50, 340, 220, 385), None)
    _text(page, *CAPTION_AT, "рибосома")
    _cell(page, pymupdf.Rect(50, 385, 220, 455))
    for rect in CAPTION_PHOTOS:
        page.insert_image(rect, stream=IMG, keep_proportion=False)
    _text(page, 50, 480, "1.3 Сколько хромосом содержит соматическая клетка")
    _text(page, 50, 496, "организма? Ответ запишите числом.")
    _answer_row(page, 510, "28")
    _text(page, 50, 565, "1.4 Выберите верные суждения.")
    _lines(page, 50, 583, STATEMENTS, step=15)
    _answer_row(page, 685, "2 4 6")


def _page_three(doc):
    page = doc.new_page(width=W, height=H)
    _text(page, 50, 60, "1.5 Расположите события в порядке их наступления.")
    _lines(page, 50, 78, EVENTS, step=15)
    _answer_row(page, 205, "№ 2 … № 7")
    _text(page, 50, 260, "1.6 Определите орган растения по описанию в каждой строке.")
    rows = ["1 Стебель", "2 Лист", "3 Корень", "4 Стебель", "5 Корень", "10 баллов"]
    for i, line in enumerate(rows):
        _cell(page, pymupdf.Rect(50, 275 + 26 * i, 300, 301 + 26 * i), line)
    _lines(
        page,
        50,
        470,
        ["1.7 Нарисуйте схему строения сердца человека и", "подпишите камеры."],
    )
    _lines(
        page,
        50,
        510,
        [
            "Элементы ответа:",
            "1) нарисованы четыре камеры (1 балл)",
            "2) подписаны клапаны (1 балл)",
        ],
    )
    _lines(
        page,
        50,
        580,
        [
            "1.8 Объясните, почему растения нуждаются в свете.",
            "ОТВЕТ: фотосинтез (1 балл), хлорофилл (1 балл)",
        ],
    )


def _page_four(doc):
    page = doc.new_page(width=W, height=H)
    _text(page, 50, 60, "Вариант 2. Отборочный этап", 13)
    _text(page, 50, 95, "1.1 Какой орган растения поглощает воду из почвы?")
    _answer_row(page, 115, "корень")
    _text(page, 50, 170, "1.3 Сколько пар хромосом у человека? Ответ запишите числом.")
    _answer_row(page, 190, "23")


def layout_pdf(tiny_label=False) -> bytes:
    doc = pymupdf.open()
    _page_one(doc)
    _page_two(doc, tiny_label)
    _page_three(doc)
    _page_four(doc)
    return doc_bytes(doc)
