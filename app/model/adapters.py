"""Адаптеры CLI: сборка команды, окружения и разбор JSON-ответа (SPEC-1.0 1.4)."""

import html
import json
import re
from dataclasses import dataclass

from app.model.types import ChannelError

TOKEN_VARS = {"claude": "CLAUDE_CODE_OAUTH_TOKEN", "cursor": "CURSOR_API_KEY"}
# Служебные переменные, больше ничего из окружения контейнера процессу CLI не передаётся.
SERVICE_VARS = ("PATH", "LANG", "LC_ALL", "TZ")
# На k8 исполнитель выходит наружу только через прокси egress (HI-1): CLI получает его адрес.
# Секретов в этих переменных нет; правило «ничего сверх списка» то же.
_PROXY_NAMES = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")  # Node и curl читают оба регистра
PROXY_VARS = (*_PROXY_NAMES, *(n.lower() for n in _PROXY_NAMES), "NODE_USE_ENV_PROXY")
# Прокси sbx подменяет TLS своим CA: CLI нужны пути к нему (файлы, не секреты).
CA_VARS = ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]=,+-]*")


def build_env(channel: str, source: dict, home: str) -> dict:
    """Окружение процесса CLI: служебные переменные, прокси и токен ТОЛЬКО своего канала."""
    env = {k: source[k] for k in (*SERVICE_VARS, *PROXY_VARS, *CA_VARS) if k in source}
    env["HOME"] = home
    # Корень контейнера только для чтения: временные файлы CLI - в записываемом каталоге запуска.
    env["TMPDIR"] = home
    token = TOKEN_VARS[channel]
    if source.get(token):
        env[token] = source[token]
    return env


def _check_model(model: str) -> str:
    # Имя модели попадает в argv: оно не должно стать флагом.
    if not isinstance(model, str) or not _MODEL.fullmatch(model):
        raise ChannelError("bad_model")
    return model


def render_conversation(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        role = m["role"]
        if role not in ("user", "assistant"):
            raise ChannelError("bad_request")
        parts.append(
            f'<message role="{role}">\n{html.escape(str(m["content"]), quote=False)}\n</message>'
        )
    return "<conversation>\n" + "\n".join(parts) + "\n</conversation>"


def _schema_note(schema: dict | None) -> str:
    if schema is None:
        return ""
    return (
        "\n\nОтветь только одним JSON-объектом по этой схеме, без пояснений и без разметки:\n"
        + json.dumps(schema, ensure_ascii=False)
    )


@dataclass
class Parsed:
    text: str | None
    json: object
    usage: dict | None


def _unfence(text: str) -> str:
    text = text.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    return match.group(1) if match else text


def parse_cli_json(stdout: str, schema: dict | None) -> Parsed:
    """Общий разбор: ошибка CLI, пустой или битый JSON - ChannelError, а не ответ."""
    if not stdout or not stdout.strip():
        raise ChannelError("empty_output")
    try:
        obj = json.loads(stdout)
    except ValueError:
        raise ChannelError("bad_json") from None
    if not isinstance(obj, dict):
        raise ChannelError("bad_json")
    if obj.get("is_error"):
        raise ChannelError("cli_error")
    result = obj.get("result")
    if not isinstance(result, str) or not result.strip():
        raise ChannelError("empty_result")
    usage = None
    raw = obj.get("usage")
    if isinstance(raw, dict) and all(
        isinstance(raw.get(k), int) for k in ("input_tokens", "output_tokens")
    ):
        usage = {"input_tokens": raw["input_tokens"], "output_tokens": raw["output_tokens"]}
    if schema is None:
        return Parsed(result, None, usage)
    try:
        return Parsed(None, json.loads(_unfence(result)), usage)
    except ValueError:
        raise ChannelError("bad_schema_json") from None


class ClaudeAdapter:
    channel = "claude"

    def build(self, model: str, system: str, schema: dict | None) -> list[str]:
        return [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--model",
            _check_model(model),
            "--tools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--setting-sources",
            "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--system-prompt",
            system + _schema_note(schema),
        ]

    def stdin(self, system: str, messages: list[dict], schema: dict | None) -> str:
        return render_conversation(messages)

    parse = staticmethod(parse_cli_json)


class CursorAdapter:
    channel = "cursor"

    def build(self, model: str, system: str, schema: dict | None) -> list[str]:
        # Режим ask - только чтение; запрет shell и web флагами не подтверждён (см. отчёт OH-T03),
        # поэтому опора на изоляцию контейнера. --trust (решение владельца 25.10) доверяет только
        # пустому каталогу запуска и shell не включает: в -p без него CLI отказывается работать.
        # --yolo, -f и --force запрещены.
        return [
            "cursor-agent",
            "-p",
            "--output-format",
            "json",
            "--model",
            _check_model(model),
            "--mode",
            "ask",
            "--trust",
        ]

    def stdin(self, system: str, messages: list[dict], schema: dict | None) -> str:
        # У cursor-agent нет флага system-промпта: правила идут первым блоком запроса.
        return system + _schema_note(schema) + "\n\n" + render_conversation(messages)

    parse = staticmethod(parse_cli_json)


ADAPTERS = {"claude": ClaudeAdapter(), "cursor": CursorAdapter()}
