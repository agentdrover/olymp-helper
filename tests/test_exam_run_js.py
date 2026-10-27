"""Поведение скрипта бланка олимпиады (child/exam_run.html) на модели DOM: нужен node, на CI он есть.

Скрипт встроен в шаблон, поэтому харнесс достаёт его из файла шаблона, а не из app/static."""

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "exam_run_harness.js"
NODE = shutil.which("node")

SCENARIOS = [
    "save_reply_keeps_newer_input",
    "out_of_order_replies_keep_the_last_request",
    "single_reply_still_renders",
    "mark_reply_keeps_newer_input",
    "navigation_reply_keeps_newer_input",
    "navigation_without_edit_switches",
    "mark_without_edit_renders",
]


@pytest.mark.skipif(NODE is None, reason="node не установлен")
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_exam_run_js_scenario(scenario):
    r = subprocess.run(
        [NODE, str(HARNESS), scenario], check=False, capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, r.stdout + r.stderr
