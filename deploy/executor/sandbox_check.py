"""Проверка песочницы ИЗНУТРИ контейнера-исполнителя (AC-5, SPEC-1.0 1.4).

Запускается шагом CI executor-sandbox в контейнере с корнем только для чтения и tmpfs в /work.
Только стандартная библиотека. Код возврата 0 - все проверки прошли.
"""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, "/opt/executor")

failures: list[str] = []


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


def main() -> int:
    # 1. Запись вне временного каталога падает.
    for directory in ("/", "/opt/executor", "/usr/local", "/etc", "/home/node", "/var/tmp"):
        if os.path.isdir(directory):
            check(f"запись в {directory} закрыта", cannot_write(directory))
    # 2. Временный каталог - tmpfs, пуст на входе.
    work = Path("/work")
    check("/work существует и пуст", work.is_dir() and not any(work.iterdir()))
    check(
        "/work - tmpfs",
        any(
            line.split()[1] == "/work" and line.split()[2] == "tmpfs"
            for line in Path("/proc/mounts").read_text().splitlines()
        ),
    )
    # 3. Тома с данными нет.
    for data in ("/srv/olymp", "/data", "/var/lib/olymp", "/app/data"):
        check(f"нет тома {data}", not os.path.exists(data))
    mounts = [line.split()[1] for line in Path("/proc/mounts").read_text().splitlines()]
    bad = [m for m in mounts if m.startswith(("/srv", "/data", "/mnt", "/Users", "/host"))]
    check("нет монтирований данных", not bad, ",".join(bad))
    # 4. Пользовательских настроек и MCP-конфигов CLI нет.
    home = Path(os.environ.get("HOME", "/home/node"))
    found = []
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
    ]
    for path in candidates:
        if path.exists():
            found.append(str(path))
    check("нет настроек CLI и MCP-конфигов", not found, ",".join(found))
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "CURSOR_API_KEY", "ANTHROPIC_API_KEY"):
        check(
            f"{var} не вшит в образ",
            not os.environ.get(var) or os.environ.get("SANDBOX_ALLOW_TOKENS"),
        )
    # 5. Временный каталог пуст после ответа, следы CLI уходят вместе с ним.
    from app.model.executor_server import handle_request

    def noisy_cli(argv, *, input, env, cwd, timeout):
        Path(cwd, "cli.log").write_text("log")
        Path(env["HOME"], ".claude").mkdir()
        return SimpleNamespace(returncode=0, stderr="", stdout='{"is_error":false,"result":"ok"}')

    payload = {
        "channel": "claude",
        "model": "m",
        "system": "s",
        "messages": [{"role": "user", "content": "q"}],
    }
    out = handle_request(payload, env={"PATH": "/usr/bin"}, spawn=noisy_cli, workroot="/work")
    check("запрос обработан", out.get("text") == "ok", str(out.get("error_kind")))
    check("/work пуст после ответа", not any(work.iterdir()))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
