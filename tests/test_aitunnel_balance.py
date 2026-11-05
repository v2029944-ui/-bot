import httpx
import pytest

import core
from conftest import FakeAdapter, run


def setup(monkeypatch, handler):
    monkeypatch.setattr(core, "PROVIDER", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.aitunnel.ru/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-secret")
    client = httpx.AsyncClient
    monkeypatch.setattr(core.httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handler), **kw))


@pytest.mark.parametrize("data,expected", [({"balance": 4999.55, "budget": 850}, "Бюджет ключа: 850,00 ₽"),
                                         ({"balance": 0}, "Баланс AITunnel: 0,00 ₽")])
def test_status_fetches_balance_with_existing_key(monkeypatch, llm, data, expected):
    calls = []

    def handler(request):
        calls.append(request)
        assert str(request.url) == "https://api.aitunnel.ru/v1/aitunnel/balance"
        assert request.headers["Authorization"] == "Bearer test-secret"
        return httpx.Response(200, json=data)

    setup(monkeypatch, handler)
    ad = FakeAdapter("max", ["5", "6"])
    ad.admins = ["5"]
    assert "администратору" in run(core.on_menu(ad, "6", "m:status"))[0]
    assert not calls
    text, _ = run(core.on_menu(ad, "5", "m:status"))
    assert expected in text and "test-secret" not in text and len(calls) == 1
    if "budget" not in data:
        assert "Бюджет ключа" not in text


@pytest.mark.parametrize("failure", ["timeout", "403", "missing", "nan"])
def test_balance_failure_does_not_break_status(monkeypatch, llm, failure):
    def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("test-secret", request=request)
        return httpx.Response(403 if failure == "403" else 200,
                              json={"balance": "NaN"} if failure == "nan" else {"error": "test-secret"})
    setup(monkeypatch, handler)
    text = run(core.status_text(FakeAdapter("max", ["5"])))
    assert "Баланс AITunnel:" in text and "Свободно на диске" in text
    assert "test-secret" not in text


@pytest.mark.parametrize("base", ["https://other.example/v1", "https://api.aitunnel.ru.evil.example/v1", "http://api.aitunnel.ru/v1"])
def test_other_providers_do_not_send_key_to_aitunnel(monkeypatch, base):
    def handler(request):
        pytest.fail("Unexpected request")
    setup(monkeypatch, handler)
    monkeypatch.setenv("LLM_BASE_URL", base)
    assert run(core.aitunnel_balance()) is None
