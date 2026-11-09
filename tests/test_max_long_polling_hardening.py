import asyncio
from types import SimpleNamespace

import httpx
import pytest
from conftest import run

import max_
import max_queue


def test_nullable_message_body_is_not_material_and_does_not_break_routing(monkeypatch):
    async def scenario():
        adapter = max_.Max("token", "-1", ["5"], ["9"], admins=["5"])
        seen = []

        async def handle(update):
            seen.append(update)

        monkeypatch.setattr(adapter, "handle", handle)
        update = {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 9},
                "recipient": {"chat_type": "dialog"},
                "body": None,
            },
        }
        assert not max_queue.is_material(update)
        await adapter._handle_limited(update)
        assert seen == [update]
        await adapter.http.aclose()

    run(scenario())


def test_on_message_accepts_nullable_body(monkeypatch):
    async def scenario():
        adapter = max_.Max("token", "-1", ["5"], ["9"], admins=["5"])
        notices = []

        async def notify(uid, text):
            notices.append((str(uid), text))

        monkeypatch.setattr(adapter, "notify", notify)
        await adapter.on_message({
            "sender": {"user_id": 9, "first_name": "A"},
            "recipient": {"chat_type": "dialog"},
            "body": None,
        })
        assert notices and "фото или видео" in notices[-1][1]
        await adapter.http.aclose()

    run(scenario())


def test_rate_gate_waits_when_window_is_full(monkeypatch):
    async def scenario():
        now = [10.0]
        delays = []

        async def sleep(delay):
            delays.append(delay)
            now[0] += delay

        monkeypatch.setattr(max_, "time", SimpleNamespace(monotonic=lambda: now[0]))
        monkeypatch.setattr(asyncio, "sleep", sleep)
        gate = max_._RateGate(2)
        await gate.wait()
        await gate.wait()
        await gate.wait()
        assert delays == pytest.approx([1.0])

    run(scenario())


def test_upload_failure_is_definitely_not_sent(monkeypatch, tmp_path):
    async def scenario():
        adapter = max_.Max("token", "-1", [], [], admins=[])
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"x")

        async def fail(*args, **kwargs):
            raise RuntimeError("upload failed")

        monkeypatch.setattr(adapter, "_cached_upload", fail)
        with pytest.raises(RuntimeError) as caught:
            await adapter.publish(str(photo), "post")
        assert adapter.not_sent(caught.value)
        await adapter.http.aclose()

    run(scenario())


def test_attachment_not_ready_uses_increasing_backoff(monkeypatch, tmp_path):
    async def scenario():
        adapter = max_.Max("token", "-1", [], [], admins=[])
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"x")
        calls = 0
        delays = []

        async def upload(*args, **kwargs):
            return "token"

        async def call(method, path, **kwargs):
            nonlocal calls
            calls += 1
            if calls < 3:
                request = httpx.Request("POST", "https://platform-api2.max.ru/messages")
                response = httpx.Response(400, request=request, text="attachment.not.ready")
                raise httpx.HTTPStatusError("not ready", request=request, response=response)
            return {"message": {"body": {"mid": "m1"}}}

        async def sleep(delay):
            delays.append(delay)

        monkeypatch.setattr(adapter, "_cached_upload", upload)
        monkeypatch.setattr(adapter, "call", call)
        monkeypatch.setattr(asyncio, "sleep", sleep)
        await adapter.publish(str(photo), "post")
        assert calls == 3
        assert delays == pytest.approx([1.5, 2.4])
        await adapter.http.aclose()

    run(scenario())


def test_long_polling_without_marker_keeps_official_uncommitted_semantics(monkeypatch):
    async def scenario():
        adapter = max_.Max("token", "-1", [], [], admins=[])
        requests = []

        async def call(method, path, **kwargs):
            requests.append(kwargs["params"].copy())
            if len(requests) == 1:
                return {"updates": [], "marker": 77}
            assert kwargs["params"]["marker"] == 77
            raise asyncio.CancelledError

        monkeypatch.setattr(adapter, "call", call)
        with pytest.raises(asyncio.CancelledError):
            await adapter.run()
        assert "marker" not in requests[0]
        await adapter.http.aclose()

    run(scenario())


def test_rate_registry_is_shared_by_workspaces_using_same_token():
    async def scenario():
        first = max_.Max("same-token", "-1", [], [], admins=[])
        second = max_.Max("same-token", "-2", [], [], admins=[])
        other = max_.Max("other-token", "-3", [], [], admins=[])
        assert first._rates() is second._rates()
        assert first._rates() is not other._rates()
        await first.http.aclose()
        await second.http.aclose()
        await other.http.aclose()

    run(scenario())
