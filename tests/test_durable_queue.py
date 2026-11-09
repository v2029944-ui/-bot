import asyncio
import sqlite3
import time
from pathlib import Path

import pytest
from conftest import run

import core
import max_
import max_actions
import max_queue


def event(mid="message-1", count=1):
    return {"update_type": "message_created", "timestamp": 1,
            "message": {"sender": {"user_id": 9, "first_name": "Анна"},
                        "recipient": {"chat_type": "dialog"},
                        "body": {"mid": mid, "text": "Установили новый насос",
                                 "attachments": [{"type": "image", "payload": {"url": "https://example.com/photo"}}] * count}}}


@pytest.fixture
def adapter(monkeypatch, png):
    ad = max_.Max("token", "-1", ["5"], ["9"], admins=["5"])
    ad.downloads, ad.approvals, ad.notices = [], [], []

    async def download(att, limit, kind):
        ad.downloads.append(kind)
        return png

    async def approval(uid, did, *args, **kwargs):
        ad.approvals.append(did)

    async def notify(uid, text):
        ad.notices.append((uid, text))

    async def menu(uid, text, rows, **kwargs):
        ad.notices.append((uid, text))

    monkeypatch.setattr(ad, "_download", download)
    monkeypatch.setattr(ad, "send_for_approval", approval)
    monkeypatch.setattr(ad, "notify", notify)
    monkeypatch.setattr(ad, "send_menu", menu)
    core.ADAPTERS["max"] = ad
    yield ad
    run(ad.http.aclose())


def inbox(key):
    return dict(core.db.execute("SELECT * FROM max_inbox WHERE event_key=?", (key,)).fetchone())


def test_durable_enqueue_before_marker_and_duplicate_delivery(adapter, llm, monkeypatch):
    calls = []

    async def call(method, path, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"marker": 42, "updates": [event()]}
        assert kwargs["params"]["marker"] == 42
        with sqlite3.connect(core.DATA / "drafts.db") as reader:
            # The batch may be committed while the material is still in max_actions:
            # this queue is durable and preserves per-user ordering before max_inbox routing.
            assert reader.execute("SELECT COUNT(*) FROM max_actions WHERE state='pending'").fetchone()[0] == 1
            assert reader.execute("SELECT COUNT(*) FROM max_inbox WHERE state='pending'").fetchone()[0] == 0
        assert not adapter.downloads
        raise asyncio.CancelledError

    monkeypatch.setattr(adapter, "call", call)
    with pytest.raises(asyncio.CancelledError):
        run(adapter.run())
    action = core.db.execute("SELECT * FROM max_actions WHERE state='pending'").fetchone()
    core.db.execute("UPDATE max_actions SET state='processing' WHERE id=?", (action["id"],))
    core.db.commit()
    actions = max_actions.Worker(adapter)
    run(actions.process(dict(action)))
    handoff = core.db.execute("SELECT * FROM max_actions WHERE id=?", (action["id"],)).fetchone()
    assert handoff["state"] == "handoff"
    core.db.execute("UPDATE max_actions SET state='handoff_processing' WHERE id=?", (action["id"],))
    core.db.commit()
    run(actions.process_handoff(dict(handoff)))
    assert core.db.execute("SELECT COUNT(*) FROM max_inbox WHERE state='pending'").fetchone()[0] == 1
    repeated = event()
    repeated["timestamp"] = 999
    key = max_queue.enqueue(repeated)
    worker = max_queue.Worker(adapter)
    assert run(worker.process_once())
    assert inbox(key)["state"] == "done" and inbox(key)["payload"] == ""
    max_queue.enqueue(event())
    assert not run(worker.process_once())
    assert len(llm.calls) == 1 and len(adapter.approvals) == 1
    assert core.db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1


@pytest.mark.parametrize("count", [1, 2])
def test_retry_uses_downloaded_cache_and_cleans_it_after_success(adapter, llm, count):
    key = max_queue.enqueue(event(count=count))
    worker = max_queue.Worker(adapter)
    llm.reply = RuntimeError("Временно недоступно")
    now = time.time()
    assert run(worker.process_once(now))
    assert inbox(key)["state"] == "pending"
    assert len(list(max_queue.cache_dir(key).iterdir())) == count
    assert not run(worker.process_once(now + 29))
    llm.reply = "Готовый пост"
    assert run(worker.process_once(now + 31))
    assert len(adapter.downloads) == count
    assert not max_queue.cache_dir(key).exists()
    assert core.db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1


@pytest.mark.parametrize("count", [1, 2])
def test_failed_approval_keeps_draft_and_retry_does_not_regenerate(adapter, llm, monkeypatch, count):
    original = adapter.send_for_approval

    async def fail(*args, **kwargs):
        raise RuntimeError("MAX недоступен")

    monkeypatch.setattr(adapter, "send_for_approval", fail)
    key = max_queue.enqueue(event(count=count))
    worker = max_queue.Worker(adapter)
    now = time.time()
    run(worker.process_once(now))
    d = dict(core.db.execute("SELECT * FROM drafts").fetchone())
    assert not d["last_sent"] and d["source_key"] == key
    assert all(Path(p).exists() for p in core.draft_files(d))
    monkeypatch.setattr(adapter, "send_for_approval", original)
    run(worker.process_once(now + 31))
    assert len(llm.calls) == 1 and adapter.approvals == [d["id"]]
    assert inbox(key)["state"] == "done"


def test_restart_after_draft_commit_does_not_create_another(adapter, llm, png):
    key = max_queue.enqueue(event())
    did = run(core.submit(adapter, "9", "Анна", "Установили новый насос", png, source_key=key))
    core.db.execute("UPDATE max_inbox SET state='processing' WHERE event_key=?", (key,))
    core.db.commit()
    max_queue.recover()
    assert inbox(key)["state"] == "pending"
    run(max_queue.Worker(adapter).process_once())
    assert len(llm.calls) == 1 and adapter.approvals == [did] and not adapter.downloads
    assert core.db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1


def test_failure_limit_admin_retry_and_seven_day_cleanup(adapter, llm):
    key = max_queue.enqueue(event())
    llm.reply = RuntimeError("Недоступно")
    worker = max_queue.Worker(adapter)
    now = time.time()
    for attempt in range(8):
        run(worker.process_once(now + attempt * 4000))
    assert inbox(key)["state"] == "failed"
    assert any("8 попыток" in text for _, text in adapter.notices)
    assert "только администратору" in run(core.on_menu(adapter, "9", f"m:inretry:{key}"))[0] or "нет прав" in run(core.on_menu(adapter, "9", f"m:inretry:{key}"))[0]
    run(core.on_menu(adapter, "5", f"m:inretry:{key}"))
    assert inbox(key)["state"] == "pending" and inbox(key)["attempts"] == 0
    run(worker.process_once(now + 8 * 86400))
    assert inbox(key)["state"] == "expired" and not inbox(key)["payload"]
    failure = core.db.execute("SELECT * FROM max_handoff_failures WHERE event_key=?", (key,)).fetchone()
    assert failure["uid"] == "9" and failure["source_mid"] == "message-1"
    assert "max_inbox" in failure["reason"]
    notices = core.db.execute(
        "SELECT kind,uid,state FROM max_handoff_notices WHERE event_key=? ORDER BY kind,uid", (key,)).fetchall()
    assert [tuple(row) for row in notices] == [("staff", "5", "pending"), ("user", "9", "pending")]
    assert not max_queue.cache_dir(key).exists()


def test_duplicate_direct_submission_resumes_same_draft(adapter, llm, png):
    first = run(core.submit(adapter, "9", "Анна", "Работа", png, source_key="same"))
    second = run(core.submit(adapter, "9", "Анна", "Работа", png, source_key="same"))
    assert first == second and len(llm.calls) == 1


def test_retry_does_not_spend_employee_rate_limit_twice(adapter, llm, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_HOUR", "1")
    key = max_queue.enqueue(event())
    worker = max_queue.Worker(adapter)
    now = time.time()
    llm.reply = RuntimeError("Временная ошибка")
    run(worker.process_once(now))
    llm.reply = "Готово"
    run(worker.process_once(now + 31))
    assert inbox(key)["state"] == "done"
    second = max_queue.enqueue(event(mid="second"))
    run(worker.process_once(now + 32))
    assert inbox(second)["state"] == "pending" and inbox(second)["next_try"] == now + 32 + 3600
    assert inbox(second)["attempts"] == 0


def test_worker_restart_recovers_interrupted_job(adapter, monkeypatch):
    key = max_queue.enqueue(event())
    core.db.execute("UPDATE max_inbox SET state='processing' WHERE event_key=?", (key,))
    core.db.commit()
    worker = max_queue.Worker(adapter)

    async def process():
        assert inbox(key)["state"] == "pending"
        monkeypatch.setattr(core, "SHUTTING_DOWN", True)
        return True

    monkeypatch.setattr(worker, "process_once", process)
    run(worker.run())


def test_polling_persists_callback_before_following_material(adapter, monkeypatch):
    callback = {"update_type": "message_callback", "timestamp": 1,
                "callback": {"callback_id": "switch-first", "user": {"user_id": 9}, "payload": "m:menu"}}
    material = event(mid="photo-after-switch")
    calls = 0

    async def call(method, path, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"marker": 55, "updates": [callback, material]}
        rows = core.db.execute("SELECT payload FROM max_actions ORDER BY id").fetchall()
        assert len(rows) == 2
        assert "switch-first" in rows[0]["payload"]
        assert "photo-after-switch" in rows[1]["payload"]
        assert core.db.execute("SELECT COUNT(*) FROM max_inbox").fetchone()[0] == 0
        raise asyncio.CancelledError

    monkeypatch.setattr(adapter, "call", call)
    with pytest.raises(asyncio.CancelledError):
        run(adapter.run())


def test_expiry_keeps_payload_if_durable_failure_staging_fails(adapter, monkeypatch):
    key = max_queue.enqueue(event(mid="expiry-atomic"))
    original = inbox(key)["payload"]
    core.db.execute("UPDATE max_inbox SET created=?,state='failed' WHERE event_key=?",
                    (time.time() - 8 * 86400, key))
    core.db.commit()

    def fail_stage(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(max_actions, "stage_material_failure", fail_stage)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        run(max_queue.Worker(adapter).process_once())
    row = inbox(key)
    assert row["state"] == "failed" and row["payload"] == original
    assert not core.db.execute("SELECT 1 FROM max_handoff_failures WHERE event_key=?", (key,)).fetchone()
