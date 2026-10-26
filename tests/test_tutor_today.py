"""Экран «Сегодня» (OH-T06, экран 2 холста)."""

from tests.tutor_helpers import CHILD, attempt, boot, seed


def test_today_lists_progress_and_revisit_order(tmp_path):
    c, _clock, _model = boot(tmp_path)
    with c:
        calm = seed(tmp_path, key="Б", number="21")
        sure = seed(tmp_path, key="Б", number="22")
        half = seed(tmp_path, key="Б", number="23")
        attempt(c, calm, "А")  # ошибка без отметки
        attempt(c, calm, "Б")
        attempt(c, sure, "А", confidence="sure")  # уверен и ошибся
        attempt(c, sure, "Б")
        attempt(c, half, "А")  # остановился здесь
        html = c.get("/today", headers=CHILD).text
        assert 'aria-current="page"' in html and "Сегодня" in html
        assert "Продолжить разбор" in html and f'/tasks/{half}"' in html
        assert "был уверен, но ошибся" in html
        assert html.index("№ 22") < html.index("№ 21")  # «уверен и ошибся» - первыми
        assert "2 из 3 разобрано" in html
        assert "[ИМЯ]" not in html
        assert "До этапа" not in html  # счётчик выключен по умолчанию
