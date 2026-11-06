"""Trust the pinned Russian root CA for the MAX API without changing other clients."""
import hashlib
import ssl
from pathlib import Path

import httpx

ROOT_CA = Path(__file__).resolve().parent / "certs" / "russian_trusted_root_ca.crt"
ROOT_SHA256 = "d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31"


def create_ssl_context() -> ssl.SSLContext:
    certificate = ROOT_CA.read_text(encoding="ascii")
    if certificate.count("-----BEGIN CERTIFICATE-----") != 1 or certificate.count("-----END CERTIFICATE-----") != 1:
        raise ValueError("Для MAX нужен один закреплённый корневой сертификат.")
    der = ssl.PEM_cert_to_DER_cert(certificate)
    if hashlib.sha256(der).hexdigest() != ROOT_SHA256:
        raise ValueError("Отпечаток корневого сертификата MAX не совпадает с ожидаемым.")
    # Keep HTTPX's ordinary roots and its SSL_CERT_FILE/SSL_CERT_DIR configuration.
    context = httpx.create_ssl_context()
    context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(der))
    return context
