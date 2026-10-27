"""Имя загруженного файла и подпись источника: имя - данные пользователя, на диск не попадает."""

import unicodedata

MAX_NAME = 120
TEXTBOOK_LABEL = "Учебник"


def clean_original_name(filename: str | None) -> str | None:
    if not filename:
        return None
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    base = unicodedata.normalize("NFC", base)
    base = "".join(
        ch for ch in base if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Zl", "Zp")
    )
    base = base.strip()[:MAX_NAME].strip()
    return base or None


def material_label(material, page: int) -> str:
    """«<имя без .pdf>, стр. N»; без имени - «Учебник, стр. N» (хеш не показываем)."""
    name = material["original_name"] if material is not None else None
    if name and name.lower().endswith(".pdf"):
        name = name[:-4].strip()
    return f"{name or TEXTBOOK_LABEL}, стр. {page}"
