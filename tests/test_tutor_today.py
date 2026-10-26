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


def test_the_exam_countdown_number_has_the_same_style_as_the_other_numbers(tmp_path):
    """SPEC 1.3а / OH-T08 AC-10: одна цифра - один размер на экране «Сегодня»."""
    import re
    from pathlib import Path

    from tests.bank_helpers import db

    c, clock, _model = boot(tmp_path)
    with c:
        tid = seed(tmp_path, key="Б")
        attempt(c, tid, "Б")
        conn = db(tmp_path)
        conn.execute(
            "UPDATE settings SET show_exam_countdown = 1, stage_date = '2026-11-22' WHERE id = 1"
        )
        html = c.get("/today", headers=CHILD).text
        assert 'id="exam-countdown"' in html and "до этапа, 22 ноября" in html
        nums = re.findall(r'<div class="stat"[^>]*>\s*<span class="(num[^"]*)"', html)
        assert len(nums) >= 3 and set(nums) == {"num"}  # разобрано, подсказки, дни до этапа
        css = Path("app/static/app.css").read_text()
        assert len(re.findall(r"\.stat \.num \{", css)) == 1 and "today-num" not in css
        # выключен - счётчика нет
        conn.execute("UPDATE settings SET show_exam_countdown = 0 WHERE id = 1")
        assert "exam-countdown" not in c.get("/today", headers=CHILD).text
