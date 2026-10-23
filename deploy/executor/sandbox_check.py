"""Проверка песочницы ИЗНУТРИ контейнера-исполнителя (AC-5, SPEC-1.0 1.4, уточнение по ревью #2013).

Запускается шагом CI executor-sandbox дважды в каждом контейнере (по одному на канал):
  srv - от имени сервера (exec-srv): окружение, /work, уборка, переход на UID CLI;
  cli - от имени CLI (exec-cli): запись закрыта, сервер и его окружение недоступны.
Только стандартная библиотека. Код возврата 0 - все проверки прошли.
"""

import os
import pwd
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/opt/executor")

failures: list[str] = []
TOKENS = {"claude": "CLAUDE_CODE_OAUTH_TOKEN", "cursor": "CURSOR_API_KEY"}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("OK   " if ok else "FAIL ") + name + (f" ({detail})" if detail else ""), flush=True)
    if not ok:
        failures.append(name)


def cannot_write(directory: str) -> bool:
    try:
        with tempfile.NamedTemporaryFile(dir=directory):
            return False
    except OSError:
        return True


def common() -> None:
    # Запись вне временного каталога падает.
    for directory in ("/", "/opt/executor", "/usr/local", "/etc", "/home", "/var/tmp"):
        if os.path.isdir(directory):
            check(f"запись в {directory} закрыта", cannot_write(directory))
    # Тома с данными нет.
    for data in ("/srv/olymp", "/data", "/var/lib/olymp", "/app/data"):
        check(f"нет тома {data}", not os.path.exists(data))
    mounts = [line.split()[1] for line in Path("/proc/mounts").read_text().splitlines()]
    bad = [m for m in mounts if m.startswith(("/srv", "/data", "/mnt", "/Users", "/host"))]
    check("нет монтирований данных", not bad, ",".join(bad))
    # Пользовательских настроек и MCP-конфигов CLI нет.
    home = Path(os.environ.get("HOME", "/nonexistent"))
    candidates = [
        home / ".claude",
        home / ".claude.json",
        home / ".cursor",
        home / ".config" / "cursor",
        home / ".mcp.json",
        Path("/opt/executor/.mcp.json"),
        Path("/work/.mcp.json"),
        Path("/etc/claude-code"),
        Path("/etc/cursor"),
        Path("/etc/mcp.json"),
        Path("/home/node/.claude"),
        Path("/home/node/.cursor"),
        Path("/root/.claude"),
    ]
    found = []
    for path in candidates:
        try:
            if path.exists():
                found.append(str(path))
        except PermissionError:
            # Недоступно = нет: в закрытый каталог (например /root) процесс не заглянет.
            print(f"OK   {path} недоступен", flush=True)
    check("нет настроек CLI и MCP-конфигов", not found, ",".join(found))


def server_mode() -> None:
    common()
    channel = os.environ.get("EXECUTOR_CHANNEL", "")
    check("канал задан", channel in TOKENS, channel)
    # Контейнер на канал: в окружении сервера токен ТОЛЬКО своего канала.
    present = {c for c, var in TOKENS.items() if os.environ.get(var)}
    check("в окружении только токен своего канала", present == {channel}, ",".join(sorted(present)))
    check("ANTHROPIC_API_KEY нет", not os.environ.get("ANTHROPIC_API_KEY"))
    # /work - ограниченный tmpfs, пуст на входе.
    work = Path("/work")
    check("/work пуст", work.is_dir() and not any(work.iterdir()))
    opts = [line.split() for line in Path("/proc/mounts").read_text().splitlines()]
    row = next((r for r in opts if r[1] == "/work"), None)
    flags = set(row[3].split(",")) if row else set()
    check("/work - tmpfs", bool(row) and row[2] == "tmpfs")
    check(
        "/work noexec,nosuid,nodev", {"noexec", "nosuid", "nodev"} <= flags, ",".join(sorted(flags))
    )
    check("/work с лимитом размера", any(f.startswith("size=") for f in flags))
    live_checks(channel)


IPC_CREATE = """
import ctypes, json, struct
libc = ctypes.CDLL("libc.so.6", use_errno=True)
C = 0o1000
q = libc.msgget(0x4F4C0001, C | 0o666)
rs = [q, libc.msgsnd(q, struct.pack("l8s", 1, b"leakleak"), 8, 0),
      libc.shmget(0x4F4C0002, 4096, C | 0o666), libc.semget(0x4F4C0003, 1, C | 0o666),
      libc.mq_open(b"/olymp-leak", 0o102, 0o666, None)]
blocked = all(r == -1 for r in rs)
print(json.dumps({"is_error": False,
                  "result": "blocked" if blocked else "created %s" % rs}))
"""

IPC_PROBE = """
import ctypes, json
libc = ctypes.CDLL("libc.so.6", use_errno=True)
rs = [libc.msgget(0x4F4C0001, 0), libc.shmget(0x4F4C0002, 0, 0), libc.semget(0x4F4C0003, 0, 0),
      libc.mq_open(b"/olymp-leak", 2)]
print(json.dumps({"is_error": False,
                  "result": "found %s" % rs if any(r != -1 for r in rs) else "none"}))
"""


def ok_json(text):
    """Команда shell, печатающая успешный JSON-ответ CLI."""
    return 'echo "{\\"is_error\\":false,\\"result\\":\\"' + text + '\\"}"'


def is_helper(argv, launcher):
    """Служебные команды сервера (создание и уборка каталогов) идут как есть."""
    return argv[len(launcher)] == "mkdir" or "rm -rf" in " ".join(argv)


def in_work(argv, body):
    """Подмена CLI: тот же запуск (launcher, переход в каталог CLI), но команда своя."""
    n = len(launcher_of(argv))
    return [*argv[:n], "sh", "-c", 'cd -- "$0" && ' + body, argv[n + 3]]


def launcher_of(argv):
    return argv[: argv.index("sh")]


def http_status(path, key=None, payload=None):
    import json
    import urllib.error
    import urllib.request

    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["X-Executor-Key"] = key
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(f"http://127.0.0.1:8099{path}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def live_checks(channel):
    """То, что проверяется только в контейнере: UID, живые процессы, ключ, уборка."""
    from app.model.executor_server import Gate, cli_launcher, handle_request
    from app.model.runner import cli_processes, make_spawn, make_sweeper, run_cli

    work = Path("/work")
    user = os.environ.get("EXECUTOR_CLI_USER", "exec-cli")
    launcher = cli_launcher(user)
    uid = pwd.getpwnam(user).pw_uid
    sweeper = make_sweeper(launcher, user)
    payload = {
        "channel": channel,
        "model": "m",
        "system": "s",
        "messages": [{"role": "user", "content": "q"}],
    }
    # Ключ приложения: без него и с чужим - 401, с верным запрос принят (проверка формы, без CLI).
    key = os.environ.get("EXECUTOR_SHARED_KEY", "")
    check("ключ исполнителя задан", len(key) >= 8)
    check("без ключа 401", http_status("/run", None, payload)[0] == 401)
    check("с чужим ключом 401", http_status("/run", "wrong-key-wrong", payload)[0] == 401)
    code, body = http_status("/run", key, {"channel": channel})
    check("с верным ключом запрос принят", code == 200 and body.get("error_kind") == "bad_request")
    check("healthz без ключа", http_status("/healthz")[0] == 200)

    # CLI под другим UID; уборка файлов, созданных им (в том числе каталог с правами 700).
    script = "echo log > cli.log; mkdir d; echo x > d/f; chmod 700 d; " + ok_json("uid=$(id -u)")
    spawn = make_spawn(launcher)

    def fake_cli(body):
        def run(argv, **kw):
            if is_helper(argv, launcher):
                return spawn(argv, **kw)
            return spawn(in_work(argv, body), **kw)

        return run

    gate = Gate()
    out = handle_request(
        payload,
        env=dict(os.environ),
        spawn=fake_cli(script),
        workroot="/work",
        only_channel=channel,
        launcher=launcher,
        gate=gate,
        sweeper=sweeper,
    )
    check("CLI запущен под другим UID", out.get("text") == f"uid={uid}", str(out.get("error_kind")))
    left = [os.path.join(d, n) for d, dirs, files in os.walk("/work") for n in dirs + files]
    check("/work пуст после ответа", not left and not gate.fault_reason, "; ".join(left))

    # Служебные процессы и сам CLI под пользователем CLI не наследуют окружение сервера.
    from app.model.adapters import build_env
    from app.model.runner import as_cli, minimal_env, plain_spawn

    dump = ["python3", "-c", "import os,sys; sys.stdout.write(repr(sorted(os.environ)))"]
    helper = as_cli(launcher, dump, minimal_env(dict(os.environ)), spawn=plain_spawn)
    check(
        "служебный процесс CLI-пользователя без ключа сервера",
        "EXECUTOR_SHARED_KEY" not in helper.stdout
        and not any(v in helper.stdout for v in TOKENS.values()),
        helper.stdout,
    )
    cli_env = as_cli(
        launcher, dump, build_env(channel, dict(os.environ), "/tmp"), spawn=plain_spawn
    )
    check(
        "CLI получает только токен своего канала, без ключа сервера",
        TOKENS[channel] in cli_env.stdout
        and "EXECUTOR_SHARED_KEY" not in cli_env.stdout
        and not any(v in cli_env.stdout for c, v in TOKENS.items() if c != channel),
        cli_env.stdout,
    )

    # CLI создал каталог рядом с work и home: корень запуска убран целиком, исполнитель исправен.
    faults = []
    res = run_cli(
        channel, "m", "s", payload["messages"], None, env=dict(os.environ),
        spawn=fake_cli("mkdir -m 700 ../stash; echo x > ../stash/f; chmod 500 ../stash; "
                       + ok_json("stash")),
        workroot="/work", launcher=launcher, on_fault=faults.append, sweeper=sweeper,
    )  # fmt: skip
    check(
        "каталог рядом с work убран",
        res.text == "stash" and not any(work.iterdir()) and not faults,
        ",".join(faults),
    )

    # Состояние IPC не переживает запуск: запуск 1 пытается создать очередь и сегмент SysV, семафор и
    # очередь POSIX с фиксированными ключами и именем (нулевые лимиты IPC не дают); запуск 2 пытается их открыть (ревью #2013,
    # круг 5). Оба прогона идут обычным путём сервера (as_cli, сброс и проверка после запуска).
    faults = []
    res1 = run_cli(
        channel, "m", "s", payload["messages"], None, env=dict(os.environ),
        spawn=fake_cli("python3 -c " + shlex.quote(IPC_CREATE)),
        workroot="/work", launcher=launcher, on_fault=faults.append, sweeper=sweeper,
    )  # fmt: skip
    check("запуск 1 не смог создать ни один объект IPC", res1.text == "blocked", str(res1.text))
    res2 = run_cli(
        channel, "m", "s", payload["messages"], None, env=dict(os.environ),
        spawn=fake_cli("python3 -c " + shlex.quote(IPC_PROBE)),
        workroot="/work", launcher=launcher, on_fault=faults.append, sweeper=sweeper,
    )  # fmt: skip
    check("запуск 2 не находит объекты запуска 1", res2.text == "none", str(res2.text))
    check("после IPC-запусков исполнитель исправен", not faults, ",".join(faults))
    check("очереди POSIX убраны", not os.listdir("/dev/mqueue"))

    # Зависший CLI убивается через launcher (у сервера нет права сигналить чужому UID).
    faults = []
    res = run_cli(
        channel, "m", "s", payload["messages"], None, env=dict(os.environ),
        spawn=fake_cli("sleep 300"), workroot="/work", timeout=2, launcher=launcher,
        on_fault=faults.append, sweeper=sweeper, kill_wait=5,
    )  # fmt: skip
    check("зависший CLI: таймаут", res.error_kind == "timeout", str(res.error_kind))
    check(
        "зависший CLI убит, исполнитель исправен",
        not cli_processes(uid) and not faults,
        ",".join(faults),
    )

    # Фоновый процесс CLI не переживает запуск.
    bg = "sleep 300 >/dev/null 2>&1 & " + ok_json("bg")
    res = run_cli(
        channel, "m", "s", payload["messages"], None, env=dict(os.environ),
        spawn=fake_cli(bg), workroot="/work", launcher=launcher, on_fault=faults.append,
        sweeper=sweeper,
    )  # fmt: skip
    check("фоновый запуск завершён", res.text == "bg", str(res.error_kind))
    check("фоновый процесс CLI убран", not cli_processes(uid) and not faults, ",".join(faults))
    check("/work пуст после фонового запуска", not any(work.iterdir()))

    # Занят: параллельный запрос получает «занят».
    import threading

    gate = Gate()
    entered, release = threading.Event(), threading.Event()
    box = []

    def slow(argv, **kw):
        if is_helper(argv, launcher):
            return spawn(argv, **kw)
        entered.set()
        release.wait(20)
        return spawn(in_work(argv, ok_json("x")), **kw)

    t = threading.Thread(
        target=lambda: box.append(
            handle_request(
                payload,
                env=dict(os.environ),
                spawn=slow,
                workroot="/work",
                only_channel=channel,
                launcher=launcher,
                gate=gate,
                sweeper=sweeper,
            )
        )
    )
    t.start()
    entered.wait(20)
    second = handle_request(
        payload,
        env=dict(os.environ),
        spawn=slow,
        workroot="/work",
        only_channel=channel,
        launcher=launcher,
        gate=gate,
        sweeper=sweeper,
    )
    release.set()
    t.join(30)
    check("параллельный запрос получает «занят»", second.get("error_kind") == "busy")
    # Настоящие CLI запускаются под пользователем CLI внутри этой же усиленной конфигурации.
    from app.model.runner import CLEAN_SCRIPT

    wanted = {"claude": os.environ.get("WANT_CLAUDE_VERSION", ""),
              "cursor-agent": os.environ.get("WANT_CURSOR_VERSION", "")}  # fmt: skip
    for binary, chan in (("claude", "claude"), ("cursor-agent", "cursor")):
        root = tempfile.mkdtemp(prefix="run-", dir="/work")
        os.chmod(root, 0o777)
        work_dir, home_dir = os.path.join(root, "work"), os.path.join(root, "home")
        as_cli(launcher, ["mkdir", "-m", "0700", work_dir, home_dir], minimal_env(dict(os.environ)),
               spawn=plain_spawn, cwd=root)  # fmt: skip
        cli_env = build_env(chan, {"PATH": os.environ["PATH"], TOKENS[chan]: "ci-placeholder"},
                            home_dir)  # fmt: skip
        res = as_cli(launcher, ["sh", "-c", 'cd -- "$0" && exec "$@"', work_dir, binary, "--version"],
                     cli_env, spawn=plain_spawn, cwd=root, timeout=60)  # fmt: skip
        check(f"{binary} --version под exec-cli в усиленном контейнере",
              bool(wanted[binary]) and wanted[binary] in res.stdout,
              (res.stdout + res.stderr).strip()[:200])  # fmt: skip
        as_cli(launcher, ["sh", "-c", CLEAN_SCRIPT, root], minimal_env(dict(os.environ)),
               spawn=plain_spawn, cwd=root)  # fmt: skip
        shutil.rmtree(root, ignore_errors=True)
    check("/work пуст после проверки CLI", not any(work.iterdir()))
    other = next(c for c in TOKENS if c != channel)
    out = handle_request(
        dict(payload, channel=other),
        env=dict(os.environ),
        only_channel=channel,
        workroot="/work",
        launcher=launcher,
    )
    check("чужой канал отклонён", out.get("error_kind") == "bad_request")


SKIPPED = ("/proc", "/sys", "/dev/mqueue")


def skipped(path: str) -> bool:
    return any(path == s or path.startswith(s + "/") for s in SKIPPED)


def writable_outside_run_dir() -> list[str]:
    """Каталоги (создание файла реально удаётся) и обычные файлы, доступные на запись."""
    found = []
    for root, dirs, files in os.walk("/", followlinks=False):
        dirs[:] = [d for d in dirs if not skipped(os.path.join(root, d))]
        if os.access(root, os.W_OK) and not cannot_write(root):
            found.append(root)
        if root == "/dev" or root.startswith("/dev/"):
            continue  # устройства (/dev/null и т.п.) доступны на запись по определению
        for name in files:
            path = os.path.join(root, name)
            if not os.path.islink(path) and os.access(path, os.W_OK):
                found.append(path)
    return found


def cli_mode() -> None:
    common()
    server = Path("/proc/1/status").read_text()
    server_uid = next(line.split()[1] for line in server.splitlines() if line.startswith("Uid:"))
    check("UID CLI отличается от UID сервера", str(os.getuid()) != server_uid, server_uid)
    try:
        Path("/proc/1/environ").read_bytes()
        readable = True
    except OSError:
        readable = False
    check("окружение сервера (/proc/1/environ) недоступно CLI", not readable)
    # setpriv закрыт группой: CLI не может ни стать другим пользователем, ни вернуться в сервер.
    try:
        res = subprocess.run(
            ["setpriv", "--reuid=exec-srv", "true"], check=False, capture_output=True, timeout=10
        )
        ok = res.returncode != 0
    except OSError:
        ok = True
    check("CLI не может сменить UID через setpriv", ok)
    check("запись в /work закрыта для CLI", cannot_write("/work"))
    # Вне каталога запуска CLI не пишет нигде - ни в каталоги любой глубины, ни в существующие
    # файлы. Иначе данные переживают запуск (ревью #2013, круги 4-5). /proc, /sys и /dev/mqueue
    # пропущены: последнее убирает и проверяет reset (live_checks).
    writable = writable_outside_run_dir()
    check("CLI не пишет нигде вне каталога запуска", not writable, ", ".join(writable[:20]))
    check("/dev/shm нет", not os.path.isdir("/dev/shm") or cannot_write("/dev/shm"))
    # Объекты IPC невозможны: каждый вид создать от имени CLI не удаётся (нулевые лимиты).
    code = IPC_CREATE.replace("print(", "import sys; sys.exit(0 if blocked else 1)\nprint(", 1)
    res = subprocess.run(
        [sys.executable, "-c", code], check=False, capture_output=True, text=True, timeout=30
    )
    check("exec-cli не создаёт msg/shm/sem/mq", res.returncode == 0, res.stdout.strip()[:200])
    try:
        os.kill(1, 0)
        can_signal = True
    except PermissionError:
        can_signal = False
    check("CLI не может сигналить серверу", not can_signal)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "srv":
        server_mode()
    elif mode == "cli":
        cli_mode()
    else:
        print("usage: sandbox_check.py srv|cli")
        return 2
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
