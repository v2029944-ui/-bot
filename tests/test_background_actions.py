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


def material(uid=1, mid="material-1"):
    return {
        "update_type": "message_created",
        "message": {
            "sender": {"user_id": uid},
            "recipient": {"chat_type": "dialog"},
            "body": {"mid": mid, "text": "Описание", "attachments": [
                {"type": "image", "payload": {"url": "https://example.invalid/photo"}}
            ]},
        },
        "_route_on_process": True,
        "_material_to_inbox": True,
    }


def test_material_routes_atomically_to_independent_handoff():
    async def scenario():
        adapter = make(Api())
        max_actions.enqueue(material())
        row = core.db.execute("SELECT * FROM max_actions WHERE state='pending'").fetchone()
        core.db.execute("UPDATE max_actions SET state='processing' WHERE id=?", (row["id"],))
        core.db.commit()
        await max_actions.Worker(adapter).process(dict(row))
        kept = core.db.execute("SELECT state,payload,space_id,attempts,next_try FROM max_actions WHERE id=?", (row["id"],)).fetchone()
        payload = __import__("json").loads(kept["payload"])
        assert kept["state"] == "handoff" and kept["attempts"] == 0 and kept["next_try"] == 0
        assert "_route_on_process" not in payload
        assert payload["_material_to_inbox"] is True
        await adapter.http.aclose()

    run(scenario())


def test_material_handoff_failure_retries_without_blocking_later_command(monkeypatch):
    async def scenario():
        api = Api()
        adapter = make(api)
        max_actions.enqueue(material())
        max_actions.enqueue(event(1, "after-material"))
        first = core.db.execute("SELECT * FROM max_actions ORDER BY id LIMIT 1").fetchone()
        core.db.execute("UPDATE max_actions SET state='processing' WHERE id=?", (first["id"],))
        core.db.commit()
        worker = max_actions.Worker(adapter)
        await worker.process(dict(first))
        handoff = core.db.execute("SELECT * FROM max_actions WHERE id=?", (first["id"],)).fetchone()
        assert handoff["state"] == "handoff"

        def fail(_update):
            raise RuntimeError("sqlite temporarily unavailable")

        monkeypatch.setattr(max_queue, "enqueue", fail)
        core.db.execute("UPDATE max_actions SET state='handoff_processing' WHERE id=?", (first["id"],))
        core.db.commit()
        await worker.process_handoff(dict(handoff))
        kept = core.db.execute("SELECT state,payload,attempts,next_try FROM max_actions WHERE id=?", (first["id"],)).fetchone()
        assert kept["state"] == "handoff" and kept["payload"] and kept["attempts"] == 1 and kept["next_try"] > 0

        worker.schedule()
        assert "1" in worker.active  # later command is no longer blocked by the material retry
        await asyncio.gather(*worker.active.values())
        assert core.db.execute("SELECT state FROM max_actions WHERE event_key=?", (
            event(1, "after-material")["callback"]["callback_id"],)).fetchone()["state"] == "done"
        await adapter.http.aclose()

    run(scenario())


def test_handoff_claim_is_single_and_recovers_after_restart(monkeypatch):
    async def scenario():
        adapter = make(Api())
        max_actions.enqueue(material(mid="single-claim"))
        row = core.db.execute("SELECT * FROM max_actions WHERE state='pending'").fetchone()
        core.db.execute("UPDATE max_actions SET state='processing' WHERE id=?", (row["id"],))
        core.db.commit()
        first = max_actions.Worker(adapter)
        await first.process(dict(row))
        gate = asyncio.Event()

        async def slow(_row):
            await gate.wait()

        monkeypatch.setattr(first, "process_handoff", slow)
        second = max_actions.Worker(adapter)
        monkeypatch.setattr(second, "process_handoff", slow)
        first.schedule_handoffs()
        second.schedule_handoffs()
        await asyncio.sleep(0)
        assert len(first.handoffs) + len(second.handoffs) == 1
        assert core.db.execute("SELECT state FROM max_actions WHERE id=?", (row["id"],)).fetchone()["state"] == "handoff_processing"
        for task in [*first.handoffs.values(), *second.handoffs.values()]:
            task.cancel()
        await asyncio.gather(*first.handoffs.values(), *second.handoffs.values(), return_exceptions=True)
        max_actions.recover()
        assert core.db.execute("SELECT state FROM max_actions WHERE id=?", (row["id"],)).fetchone()["state"] == "handoff"
        await adapter.http.aclose()

    run(scenario())


def test_terminal_handoff_failure_keeps_diagnostics_and_durable_notices(monkeypatch):
    async def scenario():
        api = Api()
        adapter = make(api)
        adapter.admins = ["5"]
        core.ADAPTERS["max"] = adapter
        max_actions.enqueue(material(mid="expired-material"))
        row = core.db.execute("SELECT * FROM max_actions WHERE state='pending'").fetchone()
        core.db.execute("UPDATE max_actions SET state='processing',created=? WHERE id=?",
                        (__import__("time").time() - 8 * 86400, row["id"]))
        core.db.commit()
        worker = max_actions.Worker(adapter)
        await worker.process(dict(core.db.execute("SELECT * FROM max_actions WHERE id=?", (row["id"],)).fetchone()))
        handoff = core.db.execute("SELECT * FROM max_actions WHERE id=?", (row["id"],)).fetchone()

        def fail(_update):
            raise RuntimeError("still unavailable")

        monkeypatch.setattr(max_queue, "enqueue", fail)
        core.db.execute("UPDATE max_actions SET state='handoff_processing' WHERE id=?", (row["id"],))
        core.db.commit()
        await worker.process_handoff(dict(handoff))
        result = core.db.execute("SELECT state,payload FROM max_actions WHERE id=?", (row["id"],)).fetchone()
        assert tuple(result) == ("failed", "")
        failure = core.db.execute("SELECT * FROM max_handoff_failures").fetchone()
        assert failure["uid"] == "1" and failure["source_mid"] == "expired-material" and failure["attempts"] == 1
        notices = core.db.execute("SELECT kind,uid,state FROM max_handoff_notices ORDER BY id").fetchall()
        assert [tuple(r) for r in notices] == [("user", "1", "pending"), ("staff", "5", "pending")]
        await adapter.http.aclose()

    run(scenario())


def test_interrupted_notice_becomes_unknown_not_automatic_retry():
    core.db.execute("""INSERT INTO max_handoff_notices
                       (event_key,kind,uid,text,state,created) VALUES('e','user','1','x','sending',?)""",
                    (__import__("time").time(),))
    core.db.commit()
    max_actions.recover()
    row = core.db.execute("SELECT state,last_error FROM max_handoff_notices").fetchone()
    assert row["state"] == "unknown" and "результат неизвестен" in row["last_error"]


def test_handoff_backoff_preserves_workspace_material_order(monkeypatch):
    async def scenario():
        adapter = make(Api())
        now = __import__("time").time()
        payload = __import__("json").dumps({"message": {"_space": 1}, "_material_to_inbox": True})
        core.db.executemany("""INSERT INTO max_actions
            (event_key,uid,payload,state,created,next_try,space_id) VALUES(?,?,?,?,?,?,?)""", [
            ("space1-old", "1", payload, "handoff", now - 2, now + 3600, 1),
            ("space1-new", "2", payload, "handoff", now - 1, 0, 1),
            ("space2", "3", payload, "handoff", now, 0, 2),
        ])
        core.db.commit()
        worker = max_actions.Worker(adapter)
        gate = asyncio.Event()
        started = []

        async def slow(row):
            started.append(row["event_key"])
            await gate.wait()

        monkeypatch.setattr(worker, "process_handoff", slow)
        worker.schedule_handoffs()
        await asyncio.sleep(0)
        assert started == ["space2"]  # newer space1 material cannot overtake the older backoff row
        gate.set()
        await asyncio.gather(*worker.handoffs.values())
        await adapter.http.aclose()

    run(scenario())


def test_material_routing_failure_keeps_order_until_space_is_frozen(monkeypatch):
    async def scenario():
        adapter = make(Api())
        max_actions.enqueue(material(mid="route-fail"))
        max_actions.enqueue(event(1, "after-route-fail"))
        first = core.db.execute("SELECT * FROM max_actions ORDER BY id LIMIT 1").fetchone()
        core.db.execute("UPDATE max_actions SET state='processing' WHERE id=?", (first["id"],))
        core.db.commit()
        original_stamp = max_actions.spaces.stamp

        def fail(_update):
            raise RuntimeError("routing db unavailable")

        monkeypatch.setattr(max_actions.spaces, "stamp", fail)
        worker = max_actions.Worker(adapter)
        await worker.process(dict(first))
        kept = core.db.execute("SELECT state,payload,attempts,next_try FROM max_actions WHERE id=?", (first["id"],)).fetchone()
        assert kept["state"] == "pending" and kept["payload"] and kept["attempts"] == 1 and kept["next_try"] > 0
        worker.schedule()
        assert not worker.active  # later same-user command cannot pass before workspace is known

        monkeypatch.setattr(max_actions.spaces, "stamp", original_stamp)
        core.db.execute("UPDATE max_actions SET next_try=0,state='processing' WHERE id=?", (first["id"],))
        core.db.commit()
        await worker.process(dict(core.db.execute("SELECT * FROM max_actions WHERE id=?", (first["id"],)).fetchone()))
        assert core.db.execute("SELECT state FROM max_actions WHERE id=?", (first["id"],)).fetchone()["state"] == "handoff"
        worker.schedule()
        assert "1" in worker.active
        await asyncio.gather(*worker.active.values())
        await adapter.http.aclose()

    run(scenario())
