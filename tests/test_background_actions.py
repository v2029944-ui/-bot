import asyncio

import core
import max_actions
from conftest import run
from test_admin_menu_button import Api, make


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
        assert edit["text"] == "Изменить"
        assert all(not b["payload"].startswith("d:") for a in edit["attachments"] if a["type"] == "inline_keyboard" for row in a["payload"]["buttons"] for b in row)
        assert not api.of("DELETE")
        await adapter.http.aclose()
    run(scenario())
