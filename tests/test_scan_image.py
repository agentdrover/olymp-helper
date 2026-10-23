"""OH-T04 AC-3: поиск шаблонов токенов в файлах и истории образа (на синтетическом docker save)."""

import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN = ROOT / "deploy" / "scan_image.py"
FAKE_ANTHROPIC = "sk-ant-" + "oat01-" + "A" * 40
FAKE_GITHUB = "ghp_" + "b" * 36


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _image(path: Path, *, file_content: bytes = b"hello", config: dict | None = None) -> Path:
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode="w") as lt:
        _add(lt, "app/data.txt", file_content)
    cfg = json.dumps(config or {"history": [{"created_by": "RUN echo ok"}]}).encode()
    with tarfile.open(path, "w") as t:
        _add(t, "blobs/sha256/aaa", layer.getvalue())
        _add(t, "blobs/sha256/bbb", cfg)
        _add(t, "manifest.json", b"[]")
    return path


def _scan(path: Path):
    return subprocess.run(
        [sys.executable, str(SCAN), str(path)], check=False, capture_output=True, text=True
    )


def test_clean_image_passes(tmp_path):
    res = _scan(_image(tmp_path / "i.tar"))
    assert res.returncode == 0, res.stdout + res.stderr


def test_token_in_layer_file_fails_and_is_not_echoed(tmp_path):
    res = _scan(_image(tmp_path / "i.tar", file_content=b"x " + FAKE_ANTHROPIC.encode()))
    assert res.returncode == 1
    assert "app/data.txt" in res.stdout
    assert FAKE_ANTHROPIC not in res.stdout + res.stderr


def test_token_in_image_history_fails(tmp_path):
    cfg = {"history": [{"created_by": "ENV CURSOR_API_KEY=abcdef0123456789abcdef"}]}
    res = _scan(_image(tmp_path / "i.tar", config=cfg))
    assert res.returncode == 1
    assert "abcdef0123456789abcdef" not in res.stdout + res.stderr


def test_github_token_in_layer_fails(tmp_path):
    res = _scan(_image(tmp_path / "i.tar", file_content=FAKE_GITHUB.encode()))
    assert res.returncode == 1


def test_secret_variable_name_without_value_is_fine(tmp_path):
    res = _scan(_image(tmp_path / "i.tar", file_content=b"export CLAUDE_CODE_OAUTH_TOKEN\n"))
    assert res.returncode == 0


def test_unreadable_archive_is_an_error_not_a_pass(tmp_path):
    bad = tmp_path / "bad.tar"
    bad.write_bytes(b"not a tar")
    assert _scan(bad).returncode == 2
    assert _scan(tmp_path / "missing.tar").returncode == 2
