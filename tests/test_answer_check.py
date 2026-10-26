"""Проверка по подтверждённому ключу без модели (OH-T06, AC-3)."""

from app.tutor.check import check_answer
from tests.tutor_helpers import Scripted, attempt, boot, seed


class Exploding(Scripted):
    def complete(self, *a, **kw):
        raise AssertionError("the answer check must not need the model")


def test_ac3(tmp_path):
    c, _clock, model = boot(tmp_path, Exploding())
    with c:
        # single: равенство
        s = seed(tmp_path, type="single", key="В")
        assert attempt(c, s, "Б").json()["state"] == "attempted_wrong"
        assert attempt(c, s, "В").json()["state"] == "solved"
        # multiple: множество, порядок не важен, лишний вариант - неверно
        m = seed(tmp_path, type="multiple", key="А, В")
        assert attempt(c, m, ["А"]).json()["state"] == "attempted_wrong"
        assert attempt(c, m, ["А", "В", "Г"]).json()["state"] == "attempted_wrong"
        m2 = seed(tmp_path, type="multiple", key="А, В")
        assert attempt(c, m2, ["В", "А"]).json()["state"] == "solved"
        # match: отображение целиком
        p = seed(tmp_path, type="match", key="1-А; 2-Б")
        assert attempt(c, p, {"1": "А", "2": "А"}).json()["state"] == "attempted_wrong"
        p2 = seed(tmp_path, type="match", key="1-А; 2-Б")
        assert attempt(c, p2, {"2": "Б", "1": "А"}).json()["state"] == "solved"
        # число: «28», « 28 » и «28,0» совпадают
        n = seed(tmp_path, type="numeric", key="28", options=None)
        assert attempt(c, n, " 28,0 ").json()["state"] == "solved"
        # ответ, которого нет среди вариантов, не считается попыткой
        z = seed(tmp_path, type="single", key="А")
        assert attempt(c, z, "Ж").status_code == 422
        assert c.get(f"/api/tasks/{z}/tutor").json()["attempts"] == 0
    assert model.calls == [] if hasattr(model, "calls") else True


def test_check_answer_is_pure_comparison():
    assert check_answer("single", "Б", "Б") is True
    assert check_answer("single", "Б", "А") is False
    assert check_answer("multiple", ["А", "В"], ["В", "А"]) is True
    assert check_answer("multiple", ["А", "В"], ["А"]) is False
    assert check_answer("match", {"1": "А"}, {"1": "А"}) is True
    assert check_answer("match", {"1": "А"}, {"1": "Б"}) is False
