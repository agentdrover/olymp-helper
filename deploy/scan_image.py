"""Поиск шаблонов токенов в образе (OH-T04 AC-3).

Использование: python deploy/scan_image.py <docker-save.tar>
Читает файлы всех слоёв и конфигурацию образа (в ней история сборки). Значения находок не печатает:
только шаблон и путь. Код возврата: 0 - чисто, 1 - есть совпадения, 2 - архив не прочитан.
"""

import re
import sys
import tarfile

PATTERNS: dict[str, re.Pattern[bytes]] = {
    "anthropic-key": re.compile(rb"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "openai-style-key": re.compile(rb"\bsk-[A-Za-z0-9]{40,}"),
    "cursor-key": re.compile(rb"\bkey_[0-9a-f]{64}\b"),
    "cursor-crsr": re.compile(rb"\bcrsr_[A-Za-z0-9]{20,}"),
    "github-token": re.compile(rb"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}"),
    "github-pat": re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{22,}"),
    "aws-access-key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    # Значение любого ключа *_API_KEY / *_TOKEN (имя заглавными), в форме VAR=value, VAR: value
    # и JSON {"VAR": "value"}; значение от 16 знаков.
    "secret-assignment": re.compile(
        rb"[A-Z0-9_]*(?:API_KEY|TOKEN)[A-Z0-9_]*[\"']?[ \t]*[=:][ \t]*[\"']?[A-Za-z0-9_\-]{16,}"
    ),
}
CHUNK = 8 * 1024 * 1024
OVERLAP = 512


def scan_stream(fh, label: str, findings: list[tuple[str, str]]) -> None:
    tail = b""
    seen: set[str] = set()
    while chunk := fh.read(CHUNK):
        data = tail + chunk
        for name, rx in PATTERNS.items():
            if name not in seen and rx.search(data):
                seen.add(name)
                findings.append((name, label))
        tail = data[-OVERLAP:]


def scan_tar(
    tar: tarfile.TarFile, prefix: str, findings: list[tuple[str, str]], depth: int
) -> None:
    for member in tar:
        if not member.isfile():
            continue
        fh = tar.extractfile(member)
        if fh is None:
            continue
        label = f"{prefix}{member.name}"
        head = fh.read(512)
        fh.seek(0)
        # Слой образа - вложенный tar; признак "ustar" в заголовке.
        if depth == 0 and (head[257:262] == b"ustar" or head[:2] == b"\x1f\x8b"):
            with tarfile.open(fileobj=fh, mode="r:*") as inner:
                scan_tar(inner, f"{label}:", findings, depth + 1)
        else:
            scan_stream(fh, label, findings)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: scan_image.py <docker-save.tar>", file=sys.stderr)
        return 2
    findings: list[tuple[str, str]] = []
    try:
        with tarfile.open(argv[1], mode="r:*") as tar:
            scan_tar(tar, "", findings, 0)
    except (OSError, tarfile.TarError) as exc:
        print(f"архив не прочитан: {exc}", file=sys.stderr)
        return 2
    for name, where in findings:
        print(f"НАЙДЕН ШАБЛОН {name}: {where}")
    if findings:
        return 1
    print("шаблонов токенов в образе нет")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
