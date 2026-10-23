"""OH-T04: артефакты контейнера приложения проверяются без Docker (сборку и запуск проверяет шаг CI image-smoke)."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WEIGHTS_SHA256 = "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b"


def _containerfile() -> str:
    return (ROOT / "Containerfile").read_text(encoding="utf-8")


def _stages(text: str) -> list[str]:
    return re.split(r"(?m)^(?=FROM )", text)[1:]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8"))


def test_containerfile_pins_whisper_and_weights_checksum():
    text = _containerfile()
    assert re.search(r"(?m)^ARG WHISPER_CPP_COMMIT=[0-9a-f]{40}$", text)
    assert f"ARG WHISPER_MODEL_SHA256={WEIGHTS_SHA256}" in text
    assert "sha256sum -c" in text
    assert "ggml-small.bin" in text  # multilingual small, не small.en
    assert "small.en" not in text


def test_containerfile_installs_ffmpeg_and_whisper_cli_in_runtime_stage():
    runtime = _stages(_containerfile())[-1]
    assert "ffmpeg" in runtime
    assert "COPY --from=whisper /out/whisper-cli" in runtime
    assert "ggml-small.bin" in runtime


def test_containerfile_runtime_stage_downloads_nothing():
    runtime = _stages(_containerfile())[-1]
    for tool in ("curl", "wget", "git clone", "pip install", "uv pip"):
        assert tool not in runtime, tool


def test_containerfile_base_images_are_pinned_and_user_is_not_root():
    text = _containerfile()
    for line in re.findall(r"(?m)^FROM (\S+)", text):
        assert ":" in line and not line.endswith(":latest"), line
    user = re.findall(r"(?m)^USER (\S+)", text)
    assert user and user[-1] not in ("root", "0")


def test_containerfile_has_no_secret_names_or_values():
    text = _containerfile()
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "CURSOR_API_KEY"):
        assert name not in text
    assert not re.search(r"(?mi)^(ENV|ARG)\s+\w*(TOKEN|SECRET|PASSWORD|API_KEY)\w*", text)


def test_containerfile_serves_app_with_optional_tls_entrypoint():
    text = _containerfile()
    assert "deploy/entrypoint.sh" in text
    assert "EXPOSE 8443" in text
    entry = (ROOT / "deploy" / "entrypoint.sh").read_text(encoding="utf-8")
    assert "--ssl-certfile" in entry and "--ssl-keyfile" in entry
    assert "OLYMP_TLS_CERT" in entry and "OLYMP_TLS_KEY" in entry


def test_compose_app_service_limits_volume_healthcheck_and_ports():
    app = _compose()["services"]["app"]
    assert app["build"]["dockerfile"] == "Containerfile"
    assert "/srv/olymp:/srv/olymp" in app["volumes"]
    assert any(str(p).endswith("8443:8443") for p in app["ports"])
    assert app["healthcheck"]["test"]
    assert app["mem_limit"] and app["cpus"]
    assert app.get("restart") == "unless-stopped"


def test_compose_has_two_containers_and_secrets_only_in_executor():
    services = _compose()["services"]
    assert set(services) == {"app", "executor"}
    app_env = services["app"]["environment"]
    flat = app_env if isinstance(app_env, list) else list(app_env)
    assert not [e for e in flat if re.search(r"TOKEN|API_KEY|SECRET", str(e))]
    ex_env = services["executor"]["environment"]
    ex_flat = ex_env if isinstance(ex_env, list) else list(ex_env)
    assert "CLAUDE_CODE_OAUTH_TOKEN" in ex_flat and "CURSOR_API_KEY" in ex_flat  # без значения
    assert services["executor"].get("read_only") is True
    assert not services["executor"].get("volumes")


def test_compose_has_no_literal_secret_values():
    text = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
    assert not re.search(r"(TOKEN|API_KEY)\s*[:=]\s*\S", text)


def test_ci_has_image_smoke_before_reporter_without_secrets():
    text = (ROOT / ".github/workflows/haiplane-ci.yml").read_text(encoding="utf-8")
    smoke = text.index("name: image-smoke")
    reporter = text.index("name: Report AC tests and validation to Hub")
    assert smoke < reporter
    block = text[smoke : text.index("# Carries the result")]
    assert "deploy/image-smoke.sh" in block
    assert "secrets." not in block
    assert "continue-on-error" not in block
    assert "image-build" in text[: smoke + 1]


def test_smoke_script_cleans_up_always_and_fails_loudly():
    script = (ROOT / "deploy" / "image-smoke.sh").read_text(encoding="utf-8")
    assert "set -euo pipefail" in script
    assert re.search(r"trap \S+ EXIT", script)
    assert "docker rm -f" in script
    assert "/healthz" in script
    assert script.count("--network none") == 2  # распознавание без сети: ничего не скачивается
    assert "scan_image.py" in script
    assert "whisper-cli" in script and "ffmpeg" in script
    assert "tests/fixtures" in script


def test_fixture_wav_is_a_short_mono_16k_pcm():
    import wave

    with wave.open(str(ROOT / "tests/fixtures/sample-ru.wav")) as w:
        assert w.getnchannels() == 1 and w.getframerate() == 16000 and w.getsampwidth() == 2
        assert 0.5 < w.getnframes() / w.getframerate() < 15


def test_deploy_doc_names_variables_volume_ports_hosts_and_procedures():
    doc = (ROOT / "docs" / "deploy.md").read_text(encoding="utf-8")
    env = _compose()["services"]
    names = set()
    for svc in env.values():
        e = svc.get("environment") or []
        names |= {str(x).split("=")[0] for x in (e if isinstance(e, list) else list(e))}
    assert names >= {"OLYMP_DB", "OLYMP_TLS_CERT", "OLYMP_TLS_KEY", "CLAUDE_CODE_OAUTH_TOKEN"}
    for name in names:
        assert f"| `{name}` |" in doc or f"`{name}`," in doc, name
    for must in (
        "/srv/olymp",
        "8443",
        "192.168.0.138",
        "k8_guard",
        "0,9 ГБ",
        "ca.pem",
        "make-certs.sh",
    ):
        assert must in doc, must
    for host in ("api.anthropic.com", "api2.cursor.sh"):
        assert host in doc, host
    for heading in ("Выпуск токена", "Замена токена", "Сертификаты", "Доверие на ноутбуке"):
        assert heading in doc, heading
