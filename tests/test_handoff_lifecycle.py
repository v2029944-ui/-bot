import sqlite3
from conftest import FakeAdapter, run

import core
import max_actions
import ops
import updater


def test_updater_rollback_converts_new_handoff_states_to_legacy_pending(monkeypatch, tmp_path):
    db_path = tmp_path / "drafts.db"
    db = sqlite3.connect(db_path)
    db.execute("""CREATE TABLE max_actions (
        id INTEGER PRIMARY KEY, state TEXT, payload TEXT)""")
    db.executemany("INSERT INTO max_actions(id,state,payload) VALUES(?,?,?)", [
        (1, "handoff", '{"message":{"_space":2},"_material_to_inbox":true}'),
        (2, "handoff_processing", '{"message":{"_space":3},"_material_to_inbox":true}'),
        (3, "pending", '{}'),
    ])
    db.commit()
    db.close()

    monkeypatch.setattr(updater, "DATA", tmp_path)
    updater._write("update_state.json", {"prev": "old-sha", "platform": "max", "uid": "1"})

    def git(*args, **kwargs):
        if args[:2] == ("reset", "--hard"):
            return 0, "ok"
        if args[:2] == ("rev-parse", "--short"):
            return 0, "abc123"
        return 0, "ok"

    monkeypatch.setattr(updater, "git", git)
    updater.rollback()

    db = sqlite3.connect(db_path)
    states = [row[0] for row in db.execute("SELECT state FROM max_actions ORDER BY id")]
    db.close()
    assert states == ["pending", "pending", "pending"]


def test_status_shows_handoff_failures_and_notice_problems(monkeypatch):
    async def scenario():
        adapter = FakeAdapter("max", admins=("5",))
        adapter.approvers = ["5"]
        core.ADAPTERS["max"] = adapter

        async def version():
            return "test"

        async def complete(*args, **kwargs):
            return "ОК"

        monkeypatch.setattr(ops, "version", version)
        monkeypatch.setattr(core, "_complete", complete)

        now = __import__("time").time()
        core.db.execute("""INSERT INTO max_handoff_failures
                           (event_key,uid,source_mid,space_id,created,failed_at,attempts,reason)
                           VALUES('f1','9','m9',0,?,?,?,?)""",
                        (now - 100, now, 7, "sqlite unavailable"))
        core.db.execute("""INSERT INTO max_handoff_notices
                           (event_key,kind,uid,space_id,text,state,created,last_error)
                           VALUES('f1','user','9',0,'warn','unknown',?,'result unknown')""", (now,))
        core.db.commit()

        text = await core.status_text(adapter, include_global=False)
        assert "Материалов потеряно до очереди обработки: 1" in text
        assert "пользователь 9" in text and "mid m9" in text
        assert "Не завершены предупреждения" in text

    run(scenario())


def test_notice_failure_classification_distinguishes_safe_retry_and_unknown():
    import httpx

    worker = max_actions.Worker(FakeAdapter("max"))
    now = __import__("time").time()
    core.db.execute("""INSERT INTO max_handoff_notices
                       (id,event_key,kind,uid,text,state,created)
                       VALUES(1,'e','user','1','x','sending',?)""", (now,))
    core.db.commit()
    request = httpx.Request("POST", "https://platform-api2.max.ru/messages")

    worker._notice_failed(dict(core.db.execute("SELECT * FROM max_handoff_notices WHERE id=1").fetchone()),
                          httpx.ConnectError("connect failed", request=request))
    row = core.db.execute("SELECT state,next_try FROM max_handoff_notices WHERE id=1").fetchone()
    assert row["state"] == "pending" and row["next_try"] > now

    core.db.execute("UPDATE max_handoff_notices SET state='sending',attempts=0,next_try=0 WHERE id=1")
    core.db.commit()
    worker._notice_failed(dict(core.db.execute("SELECT * FROM max_handoff_notices WHERE id=1").fetchone()),
                          httpx.ReadTimeout("reply lost", request=request))
    row = core.db.execute("SELECT state,next_try FROM max_handoff_notices WHERE id=1").fetchone()
    assert row["state"] == "unknown" and row["next_try"] == 0
