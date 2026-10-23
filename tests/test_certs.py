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


def test_app_serves_https_verified_by_ca_for_lan_address(certs, tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {
        "OLYMP_DB": str(tmp_path / "olymp.db"),
        "OLYMP_TLS_CERT": str(certs / "server.pem"),
        "OLYMP_TLS_KEY": str(certs / "server.key"),
        "OLYMP_PORT": str(port),
        "OLYMP_HOST": "127.0.0.1",
        "PATH": "/usr/bin:/bin:" + str(Path(sys.executable).parent),
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    proc = subprocess.Popen(["bash", str(ROOT / "deploy" / "entrypoint.sh")], env=env, cwd=ROOT)
    try:
        ctx = ssl.create_default_context(cafile=str(certs / "ca.pem"))
        body = b""
        for _ in range(100):
            try:
                with (
                    socket.create_connection(("127.0.0.1", port), timeout=1) as raw,
                    ctx.wrap_socket(raw, server_hostname="192.168.0.138") as tls,
                ):
                    tls.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                    while chunk := tls.recv(4096):
                        body += chunk
                break
            except OSError:
                time.sleep(0.1)
        assert b"200 OK" in body and b'"status":"ok"' in body.replace(b" ", b"")
        # HTTP-рукопожатие на TLS-порту исключением не является
        with socket.create_connection(("127.0.0.1", port), timeout=2) as raw:
            raw.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            assert b"200 OK" not in raw.recv(4096)
    finally:
        proc.terminate()
        proc.wait(timeout=10)


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
