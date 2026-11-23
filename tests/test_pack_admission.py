"""A pack remains pending while its independent critic cannot finish."""

from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.model import client, work
from app.model.types import ModelResult
from tests.bank_helpers import build_app
from tests.test_generated_packs import NEW, request_pack


@pytest.mark.parametrize("interruption", ["busy", "unknown", "stale"])
def test_pack_waits_at_the_critic_and_reuses_the_logical_operation(
    tmp_path, monkeypatch, interruption
):
    dispatches = []
    receipts = {}
    withheld = None
    payloads = {
        "question_pack": {"questions": [NEW[0]]},
        "question_critic": {"solution": "Г", "issues": []},
        "tutor_judge": {"reveals": False, "reason": "нет"},
    }

    def complete(channel, _system, _messages, schema, *, attempt, model, **_kwargs):
        nonlocal withheld
        title = schema["title"]
        dispatches.append((title, channel.channel, attempt))
        if title == "question_critic" and len(dispatches) == 2:
            if interruption == "busy":
                return ModelResult.failure(channel.channel, model, "busy")
            withheld = attempt
            return ModelResult.failure(channel.channel, model, "executor_unreachable")
        result = ModelResult(None, payloads[title], channel.channel, model, 1, None)
        receipts[attempt] = result
        return result

    def outcome(_channel, attempt, _model):
        return receipts.get(attempt)

    monkeypatch.setattr(work.ModelWork, "_loop", lambda self: self.stop.wait())
    monkeypatch.setattr(work, "worker_status", lambda _: {"state": "ready", "active": 0})
    monkeypatch.setattr(client.ExecutorChannel, "complete", complete)
    monkeypatch.setattr(client.ExecutorChannel, "outcome", outcome)
    with TestClient(build_app(tmp_path)) as web, closing(connect(tmp_path / "olymp.db")) as conn:
        conn.execute("UPDATE settings SET primary_channel='codex', cursor_model='sonnet'")
        response = request_pack(web, count=1)
        assert response.status_code == 303
        ((op_id, key),) = conn.execute("SELECT id,object_key FROM model_operation")
        pack_id = int(key)
        runner = web.app.state.model_work

        def pack():
            return conn.execute(
                "SELECT status,accepted_count,published_count FROM question_pack WHERE id=?",
                (pack_id,),
            ).fetchone()

        def operations():
            return conn.execute("SELECT id,object_key,status FROM model_operation").fetchall()

        def executions():
            return conn.execute(
                "SELECT operation_id,ordinal,attempt,disposition FROM model_dispatch ORDER BY ordinal"
            ).fetchall()

        def ledger():
            return conn.execute("SELECT status FROM model_run ORDER BY id").fetchall()

        def items():
            return conn.execute(
                "SELECT pipeline_outcome FROM task_item WHERE pack_id=?", (pack_id,)
            ).fetchall()

        assert runner.tick()
        assert [call[:2] for call in dispatches] == [
            ("question_pack", "codex"),
            ("question_critic", "cursor"),
        ]
        author = dispatches[0][2]
        assert pack() == ("running", 0, 0)
        assert items() == []
        assert request_pack(web, count=1).headers["location"] == response.headers["location"]
        assert conn.execute("SELECT COUNT(*) FROM question_pack").fetchone() == (1,)
        if interruption == "busy":
            assert operations() == [(op_id, key, "queued")]
            assert executions() == [(op_id, 1, author, "confirmed")]
            assert ledger() == [("ok",)]
        else:
            assert operations() == [(op_id, key, "unknown")]
            assert executions() == [
                (op_id, 1, author, "confirmed"),
                (op_id, 2, withheld, "unknown"),
            ]
            assert not runner.tick()
            assert len(dispatches) == 2
            receipts[withheld] = ModelResult(
                None, payloads["question_critic"], "cursor", "sonnet", 1, None
            )
            if interruption == "stale":
                conn.execute("UPDATE settings SET cursor_model='sonnet-2'")
            assert runner.tick()
            assert len(dispatches) == 2
            assert ledger() == [("ok",), ("ok",)]
            if interruption == "stale":
                assert pack() == ("failed", 0, 0)
                assert items() == []
                assert operations() == [(op_id, key, "failed")]
                assert not runner.tick()
                return
            assert pack() == ("running", 0, 0)
            assert operations() == [(op_id, key, "queued")]

        assert runner.tick()
        assert pack() == ("done", 0, 1)
        assert items() == [("passed",)]
        assert operations() == [(op_id, key, "done")]
        assert not runner.tick()
        assert ledger() == [("ok",)] * 3
        assert len({call[2] for call in dispatches}) == len(dispatches)
        assert [call[0] for call in dispatches] == [
            "question_pack",
            "question_critic",
            *([] if withheld else ["question_critic"]),
            "tutor_judge",
        ]
        confirmed = [author, dispatches[-2][2], dispatches[-1][2]]
        assert executions() == [
            (op_id, ordinal, attempt, "confirmed") for ordinal, attempt in enumerate(confirmed, 1)
        ]
