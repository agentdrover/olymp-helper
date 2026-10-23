"""Regenerates sample_tasks.pdf and sample_tasks.txt (run: uv run python tests/fixtures/ingest/make_sample.py)."""

from pathlib import Path

import pymupdf

HERE = Path(__file__).parent
PAGES = [
    [
        "Вариант 1. Задания отборочного этапа, 7 класс",
        "1. Какая органелла клетки содержит собственную ДНК?",
        "А) рибосома",
        "Б) митохондрия",
        "В) лизосома",
        "Г) комплекс Гольджи",
        "2. Ёж относится к классу:",
        "А) пресмыкающиеся",
        "Б) млекопитающие",
        "В) птицы",
        "Г) земноводные",
    ],
    [
        "3. Выберите признаки, характерные для растительной клетки.",
        "А) клеточная стенка",
        "Б) центриоли",
        "В) хлоропласты",
        "Г) крупная центральная вакуоль",
    ],
]


def main():
    font = pymupdf.Font("helv")
    doc = pymupdf.open()
    for lines in PAGES:
        page = doc.new_page()
        tw = pymupdf.TextWriter(page.rect)
        for i, line in enumerate(lines):
            tw.append((50, 80 + i * 16), line, font=font, fontsize=11)
        tw.write_text(page)
    doc.subset_fonts()
    (HERE / "sample_tasks.pdf").write_bytes(doc.tobytes(garbage=3, deflate=True))
    (HERE / "sample_tasks.txt").write_text(
        "\f".join("\n".join(lines) for lines in PAGES), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
