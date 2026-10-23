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


SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"
CLEAN_SCRIPT = (
    'find "$0" -mindepth 1 -maxdepth 1 -exec chmod -R u+rwX {} + 2>/dev/null; '
    'find "$0" -mindepth 1 -maxdepth 1 -exec rm -rf {} +'
)


# Сброс всего, что оставляет за собой пользователь CLI вне процессов и файлов: очереди POSIX и
# объекты SysV IPC (msg, shm, sem) в IPC-пространстве имён контейнера. Запускается через as_cli.
RESET_SCRIPT = "rm -f /dev/mqueue/* 2>/dev/null; ipcrm --all 2>/dev/null; true"


def cli_launcher(user: str) -> list[str]:
    """Префикс запуска под другим UID без привилегий: сервер, его /proc и токены недоступны."""
    return [
        "setpriv",
        f"--reuid={user}",
        f"--regid={user}",
        "--clear-groups",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--no-new-privs",
        "--",
    ]


def minimal_env(source=None, home="/nonexistent") -> dict:
    """Окружение служебного процесса: ничего от сервера, кроме PATH и LANG."""
    source = source or {}
    env = {"PATH": source.get("PATH", SAFE_PATH), "HOME": home}
    if "LANG" in source:
        env["LANG"] = source["LANG"]
    return env


def plain_spawn(argv, *, input, env, cwd, timeout):
    """Короткая служебная команда без собственной логики убийства."""
    res = subprocess.run(
        argv, input=input, env=env, cwd=cwd, timeout=timeout, capture_output=True,
        text=True, encoding="utf-8", check=False,
    )  # fmt: skip
    return SimpleNamespace(returncode=res.returncode, stdout=res.stdout, stderr=res.stderr)


def as_cli(launcher, argv, env, *, spawn, cwd=None, input="", timeout=30):
    """ЕДИНСТВЕННЫЙ путь запуска чего-либо под пользователем CLI: сам CLI, kill, sweep, уборка.

    env - полное окружение процесса, заданное вызывающим явным белым списком; окружение сервера
    (общий ключ приложения и т.п.) никогда не наследуется: оно не попадает ни в setpriv, ни дальше.
    Значения не кладутся в argv (его видно в /proc/<pid>/cmdline), поэтому окружение задаётся при
    запуске процесса, а не через `env -i NAME=VALUE`.
    """
    full = [*launcher, *argv] if launcher else list(argv)
    return spawn(full, input=input, env=dict(env), cwd=cwd, timeout=timeout)


def kill_group(launcher, pid, run=plain_spawn):
    """Убить группу процессов CLI. При разделении UID сервер сигналить CLI не может (нет
    CAP_KILL), поэтому сигнал отправляет тот же путь as_cli - от имени пользователя CLI."""
    if launcher:
        as_cli(
            launcher,
            ["python3", "-c", f"import os; os.killpg({int(pid)}, 9)"],
            minimal_env(),
            spawn=run,
            timeout=10,
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
    helper_env = minimal_env(env)
    try:
        try:
            _make_dirs(spawn, launcher, helper_env, root, work, home)
            cli = adapter.build(model, system, schema)
            stdin = adapter.stdin(system, messages, schema)
            cli_env = build_env(channel, env, home)
            if launcher:
                # Сервер не может зайти в каталог пользователя CLI (0700): в него переходит
                # уже CLI-пользователь, процесс сервера стартует из корня запуска.
                proc = as_cli(
                    launcher, ["sh", "-c", 'cd -- "$0" && exec "$@"', work, *cli], cli_env,
                    spawn=spawn, cwd=root, input=stdin, timeout=timeout,
                )  # fmt: skip
            else:
                proc = spawn(cli, input=stdin, env=cli_env, cwd=work, timeout=timeout)
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
    # Корень запуска открыт пользователю CLI: уборка (от его имени) проходит по всему корню.
    os.chmod(root, 0o777)
    res = as_cli(launcher, ["mkdir", "-m", "0700", work, home], helper_env, spawn=spawn, cwd=root)
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
            # Весь корень запуска: CLI мог создать что угодно рядом с work и home (../stash).
            as_cli(launcher, ["sh", "-c", CLEAN_SCRIPT, root], helper_env, spawn=spawn, cwd=root)
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


def parse_ipcs(output: str, uid: int, user: str) -> list[str]:
    """Объекты SysV IPC, принадлежащие пользователю CLI, по выводу `ipcs`."""
    found = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].startswith("0x") and parts[2] in (user, str(uid)):
            found.append(" ".join(parts[:3]))
    return found


def sysv_objects(uid: int, user: str) -> list[str]:
    found = []
    for flag in ("-m", "-q", "-s"):
        res = plain_spawn(["ipcs", flag], input="", env=minimal_env(), cwd=None, timeout=10)
        if res.returncode != 0:
            raise OSError("ipcs failed")  # не смогли проверить - значит нельзя считать чистым
        found += parse_ipcs(res.stdout, uid, user)
    return found


def mqueue_entries(path="/dev/mqueue") -> list[str]:
    try:
        return os.listdir(path)
    except FileNotFoundError:
        return []


def make_sweeper(
    launcher, user, attempts=3, *, processes=None, kill_cmd=None, reset_cmd=None, mqueue=None,
    ipc=None, uid=None,
):  # fmt: skip
    """Сброс всего, чем владеет пользователь CLI, и проверка «ничего не осталось».

    Сброс (всегда, под as_cli): убить все процессы пользователя CLI, удалить очереди POSIX и
    объекты SysV IPC. Проверка: процессов нет, /dev/mqueue пуст, объектов SysV нет. Если после
    попыток что-то осталось, возвращается False: исполнитель неисправен.
    """
    import pwd

    uid = pwd.getpwnam(user).pw_uid if uid is None else uid
    processes = processes or cli_processes
    kill_cmd = kill_cmd or ["python3", "-c", "import os; os.kill(-1, 9)"]
    reset_cmd = reset_cmd or ["sh", "-c", RESET_SCRIPT]
    mqueue = mqueue or mqueue_entries
    ipc = ipc or sysv_objects

    def clean() -> bool:
        try:
            return not processes(uid) and not mqueue() and not ipc(uid, user)
        except Exception:  # noqa: BLE001 - не удалось проверить: не считаем чистым
            return False

    def sweep() -> bool:
        for _ in range(attempts):
            as_cli(launcher, kill_cmd, minimal_env(), spawn=plain_spawn, timeout=10)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and processes(uid):
                time.sleep(0.05)
            as_cli(launcher, reset_cmd, minimal_env(), spawn=plain_spawn, timeout=30)
            if clean():
                return True
        return False

    return sweep


def _fail(channel, model, kind, duration_ms, secrets=()):
    # В журнал - только канал и код причины; stderr и тексты исключений не пишутся никуда.
    log.warning("model run failed: channel=%s kind=%s", channel, redact(kind, secrets))
    return ModelResult.failure(channel, model, kind, duration_ms)
