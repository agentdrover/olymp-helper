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


def kill_group(launcher, pid, run=subprocess.run):
    """Убить группу процессов CLI. При разделении UID сервер сигналить CLI не может (нет
    CAP_KILL), поэтому сигнал отправляет тот же launcher - от имени пользователя CLI."""
    if launcher:
        run(
            [*launcher, "python3", "-c", f"import os; os.killpg({int(pid)}, 9)"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    else:
        os.killpg(pid, signal.SIGKILL)


def make_spawn(launcher=None, on_fault=None, kill_wait=5.0):
    """Запуск в своей группе процессов; по таймауту группа убивается и ждётся её завершение."""

    def spawn(argv, *, input, env, cwd, timeout):
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
            try:
                kill_group(launcher, proc.pid)
            except Exception:  # noqa: BLE001 - исход проверяется ожиданием ниже
                log.warning("kill failed")
            try:
                proc.wait(timeout=kill_wait)
            except subprocess.TimeoutExpired:
                if on_fault:
                    on_fault("kill")
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except Exception:  # noqa: BLE001, S110
                    pass
            raise
        return SimpleNamespace(returncode=proc.returncode, stdout=out, stderr=err)

    return spawn


default_spawn = make_spawn()


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
    on_fault=None,
    sweeper=None,
    kill_wait=5.0,
) -> ModelResult:
    """Один запуск CLI. Не бросает исключений: любой сбой - ModelResult с error.

    launcher - префикс команды, под которым CLI уходит на другой UID (setpriv), чем сервер:
    CLI не читает /proc/<pid>/environ сервера. Каталоги запуска создаются, убиваются процессы и
    убираются файлы от имени пользователя CLI. on_fault(причина) вызывается, когда после запуска
    остался процесс или не удалось убрать каталог: исполнитель после этого неисправен.
    sweeper() убивает всех оставшихся процессов пользователя CLI и сообщает, удалось ли.
    """
    adapter = ADAPTERS[channel]
    spawn = spawn or make_spawn(launcher, on_fault, kill_wait)
    secrets = [v for v in (env.get(t) for t in TOKEN_VARS.values()) if v]
    started = clock()

    def elapsed():
        return max(0, int((clock() - started) * 1000))

    try:
        root = tempfile.mkdtemp(prefix="run-", dir=workroot)
    except OSError:  # нет места, нет каталога: ошибка запуска, а не падение сервера
        return _fail(channel, model, "workdir", elapsed())
    work, home = os.path.join(root, "work"), os.path.join(root, "home")
    helper_env = {"PATH": env.get("PATH", "/usr/bin:/bin")}
    try:
        try:
            _make_dirs(spawn, launcher, helper_env, root, work, home)
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
        _cleanup(spawn, launcher, helper_env, root, work, home, sweeper, on_fault)


def _make_dirs(spawn, launcher, helper_env, root, work, home):
    if not launcher:
        os.mkdir(work)
        os.mkdir(home)
        return
    # Каталоги принадлежат пользователю CLI и закрыты (0700); сервер в них не заглядывает.
    # Корень запуска: пользователь CLI может в нём создавать (wx), но не читать список.
    os.chmod(root, 0o733)
    res = spawn(
        [*launcher, "mkdir", "-m", "0700", work, home],
        input="",
        env=helper_env,
        cwd=root,
        timeout=30,
    )
    if res.returncode != 0:
        raise ChannelError("workdir")


def _cleanup(spawn, launcher, helper_env, root, work, home, sweeper, on_fault):
    def fault(reason):
        log.error("executor is faulty: %s", reason)
        if on_fault:
            on_fault(reason)

    if sweeper is not None:
        try:
            if not sweeper():
                fault("sweep")
        except Exception:  # noqa: BLE001
            fault("sweep")
    if launcher:
        try:
            spawn(
                [
                    *launcher,
                    "sh",
                    "-c",
                    'chmod -R u+rwX -- "$@" 2>/dev/null; rm -rf -- "$@"',
                    "sh",
                    work,
                    home,
                ],
                input="",
                env=helper_env,
                cwd=root,
                timeout=30,
            )
        except Exception:  # noqa: BLE001 - итог проверяется ниже
            log.warning("scrub failed")
    shutil.rmtree(root, ignore_errors=True)
    if os.path.exists(root):
        fault("cleanup")


def cli_processes(uid: int) -> list[int]:
    """Живые (не зомби) процессы пользователя CLI по /proc."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            status = open(f"/proc/{entry}/status", encoding="utf-8").read()  # noqa: SIM115
        except OSError:
            continue
        fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
        if fields.get("Uid", "").split()[:1] == [str(uid)] and not fields.get(
            "State", ""
        ).strip().startswith("Z"):
            found.append(int(entry))
    return found


def make_sweeper(launcher, user, attempts=3):
    """Убить всех процессов пользователя CLI (kill -KILL -1 от его имени) и проверить /proc."""
    import pwd

    uid = pwd.getpwnam(user).pw_uid

    def sweep() -> bool:
        for _ in range(attempts):
            if not cli_processes(uid):
                return True
            subprocess.run(
                [*launcher, "python3", "-c", "import os; os.kill(-1, 9)"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and cli_processes(uid):
                time.sleep(0.05)
        return not cli_processes(uid)

    return sweep


def _fail(channel, model, kind, duration_ms, secrets=()):
    # В журнал - только канал и код причины; stderr и тексты исключений не пишутся никуда.
    log.warning("model run failed: channel=%s kind=%s", channel, redact(kind, secrets))
    return ModelResult.failure(channel, model, kind, duration_ms)
