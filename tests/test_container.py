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
    for tool in ("curl", "wget", "git clone", "pip install"):
        assert tool not in runtime, tool
    assert not re.search(r"\buv\b", runtime)  # ни установщика, ни его образа в рантайме
    assert "COPY --from=venv /opt/venv /opt/venv" in runtime
    assert "uv sync" in _containerfile().split("FROM python")[1]  # зависимости ставит стадия сборки


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
    assert "OLYMP_INSECURE_HTTP" in entry


def test_compose_app_service_limits_volume_healthcheck_and_ports():
    app = _compose()["services"]["app"]
    assert app["build"]["dockerfile"] == "Containerfile"
    assert "/srv/olymp:/srv/olymp" in app["volumes"]
    assert any(str(p).endswith("8443:8443") for p in app["ports"])
    assert app["healthcheck"]["test"]
    assert app["mem_limit"] and app["cpus"]
    assert app.get("restart") == "unless-stopped"


def _env_names(svc: dict) -> list[str]:
    env = svc.get("environment") or []
    return [str(x).split("=")[0] for x in (env if isinstance(env, list) else list(env))]


def test_compose_app_and_two_executors_each_with_only_its_own_token():
    services = _compose()["services"]
    assert set(services) == {"app", "executor-codex", "executor-cursor"}
    assert not [e for e in _env_names(services["app"]) if re.search(r"TOKEN|API_KEY|SECRET", e)]
    codex = _env_names(services["executor-codex"])
    cursor = _env_names(services["executor-cursor"])
    assert "CODEX_SEED_DIR" in codex and "CURSOR_API_KEY" not in codex
    assert "CURSOR_API_KEY" in cursor and "CLAUDE_CODE_OAUTH_TOKEN" not in cursor


def test_compose_executors_have_writable_limited_work_tmpfs_and_read_only_root():
    services = _compose()["services"]
    for name in ("executor-codex", "executor-cursor"):
        svc = services[name]
        assert svc.get("read_only") is True
        if name == "executor-codex":
            assert svc["volumes"] == ["${CODEX_SEED_DIR:?set the sbx proxy seed directory}:/codex-seed:ro"]
        else:
            assert not svc.get("volumes")
        assert svc["environment"]["EXECUTOR_WORKROOT"] == "/work"
        work = [t for t in svc["tmpfs"] if str(t).startswith("/work")]
        assert len(work) == 1, name
        opts = str(work[0]).split(":", 1)[1].split(",")
        assert {"noexec", "nosuid", "nodev"} <= set(opts)
        assert any(o.startswith("size=") for o in opts)
        assert any(o.startswith("uid=") for o in opts) and any(o.startswith("gid=") for o in opts)


def test_compose_has_no_literal_secret_values():
    text = (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
    # Только подстановка из окружения хоста (${VAR:?...}); любое иное значение - литерал.
    literal = r"^\s*-?\s*\w*(TOKEN|API_KEY|SHARED_KEY)\s*[:=]\s*(?!\$\{)\S"
    assert not re.search(literal, text, re.MULTILINE)


HUB_ACTION = "agentdrover/haiplane/.github/actions/hub-ci-report"


def test_ci_has_image_smoke_before_hub_step_without_secrets():
    text = (ROOT / ".github/workflows/haiplane-ci.yml").read_text(encoding="utf-8")
    smoke = text.index("- name: image-smoke")
    assert "- name: image-build" in text[:smoke]
    # порядок задаётся действием хаба (uses), а не названием шага: имя шага у хаба может смениться
    assert smoke < text.index(f"uses: {HUB_ACTION}")
    step = re.search(r"- name: image-smoke\n(?:        .*\n)+", text).group(0)
    assert "deploy/image-smoke.sh" in step
    assert "secrets." not in step
    assert "continue-on-error" not in step
    assert "if:" not in step


def test_smoke_script_cleans_up_always_and_fails_loudly():
    script = (ROOT / "deploy" / "image-smoke.sh").read_text(encoding="utf-8")
    assert "set -euo pipefail" in script
    assert re.search(r"trap \S+ EXIT", script)
    assert "docker rm -f" in script
    assert "/healthz" in script
    assert script.count("--network none") == 4  # распознавание без сети: ничего не скачивается
    assert "scan_image.py" in script
    curls = [ln for ln in script.splitlines() if re.search(r"\bcurl\s+--", ln)]
    assert curls and all("--connect-timeout" in ln and "--max-time" in ln for ln in curls)
    assert "for t in uv pip pip3 curl wget" in script  # в рантайме нет uv, pip, curl, wget
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
    names.add("OLYMP_INSECURE_HTTP")
    assert names >= {"OLYMP_DB", "OLYMP_TLS_CERT", "OLYMP_TLS_KEY", "CODEX_SEED_DIR"}
    assert {"executor-codex", "executor-cursor"} <= set(env)
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
    for host in ("chatgpt.com", "api2.cursor.sh"):
        assert host in doc, host
    for heading in ("Выпуск токена", "Замена токена", "Сертификаты", "Доверие на ноутбуке"):
        assert heading in doc, heading
