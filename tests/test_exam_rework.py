"""Regression tests for the olympiad form and its frozen figure tiles."""

import json
import subprocess
from pathlib import Path

import pytest

from tests.bank_helpers import quad_png
from tests.test_exam import NODE, act, at, current, form_of, save_format, start_real, submit
from tests.tutor_helpers import CHILD, boot, db, seed


def test_exam_blank_has_no_background_save(tmp_path):
    client, _clock, _model = boot(tmp_path)
    with client:
        seed(tmp_path, number="1")
        save_format(client, tmp_path)
        assert start_real(client).status_code == 200
        response = client.get("/exam/run", headers=CHILD)
        assert response.headers["server-timing"].startswith("exam;dur=")
        page = response.text
        assert 'method="post" action="/exam/act"' in page
        assert "fetch(" not in page and "hx-" not in page
        assert "Срок определяет сервер" in page


@pytest.mark.skipif(NODE is None, reason="node не установлен")
def test_exam_timer_accounts_for_navigation_delay():
    harness = Path(__file__).parent / "js" / "exam_form_harness.js"
    run = subprocess.run(
        [NODE, str(harness)], check=False, capture_output=True, text=True, timeout=60
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_exam_accepts_individual_tokens_without_unpacking_a_token_as_free_text(tmp_path):
    client, _clock, _model = boot(tmp_path)
    with client:
        seed(tmp_path, type="multiple", key="Б", number="1")
        save_format(client, tmp_path)
        assert start_real(client).status_code == 200
        original = form_of(client)
        rejected = act(client, "save", original, a=["ББ"])
        assert rejected.status_code == 422
        assert form_of(client)["rev"] == original["rev"]
        assert current(client)[1]["items"][0]["answer"] is None
        assert at(client, 1, "save", a=["Б)"]).status_code == 303
        assert submit(client)["score"] == 2


def test_exam_figure_tiles_keep_snapshot_order_labels_and_files(tmp_path):
    client, _clock, _model = boot(tmp_path)
    with client:
        task = seed(tmp_path, type="match", key="1-А, 2-Б", number="1", figure={"ack": True})
        paths = [tmp_path / f"figures/tile-{n}.png" for n in (1, 2)]
        images = [quad_png(120, 90), quad_png(180, 90)]
        for path, content in zip(paths, images, strict=True):
            path.write_bytes(content)
        conn = db(tmp_path)
        figure = json.loads(
            conn.execute("SELECT figure FROM task_item WHERE id=?", (task,)).fetchone()[0]
        )
        figure.update(
            tiles_v=1,
            tiles=[
                {"id": "a", "label": "1", "file": "figures/tile-1.png"},
                {"id": "b", "label": "2", "file": "figures/tile-2.png"},
            ],
            tile_order=["b", "a"],
        )
        conn.execute("UPDATE task_item SET figure=? WHERE id=?", (json.dumps(figure), task))
        save_format(client, tmp_path)
        assert start_real(client).status_code == 200

        view = current(client)[1]["items"][0]["figure"]["tiles"]
        assert [(tile["id"], tile["label"]) for tile in view] == [("b", "2"), ("a", "1")]
        # Original files and bindings may change after the attempt starts.
        conn.execute("UPDATE task_item SET figure=NULL WHERE id=?", (task,))
        conn.close()
        for path in paths:
            path.write_bytes(quad_png(200, 90))
        for tile, original in zip(view, reversed(images), strict=True):
            response = client.get(tile["url"], headers=CHILD)
            assert response.status_code == 200 and response.content == original
        page = client.get("/exam/run", headers=CHILD).text
        assert page.index(view[0]["url"]) < page.index(view[1]["url"])
        assert 'data-label="2"' in page and 'data-label="1"' in page
        submit(client)
        assert client.get(view[0]["url"], headers=CHILD).status_code == 404
