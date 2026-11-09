import asyncio

from conftest import run
from test_admin_menu_button import Api, make

import core
import max_actions
import max_queue


def event(uid, cid):
    return {"update_type": "message_callback", "callback": {"user": {"user_id": uid}, "callback_id": cid, "payload": "m:status"}}


def test_slow_user_does_not_block_other_user_and_same_user_is_serial():
    async def scenario():
        gate, started, second = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        class Adapter:
            async def handle(self, update):
                cb = update["callback"]
                calls.append(cb["callback_id"])
                if cb["callback_id"] == "a":
                    started.set()
                    await gate.wait()
                else:
                    second.set()

        max_actions.enqueue(event(1, "a"))
        max_actions.enqueue(event(1, "b"))
        max_actions.enqueue(event(2, "c"))
        worker = max_actions.Worker(Adapter())
        worker.schedule()
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(second.wait(), 2)
        assert calls == ["a", "c"]
        gate.set()
        await asyncio.gather(*worker.active.values())
        worker.schedule()
        await asyncio.gather(*worker.active.values())
        assert calls == ["a", "c", "b"]
    run(scenario())


def test_poll_dispatch_saves_action_without_running_it(monkeypatch):
    async def scenario():
        api = Api()
        adapter = make(api)

        async def fail(update):
            raise AssertionError("Polling must not run status")

        monkeypatch.setattr(adapter, "handle", fail)
        await adapter._handle_limited(event(1, "a"))
        assert core.db.execute("SELECT COUNT(*) FROM max_actions WHERE state='pending'").fetchone()[0] == 1
        await adapter.http.aclose()
    run(scenario())


def test_text_deduplication_uses_message_id_not_profile_or_route():
    update = {"update_type": "message_created", "message": {
        "sender": {"user_id": 1, "first_name": "Первое имя"}, "body": {"mid": "text-1", "text": "/menu"}}}
    max_actions.enqueue(update)
    max_actions.enqueue({**update, "_route_on_process": True, "message": {
        **update["message"], "_space": 5, "sender": {"user_id": 1, "first_name": "Новое имя"}}})
    assert core.db.execute("SELECT COUNT(*) FROM max_actions").fetchone()[0] == 1
    max_actions.enqueue({**update, "message": {**update["message"], "body": {"mid": "text-2", "text": "/menu"}}})
    assert core.db.execute("SELECT COUNT(*) FROM max_actions").fetchone()[0] == 2


def test_adapter_error_is_recorded_as_failed_action(monkeypatch):
    async def scenario():
        api = Api()
        adapter = make(api)

        async def fail(*args, **kwargs):
            raise RuntimeError("cannot handle command")

        monkeypatch.setattr(adapter, "on_message", fail)
        max_actions.enqueue({"update_type": "message_created", "message": {
            "sender": {"user_id": 1}, "body": {"mid": "broken-command", "text": "/menu"}}})
        row = core.db.execute("SELECT * FROM max_actions WHERE state='pending'").fetchone()
        await max_actions.Worker(adapter).process(row)
        result = core.db.execute("SELECT state,payload FROM max_actions WHERE id=?", (row["id"],)).fetchone()
        assert tuple(result) == ("failed", "")
        assert any("Не удалось выполнить действие" in entry[3].get("text", "") for entry in api.of("POST"))
        await adapter.http.aclose()
    run(scenario())


def test_completed_message_locks_are_not_retained():
    async def scenario():
        api = Api()
        adapter = make(api)
        for i in range(25):
            await adapter.notify("1", f"Сообщение {i}")
        assert not adapter._send_locks
        await adapter.http.aclose()
    run(scenario())


def test_message_locks_keep_waiting_sends_serial(monkeypatch):
    async def scenario():
        adapter = make(Api())
        started, release = asyncio.Event(), asyncio.Event()
        active, peak = 0, 0

        async def send(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            started.set()
            await release.wait()
            active -= 1

        monkeypatch.setattr(adapter, "_send_inner", send)
        tasks = [asyncio.create_task(adapter.notify("1", str(i))) for i in range(3)]
        await asyncio.wait_for(started.wait(), 2)
        assert len(adapter._send_locks) == 1
        release.set()
        await asyncio.gather(*tasks)
        assert peak == 1 and not adapter._send_locks
        await adapter.http.aclose()
    run(scenario())


def test_old_post_keyboard_removed_but_post_preserved():
    async def scenario():
        api = Api()
        adapter = make(api)
        core.db.execute("INSERT INTO drafts(id,status,attempts) VALUES(123,'pending',1)")
        core.db.commit()
        await adapter.send_menu("1", "Изменить", [[("Заново", "d:redo:123:1")]])
        core._set(123, attempts=2)
        await adapter.cleanup_stale_buttons()
        edit = api.of("PUT")[-1][3]
        assert api.messages["m1"]["text"] == "Изменить" and "text" not in edit
        assert all(not b["payload"].startswith("d:") for a in edit["attachments"] if a["type"] == "inline_keyboard" for row in a["payload"]["buttons"] for b in row)
        assert not api.of("DELETE")
        await adapter.http.aclose()
    run(scenario())


def test_material_handoff_failure_keeps_payload_and_retries(monkeypatch):
    async def scenario():
        api = Api()
        adapter = make(api)
        update = {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 1},
                "recipient": {"chat_type": "dialog"},
                "body": {"mid": "material-1", "text": "Описание", "attachments": [
                    {"type": "image", "payload": {"url": "https://example.invalid/photo"}}
                ]},
            },
            "_route_on_process": True,
            "_material_to_inbox": True,
        }
        max_actions.enqueue(update)
        row = core.db.execute("SELECT * FROM max_actions WHERE state='pending'").fetchone()
        original = max_queue.enqueue

        def fail(_update):
            raise RuntimeError("sqlite temporarily unavailable")

        monkeypatch.setattr(max_queue, "enqueue", fail)
        await max_actions.Worker(adapter).process(row)
        kept = core.db.execute("SELECT state,payload,attempts,next_try FROM max_actions WHERE id=?", (row["id"],)).fetchone()
        assert kept["state"] == "pending" and kept["payload"] and kept["attempts"] == 1 and kept["next_try"] > 0

        monkeypatch.setattr(max_queue, "enqueue", original)
        core.db.execute("UPDATE max_actions SET next_try=0 WHERE id=?", (row["id"],))
        core.db.commit()
        row = core.db.execute("SELECT * FROM max_actions WHERE id=?", (row["id"],)).fetchone()
        await max_actions.Worker(adapter).process(row)
        done = core.db.execute("SELECT state,payload FROM max_actions WHERE id=?", (row["id"],)).fetchone()
        assert tuple(done) == ("done", "")
        assert core.db.execute("SELECT COUNT(*) FROM max_inbox WHERE state='pending'").fetchone()[0] == 1
        await adapter.http.aclose()

    run(scenario())


def test_material_backoff_blocks_later_same_user_command(monkeypatch):
    async def scenario():
        adapter = make(Api())
        material = {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 1},
                "recipient": {"chat_type": "dialog"},
                "body": {"mid": "material-backoff", "text": "Описание", "attachments": [
                    {"type": "image", "payload": {"url": "https://example.invalid/photo"}}
                ]},
            },
            "_route_on_process": True,
            "_material_to_inbox": True,
        }
        max_actions.enqueue(material)
        max_actions.enqueue(event(1, "after-material"))
        first = core.db.execute("SELECT * FROM max_actions ORDER BY id LIMIT 1").fetchone()

        def fail(_update):
            raise RuntimeError("temporary")

        monkeypatch.setattr(max_queue, "enqueue", fail)
        await max_actions.Worker(adapter).process(first)

        worker = max_actions.Worker(adapter)
        worker.schedule()
        assert not worker.active  # later callback cannot overtake backoff material
        await adapter.http.aclose()

    run(scenario())


def test_material_handoff_expires_after_seven_days(monkeypatch):
    async def scenario():
        api = Api()
        adapter = make(api)
        update = {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 1},
                "recipient": {"chat_type": "dialog"},
                "body": {"mid": "expired-material", "text": "Описание", "attachments": [
                    {"type": "image", "payload": {"url": "https://example.invalid/photo"}}
                ]},
            },
            "_route_on_process": True,
            "_material_to_inbox": True,
        }
        max_actions.enqueue(update)
        core.db.execute("UPDATE max_actions SET created=? WHERE event_key=(SELECT event_key FROM max_actions LIMIT 1)",
                        (__import__("time").time() - 8 * 86400,))
        core.db.commit()
        row = core.db.execute("SELECT * FROM max_actions LIMIT 1").fetchone()

        def fail(_update):
            raise RuntimeError("still unavailable")

        monkeypatch.setattr(max_queue, "enqueue", fail)
        await max_actions.Worker(adapter).process(row)
        result = core.db.execute("SELECT state,payload FROM max_actions WHERE id=?", (row["id"],)).fetchone()
        assert tuple(result) == ("failed", "")
        assert any("Материал не удалось поставить" in call[3].get("text", "")
                   for call in api.of("POST"))
        await adapter.http.aclose()

    run(scenario())
