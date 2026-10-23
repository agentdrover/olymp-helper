"""Проверка песочницы ИЗНУТРИ контейнера-исполнителя (AC-5, SPEC-1.0 1.4, уточнение по ревью #2013).

Запускается шагом CI executor-sandbox дважды в каждом контейнере (по одному на канал):
  srv - от имени сервера (exec-srv): окружение, /work, уборка, переход на UID CLI;
  cli - от имени CLI (exec-cli): запись закрыта, сервер и его окружение недоступны.
Только стандартная библиотека. Код возврата 0 - все проверки прошли.
"""

import os
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
    found = [str(p) for p in candidates if p.exists()]
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
    # Следы CLI и права: настоящий переход на UID CLI и уборка его файлов после ответа.
    from app.model.executor_server import cli_launcher, handle_request
    from app.model.runner import default_spawn

    launcher = cli_launcher(os.environ.get("EXECUTOR_CLI_USER", "exec-cli"))
    script = (
        "echo log > cli.log; mkdir d; echo x > d/f; chmod 700 d; "
        'echo "{\\"is_error\\":false,\\"result\\":\\"uid=$(id -u)\\"}"'
    )

    def spawn(argv, **kw):
        if argv[len(launcher)] == "find":  # уборка - как есть, под тем же пользователем
            return default_spawn(argv, **kw)
        return default_spawn([*launcher, "sh", "-c", script], **kw)

    payload = {
        "channel": channel,
        "model": "m",
        "system": "s",
        "messages": [{"role": "user", "content": "q"}],
    }
    out = handle_request(
        payload,
        env=dict(os.environ),
        spawn=spawn,
        workroot="/work",
        only_channel=channel,
        launcher=launcher,
    )
    check("CLI запущен под другим UID", out.get("text") == "uid=10002", str(out.get("text")))
    check("/work пуст после ответа", not any(work.iterdir()))
    other = next(c for c in TOKENS if c != channel)
    out = handle_request(
        dict(payload, channel=other),
        env=dict(os.environ),
        spawn=spawn,
        workroot="/work",
        only_channel=channel,
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
