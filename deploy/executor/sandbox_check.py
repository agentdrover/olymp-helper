"""Проверка песочницы ИЗНУТРИ контейнера-исполнителя (AC-5, SPEC-1.0 1.4, уточнение по ревью #2013).

Запускается шагом CI executor-sandbox дважды в каждом контейнере (по одному на канал):
  srv - от имени сервера (exec-srv): окружение, /work, уборка, переход на UID CLI;
  cli - от имени CLI (exec-cli): запись закрыта, сервер и его окружение недоступны.
Только стандартная библиотека. Код возврата 0 - все проверки прошли.
"""

import os
import pwd
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
    other = next(c for c in TOKENS if c != channel)
    out = handle_request(
        dict(payload, channel=other),
        env=dict(os.environ),
        only_channel=channel,
        workroot="/work",
        launcher=launcher,
    )
    check("чужой канал отклонён", out.get("error_kind") == "bad_request")


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
