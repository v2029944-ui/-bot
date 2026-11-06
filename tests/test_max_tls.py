"""MAX API2, scoped CA trust and real TLS verification over a local test server."""
import asyncio
import hashlib
import shutil
import ssl
import subprocess

import httpx
import pytest
from conftest import run

import max_
import max_tls


def fingerprints(context):
    return {hashlib.sha256(der).hexdigest() for der in context.get_ca_certs(binary_form=True)}


def test_pinned_root_is_added_without_replacing_existing_trust():
    original = httpx.create_ssl_context()
    context = max_tls.create_ssl_context()
    assert fingerprints(original) <= fingerprints(context)
    assert max_tls.ROOT_SHA256 in fingerprints(context)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


def test_unexpected_certificate_is_rejected_before_client_creation(monkeypatch):
    monkeypatch.setattr(max_tls, "ROOT_SHA256", "0" * 64)
    def unexpected_client(**kwargs):
        pytest.fail("A client must not be created with an unverified root")
    monkeypatch.setattr(max_.httpx, "AsyncClient", unexpected_client)
    with pytest.raises(ValueError, match="Отпечаток"):
        max_.Max("test-token", "-100", [], [])


def test_missing_root_is_not_silently_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(max_tls, "ROOT_CA", tmp_path / "missing.crt")
    with pytest.raises(FileNotFoundError):
        max_tls.create_ssl_context()


def test_extra_certificates_cannot_bypass_the_root_pin(tmp_path, monkeypatch):
    certificate = max_tls.ROOT_CA.read_text()
    bundle = tmp_path / "bundle.crt"
    bundle.write_text(certificate + certificate)
    monkeypatch.setattr(max_tls, "ROOT_CA", bundle)
    with pytest.raises(ValueError, match="один закреплённый"):
        max_tls.create_ssl_context()


def test_api2_uses_verified_context_and_raw_authorization(monkeypatch):
    async def scenario():
        requests, options = [], []
        original = httpx.AsyncClient
        async def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"user_id": 1})
        def client(**kwargs):
            options.append(kwargs.copy())
            return original(transport=httpx.MockTransport(handler), **kwargs)
        monkeypatch.setattr(max_.httpx, "AsyncClient", client)
        ad = max_.Max("test-token", "-100", [], [])
        try:
            await ad.call("GET", "/me")
            assert str(requests[0].url) == "https://platform-api2.max.ru/me"
            assert requests[0].headers["Authorization"] == "test-token"
            assert "access_token" not in requests[0].url.params
            context = options[0]["verify"]
            assert isinstance(context, ssl.SSLContext)
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            assert max_tls.ROOT_SHA256 in fingerprints(context)
        finally:
            await ad.http.aclose()
    run(scenario())


def test_cdn_clients_keep_default_trust_and_do_not_receive_bot_token(tmp_path, monkeypatch):
    async def scenario():
        options, requests = [], []
        original = httpx.AsyncClient
        async def handler(request):
            requests.append(request)
            if request.method == "POST":
                return httpx.Response(200, json={"photos": {"image": {"token": "uploaded"}}})
            return httpx.Response(200, content=b"image")
        def client(**kwargs):
            options.append(kwargs.copy())
            return original(transport=httpx.MockTransport(handler), **kwargs)
        monkeypatch.setattr(max_.httpx, "AsyncClient", client)
        ad = max_.Max("test-token", "-100", [], [])
        async def upload_info(*args, **kwargs):
            return {"url": "https://cdn.example/upload"}
        monkeypatch.setattr(ad, "call", upload_info)
        image = tmp_path / "image.jpg"
        image.write_bytes(b"image")
        try:
            assert await ad._upload_image(image) == "uploaded"
            assert await ad._download({"payload": {"url": "https://cdn.example/image"}}, 100, "image") == b"image"
            assert "verify" in options[0]
            assert all("verify" not in option for option in options[1:])
            assert all("Authorization" not in request.headers for request in requests)
        finally:
            await ad.http.aclose()
    run(scenario())


@pytest.fixture(scope="module")
def local_certificates(tmp_path_factory):
    executable = shutil.which("openssl")
    if executable is None:
        pytest.skip("Для локального TLS-теста нужен openssl")
    folder = tmp_path_factory.mktemp("max-tls")
    def openssl(*args):
        subprocess.run([executable, *args], cwd=folder, check=True, capture_output=True, timeout=20)
    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-sha256",
            "-subj", "/CN=Local MAX Test Root", "-addext", "basicConstraints=critical,CA:TRUE",
            "-keyout", "root.key", "-out", "root.crt")
    openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost",
            "-keyout", "server.key", "-out", "server.csr")
    (folder / "extensions.cnf").write_text("subjectAltName=DNS:localhost\nbasicConstraints=critical,CA:FALSE\n")
    openssl("x509", "-req", "-in", "server.csr", "-CA", "root.crt", "-CAkey", "root.key",
            "-CAcreateserial", "-days", "1", "-sha256", "-extfile", "extensions.cnf", "-out", "server.crt")
    return folder


def test_existing_custom_ca_file_is_preserved(local_certificates, monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", str(local_certificates / "root.crt"))
    root = ssl.PEM_cert_to_DER_cert((local_certificates / "root.crt").read_text())
    context = max_tls.create_ssl_context()
    assert hashlib.sha256(root).hexdigest() in fingerprints(context)
    assert max_tls.ROOT_SHA256 in fingerprints(context)


def test_actual_tls_accepts_trusted_chain_but_rejects_unknown_ca_and_wrong_host(local_certificates, monkeypatch):
    root = local_certificates / "root.crt"
    monkeypatch.setattr(max_tls, "ROOT_CA", root)
    monkeypatch.setattr(max_tls, "ROOT_SHA256", hashlib.sha256(ssl.PEM_cert_to_DER_cert(root.read_text())).hexdigest())
    async def scenario():
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(local_certificates / "server.crt", local_certificates / "server.key")
        async def handle(reader, writer):
            try:
                await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_context)
        port = server.sockets[0].getsockname()[1]
        async with server:
            async with httpx.AsyncClient(verify=max_tls.create_ssl_context(), trust_env=False) as client:
                assert (await client.get(f"https://localhost:{port}")).text == "OK"
                with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                    await client.get(f"https://127.0.0.1:{port}")
            async with httpx.AsyncClient(trust_env=False) as client:
                with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                    await client.get(f"https://localhost:{port}")
    run(scenario())
