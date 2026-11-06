"""Работа с нейросетью (подставной HTTP) и обработка присланных файлов."""
import io
import json

import httpx
import pytest
from conftest import run
from PIL import Image

import core

OK = {"choices": [{"message": {"content": " Пост "}}]}


def mock_http(monkeypatch, responses):
    sent, it, real = [], iter(responses), httpx.AsyncClient

    def handler(req):
        sent.append(json.loads(req.content))
        return next(it)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return sent


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.test/v1")


def test_openai_request_format(monkeypatch):
    sent = mock_http(monkeypatch, [httpx.Response(200, json=OK)])
    assert run(core._openai("система", "текст", "QUJD")) == "Пост"
    body = sent[0]
    assert body["max_tokens"] == 1000 and body["messages"][0] == {"role": "system", "content": "система"}
    assert body["messages"][1]["content"][0]["type"] == "image_url"


def test_new_models_use_max_completion_tokens(monkeypatch):
    sent = mock_http(monkeypatch, [httpx.Response(400, text="Unsupported parameter: 'max_tokens'"),
                                   httpx.Response(200, json=OK)])
    assert run(core._openai("s", "t", None)) == "Пост"
    assert "max_completion_tokens" in sent[1] and "max_tokens" not in sent[1]


def test_temporary_errors_are_retried(monkeypatch):
    mock_http(monkeypatch, [httpx.Response(429, text="slow"), httpx.Response(503, text="down"),
                            httpx.Response(200, json=OK)])
    assert run(core._openai("s", "t", None)) == "Пост"


@pytest.mark.parametrize("response, expected", [
    (httpx.Response(401, text='{"error": "bad key"}'), "401"),
    (httpx.Response(200, json={"choices": [{"message": {"content": None}}]}), "пустой"),
    (httpx.Response(200, json={"choices": []}), "пустой"),
])
def test_llm_errors_are_readable(monkeypatch, response, expected):
    mock_http(monkeypatch, [response])
    with pytest.raises(RuntimeError, match=expected):
        run(core._openai("s", "t", None))


def test_broken_json_is_not_mistaken_for_bad_photo(monkeypatch):
    mock_http(monkeypatch, [httpx.Response(200, text="<html>proxy error</html>")])
    with pytest.raises(ValueError) as e:
        run(core._openai("s", "t", None))
    assert not isinstance(e.value, core.BadImage)  # иначе ошибка сервиса молча спишется на сотрудника


def test_photo_is_normalised(png):
    img = Image.new("RGB", (3000, 2000))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    path = core.save_photo(buf.getvalue())
    assert max(Image.open(path).size) == 1600 and path.endswith(".jpg")


def test_exif_rotation_is_applied():
    img = Image.new("RGB", (200, 100))
    exif = img.getexif()
    exif[0x0112] = 6
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    assert Image.open(core.save_photo(buf.getvalue())).size == (100, 200)


def test_file_names_are_unique(png):
    assert len({core.save_photo(png) for _ in range(20)}) == 20


@pytest.mark.parametrize("data", [b"not an image", b"x" * (26 * 1024 * 1024)])
def test_bad_files_raise_badimage(data):
    with pytest.raises(core.BadImage):
        core.save_photo(data)
