"""Parents can inspect automatically published tasks in the actual HTML interface."""

import pytest

from tests.bank_helpers import PARENT, row
from tests.test_pipeline_imported import mk, start


@pytest.mark.parametrize("entry", ["queue", "card"])
def test_published_tasks_have_a_visible_parent_list_and_card(tmp_path, entry):
    client, _model, (published, held) = start(
        tmp_path,
        [mk(1, "Опубликованное условие"), mk(2, "Условие с расхождением")],
        {"1": "Б", "2": "В"},
        keys={"1": "Б", "2": "Б"},
    )
    with client:
        url = "/parent/review" if entry == "queue" else f"/parent/review/{held}"
        page = client.get(url, headers=PARENT)
        assert page.status_code == 200
        assert 'id="auto-published"' in page.text
        section = page.text.split('id="auto-published"', 1)[1].split("</section>", 1)[0]
        assert f'data-published-task="{published}"' in section
        assert "Опубликованное условие" in section
        assert "опубликовано автоматически" in section.lower()
        assert "рибосома" in section and "Цитология" in section
        assert f'data-published-task="{held}"' not in section
        assert (
            "/parent/review#auto-published" in client.get("/parent/materials", headers=PARENT).text
        )

        # An edit withdraws publication and the task disappears from both HTML readers.
        client.post(f"/parent/tasks/{published}/edit", headers=PARENT, json={"text": "Правка"})
        assert row(tmp_path, published)["auto_published"] == 0
        for path in ("/parent/review", "/parent/review/auto/list"):
            response = client.get(path, headers=PARENT)
            assert response.status_code == 200
            assert f'data-published-task="{published}"' not in response.text


def test_auto_list_fragment_requires_parent_session(tmp_path):
    client, _model, (_tid,) = start(tmp_path, [mk(1)], {"1": "Б"}, keys={"1": "Б"})
    with client:
        client.cookies.clear()
        response = client.get("/parent/review/auto/list", follow_redirects=False)
        assert response.status_code in (303, 401, 403)
        assert "рибосома" not in response.text
