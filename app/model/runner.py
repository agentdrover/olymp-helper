"""Запуск CLI в отдельном пустом каталоге; ошибки без деталей (SPEC-1.0 1.4)."""

import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time
from types import SimpleNamespace

from app.model.adapters import ADAPTERS, TOKEN_VARS, build_env
from app.model.redaction import redact
from app.model.types import ChannelError, ModelResult

TIMEOUT_S = 120
log = logging.getLogger("app.model")


def default_spawn(argv, *, input, env, cwd, timeout):
    """Запуск в своей группе процессов: по таймауту убиваются и потомки CLI."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(input, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    return SimpleNamespace(returncode=proc.returncode, stdout=out, stderr=err)


def run_cli(
    channel,
    model,
    system,
    messages,
    schema,
    *,
    env,
    spawn=None,
    timeout=TIMEOUT_S,
    workroot=None,
    clock=time.monotonic,
) -> ModelResult:
    """Один запуск CLI. Не бросает исключений: любой сбой - ModelResult с error."""
    adapter = ADAPTERS[channel]
    spawn = spawn or default_spawn
    secrets = [v for v in (env.get(t) for t in TOKEN_VARS.values()) if v]
    root = tempfile.mkdtemp(prefix="run-", dir=workroot)
    started = clock()

    def elapsed():
        return max(0, int((clock() - started) * 1000))

    try:
        work, home = os.path.join(root, "work"), os.path.join(root, "home")
        os.mkdir(work)
        os.mkdir(home)
        try:
            argv = adapter.build(model, system, schema)
            stdin = adapter.stdin(system, messages, schema)
            proc = spawn(
                argv, input=stdin, env=build_env(channel, env, home), cwd=work, timeout=timeout
            )
            if proc.returncode != 0:
                raise ChannelError("exit_code")
            parsed = adapter.parse(proc.stdout, schema)
        except subprocess.TimeoutExpired:
            return _fail(channel, model, "timeout", elapsed())
        except ChannelError as exc:
            return _fail(channel, model, exc.kind, elapsed())
        except Exception as exc:  # noqa: BLE001 - любой сбой запуска не должен дойти до ребёнка
            return _fail(channel, model, "exec_" + type(exc).__name__, elapsed(), secrets)
        return ModelResult(parsed.text, parsed.json, channel, model, elapsed(), parsed.usage)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _fail(channel, model, kind, duration_ms, secrets=()):
    # В журнал - только канал и код причины; stderr и тексты исключений не пишутся никуда.
    log.warning("model run failed: channel=%s kind=%s", channel, redact(kind, secrets))
    return ModelResult.failure(channel, model, kind, duration_ms)
