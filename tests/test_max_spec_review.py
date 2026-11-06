"""Регрессии аудита OpenAPI MAX и гонок согласования."""
import asyncio
import json

import httpx
import pytest
from conftest import FakeAdapter, run

import core
import max_


@pytest.mark.parametrize("mark", ["max", "max?"])
@pytest.mark.parametrize("action", ["edit", "redoc", "redo"])
def test_cannot_change_post_after_any_publication_attempt(mark, action, png, llm):
    mx = FakeAdapter("max", ["5"], admins=["5"])
    core.ADAPTERS["max"] = mx
    did = run(core.submit(mx, "9", "Анна", "Описание работы", png))
    core._set(did, published_to=mark)
    before = core.get(did)
    assert "Публикация уже началась" in run(core.on_action(mx, "5", action, did, 1))[1]
    assert "Публикация уже началась" in run(core._apply_edit(mx, "5", did, 1, "Другой текст"))[1]
    assert "Публикация уже началась" in run(core._regenerate(mx, "5", did, "Комментарий"))[1]
    assert core.get(did)["post"] == before["post"] and len(llm.calls) == 1


def test_stale_approval_cannot_publish_concurrently_edited_text(png, llm):
    mx = FakeAdapter("max", ["5"], admins=["5"])
    core.ADAPTERS["max"] = mx
    did = run(core.submit(mx, "9", "Анна", "Описание работы", png))

    async def scenario():
        return await asyncio.gather(core.on_action(mx, "5", "okyes", did, 1),
                                    core._apply_edit(mx, "5", did, 1, "Новый ещё не согласованный текст"))
    answers = run(scenario())
    assert "устаревший" in answers[0][1] and not mx.sent("publish")
    assert core.get(did)["status"] == "pending"


@pytest.mark.parametrize("answer", ["x" * 4001, "😀" * 2001])
def test_generated_text_rejected_before_unsendable_draft(answer, png, llm):
    mx = FakeAdapter("max", ["5"], admins=["5"])
    llm.reply = answer
    before = set(core.PHOTOS.iterdir())
    with pytest.raises(RuntimeError, match="слишком длинный"):
        run(core.submit(mx, "9", "Анна", "Описание работы", png))
    assert not mx.sent("approval") and set(core.PHOTOS.iterdir()) == before


def test_manual_emoji_text_obeys_telegram_utf16_limit(png, llm):
    mx = FakeAdapter("max", ["5"], admins=["5"])
    core.ADAPTERS["max"] = mx
    did = run(core.submit(mx, "9", "Анна", "Описание работы", png))
    assert "Слишком длинно" in run(core._apply_edit(mx, "5", did, 1, "😀" * 2001))[1]
    assert core.get(did)["attempts"] == 1


def test_marker_waits_for_processing_and_tracks_download_phase(monkeypatch):
    async def scenario():
        mx = max_.Max("token", "-1", ["5"], ["9"])
        entered, release = asyncio.Event(), asyncio.Event()
        requests = []

        async def call(method, path, **kw):
            requests.append(kw["params"])
            if len(requests) == 1:
                return {"marker": 42, "updates": [{"update_type": "message_created"}]}
            assert release.is_set() and kw["params"]["marker"] == 42
            raise asyncio.CancelledError

        async def handle(update):
            assert core.INFLIGHT
            entered.set()
            await release.wait()  # скачивание ещё не закончено

        monkeypatch.setattr(mx, "call", call)
        monkeypatch.setattr(mx, "handle", handle)
        task = asyncio.create_task(mx.run())
        await asyncio.wait_for(entered.wait(), 2)
        assert len(requests) == 1 and "marker" not in requests[0]
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await mx.http.aclose()
    run(scenario())


def test_max_batches_bound_concurrent_handlers(monkeypatch):
    async def scenario():
        mx = max_.Max("token", "-1", ["5"], ["9"])
        two_started, release = asyncio.Event(), asyncio.Event()
        active, peak = 0, 0

        async def handle(update):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_started.set()
            await release.wait()
            active -= 1

        monkeypatch.setattr(mx, "handle", handle)
        tasks = [asyncio.create_task(mx._handle_limited({})) for _ in range(10)]
        await asyncio.wait_for(two_started.wait(), 2)
        release.set()
        await asyncio.gather(*tasks)
        assert peak == 2
        await mx.http.aclose()
    run(scenario())


def test_official_callback_without_payload_is_acknowledged(monkeypatch):
    async def scenario():
        mx = max_.Max("token", "-1", ["5"], ["9"])
        requests = []

        async def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"success": True})

        await mx.http.aclose()
        mx.http = httpx.AsyncClient(base_url=max_.API, headers={"Authorization": "token"},
                                   transport=httpx.MockTransport(handler))
        await mx.on_callback({"timestamp": 1, "callback_id": "cb", "user": {"user_id": 5, "first_name": "A", "is_bot": False}})
        assert len(requests) == 1
        r = requests[0]
        assert r.method == "POST" and r.url.path == "/answers" and r.url.params["callback_id"] == "cb"
        assert r.headers["Authorization"] == "token" and json.loads(r.content) == {"notification": "Работаю…"}
        await mx.http.aclose()
    run(scenario())


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_unsuccessful_query_result_does_not_remove_tracking(method):
    async def scenario():
        mx = max_.Max("token", "-1", ["5"], ["9"])
        await mx.http.aclose()
        mx.http = httpx.AsyncClient(base_url=max_.API, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"success": False, "message": "denied"})))
        try:
            with pytest.raises(RuntimeError, match="denied"):
                await mx.call(method, "/messages", params={"message_id": "m1"})
        finally:
            await mx.http.aclose()
    run(scenario())
