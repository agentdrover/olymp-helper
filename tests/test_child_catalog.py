"""OH-K1a: карта 14 блоков ЦПМК на экране «Задания», статусы заданий, поиск и фильтры."""

import re

from tests.bank_helpers import db
from tests.tutor_helpers import CHILD, attempt, boot, post, seed


def put(tmp_path, tid, **cols):
    conn = db(tmp_path)
    try:
        sets = ", ".join(f"{k} = ?" for k in cols)
        conn.execute(f"UPDATE task_item SET {sets} WHERE id = ?", (*cols.values(), tid))
    finally:
        conn.close()


def card(html, code):
    start = html.index(f'id="block-{code}"')
    return html[start : html.index("</article>", start)]


def row(html, tid):
    start = html.index(f'<a href="/tasks/{tid}"')
    return html[html.rindex("<tr>", 0, start) : html.index("</tr>", start)]


def listed(html):
    return {int(n) for n in re.findall(r'<a href="/tasks/(\d+)">Задание', html)}


def test_blocks_show_progress_and_task_statuses(tmp_path):
    c, clock, _model = boot(tmp_path)
    with c:
        a, b, wrong = (seed(tmp_path, key="Б") for _ in range(3))
        d, e, g, free = (seed(tmp_path, key="Б") for _ in range(4))
        put(tmp_path, a, topic="b05.morph")  # подтема входит в свой блок
        put(tmp_path, b, topic="b05")
        put(tmp_path, wrong, topic="b05.process")
        for t in (d, e, g):
            put(tmp_path, t, topic="b10.cell")
        attempt(c, a, "Б")  # верно с 1-й попытки
        c.get(f"/tasks/{b}")  # экран открыт: идут секунды
        clock.advance(61)
        assert post(c, b, "hint", level=1).status_code == 200
        attempt(c, b, "Б")  # верно, но после подсказки
        attempt(c, wrong, "А")  # ошибся
        attempt(c, g, "Б")
        attempt(c, e, "А")  # ошибся, потом верно без подсказки: разобрано, но не с 1-й попытки
        attempt(c, e, "Б")
        attempt(c, d, "Б")
        put(tmp_path, d, revision=2)  # правка родителя: прогон по старой ревизии не считается

        html = c.get("/tasks", headers=CHILD).text
        assert len(re.findall(r'id="block-b\d\d"', html)) == 14
        assert 'id="block-none"' in html and "Без темы" in html
        b05 = card(html, "b05")
        assert "разобрано 2 из 3" in b05 and "с 1-й попытки: 50%" in b05
        assert "Решать по теме" in b05 and f'href="/tasks/{wrong}"' in b05
        b10 = card(html, "b10")
        assert "разобрано 2 из 3" in b10 and "с 1-й попытки: 50%" in b10
        assert f'href="/tasks/{d}"' in b10  # первое нерешённое: d (ревизия сменилась)
        none = card(html, "none")
        assert "разобрано 0 из 1" in none and "с 1-й попытки: —" in none
        assert "разобрано 0 из 0" in card(html, "b01")

        page = c.get("/tasks?block=007", headers=CHILD).text
        assert listed(page) == {a, b, wrong}
        assert "верно" in row(page, a) and "с подсказкой" in row(page, b)
        assert "ошибся" in row(page, wrong)
        page = c.get("/tasks?block=021", headers=CHILD).text
        assert "не решал" in row(page, d) and "верно" in row(page, e)
        assert "верно" in row(page, g)
        assert "не решал" in row(c.get("/tasks?theme=none", headers=CHILD).text, free)


def test_the_catalog_keeps_the_notice_gate(tmp_path):
    c, _clock, _model = boot(tmp_path, notice=False)
    with c:
        r = c.get("/tasks?q=клетка", headers=CHILD, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/notice")


def test_search_and_filters_never_reveal_answers(tmp_path):
    c, _clock, _model = boot(tmp_path)
    with c:
        cell = seed(tmp_path, key="Б", text="Митохондрия снабжает клетку энергией", number="41")
        leaf = seed(tmp_path, key="Б", text="Хлоропласт содержится в клетке листа", number="42")
        num = seed(
            tmp_path, type="numeric", key="17", text="Сколько хромосом у клетки", number="43"
        )
        draft = seed(tmp_path, key="Б", text="Клетка черновика", number="44")
        hidden = seed(tmp_path, key="Б", text="Клетка исключённого", number="45")
        guess = seed(
            tmp_path, key=None, model_answer="В", text="Клетка без ключа", number="46",
            model_explanation="СЕКРЕТНОЕОБЪЯСНЕНИЕМОДЕЛИ",
        )  # fmt: skip
        put(tmp_path, cell, topic="b10.cell")
        put(tmp_path, leaf, topic="b05.process")
        put(tmp_path, num, topic="b10")
        put(tmp_path, draft, import_status="draft", topic="b10")
        put(tmp_path, hidden, excluded=1, topic="b10")
        attempt(c, cell, "Б")

        def ask(query):
            r = c.get(f"/tasks?{query}", headers=CHILD)
            assert r.status_code == 200
            for banned in ("answer_key", "model_answer", "СЕКРЕТНОЕОБЪЯСНЕНИЕМОДЕЛИ", "key_source"):
                assert banned not in r.text
            return listed(r.text)

        everyone = {cell, leaf, num, guess}
        assert ask("q=клетка") == everyone  # условие; черновик и исключённое не видны
        assert ask("q=43") == {num}  # номер
        assert ask("q=Цитология") == {cell, num}  # название темы (блок и подтема)
        assert ask("q=органоиды") == {cell}  # название подтемы
        assert ask("q=клетка&theme=021") == {cell, num}  # блок: подтема входит
        assert ask("q=клетка&theme=022") == {cell}  # сама подтема
        assert ask("q=клетка&type=numeric") == {num}
        assert ask("q=клетка&status=ok") == {cell}
        assert ask("q=клетка&status=new") == {leaf, num, guess}
        assert ask("q=клетка&source=2022") == everyone
        assert ask("q=клетка&source=2019") == set()
        assert ask("q=клетка&source=generated") == set()
        assert ask("q=клетка&theme=none") == {guess}
        assert ask("status=hint") == set()
        assert draft not in ask("") and hidden not in ask("theme=021")
        # JSON-вход ребёнка не изменился: условия и номер, без ответов
        assert "answer_key" not in c.get("/api/tasks", headers=CHILD).text
