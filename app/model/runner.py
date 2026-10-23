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
from app.model.redaction import redact, redact_json
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
    launcher=None,
) -> ModelResult:
    """Один запуск CLI. Не бросает исключений: любой сбой - ModelResult с error.

    launcher - префикс команды, под которым CLI уходит на другой UID (setpriv), чем сервер:
    CLI не читает /proc/<pid>/environ сервера. Уборка рабочих каталогов идёт под тем же UID.
    """
    adapter = ADAPTERS[channel]
    spawn = spawn or default_spawn
    secrets = [v for v in (env.get(t) for t in TOKEN_VARS.values()) if v]
    root = tempfile.mkdtemp(prefix="run-", dir=workroot)
    work, home = os.path.join(root, "work"), os.path.join(root, "home")
    started = clock()

    def elapsed():
        return max(0, int((clock() - started) * 1000))

    try:
        os.mkdir(work)
        os.mkdir(home)
        if launcher:
            # Каталог запуска закрыт для списка, рабочие - открыты пользователю CLI.
            os.chmod(root, 0o711)
            os.chmod(work, 0o777)
            os.chmod(home, 0o777)
        try:
            argv = list(launcher or []) + adapter.build(model, system, schema)
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
        # Успешный ответ тоже чистится: CLI мог прочитать свой токен и вернуть его в тексте.
        text = None if parsed.text is None else redact(parsed.text, secrets)
        return ModelResult(
            text, redact_json(parsed.json, secrets), channel, model, elapsed(), parsed.usage
        )
    finally:
        if launcher:
            scrub(spawn, launcher, env, root, (work, home))
        shutil.rmtree(root, ignore_errors=True)


def scrub(spawn, launcher, env, root, dirs):
    """Очистить рабочие каталоги от имени пользователя CLI (файлы и права там его)."""
    try:
        spawn(
            [*launcher, "find", *dirs, "-mindepth", "1", "-delete"],
            input="",
            env={"PATH": env.get("PATH", "/usr/bin:/bin")},
            cwd=root,
            timeout=30,
        )
    except Exception:  # noqa: BLE001 - остаток уберёт rmtree или перезапуск tmpfs
        log.warning("scrub failed")


def _fail(channel, model, kind, duration_ms, secrets=()):
    # В журнал - только канал и код причины; stderr и тексты исключений не пишутся никуда.
    log.warning("model run failed: channel=%s kind=%s", channel, redact(kind, secrets))
    return ModelResult.failure(channel, model, kind, duration_ms)
