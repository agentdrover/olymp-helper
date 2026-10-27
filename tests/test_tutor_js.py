"""Поведение app/static/tutor.js на модели DOM (tests/js/tutor_harness.js): нужен node, на CI он есть."""

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "tutor_harness.js"
NODE = shutil.which("node")

SCENARIOS = [
    "poll_keeps_unsent_answer",
    "final_poll_keeps_unsent_answer",
    "post_failure_polls_and_shows_started_step",
    "post_failure_without_step_returns_to_idle",
    "stale_poll_does_not_overwrite_newer_step",
    "lost_retry_reply_keeps_recovery_polling",
    "failed_posts_extend_polling_deadline",
    "loop_survives_failed_and_stale_polls_while_older_post_pends",
]


@pytest.mark.skipif(NODE is None, reason="node не установлен")
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_tutor_js_scenario(scenario):
    r = subprocess.run(
        [NODE, str(HARNESS), scenario], check=False, capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, r.stdout + r.stderr
