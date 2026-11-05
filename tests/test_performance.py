import asyncio

import httpx

import core
import max_
from conftest import FakeAdapter, run


def test_status_runs_balance_and_model_check_concurrently(monkeypatch):
    async def scenario():
        model_started, balance_started = asyncio.Event(), asyncio.Event()

        async def complete(*args):
            model_started.set()
            await balance_started.wait()
            return "OK"

        async def balance():
            balance_started.set()
            await model_started.wait()
            return "Баланс: 100 ₽"

        monkeypatch.setattr(core, "_complete", complete)
        monkeypatch.setattr(core, "aitunnel_balance", balance)
        text = await asyncio.wait_for(core.status_text(FakeAdapter()), 2)
        assert "Баланс: 100 ₽" in text and "✅ отвечает" in text
    run(scenario())


def test_reviewers_and_publication_share_upload_then_clear_cache(monkeypatch, png, llm):
    async def scenario():
        adapter = max_.Max("token", "-1", ["1", "2"], ["9"], ["1", "2"])
        core.ADAPTERS["max"] = adapter
        uploads = []

        async def upload(path):
            uploads.append(path)
            return "shared-token"

        async def call(*args, **kwargs):
            return {}

        monkeypatch.setattr(adapter, "_upload_image", upload)
        monkeypatch.setattr(adapter, "call", call)
        did = await core.submit(adapter, "9", "Анна", "Описание работы", png)
        assert len(uploads) == 1
        await core.on_action(adapter, "1", "okyes", did, 1)
        assert len(uploads) == 1 and not adapter._upload_cache
        await adapter.http.aclose()
    run(scenario())


def test_concurrent_uploads_expiry_and_file_change(monkeypatch, tmp_path):
    async def scenario():
        adapter = max_.Max("token", "-1", ["1"], [])
        path = tmp_path / "photo.jpg"
        path.write_bytes(b"data")
        calls = []

        async def upload(path):
            calls.append(path)
            return str(len(calls))

        monkeypatch.setattr(adapter, "_upload_image", upload)
        assert await asyncio.gather(*(adapter._cached_upload(path, "image") for _ in range(3))) == ["1"] * 3
        entry = adapter._upload_cache[str(path.resolve())]
        adapter._upload_cache[str(path.resolve())] = (entry[0], entry[1], 0)
        assert await adapter._cached_upload(path, "image") == "2"
        path.write_bytes(b"changed-data")
        assert await adapter._cached_upload(path, "image") == "3"
        await adapter.http.aclose()
    run(scenario())


def test_explicit_attachment_rejection_refreshes_token_once(monkeypatch, tmp_path):
    async def scenario():
        adapter = max_.Max("token", "-1", ["1"], [])
        path = tmp_path / "photo.jpg"
        path.write_bytes(b"data")
        tokens, sends = [], []

        async def upload(path):
            token = f"token-{len(tokens)}"
            tokens.append(token)
            return token

        async def call(method, url, **kwargs):
            sends.append(kwargs["json"]["attachments"][0]["payload"]["token"])
            if len(sends) == 1:
                request = httpx.Request(method, "https://platform-api.max.ru/messages")
                response = httpx.Response(400, json={"code": "attachment.invalid"}, request=request)
                raise httpx.HTTPStatusError("invalid", request=request, response=response)
            return {}

        monkeypatch.setattr(adapter, "_upload_image", upload)
        monkeypatch.setattr(adapter, "call", call)
        await adapter.publish(str(path), "Пост")
        assert sends == tokens == ["token-0", "token-1"]
        await adapter.http.aclose()
    run(scenario())
