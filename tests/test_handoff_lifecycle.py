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


def test_staff_notice_rechecks_workspace_rights_before_send(monkeypatch):
    async def scenario():
        import max_
        import spaces
        import tg

        root_max = max_.Max("token", "-100", ["1"], [], ["1"])
        root_tg = tg.Telegram("123456:ABCDEF", "-200")
        core.ADAPTERS.update(max=root_max, tg=root_tg)
        monkeypatch.setenv("TG_TOKEN", "123456:ABCDEF")
        spaces.install()
        root = spaces.ROOT
        root.db.execute("INSERT INTO workspaces(name,owner,max_channel,tg_channel) VALUES('B','22','-102',NULL)")
        sid = root.db.execute("SELECT last_insert_rowid()").fetchone()[0]
        root.db.commit()
        space = spaces.get(sid)
        with spaces.enter(space):
            adapter = core.ADAPTERS["max"]
            core._add_person(adapter, "55", "Admin", "adm")
            assert "55" in core.staff(adapter)

        root.db.execute("""INSERT INTO max_handoff_notices
                           (event_key,kind,uid,space_id,text,state,created)
                           VALUES('e','staff','55',?,'secret','sending',?)""",
                        (sid, __import__("time").time()))
        root.db.commit()

        with spaces.enter(space):
            adapter = core.ADAPTERS["max"]
            core._remove_person(adapter, "55", "Admin")
            assert "55" not in core.staff(adapter)

        sent = []

        async def notify(self, uid, text):
            sent.append((str(uid), text))

        monkeypatch.setattr(max_.Max, "notify", notify)
        row = dict(root.db.execute("SELECT * FROM max_handoff_notices WHERE event_key='e'").fetchone())
        await max_actions.Worker(root_max).process_notice(row)

        result = root.db.execute("SELECT state,last_error FROM max_handoff_notices WHERE event_key='e'").fetchone()
        assert result["state"] == "skipped"
        assert "отозваны" in result["last_error"]
        assert sent == []

    run(scenario())


def test_terminal_handoff_falls_back_only_to_global_admins_when_space_unavailable(monkeypatch):
    async def scenario():
        import max_
        import spaces
        import tg

        root_max = max_.Max("token", "-100", ["1", "2"], [], ["1", "2"])
        root_tg = tg.Telegram("123456:ABCDEF", "-200")
        core.ADAPTERS.update(max=root_max, tg=root_tg)
        monkeypatch.setenv("TG_TOKEN", "123456:ABCDEF")
        spaces.install()
        root = spaces.ROOT
        root_max.global_admins = ["1", "2"]

        root.db.execute("INSERT INTO workspaces(name,owner,max_channel,tg_channel) VALUES('B','22','-102',NULL)")
        sid = root.db.execute("SELECT last_insert_rowid()").fetchone()[0]
        root.db.commit()

        original_get = spaces.get

        def broken_get(value):
            if value == sid:
                raise RuntimeError("workspace db unavailable")
            return original_get(value)

        monkeypatch.setattr(spaces, "get", broken_get)

        now = __import__("time").time()
        row = {
            "id": 1, "event_key": "fallback", "uid": "9", "space_id": sid,
            "created": now - 8 * 86400, "attempts": 6,
        }
        root.db.execute("""INSERT INTO max_actions
            (id,event_key,uid,payload,state,created,attempts,next_try,space_id)
            VALUES(1,'fallback','9','{}','handoff_processing',?,6,0,?)""", (row["created"], sid))
        root.db.commit()

        max_actions.Worker(root_max)._terminal_handoff(
            row, {"message": {"_space": sid, "body": {"mid": "m-fallback"}}}, 7, now, "boom")

        notices = root.db.execute(
            "SELECT kind,uid,space_id,state FROM max_handoff_notices WHERE event_key='fallback' ORDER BY kind,uid"
        ).fetchall()
        assert [tuple(r) for r in notices] == [
            ("global_staff", "1", sid, "pending"),
            ("global_staff", "2", sid, "pending"),
            ("user", "9", sid, "pending"),
        ]

        # A non-global workspace administrator must never receive the fallback.
        assert not root.db.execute(
            "SELECT 1 FROM max_handoff_notices WHERE event_key='fallback' AND uid='22'"
        ).fetchone()

    run(scenario())
