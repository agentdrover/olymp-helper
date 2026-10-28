"""Адаптеры CLI: сборка команды, окружения и разбор JSON-ответа (SPEC-1.0 1.4)."""

import base64
import html
import json
import re
from dataclasses import dataclass

from app.model.images import UNTRUSTED_IMAGE_RULE
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
    # Под лаунчером с capabilities (secure-execution) glibc эту переменную выбросит: runner ставит её заново.
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


def image_names(count: int) -> list[str]:
    return [f"image-{i}.png" for i in range(1, count + 1)]


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


_NOTHING = object()
# Блок кода с любым тегом (json, JSON, jsonc, js, text, пробелы): тег не решает, что это не данные.
_FENCE = re.compile(r"```[ \t]*[\w+.-]*[ \t]*\n?(.*?)```", re.DOTALL)
STRICT_KEY = "x-strict-json"  # метка схемы: ответ - ровно один объект, без вступления


def _group_end(text: str, start: int) -> int:
    """Индекс закрывающей скобки группы, открытой в start; -1, если не закрыта."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _extract_json(text: str):
    """JSON из ответа со вступлением (мягкий режим), при любом сомнении - ошибка.
    Считаются все разбираемые объекты: в блоках кода с любым тегом и вне их (вложенные - нет).
    Ошибка: больше одного; блок с '{', который не разбирается; '{' вне блоков, который не
    разбирается и не похож на безобидную группу вроде LaTeX \\frac{1}{2} (без кавычек и
    двоеточий). Внутрь неразобранного объекта не заглядываем."""
    found = []
    for block in _FENCE.findall(text):
        if "{" not in block and "[" not in block:
            continue
        try:
            found.append(json.loads(block.strip()))
        except ValueError:
            raise ChannelError("bad_schema_json") from None
    decoder = json.JSONDecoder()
    rest = _FENCE.sub(" ", text)
    i = 0
    while (i := rest.find("{", i)) != -1:
        try:
            obj, end = decoder.raw_decode(rest, i)
        except ValueError:
            end = _group_end(rest, i)
            if end == -1 or any(ch in rest[i:end] for ch in '":'):
                raise ChannelError("bad_schema_json") from None
            i = end + 1  # безобидная группа не JSON: пропускаем целиком
            continue
        found.append(obj)
        i = end  # вложенные объекты внутри найденного не считаются
    if len(found) > 1:
        raise ChannelError("bad_schema_json")
    return found[0] if found else _NOTHING


def _strict_json(text: str):
    """Строгий режим (судья): весь ответ - один объект либо один блок кода с любым тегом."""
    text = text.strip()
    candidates = [text]
    fenced = re.fullmatch(r"```(.*)```", text, re.DOTALL)
    if fenced:
        body = fenced.group(1).strip()
        candidates = [body]
        if "\n" in body:
            candidates.append(body.split("\n", 1)[1].strip())  # первая строка - тег
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ChannelError("bad_schema_json")


def _result_event(stdout: str):
    """Поток stream-json: по строке на событие; ответ - последнее событие type=result."""
    found = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            found = event
    if found is None:
        raise ChannelError("bad_json")
    return found


def parse_cli_json(stdout: str, schema: dict | None, strict: bool | None = None) -> Parsed:
    """Общий разбор: ошибка CLI, пустой или битый JSON - ChannelError, а не ответ."""
    if not stdout or not stdout.strip():
        raise ChannelError("empty_output")
    try:
        obj = json.loads(stdout)
    except ValueError:
        obj = _result_event(stdout)
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
    if strict is None:
        strict = bool(schema.get(STRICT_KEY))
    if strict:
        return Parsed(None, _strict_json(result), usage)
    try:
        return Parsed(None, json.loads(_unfence(result)), usage)
    except ValueError:
        pass
    found = _extract_json(result)
    if found is _NOTHING:
        raise ChannelError("bad_schema_json")
    return Parsed(None, found, usage)


class ClaudeAdapter:
    channel = "claude"

    def build(self, model: str, system: str, schema: dict | None, images=()) -> list[str]:
        if images:
            system += UNTRUSTED_IMAGE_RULE
        return [
            "claude",
            "-p",
            "--output-format",
            "stream-json" if images else "json",
            # Потоковый ввод требует потокового вывода (и --verbose): ответ - строка type=result.
            *(("--input-format", "stream-json", "--verbose") if images else ()),
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

    def stdin(self, system: str, messages: list[dict], schema: dict | None, images=()) -> str:
        if not images:
            return render_conversation(messages)
        # Claude CLI принимает изображение блоком base64 в сообщении формата stream-json.
        blocks = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(data).decode("ascii"),
                },
            }
            for data in images
        ]
        blocks.append({"type": "text", "text": render_conversation(messages)})
        line = {"type": "user", "message": {"role": "user", "content": blocks}}
        return json.dumps(line, ensure_ascii=False) + "\n"

    parse = staticmethod(parse_cli_json)


class CursorAdapter:
    channel = "cursor"

    def build(self, model: str, system: str, schema: dict | None, images=()) -> list[str]:
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

    def stdin(self, system: str, messages: list[dict], schema: dict | None, images=()) -> str:
        # У cursor-agent нет флага system-промпта: правила идут первым блоком запроса.
        # Изображения у него - файлы в каталоге запуска (см. image_names), модель читает их по пути.
        text = system + (UNTRUSTED_IMAGE_RULE if images else "") + _schema_note(schema)
        if images:
            files = "\n".join(f"./{n}" for n in image_names(len(images)))
            text += "\n\nПрикреплённые изображения (файлы PNG в рабочем каталоге):\n" + files
        return text + "\n\n" + render_conversation(messages)

    parse = staticmethod(parse_cli_json)


ADAPTERS = {"claude": ClaudeAdapter(), "cursor": CursorAdapter()}
