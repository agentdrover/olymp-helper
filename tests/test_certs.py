"""OH-T04: локальный корневой сертификат и сертификат сервера с SAN на адрес K8."""

import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "make-certs.sh"


def _run(*args, **kw):
    return subprocess.run(args, check=False, capture_output=True, text=True, **kw)


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    out = tmp_path_factory.mktemp("certs")
    res = _run("bash", str(SCRIPT), str(out))
    assert res.returncode == 0, res.stderr
    return out


def test_script_creates_ca_and_server_files(certs):
    for name in ("ca.pem", "ca.key", "server.pem", "server.key"):
        assert (certs / name).stat().st_size > 0, name
    assert (certs / "server.key").stat().st_mode & 0o077 == 0
    assert (certs / "ca.key").stat().st_mode & 0o077 == 0


def test_server_cert_has_lan_ip_san_and_server_auth(certs):
    text = _run("openssl", "x509", "-in", str(certs / "server.pem"), "-noout", "-text").stdout
    assert "IP Address:192.168.0.138" in text
    assert "TLS Web Server Authentication" in text
    assert "CA:FALSE" in text


def test_ca_is_a_ca_and_signs_server(certs):
    text = _run("openssl", "x509", "-in", str(certs / "ca.pem"), "-noout", "-text").stdout
    assert "CA:TRUE" in text
    res = _run("openssl", "verify", "-CAfile", str(certs / "ca.pem"), str(certs / "server.pem"))
    assert res.returncode == 0 and "OK" in res.stdout


def test_extra_san_env_adds_a_dns_name(tmp_path):
    import os

    env = {**os.environ, "OLYMP_EXTRA_SAN": "DNS:olymp.lan"}
    res = _run("bash", str(SCRIPT), str(tmp_path), env=env)
    assert res.returncode == 0, res.stderr
    text = _run("openssl", "x509", "-in", str(tmp_path / "server.pem"), "-noout", "-text").stdout
    assert "DNS:olymp.lan" in text and "IP Address:192.168.0.138" in text


def test_script_refuses_to_overwrite_existing_ca(certs):
    res = _run("bash", str(SCRIPT), str(certs))
    assert res.returncode != 0
    assert "уже" in res.stderr


def test_renew_server_keeps_ca_and_reissues_server(tmp_path):
    assert _run("bash", str(SCRIPT), str(tmp_path)).returncode == 0
    ca_before = (tmp_path / "ca.pem").read_bytes()
    server_before = (tmp_path / "server.pem").read_bytes()
    res = _run("bash", str(SCRIPT), str(tmp_path), "--renew-server")
    assert res.returncode == 0, res.stderr
    assert (tmp_path / "ca.pem").read_bytes() == ca_before
    assert (tmp_path / "server.pem").read_bytes() != server_before
    ok = _run(
        "openssl", "verify", "-CAfile", str(tmp_path / "ca.pem"), str(tmp_path / "server.pem")
    )
    assert ok.returncode == 0


def test_renew_server_without_ca_fails(tmp_path):
    assert _run("bash", str(SCRIPT), str(tmp_path), "--renew-server").returncode != 0


def _entry(env_extra, tmp_path, timeout=5):
    env = {
        "PATH": "/usr/bin:/bin:" + str(Path(sys.executable).parent),
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        "OLYMP_DB": str(tmp_path / "olymp.db"),
        **env_extra,
    }
    return subprocess.Popen(
        ["bash", str(ROOT / "deploy" / "entrypoint.sh")],
        env=env,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def test_entrypoint_without_tls_exits_with_error_by_default(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = _entry({"OLYMP_PORT": str(port), "OLYMP_HOST": "127.0.0.1"}, tmp_path)
    try:
        out, _ = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("без сертификата запущен HTTP-сервер")
    assert proc.returncode != 0
    assert "OLYMP_TLS_CERT" in out and "OLYMP_INSECURE_HTTP" in out


def test_entrypoint_with_only_one_tls_variable_exits_with_error(certs, tmp_path):
    proc = _entry({"OLYMP_TLS_CERT": str(certs / "server.pem")}, tmp_path)
    proc.communicate(timeout=10)
    assert proc.returncode != 0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _stop(proc) -> str:
    """Останавливает сервер всегда и возвращает его вывод."""
    if proc.poll() is None:
        proc.terminate()
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return out or ""


def _get_until_status(proc, port, ctx=None, hostname=None, deadline_s=30.0) -> bytes:
    """Повторяет GET /healthz, пока не придёт ответ со строкой статуса или не выйдет срок."""
    deadline = time.monotonic() + deadline_s
    last_error = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
                conn = ctx.wrap_socket(raw, server_hostname=hostname) if ctx else raw
                with conn:
                    conn.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                    body = b""
                    while chunk := conn.recv(4096):
                        body += chunk
            if body.startswith(b"HTTP/1."):
                return body
            last_error = f"ответ без строки статуса: {body[:60]!r}"
        except OSError as exc:
            last_error = repr(exc)
        time.sleep(0.2)
    out = _stop(proc)
    pytest.fail(f"нет ответа за {deadline_s} с ({last_error}); вывод сервера:\n{out}")


def test_app_serves_https_verified_by_ca_for_lan_address(certs, tmp_path):
    port = _free_port()
    proc = _entry(
        {
            "OLYMP_TLS_CERT": str(certs / "server.pem"),
            "OLYMP_TLS_KEY": str(certs / "server.key"),
            "OLYMP_PORT": str(port),
            "OLYMP_HOST": "127.0.0.1",
        },
        tmp_path,
    )
    try:
        ctx = ssl.create_default_context(cafile=str(certs / "ca.pem"))
        body = _get_until_status(proc, port, ctx, "192.168.0.138")
        assert b"200 OK" in body and b'"status":"ok"' in body.replace(b" ", b"")
        # HTTP на TLS-порту исключением не является
        with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
            raw.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            try:
                assert b"200 OK" not in raw.recv(4096)
            except OSError:
                pass  # сброс соединения тоже отказ
    finally:
        _stop(proc)


def test_entrypoint_insecure_http_only_when_explicit_and_warns(tmp_path):
    port = _free_port()
    proc = _entry(
        {"OLYMP_INSECURE_HTTP": "1", "OLYMP_PORT": str(port), "OLYMP_HOST": "127.0.0.1"}, tmp_path
    )
    try:
        body = _get_until_status(proc, port)
        assert b"200 OK" in body
    finally:
        out = _stop(proc)
    assert "OLYMP_INSECURE_HTTP" in out and "WARNING" in out


def test_entrypoint_insecure_flag_other_than_1_does_not_enable_http(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = _entry(
        {"OLYMP_INSECURE_HTTP": "true", "OLYMP_PORT": str(port), "OLYMP_HOST": "127.0.0.1"},
        tmp_path,
    )
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("OLYMP_INSECURE_HTTP=true запустил HTTP-сервер")
    assert proc.returncode != 0
