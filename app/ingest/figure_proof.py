"""A figure's machine approval belongs to its precise inputs, never to a parent tickbox."""

import hashlib
import json
import re

from app.ingest.store import figure_of


def text_leak(task, key) -> bool:
    """Ordinary crop labels/numeric inputs may equal a key; explicit answer markers may not."""
    from app.ingest.answers import display
    from app.ingest.leaks import condition_leaks
    from app.tutor.guard import prefilter_reason

    options = json.loads(task["options"]) if task["options"] else None
    rows = (
        options
        if isinstance(options, list)
        else [o for side in (options or {}).values() for o in side]
    )
    pieces = [task["text"], *[o.get("text", "") for o in rows]]
    marked = [
        part
        for text in pieces
        for part in re.split(r"[.!?;\n]+", text)
        if re.search(r"\b(?:ответ\w*|правильн\w*|верн\w*|answer|correct)\b", part, re.IGNORECASE)
    ]
    return key is not None and any(
        condition_leaks(part, display(key), task["type"])
        or prefilter_reason(part, task["type"], options, key)
        for part in marked
    )


def crop_leaks(lines, units):
    """Bare labels and axis values are data; an answer marker makes them protected text."""
    from app.ingest.answers import normalize_answer
    from app.ingest.figures import leaks_in_lines

    def ordinary(value):
        return (
            bool(re.fullmatch(r"[A-ZА-ЯЁ]", value, re.IGNORECASE))
            or normalize_answer(value, "numeric") is not None
        )

    protected = [value for value in units if not ordinary(value)]
    marked = [
        line for line in lines if re.search(r"\b(?:ответ\w*|answer|correct)\b", line, re.IGNORECASE)
    ]
    return list(dict.fromkeys(leaks_in_lines(lines, protected) + leaks_in_lines(marked, units)))


def alt_problem(task, alt, value):
    from app.ingest import figures
    from app.ingest.store import options_of

    labels = [value] if task["type"] == "numeric" and value is not None else []
    problem = figures.alt_problem(alt, options_of(task), labels)
    alt_task = {"type": task["type"], "text": alt, "options": task["options"]}
    return problem or ("подпись к рисунку раскрывает ответ" if text_leak(alt_task, value) else None)


def digest(task, figure=None) -> str:
    figure = figure if figure is not None else figure_of(task) or {}
    content = {
        key: task[key]
        for key in (
            "id",
            "revision",
            "material_id",
            "text",
            "type",
            "options",
            "topic",
            "answer_key",
            "key_source",
            "key_version",
            "answer_text",
        )
    }
    content["figure"] = {key: value for key, value in figure.items() if key != "auto_verification"}
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def verified(task) -> bool:
    figure = figure_of(task)
    proof = (figure or {}).get("auto_verification") or {}
    if not proof:
        return False
    return bool(
        figure
        and proof.get("revision") == task["revision"]
        and proof.get("digest") == digest(task)
        and proof.get("descriptions_verified") is True
        and len(set(proof.get("families") or [])) == 2
        and all(proof.get("families") or [])
    )


def invalidate(figure):
    if figure and figure.get("auto_verification"):
        figure = dict(figure)
        figure.pop("auto_verification", None)
        figure.update(confirmed=False, teacher_note_ack=False, no_answer_ack=False)
    return figure


def trusted_note(task):
    """Unverified machine prefills never become tutor/judge evidence."""
    figure = figure_of(task) or {}
    if figure.get("description_source") == "models" and not (
        figure.get("teacher_note_ack") or verified(task)
    ):
        return None
    return figure.get("teacher_note")
