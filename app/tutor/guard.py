"""Защита вывода модели ребёнку (SPEC-1.0 1.2, 1.3 правило 3, 1.5).

1. Разметка: всё, что похоже на HTML, вырезается, остальное экранируется; из Markdown
   разрешены только абзацы, списки, **жирный**, *курсив* и `код`. Ссылки и картинки - в текст.
2. Верный вариант (до открытия решения): текст модели ОТКЛОНЯЕТСЯ и заменяется общей подсказкой,
   если он содержит ЛЮБОЙ признак ответа: обозначение верного варианта как отдельное слово,
   значимое слово его текста (которого нет в условии), число эталона цифрами или словами,
   слова «верн*/правильн*/ответ*» рядом с обозначением или словом варианта. Это не список
   формулировок, а закрытие по составу слов: проходит только текст без таких признаков.
3. teacher_note: ни его фразы, ни пары «обозначение + слово» ребёнку не показываются никогда.
4. Без ключа в решении и «объясни иначе» предложения с «верно/правильно/правильный» вырезаются:
   ответ модели не проверен, утверждать его верность нельзя.
"""

import html
import json
import re

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
CUES = ("верн", "правильн", "ответ")
CUE_RANGE = 3

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


def tokens_of(text: str) -> list[str]:
    return _TOKENS.findall((text or "").lower().replace("ё", "е"))


def _stem(word: str) -> str:
    return word[:5]


def _is_word(tok: str) -> bool:
    return len(tok) >= 4 and tok.isalpha()


def _label(raw: str) -> str | None:
    return normalize_answer(raw, "single")


def _label_of_token(tok: str) -> str | None:
    return _label(tok) if len(tok) <= 2 else None


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


def answer_reason(
    text: str, task_type: str, options, reference, condition: str = "", own=()
) -> str | None:
    """Почему текст нельзя показывать до открытия решения; None - признаков ответа нет."""
    toks = tokens_of(text)
    if not toks:
        return None
    items = _options_flat(options)
    all_labels = {x for o in items if (x := _label(o["label"]))}
    own = set(own)
    correct = {x for v in _flat(reference) if (x := _label(v))} if task_type != "numeric" else set()
    # 1. обозначение верного варианта как отдельное слово, в любом регистре
    if any(lab in correct for lab in (_label_of_token(t) for t in toks) if lab):
        return "label"
    # 2. значимые слова текста верного варианта: их нет в условии и не у всех вариантов
    cond = {_stem(t) for t in tokens_of(condition) if _is_word(t)}
    per_option = [{_stem(t) for t in tokens_of(o["text"]) if _is_word(t)} for o in items]
    common = set.intersection(*per_option) if per_option else set()
    banned: set[str] = set()
    for o in items:
        if _label(o["label"]) in correct:
            banned |= {_stem(t) for t in tokens_of(o["text"]) if _is_word(t)}
    banned -= cond | common
    if banned & {_stem(t) for t in toks if _is_word(t)}:
        return "option_word"
    # 3. число эталона цифрами и словами (и числовое обозначение варианта словами)
    wanted = []
    if task_type == "numeric" and reference is not None:
        want = normalize_answer(reference, "numeric")
        found = _NUMBER.findall(text.replace("ё", "е"))
        if any(normalize_answer(n, "numeric") == want for n in found):
            return "number"
        wanted = [want]
    wanted += [lab for lab in correct if lab.isdigit()]
    for want in wanted:
        spelled = want and re.fullmatch(r"\d+", want) and int(want) <= 1000
        if spelled and _number_words_in(toks, number_words(int(want))):
            return "number"
    # 4. «верн*/правильн*/ответ*» рядом с обозначением или словом любого варианта
    option_words = {_stem(t) for o in items for t in tokens_of(o["text"]) if _is_word(t)}
    for i, tok in enumerate(toks):
        if not tok.startswith(CUES) or (i and toks[i - 1] == "не"):
            continue
        for j in range(max(0, i - CUE_RANGE), min(len(toks), i + CUE_RANGE + 1)):
            if j == i:
                continue
            lab = _label_of_token(toks[j])
            if lab and lab in all_labels and lab not in own:
                return "cue"
            if _is_word(toks[j]) and _stem(toks[j]) in option_words:
                return "cue"
    return None


def names_answer(text: str, task_type: str, options, reference, condition: str = "") -> bool:
    return answer_reason(plain(text), task_type, options, reference, condition) is not None


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
        reason = answer_reason(body, task_type, options, reference, condition, own)
    if reason is None and reveal and not keyed:
        text = strip_claims(text)
        if not text.strip():
            reason = "claims"
    if reason or not body.strip():
        return render_markdown(GENERIC.get(kind, GENERIC["explain"])), reason or "empty"
    return render_markdown(text), None


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)
