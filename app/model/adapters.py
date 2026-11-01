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
    token = TOKEN_VARS.get(channel)
    if token and source.get(token):
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
_TAG_LINE = re.compile(r"[ \t]*[\w+.-]*[ \t]*")
_TAG_BEFORE_OBJECT = re.compile(r"[ \t]*[\w+.-]+[ \t]*(?=\{)")
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


def _no_duplicate_keys(pairs):
    """object_pairs_hook строгого режима: повтор ключа (после декодирования escape) - ошибка,
    на любой глубине; иначе json.loads молча берёт последнее значение (fail closed)."""
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _strict_json(text: str):
    """Строгий режим (судья): вступление без скобок { } [ ], затем ровно один объект или один
    блок кода с любым тегом, затем только пробелы. Пример-объект во вступлении невозможен:
    скобка во вступлении - ошибка."""
    text = text.strip()
    cut = min((i for i in (text.find(c) for c in "{}[]") if i != -1), default=len(text))
    fence = text.find("```")
    start = fence if 0 <= fence < cut else cut
    rest = text[start:]
    candidates = [rest]
    fenced = re.fullmatch(r"```(.*)```", rest, re.DOTALL)
    if fenced:
        body = fenced.group(1).strip()
        candidates = [body]
        first, _, tail = fenced.group(1).partition("\n")
        if tail and _TAG_LINE.fullmatch(first):  # первая строка - тег, только если она тег
            candidates.append(tail.strip())
        one_line = _TAG_BEFORE_OBJECT.match(fenced.group(1))  # тег в одной строке с объектом
        if one_line:
            candidates.append(fenced.group(1)[one_line.end() :].strip())
    for cand in candidates:
        try:
            obj = json.loads(cand, object_pairs_hook=_no_duplicate_keys)
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


def parse_codex_jsonl(stdout: str, schema: dict | None) -> Parsed:
    """Accept only the final agent message of a successful, tool-free Codex turn."""
    text = None
    complete = False
    usage = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            raise ChannelError("bad_json") from None
        if not isinstance(event, dict):
            raise ChannelError("bad_json")
        kind = event.get("type")
        if kind in ("turn.failed", "error"):
            raise ChannelError("cli_error")
        if kind == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                raise ChannelError("bad_json")
            if item.get("type") == "agent_message":
                text = item.get("text")
            elif item.get("type") in ("command_execution", "file_change", "mcp_tool_call", "web_search"):
                raise ChannelError("unexpected_tool")
        if kind == "turn.completed":
            complete = True
            usage = event.get("usage")
    if not complete or not isinstance(text, str) or not text.strip():
        raise ChannelError("empty_result")
    parsed = parse_cli_json(json.dumps({"result": text, "usage": usage}), schema)
    return Parsed(parsed.text, restore_optional_fields(parsed.json, schema), parsed.usage)


def codex_error_kind(stdout: str, stderr: str) -> str:
    """Classify process errors without returning stderr or model-authored text."""
    messages = [stderr]
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") not in ("error", "turn.failed"):
            continue
        error = event.get("error")
        message = error.get("message", "") if isinstance(error, dict) else event.get("message", "")
        if isinstance(message, str):
            messages.append(message)
    text = "\n".join(messages).lower()
    if re.search(r"usage limit|quota exceeded|subscription.*limit", text):
        return "provider_limit"
    if re.search(r"rate[ -]?limit|too many requests|\b429\b", text):
        return "provider_rate_limit"
    if re.search(r"not logged in|not authenticated|\b401\b|unauthorized", text):
        return "codex_auth_unavailable"
    return "exit_code"


class CodexAdapter:
    """Codex CLI contract; caller creates schema.json and PNG files in the run directory."""

    channel = "codex"

    def build(self, model: str, system: str, schema: dict | None, images=()) -> list[str]:
        return [
            # sbx's trusted config supplies its host-side OAuth proxy; discarding it breaks login.
            # The caller must supply an isolated HOME with a trusted config, never a prior run's.
            "codex", "exec", "--skip-git-repo-check", "--ephemeral",
            "--sandbox", "read-only", "-c", "approval_policy=never", "-c", "features.shell_tool=false",
            "-c", "web_search=disabled", "--json", "-m", _check_model(model),
            *(["--output-schema", "schema.json"] if schema is not None else []),
            "-", *(part for name in image_names(len(images)) for part in ("-i", name)),
        ]

    def stdin(self, system: str, messages: list[dict], schema: dict | None, images=()) -> str:
        return system + (UNTRUSTED_IMAGE_RULE if images else "") + "\n\n" + render_conversation(messages)

    parse = staticmethod(parse_codex_jsonl)


def codex_schema(schema: dict) -> dict:
    """Prepare the supported schema subset for OpenAI structured output.

    Optional properties become nullable; the caller must remove those nulls after parsing.
    Unknown unrestricted shapes are rejected rather than silently narrowing the contract.
    """
    if not isinstance(schema, dict) or not schema:
        raise ChannelError("unsupported_schema")
    result = {k: v for k, v in schema.items() if k != STRICT_KEY}
    if "enum" in result and "type" not in result:
        vals = result["enum"]
        if vals and all(isinstance(v, str) for v in vals):
            result["type"] = "string"
        else:
            raise ChannelError("unsupported_schema")
    if "anyOf" in result:
        result["anyOf"] = [codex_schema(item) for item in result["anyOf"]]
    if result.get("type") == "array":
        result["items"] = codex_schema(result.get("items"))
    if result.get("type") == "object":
        props = result.get("properties", {})
        required = set(result.get("required", []))
        converted = {}
        for name, definition in props.items():
            child = codex_schema(definition)
            converted[name] = child if name in required else {"anyOf": [child, {"type": "null"}]}
        result["properties"] = converted
        result["required"] = list(converted)
        result["additionalProperties"] = False
    return result


def restore_optional_fields(value, schema):
    """Remove null placeholders introduced for optional object properties."""
    if not isinstance(schema, dict):
        return value
    if isinstance(value, dict) and schema.get("type") == "object":
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        restored = {}
        for key, child in value.items():
            definition = props.get(key, {})
            if child is None and key not in required and not _schema_allows_null(definition):
                continue
            restored[key] = restore_optional_fields(child, definition)
        return restored
    if isinstance(value, list) and schema.get("type") == "array":
        return [restore_optional_fields(item, schema.get("items", {})) for item in value]
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            typ = option.get("type") if isinstance(option, dict) else None
            if (typ == "object" and isinstance(value, dict)) or (
                typ == "array" and isinstance(value, list)
            ):
                return restore_optional_fields(value, option)
    return value


def _schema_allows_null(schema):
    typ = schema.get("type")
    return typ == "null" or (isinstance(typ, list) and "null" in typ) or any(
        _schema_allows_null(item) for item in schema.get("anyOf", []) if isinstance(item, dict)
    ) or ("enum" in schema and None in schema["enum"])


ADAPTERS = {"claude": ClaudeAdapter(), "cursor": CursorAdapter(), "codex": CodexAdapter()}
