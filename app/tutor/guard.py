"""Защита вывода модели ребёнку (SPEC-1.0 1.2, 1.3 правило 3, 1.5).

1. Разметка: всё, что похоже на HTML, вырезается, остальное экранируется; из Markdown
   разрешены только абзацы, списки, **жирный**, *курсив* и `код`. Ссылки и картинки - в текст.
2. Верный вариант (до открытия решения): очевидное режет дешёвый фильтр `prefilter_reason`
   (обозначение в контексте обозначения, число эталона, точный текст верного варианта);
   синонимы, порядок и исключение ловит независимый судья (flow.py). Без судьи текст модели
   до решения не показывается вовсе.
3. teacher_note: ни его фразы, ни пары «обозначение + слово» ребёнку не показываются никогда.
4. Без ключа в решении и «объясни иначе» предложения с «верно/правильно/правильный» вырезаются:
   ответ модели не проверен, утверждать его верность нельзя.
"""

import html
import json
import re
import unicodedata

from app.ingest.answers import normalize_answer

GENERIC = {
    "hint1": "Перечитай условие. О чём именно спрашивают и что в нём главное?",
    "hint2": "Вспомни, что ты знаешь по этой теме, и проверь каждый вариант: "
    "что в нём подходит к условию, а что нет и почему.",
    "mistake": "Посмотри ещё раз, почему этот вариант мог показаться подходящим, "
    "и сверь его с условием по словам.",
    "explain": "Объяснение сейчас недоступно: вернись к условию и разбору выше.",
}

_BLOCK = re.compile(
    r"<\s*(script|style|iframe|object|embed|noscript)\b.*?<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL
)
_TAG = re.compile(r"</?[A-Za-z!?][^>]*>?")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])", re.DOTALL)
_CODE = re.compile(r"`([^`\n]+)`")
_LIST = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*)$")


def strip_markup(text: str) -> str:
    text = _BLOCK.sub("", text or "")
    text = _TAG.sub("", text)
    text = _IMAGE.sub(lambda m: m.group(1), text)
    return _LINK.sub(lambda m: m.group(1), text)


def _inline(line: str) -> str:
    line = html.escape(line, quote=True)
    line = _CODE.sub(r"<code>\1</code>", line)
    line = _BOLD.sub(r"<strong>\1</strong>", line)
    return _ITALIC.sub(r"<em>\1</em>", line)


def render_markdown(text: str) -> str:
    """Безопасный HTML из Markdown модели."""
    blocks, para, items = [], [], []

    def flush():
        if para:
            blocks.append("<p>" + "<br>".join(_inline(p) for p in para) + "</p>")
            para.clear()
        if items:
            blocks.append("<ul>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + "</ul>")
            items.clear()

    for line in strip_markup(text).splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        match = _LIST.match(line)
        if match:
            if para:
                flush()
            items.append(match.group(1))
        else:
            if items:
                flush()
            para.append(stripped)
    flush()
    return "".join(blocks)


def plain(text: str) -> str:
    """Текст без разметки - для проверки утечек."""
    text = strip_markup(text)
    return re.sub(r"[*_`#>]+", "", text)


# ----------------------------------------------------------------------- утечки

_TOKENS = re.compile(r"[0-9a-zа-я]+")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")

_UNITS = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
          "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]  # fmt: skip
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят",
         "восемьдесят", "девяносто"]  # fmt: skip
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот",
             "восемьсот", "девятьсот"]  # fmt: skip


def number_words(n: int) -> list[str]:
    """Число 0..1000 словами (именительный падеж), по словам."""
    if n == 1000:
        return ["тысяча"]
    out = []
    if n >= 100:
        out.append(_HUNDREDS[n // 100])
        n %= 100
    if n >= 20:
        out.append(_TENS[n // 10])
        n %= 10
        if n:
            out.append(_UNITS[n])
    elif n >= 10:
        out.append(_TEENS[n - 10])
    elif n or not out:
        out.append(_UNITS[n])
    return out


_DINGBAT_DIGITS = {
    **{chr(0x2776 + i): str(i + 1) for i in range(10)},  # ❶-❿
    **{chr(0x2780 + i): str(i + 1) for i in range(10)},  # ➀-➉
    **{chr(0x278A + i): str(i + 1) for i in range(10)},  # ➊-➓
}
_KEYCAP = re.compile("([0-9])[\ufe0f]?\u20e3")


def fold_numerals(text: str) -> str:
    """①, ⑤, ❷, 5️⃣ и цифры полной ширины -> обычные цифры (ребёнок видит те же обозначения)."""
    text = _KEYCAP.sub(r" \1 ", text or "")
    text = "".join(f" {_DINGBAT_DIGITS[c]} " if c in _DINGBAT_DIGITS else c for c in text)
    return unicodedata.normalize("NFKC", text)


def tokens_of(text: str) -> list[str]:
    return _TOKENS.findall(fold_numerals(text).lower().replace("ё", "е"))


def _stem(word: str) -> str:
    return word[:5]


def _is_word(tok: str) -> bool:
    return len(tok) >= 4 and tok.isalpha()


def _label(raw: str) -> str | None:
    return normalize_answer(raw, "single")


def _options_flat(options) -> list[dict]:
    if isinstance(options, list):
        return options
    if isinstance(options, dict):
        return [o for side in options.values() for o in side]
    return []


def _flat(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return [str(v) for v in value.values()] + [str(k) for k in value]
    return []


def _same_word(tok: str, word: str) -> bool:
    return tok.startswith(word[: max(3, len(word) - 3)])


def _number_words_in(toks: list[str], words: list[str]) -> bool:
    """Все слова числа по порядку, с промежутком не более одного слова."""
    for start, tok in enumerate(toks):
        if not _same_word(tok, words[0]):
            continue
        at, ok = start, True
        for word in words[1:]:
            nxt = next(
                (i for i in range(at + 1, min(len(toks), at + 3)) if _same_word(toks[i], word)),
                None,
            )
            if nxt is None:
                ok = False
                break
            at = nxt
        if ok:
            return True
    return False


def note_segments(note: str | None) -> list[str]:
    parts = (" ".join(tokens_of(p)) for p in re.split(r"[.;,\n]+", note or ""))
    return [p for p in parts if len(p) >= 5]


def note_pairs(note: str | None) -> list[tuple[str, str]]:
    """Пары (обозначение, значимое слово) из каждой фразы пояснения: «3 - лепесток»."""
    pairs = []
    for part in re.split(r"[.;,\n]+", note or ""):
        toks = tokens_of(part)
        labels = [t for t in toks if (t.isdigit() and len(t) <= 2) or (len(t) == 1 and t.isalpha())]
        words = [_stem(t) for t in toks if _is_word(t)]
        pairs.extend((lab, w) for lab in labels for w in words)
    return pairs


def note_reason(text: str, note: str | None) -> str | None:
    if not note:
        return None
    toks = tokens_of(text)
    joined = " ".join(toks)
    if any(seg in joined for seg in note_segments(note)):
        return "note"
    have = set(toks)
    stems = {_stem(t) for t in toks if _is_word(t)}
    for lab, word in note_pairs(note):
        if lab in have and word in stems:
            return "note_pair"
    return None


def leaks_note(text: str, note: str | None) -> bool:
    return note_reason(text, note) is not None


_TOKEN_SPAN = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")
# Слова, которые в речи чаще всего предлоги и союзы: строчными вне контекста они не обозначения.
_FUNCTION_LETTERS = frozenset("авиоксуяэ")
_KEYWORDS = ("вариант", "ответ", "пункт", "букв", "номер")
_LABEL_LIST = re.compile(
    r"(?<![\w])([A-Za-zА-Яа-яЁё])(?=(?:\s*,\s*|\s+и\s+|\s+или\s+)([A-Za-zА-Яа-яЁё])(?![\w]))"
)


def _sentence_start(text: str, pos: int) -> bool:
    i = pos - 1
    while i >= 0 and text[i] in ' \t«"“(':
        i -= 1
    return i < 0 or text[i] in ".!?…\n"


_TRANSLIT = {"a": "А", "b": "Б", "v": "В", "g": "Г", "d": "Д", "e": "Е"}


def _candidates(raw: str, all_labels: set[str]) -> set[str]:
    """Какие обозначения может значить слово: для латинской буквы - и похожая кириллическая
    (B -> В), и звуковая транслитерация (b -> Б)."""
    cands = {_label(raw)}
    if raw.isascii() and raw.isalpha():
        cands.add(_TRANSLIT.get(raw.lower()))
    return {c for c in cands if c and c in all_labels}


def label_positions(text: str, all_labels: set[str]) -> list[tuple[int, frozenset]]:
    """(номер слова, возможные обозначения) для слов, употреблённых КАК обозначение варианта.

    Цифры - всегда обозначения; латинская буква - тоже (кириллических вариантов она не бывает
    в обычной речи). Кириллическая одиночная буква - только в контексте обозначения: заглавная не
    в начале предложения; в скобках или кавычках; перед тире или двоеточием; после слов
    «вариант», «ответ», «пункт», «буква», «номер» (не дальше двух слов); в перечне «а, б и в»;
    строчная, не бывающая предлогом или союзом (б, г, д). Строчные «в», «а», «и», «с» в обычной
    речи - предлоги и союзы, их это не задевает."""
    text = fold_numerals(text)
    spans = [(m.start(), m.end(), m.group(0)) for m in _TOKEN_SPAN.finditer(text)]
    low = [raw.lower().replace("ё", "е") for _, _, raw in spans]
    found: dict[int, set[str]] = {}
    for i, (start, end, raw) in enumerate(spans):
        if len(raw) > 2:
            continue
        cands = _candidates(raw, all_labels)
        if not cands:
            continue
        if raw.isdigit() or raw.isascii():
            found.setdefault(i, set()).update(cands)
            continue
        if len(raw) != 1:
            continue
        before, after = text[max(0, start - 1) : start], text[end : end + 3]
        if (
            (raw.isupper() and not _sentence_start(text, start))
            or (raw.islower() and raw.replace("ё", "е") not in _FUNCTION_LETTERS)
            or before in '([«"“'
            and before
            or re.match(r"[)\]»\"”]", after)
            or re.match(r"\s*[—–:]|\s+-\s|\.\s*[-—–:]", after)
            or any(low[k].startswith(_KEYWORDS) for k in range(max(0, i - 2), i))
        ):
            found.setdefault(i, set()).update(cands)
    # перечень «а, б и в»: одиночные буквы-обозначения через запятую, «и», «или»
    index = {start: i for i, (start, _, _) in enumerate(spans)}
    for m in _LABEL_LIST.finditer(text):
        pair = [(index.get(m.start(g)), _candidates(m.group(g), all_labels)) for g in (1, 2)]
        if all(i is not None and cands for i, cands in pair):
            for i, cands in pair:
                found.setdefault(i, set()).update(cands)
    return sorted((i, frozenset(c)) for i, c in found.items())


def prefilter_reason(
    text: str, task_type: str, options, reference, condition: str = "", own=()
) -> str | None:
    """Дешёвая проверка только очевидного: обозначение верного варианта в контексте обозначения
    (с цифрами, ②, 5️⃣ и транслитом), число эталона, точный текст верного варианта (все его
    значимые слова вместе). Всё остальное - синонимы, порядок, исключение - ловит судья."""
    text = fold_numerals(text)
    toks = tokens_of(text)
    if not toks:
        return None
    items = _options_flat(options)
    all_labels = {x for o in items if (x := _label(o["label"]))}
    correct = {x for v in _flat(reference) if (x := _label(v))} if task_type != "numeric" else set()
    if any(labs & correct for _, labs in label_positions(text, all_labels)):
        return "label"
    if task_type == "numeric" and reference is not None:
        want = normalize_answer(reference, "numeric")
        if any(normalize_answer(n, "numeric") == want for n in _NUMBER.findall(text)):
            return "number"
        spelled = want and re.fullmatch(r"\d+", want) and int(want) <= 1000
        if spelled and _number_words_in(toks, number_words(int(want))):
            return "number"
    cond = {_stem(t) for t in tokens_of(condition) if _is_word(t)}
    have = {_stem(t) for t in toks if _is_word(t)}
    joined = " ".join(toks)
    for o in items:
        if _label(o["label"]) not in correct:
            continue
        words = {_stem(t) for t in tokens_of(o["text"]) if _is_word(t)}
        if words:
            if not words <= cond and words <= have:
                return "option_text"
        elif len(" ".join(tokens_of(o["text"]))) >= 3 and " ".join(tokens_of(o["text"])) in joined:
            return "option_text"
    return None


_CLAIM = re.compile(r"(?<![\w])(верн\w*|правильн\w*)", re.IGNORECASE)


def strip_claims(text: str) -> str:
    """Предложения с «верно/правильно/правильный» вырезаются (ответ модели не проверен)."""
    parts = re.split(r"(?<=[.!?])\s+|\n+", plain(text))
    return "\n".join(p for p in parts if p.strip() and not _CLAIM.search(p))


def finish(
    text: str,
    kind: str,
    task_type: str,
    options,
    reference,
    note: str | None,
    *,
    reveal: bool = False,
    keyed: bool = True,
    condition: str = "",
    own=(),
) -> tuple[str, str | None]:
    """(безопасный HTML, причина отклонения или None). reveal=True - решение уже открыто:
    ответ называть можно, пояснение к рисунку - нет; без ключа утверждения о верности вырезаются."""
    body = plain(text)
    reason = note_reason(body, note)
    if reason is None and not reveal:
        reason = prefilter_reason(body, task_type, options, reference, condition, own)
    if reason is None and reveal and not keyed:
        text = strip_claims(text)
        if not text.strip():
            reason = "claims"
    if reason or not body.strip():
        return render_markdown(GENERIC.get(kind, GENERIC["explain"])), reason or "empty"
    return render_markdown(text), None


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)
