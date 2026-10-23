"""Ревью #2013, раунд 2: убийство CLI при разных UID, один запуск за раз, ключ, неисправность."""

import errno
import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.model import runner
from app.model.adapters import ClaudeAdapter
from app.model.executor_server import Gate, handle_request, make_server
from app.model.runner import run_cli

ENV = {"PATH": "/usr/bin:/bin", "CLAUDE_CODE_OAUTH_TOKEN": "tok", "CURSOR_API_KEY": "key"}
MSGS = [{"role": "user", "content": "q"}]
REQ = {"channel": "claude", "model": "m", "system": "s", "messages": MSGS, "schema": None}
OK = '{"is_error":false,"result":"ok"}'


def ok_spawn(argv, *, input, env, cwd, timeout):
    return SimpleNamespace(returncode=0, stderr="", stdout=OK)


def stuck_cli(tmp_path):
    """Заглушка claude: пишет свой pid и зависает (как зависший CLI)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    pidfile = tmp_path / "pid"
    script = bindir / "claude"
    script.write_text(f"#!/bin/sh\necho $$ > {pidfile}\nsleep 60\n")
    script.chmod(0o755)
    return str(bindir), pidfile


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_timeout_kills_cli_through_launcher_when_server_cannot_signal(tmp_path, monkeypatch):
    """P1: сервер (другой UID) не может сделать killpg; убивает тот же launcher, процесс исчез."""

    def eperm(*a, **k):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "killpg", eperm)  # сервер на другом UID: нет CAP_KILL
    bindir, pidfile = stuck_cli(tmp_path)
    work = tmp_path / "w"
    work.mkdir()
    env = dict(ENV, PATH=f"{bindir}:/usr/bin:/bin")
    pid = None
    try:
        res = run_cli(
            "claude",
            "m",
            "s",
            MSGS,
            None,
            env=env,
            workroot=str(work),
            timeout=3,
            launcher=["env", "--"],
            kill_wait=3,
        )
        pid = int(pidfile.read_text())
        assert res.error_kind == "timeout"
        assert not alive(pid)
    finally:
        if pid and alive(pid):
            os.kill(pid, 9)


def test_failed_kill_marks_the_executor_faulty(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "kill_group", lambda *a, **k: None)
    bindir, pidfile = stuck_cli(tmp_path)
    work = tmp_path / "w"
    work.mkdir()
    env = dict(ENV, PATH=f"{bindir}:/usr/bin:/bin")
    faults = []
    pid = None
    try:
        res = run_cli(
            "claude",
            "m",
            "s",
            MSGS,
            None,
            env=env,
            workroot=str(work),
            timeout=3,
            kill_wait=0.5,
            on_fault=faults.append,
        )
        pid = int(pidfile.read_text())
        assert res.error_kind == "timeout" and faults == ["kill"]
    finally:
        if pid and alive(pid):
            os.killpg(pid, 9)


def test_only_one_run_at_a_time_second_request_gets_busy(tmp_path):
    """P1: один активный запуск на контейнер; параллельный получает «занят» и CLI не запускает."""
    entered, release, calls = threading.Event(), threading.Event(), []

    def slow(argv, *, input, env, cwd, timeout):
        calls.append(argv)
        entered.set()
        release.wait(5)
        return SimpleNamespace(returncode=0, stderr="", stdout=OK)

    gate = Gate()
    first = []
    t = threading.Thread(
        target=lambda: first.append(
            handle_request(REQ, env=ENV, spawn=slow, workroot=str(tmp_path), gate=gate)
        )
    )
    t.start()
    assert entered.wait(5)
    second = handle_request(REQ, env=ENV, spawn=slow, workroot=str(tmp_path), gate=gate)
    release.set()
    t.join(5)
    assert second["error_kind"] == "busy" and second["error"]
    assert first[0]["text"] == "ok" and len(calls) == 1
    # Замок отпущен: следующий запрос проходит.
    assert handle_request(REQ, env=ENV, spawn=ok_spawn, workroot=str(tmp_path), gate=gate)["text"]


def test_leftover_processes_are_swept_before_and_after_a_run(tmp_path):
    events = []

    def sweeper():
        events.append("sweep")
        return True

    def spawn(argv, *, input, env, cwd, timeout):
        events.append("run")
        return SimpleNamespace(returncode=0, stderr="", stdout=OK)

    gate = Gate()
    handle_request(REQ, env=ENV, spawn=spawn, workroot=str(tmp_path), gate=gate, sweeper=sweeper)
    assert events == ["sweep", "run", "sweep"]
    # Если прежний процесс убрать не удалось - CLI не запускается, исполнитель неисправен.
    events.clear()
    gate = Gate()
    out = handle_request(
        REQ, env=ENV, spawn=spawn, workroot=str(tmp_path), gate=gate, sweeper=lambda: False
    )
    assert out["error_kind"] == "faulty" and "run" not in events and gate.fault_reason


def test_cleanup_failure_makes_the_executor_faulty(tmp_path, monkeypatch):
    """P2: уборка не удалась (rmtree ничего не удалил) - неисправен, дальше отказ."""
    monkeypatch.setattr(runner.shutil, "rmtree", lambda *a, **k: None)
    gate = Gate()
    out = handle_request(REQ, env=ENV, spawn=ok_spawn, workroot=str(tmp_path), gate=gate)
    assert out["text"] == "ok" and gate.fault_reason == "cleanup"
    monkeypatch.undo()
    again = handle_request(REQ, env=ENV, spawn=ok_spawn, workroot=str(tmp_path), gate=gate)
    assert again["error_kind"] == "faulty" and again["error"]


def test_cleanup_runs_as_cli_user_with_chmod_before_rm(tmp_path):
    calls = []

    def spawn(argv, *, input, env, cwd, timeout):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stderr="", stdout=OK)

    launcher = ["setpriv", "--reuid=exec-cli", "--"]
    run_cli(
        "claude",
        "m",
        "s",
        MSGS,
        None,
        env=ENV,
        spawn=spawn,
        workroot=str(tmp_path),
        launcher=launcher,
    )
    assert [c[len(launcher)] for c in calls] == ["mkdir", "sh", "sh"]
    cli = calls[1]
    assert cli[-len(ClaudeAdapter().build("m", "s", None)) :][0] == "claude"
    assert cli[len(launcher) + 3].endswith("/work")  # в каталог переходит пользователь CLI
    assert all(c[: len(launcher)] == launcher for c in calls)
    mkdir = calls[0]
    assert "-m" in mkdir and mkdir[mkdir.index("-m") + 1] == "0700"
    script = calls[2][len(launcher) + 2]
    assert script.index("chmod -R u+rwX") < script.index("rm -rf")


def test_enospc_on_workdir_is_a_run_error_not_an_exception(tmp_path, monkeypatch):
    def full(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(runner.tempfile, "mkdtemp", full)
    out = handle_request(REQ, env=ENV, spawn=ok_spawn, workroot=str(tmp_path), gate=Gate())
    assert out["error_kind"] == "workdir" and out["error"] and out["text"] is None
    res = run_cli("claude", "m", "s", MSGS, None, env=ENV, spawn=ok_spawn)
    assert res.error_kind == "workdir"


def serve(**kw):
    kw.setdefault("shared_key", "k" * 16)
    srv = make_server("127.0.0.1", 0, env=ENV, **kw)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def post(url, payload, key=None):
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["X-Executor-Key"] = key
    req = urllib.request.Request(url + "/run", data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def test_run_requires_the_shared_key(tmp_path):
    """P1: без ключа 401 и CLI не запускается; с верным - обычный ответ; healthz открыт."""
    calls = []

    def spawn(argv, *, input, env, cwd, timeout):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stderr="", stdout=OK)

    srv, url = serve(workroot=str(tmp_path), spawn=spawn)
    try:
        assert post(url, REQ)[0] == 401
        assert post(url, REQ, key="wrong-key-wrong-k")[0] == 401
        assert post(url, REQ, key="")[0] == 401
        assert not calls
        status, body = post(url, REQ, key="k" * 16)
        assert status == 200 and body["text"] == "ok" and len(calls) == 1
        assert json.load(urllib.request.urlopen(url + "/healthz", timeout=5))["status"] == "ok"
    finally:
        srv.shutdown()
        srv.server_close()


def test_server_refuses_to_start_without_a_key(tmp_path):
    for bad in (None, ""):
        with pytest.raises(ValueError):
            make_server("127.0.0.1", 0, env=ENV, workroot=str(tmp_path), shared_key=bad)


def test_faulty_executor_answers_503_and_refuses_runs(tmp_path):
    gate = Gate()
    gate.fault("cleanup")
    srv, url = serve(workroot=str(tmp_path), spawn=ok_spawn, gate=gate)
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(url + "/healthz", timeout=5)
        assert err.value.code == 503
        status, body = post(url, REQ, key="k" * 16)
        assert status == 503 and body["error_kind"] == "faulty"
    finally:
        srv.shutdown()
        srv.server_close()


def test_enospc_over_http_keeps_the_server_alive(tmp_path, monkeypatch):
    srv, url = serve(workroot=str(tmp_path), spawn=ok_spawn)
    try:

        def full(*a, **k):
            raise OSError(errno.ENOSPC, "full")

        monkeypatch.setattr(runner.tempfile, "mkdtemp", full)
        status, body = post(url, REQ, key="k" * 16)
        assert status == 200 and body["error_kind"] == "workdir"
        monkeypatch.undo()
        assert post(url, REQ, key="k" * 16)[1]["text"] == "ok"
    finally:
        srv.shutdown()
        srv.server_close()


def test_client_sends_the_shared_key():
    from app.model.client import ExecutorChannel

    seen = {}

    def fake_post(url, payload, timeout, headers=None):
        seen["headers"] = headers
        return {
            "text": "hi",
            "json": None,
            "model": "m",
            "duration_ms": 1,
            "usage": None,
            "error": None,
            "error_kind": None,
            "channel": "claude",
        }

    res = ExecutorChannel("claude", "http://x", key="secret-shared-key", post=fake_post).complete(
        "s", MSGS, model="m"
    )
    assert res.text == "hi" and seen["headers"] == {"X-Executor-Key": "secret-shared-key"}


def test_compose_and_sandbox_carry_the_shared_key_and_restart_policy():
    root = Path(__file__).parent.parent / "deploy" / "executor"
    compose = (root.parent / "compose.yaml").read_text(encoding="utf-8")
    services = yaml.safe_load(compose)["services"]
    for name in ("executor-claude", "executor-cursor"):
        svc = services[name]
        assert svc["environment"]["EXECUTOR_SHARED_KEY"].startswith("${EXECUTOR_SHARED_KEY:?")
        assert svc["init"] is True and "healthcheck" in svc
        assert svc["restart"] == "unless-stopped"
    assert "EXECUTOR_SHARED_KEY" in (root / "sandbox.sh").read_text(encoding="utf-8")
    assert "--init" in (root / "sandbox.sh").read_text(encoding="utf-8")
    # Без /dev/shm: иначе это место записи CLI, которое переживает запуск.
    assert compose.count("ipc: none") == 2
    assert "--ipc none" in (root / "sandbox.sh").read_text(encoding="utf-8")
    assert "/dev/mqueue" not in compose  # настоящий mqueue: очереди видно и сбрасывает reset
    assert "/dev/mqueue:" not in (root / "sandbox.sh").read_text(encoding="utf-8")
    # Ключ не должен попасть в окружение CLI: белый список окружения его не содержит.
    from app.model.adapters import build_env

    assert "EXECUTOR_SHARED_KEY" not in build_env(
        "claude", dict(ENV, EXECUTOR_SHARED_KEY="x"), "/h"
    )


def test_spec_records_one_run_key_and_faulty_state():
    spec = (Path(__file__).parent.parent / "docs" / "spec" / "SPEC-1.0.md").read_text("utf-8")
    for phrase in (
        "Один запуск за раз",
        "X-Executor-Key",
        "неисправен",
        "«занят»",
        "SysV IPC",
        "/dev/mqueue",
        "ничего не осталось",
    ):
        assert phrase in spec, phrase


def test_unexpected_error_in_the_handler_is_a_run_error_and_releases_the_lock(tmp_path):
    def broken():
        raise RuntimeError("boom")

    gate = Gate()
    out = handle_request(
        REQ, env=ENV, spawn=ok_spawn, workroot=str(tmp_path), gate=gate, sweeper=broken
    )
    assert out["error_kind"] == "internal" and "boom" not in json.dumps(out)
    assert gate.try_enter()  # замок отпущен
    gate.leave()


# ---- Ревью #2013, раунд 3: один путь запуска под пользователем CLI -----------------------------

KEY = "SHARED-KEY-MUST-NOT-LEAK-0123456789"


def recording_launcher(tmp_path):
    """Заглушка launcher: пишет окружение, с которым её запустили, затем исполняет команду."""
    dump = tmp_path / "env-dump"
    script = tmp_path / "rec-launcher"
    script.write_text(f'#!/bin/sh\nenv >> {dump}\necho ---- >> {dump}\nexec "$@"\n')
    script.chmod(0o755)
    return [str(script)], dump


def test_every_helper_under_cli_user_gets_a_clean_environment(tmp_path, monkeypatch):
    """P1: kill, sweep, cleanup и сам запуск не наследуют окружение сервера (ключ приложения)."""
    monkeypatch.setenv("EXECUTOR_SHARED_KEY", KEY)
    monkeypatch.setenv("SOME_SERVER_SECRET", "server-only")
    launcher, dump = recording_launcher(tmp_path)
    bindir, pidfile = stuck_cli(tmp_path)
    work = tmp_path / "w"
    work.mkdir()
    env = dict(
        os.environ, PATH=f"{bindir}:/usr/bin:/bin", **{k: v for k, v in ENV.items() if k != "PATH"}
    )
    calls = iter([[1], []])
    sweeper = runner.make_sweeper(
        launcher, "nobody", processes=lambda uid: next(calls, []), kill_cmd=["true"],
        reset_cmd=["true"], mqueue=list, ipc=lambda uid, user: [], uid=0,
    )  # fmt: skip

    sweeper()  # sweeper: процесс найден, вызывается kill, затем пусто
    pid = None
    try:
        res = run_cli(
            "claude",
            "m",
            "s",
            MSGS,
            None,
            env=env,
            workroot=str(work),
            timeout=3,
            launcher=launcher,
            kill_wait=3,
            sweeper=lambda: True,
        )
        pid = int(pidfile.read_text())
        assert res.error_kind == "timeout" and not alive(pid)
    finally:
        if pid and alive(pid):
            os.kill(pid, 9)
    blocks = dump.read_text().split("----\n")
    blocks = [b for b in blocks if b.strip()]
    assert len(blocks) >= 4  # sweep-kill, mkdir, CLI, kill-group, cleanup
    for block in blocks:
        assert KEY not in block and "SOME_SERVER_SECRET" not in block
    cli_blocks = [b for b in blocks if "CLAUDE_CODE_OAUTH_TOKEN" in b]
    assert len(cli_blocks) == 1 and "CURSOR_API_KEY" not in cli_blocks[0]
    for block in blocks:
        names = {line.split("=", 1)[0] for line in block.splitlines() if "=" in line}
        assert names <= {
            "PATH",
            "HOME",
            "LANG",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "PWD",
            "SHLVL",
            "_",
            "OLDPWD",
            "LC_ALL",
            "TZ",
            "TMPDIR",
        }, names


def test_setpriv_is_started_only_by_the_single_entry_point():
    """Структурно: запуск под пользователем CLI - одна функция as_cli; setpriv назван в одном месте."""
    import inspect
    import re as _re

    root = Path(__file__).parent.parent / "app"
    users = {}
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        users[path.name] = (text.count("setpriv"), len(_re.findall(r"\*launcher", text)))
    assert {k: v[0] for k, v in users.items() if v[0]} == {"runner.py": 1} or all(
        k == "runner.py" for k, v in users.items() if v[0]
    )
    assert sum(v[1] for v in users.values()) == 1  # `*launcher` - только внутри as_cli
    assert "*launcher" in inspect.getsource(runner.as_cli)
    assert "setpriv" in inspect.getsource(runner.cli_launcher)


def test_cleanup_covers_the_whole_run_root_not_only_work_and_home(tmp_path, monkeypatch):
    """P2: CLI создал ../stash (0700) - корень убран целиком, исполнитель не неисправен."""

    # Сервер не может удалить чужое: rmtree умеет только пустой каталог (как под другим UID).
    def empty_only(path, **kw):
        try:
            os.rmdir(path)
        except OSError:
            pass

    monkeypatch.setattr(runner.shutil, "rmtree", empty_only)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cli = bindir / "claude"
    cli.write_text(
        "#!/bin/sh\ncat >/dev/null\nmkdir -m 700 ../stash && echo x > ../stash/f && chmod 500 ../stash\n"
        f"echo '{OK}'\n"
    )
    cli.chmod(0o755)
    work = tmp_path / "w"
    work.mkdir()
    env = dict(ENV, PATH=f"{bindir}:/usr/bin:/bin")
    faults = []
    res = run_cli(
        "claude",
        "m",
        "s",
        MSGS,
        None,
        env=env,
        workroot=str(work),
        launcher=["env", "--"],
        on_fault=faults.append,
    )
    assert res.text == "ok"
    assert os.listdir(work) == [] and faults == []


def test_fault_is_rechecked_after_taking_the_lock(tmp_path):
    """P2: B прошёл проверку неисправности, A сломал исполнитель и вышел - B не запускает CLI."""
    calls = []

    def spawn(argv, *, input, env, cwd, timeout):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stderr="", stdout=OK)

    class RacyGate(Gate):
        def __init__(self):
            super().__init__()
            self.first_read_done = threading.Event()
            self.proceed = threading.Event()
            self._first = True

        @property
        def fault_reason(self):
            if self._first and threading.current_thread().name == "B":
                self._first = False
                value = self._reason
                self.first_read_done.set()
                self.proceed.wait(5)
                return value
            return self._reason

        @fault_reason.setter
        def fault_reason(self, value):
            self._reason = value

    gate = RacyGate()
    box = []
    b = threading.Thread(
        target=lambda: box.append(
            handle_request(REQ, env=ENV, spawn=spawn, workroot=str(tmp_path), gate=gate)
        ),
        name="B",
    )
    b.start()
    assert gate.first_read_done.wait(5)
    gate.fault("cleanup")  # A успел сломать исполнитель и отпустить замок
    gate.proceed.set()
    b.join(5)
    assert box[0]["error_kind"] == "faulty" and not calls


# ---- Ревью #2013, раунд 5: один сброс и одна проверка «ничего не осталось» ---------------------


def reset_sweeper(launcher, **over):
    kw = {
        "processes": lambda uid: [],
        "kill_cmd": ["true"],
        "reset_cmd": ["true"],
        "mqueue": list,
        "ipc": lambda uid, user: [],
        "uid": 0,
        "attempts": 1,
    }
    kw.update(over)
    return runner.make_sweeper(launcher, "nobody", **kw)


def test_reset_always_runs_kill_then_reset_even_if_everything_looks_clean(tmp_path):
    log = tmp_path / "log"
    sweeper = reset_sweeper(
        ["env", "--"],
        kill_cmd=["sh", "-c", f"echo kill >> {log}"],
        reset_cmd=["sh", "-c", f"echo reset >> {log}"],
    )
    assert sweeper() is True
    assert log.read_text().split() == ["kill", "reset"]


@pytest.mark.parametrize(
    "leftover",
    [
        {"processes": lambda uid: [42]},
        {"mqueue": lambda: ["leaked-queue"]},
        {"ipc": lambda uid, user: ["msq 1234 nobody"]},
        {"ipc": lambda uid, user: (_ for _ in ()).throw(OSError("ipcs failed"))},
    ],
    ids=["process", "posix-mqueue", "sysv-ipc", "cannot-check"],
)
def test_verify_fails_when_anything_of_the_cli_user_is_left(leftover):
    assert reset_sweeper(["env", "--"], **leftover)() is False


def test_failed_reset_or_verify_after_a_run_makes_the_executor_faulty(tmp_path):
    answers = iter([True, False])  # до запуска чисто, после запуска в IPC осталось
    gate = Gate()
    out = handle_request(
        REQ,
        env=ENV,
        spawn=ok_spawn,
        workroot=str(tmp_path),
        gate=gate,
        sweeper=lambda: next(answers),
    )
    assert out["text"] == "ok" and gate.fault_reason == "sweep"
    again = handle_request(REQ, env=ENV, spawn=ok_spawn, workroot=str(tmp_path), gate=gate)
    assert again["error_kind"] == "faulty"


def test_reset_goes_through_as_cli_with_a_clean_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("EXECUTOR_SHARED_KEY", KEY)
    launcher, dump = recording_launcher(tmp_path)
    assert reset_sweeper(launcher)() is True
    blocks = [b for b in dump.read_text().split("----\n") if b.strip()]
    assert len(blocks) == 2  # kill и reset: оба под пользователем CLI
    assert all(KEY not in b for b in blocks)
    # Настоящий сброс: очереди POSIX, объекты SysV и всё, что осталось от пользователя CLI.
    assert runner.RESET_SCRIPT and "/dev/mqueue" in runner.RESET_SCRIPT
    assert "ipcrm" in runner.RESET_SCRIPT
    import inspect

    assert "RESET_SCRIPT" in inspect.getsource(runner.make_sweeper)


MSG_HEADER = (
    "       key      msqid perms      cbytes       qnum lspid lrpid   uid   gid  cuid  cgid"
    "      stime      rtime      ctime\n"
)


def test_sysvipc_files_are_read_whoever_owns_the_object(tmp_path):
    """Объект, у которого creator - пользователь CLI, а uid и mode поменяли (IPC_SET), виден."""
    (tmp_path / "msg").write_text(
        MSG_HEADER
        + "1329397761      0     0          0          0     0     0     0     0 10002 10002 0 0 0\n"
    )
    (tmp_path / "shm").write_text(
        "       key      shmid perms size cpid lpid nattch uid gid cuid cgid\n"
    )
    (tmp_path / "sem").write_text(
        "       key      semid perms nsems uid gid cuid cgid otime ctime\n"
    )
    found = runner.sysv_objects(10002, "exec-cli", root=str(tmp_path))
    assert len(found) == 1 and found[0].startswith("msg ")
    assert "10002" in found[0]  # в отчёте видно cuid
    assert runner.parse_sysvipc(MSG_HEADER, "msg") == []
    for name in ("msg", "shm", "sem"):
        (tmp_path / name).write_text("key id perms\n")
    assert runner.sysv_objects(10002, "exec-cli", root=str(tmp_path)) == []
    (tmp_path / "sem").unlink()
    with pytest.raises(OSError):  # не смогли прочитать - нельзя считать чистым
        runner.sysv_objects(10002, "exec-cli", root=str(tmp_path))


SYSCTLS = {
    "kernel.msgmni": "0",
    "kernel.sem": "0 0 0 0",
    "kernel.shmmni": "0",
    "kernel.shm_rmid_forced": "1",
    "fs.mqueue.queues_max": "0",
}


def test_zero_ipc_limits_are_pinned_in_compose_and_sandbox():
    """Слабый тест, но он закрепляет конфигурацию: объекты IPC невозможны по построению."""
    root = Path(__file__).parent.parent / "deploy" / "executor"
    compose = (root.parent / "compose.yaml").read_text(encoding="utf-8")
    sandbox = (root / "sandbox.sh").read_text(encoding="utf-8")
    assert compose.count("    sysctls:\n") == 2
    for name, value in SYSCTLS.items():
        assert compose.count(f'      {name}: "{value}"\n') == 2, name
        assert sandbox.count(f'--sysctl "{name}={value}"') == 1, name
