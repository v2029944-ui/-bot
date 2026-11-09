"""Regression scenarios from the full audit: real scheduling, durable state and rollback contracts."""
import asyncio
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from conftest import FakeAdapter, run
from PIL import Image
from test_admin_menu_button import Api
from test_ops import run_entry
from test_ops import sandbox as sandbox

import command_context
import configuration
import core
import editor_rules
import max_
import max_actions
import ops
import schema
import spaces


def test_confirmation_binds_its_own_destinations(png, llm, monkeypatch):
    async def scenario():
        api = Api()
        ad = max_.Max("token", "-100", [], ["9"], ["1", "2"])
        await ad.http.aclose()
        ad.http = httpx.AsyncClient(base_url=max_.API, transport=httpx.MockTransport(api.handler))
        core.ADAPTERS["max"] = ad
        async def upload(path):
            return "photo-token"
        monkeypatch.setattr(ad, "_upload_image", upload)
        try:
            did = await core.submit(ad, "9", "Author", "Private caption", png)
            async def callback(uid, payload):
                await ad.on_callback({"callback_id": str(uid), "user": {"user_id": uid}, "payload": payload})
            await callback(1, f"d:ok:{did}:1")
            first = ad._confirmations()[("max", "1")][0]
            ad.channel = "-200"
            await callback(2, f"d:ok:{did}:1")
            second = ad._confirmations()[("max", "2")][0]
            await callback(1, first)
            assert not [c for c in api.of("POST") if "chat_id" in c[2]]
            assert core.get(did)["status"] == "pending"
            await callback(2, second)
            assert [c[2]["chat_id"] for c in api.of("POST") if "chat_id" in c[2]] == ["-200"]
        finally:
            await ad.http.aclose()
    run(scenario())


def test_cancelled_deferred_delivery_can_be_retried(tg, llm, png, monkeypatch):
    async def scenario():
        did = await core.submit(tg, "9", "Author", "Private caption", png)
        ops.enqueue(tg, "9", did)
        entered = asyncio.Event()
        original = tg.send_for_approval
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(tg, "send_for_approval", blocked)
        task = asyncio.create_task(ops.flush_deferred())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        row = core.db.execute("SELECT * FROM deferred").fetchone()
        assert row is not None and row["lease_until"] == 0
        monkeypatch.setattr(tg, "send_for_approval", original)
        assert await ops.flush_deferred() == 1
        assert core.db.execute("SELECT COUNT(*) FROM deferred").fetchone()[0] == 0
    run(scenario())


def test_startup_recovers_delivery_lease(tg, llm, png):
    did = run(core.submit(tg, "9", "Author", "Private caption", png))
    ops.enqueue(tg, "9", did)
    core.db.execute("UPDATE deferred SET lease_until=?", (time.time() + 600,))
    core.db.commit()
    core.startup()
    assert run(ops.flush_deferred()) == 1


def test_two_deferred_deliveries_share_one_durable_lease(tg, png, llm, monkeypatch):
    async def scenario():
        did = await core.submit(tg, "9", "Author", "Private caption", png)
        ops.enqueue(tg, "9", did)
        entered, release = asyncio.Event(), asyncio.Event()
        original = tg.send_for_approval
        async def blocked(*args, **kwargs):
            entered.set()
            await release.wait()
            await original(*args, **kwargs)
        monkeypatch.setattr(tg, "send_for_approval", blocked)
        first = asyncio.create_task(ops.flush_deferred())
        await asyncio.wait_for(entered.wait(), 2)
        assert await ops.flush_deferred() == 0
        release.set()
        assert await first == 1
        assert core.db.execute("SELECT COUNT(*) FROM deferred").fetchone()[0] == 0
    run(scenario())


@pytest.mark.parametrize("command,expected", [("/rule_del 1", ["Second"]), ("/rule_add Third", ["First", "Second", "Third"])])
def test_rule_mutation_is_not_repeated_after_command_cancellation(tg, monkeypatch, command, expected):
    async def scenario():
        core.rules_command(tg, "1", "/rule_add First")
        core.rules_command(tg, "1", "/rule_add Second")
        entered = asyncio.Event()
        original = tg.send_menu
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        async def handle(update):
            await core.on_text(tg, "1", update["message"]["body"]["text"])
        monkeypatch.setattr(tg, "send_menu", blocked)
        monkeypatch.setattr(tg, "handle", handle, raising=False)
        max_actions.enqueue({"update_type": "message_created", "message": {"sender": {"user_id": 1},
                             "body": {"mid": "one-command", "text": command}}})
        row = dict(core.db.execute("SELECT * FROM max_actions").fetchone())
        worker = max_actions.Worker(tg)
        task = asyncio.create_task(worker.process(row))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        monkeypatch.setattr(tg, "send_menu", original)
        await worker.process(dict(core.db.execute("SELECT * FROM max_actions").fetchone()))
        assert [r[0] for r in core.db.execute("SELECT text FROM rules ORDER BY id")] == expected
        assert command_context.EVENT.get() is None
    run(scenario())


def test_rule_mutation_and_result_rollback_together():
    token = command_context.EVENT.set("atomic-test")
    def fail():
        core.db.execute("INSERT INTO rules(text,created) VALUES('not committed',0)")
        raise RuntimeError("failure")
    try:
        with pytest.raises(RuntimeError):
            command_context.mutate("test", fail)
        assert not core.db.execute("SELECT * FROM rules").fetchall()
        assert not core.db.execute("SELECT * FROM command_results").fetchall()
    finally:
        command_context.EVENT.reset(token)


@pytest.mark.parametrize("action", ["prole:adm", "pdel"])
def test_people_confirmation_expires_after_cancel_and_reopen(tg, action):
    core._add_person(tg, "77", "Member", "member")
    _, rows = run(core.on_menu(tg, "1", f"m:{action}:77"))
    old = rows[0][0][1]
    run(core.on_menu(tg, "1", "m:pset:77"))
    assert "больше не действует" in run(core.on_menu(tg, "1", old))[0]
    assert core.role_of(tg, "77") == "member"
    _, rows = run(core.on_menu(tg, "1", f"m:{action}:77"))
    run(core.on_menu(tg, "1", rows[0][0][1]))
    assert core.role_of(tg, "77") == ("adm" if action.startswith("prole") else None)


def test_people_confirmation_cannot_apply_to_changed_membership(tg):
    core._add_person(tg, "77", "Member", "member")
    _, rows = run(core.on_menu(tg, "1", "m:prole:adm:77"))
    payload = rows[0][0][1]
    core._remove_person(tg, "77", "Member")
    core._add_person(tg, "77", "Re-added", "member")
    assert "больше не действует" in run(core.on_menu(tg, "1", payload))[0]
    assert "77" not in tg.admins


def test_people_pages_reach_every_member_and_obey_text_budget(tg):
    for n in range(35):
        core.db.execute("INSERT INTO people VALUES(?,?,?,?,?)", (tg.name, str(100+n), "😀" * 100, "member", n))
    core.db.commit()
    found = set()
    for page in range(3):
        text, rows = run(core.on_menu(tg, "1", f"m:people:{page}"))
        assert len(text.encode("utf-16-le")) // 2 < 4000
        found.update(p for row in rows for _, p in row if p.startswith("m:pset:"))
    assert found == {f"m:pset:{100+n}" for n in range(35)}


def test_same_guest_can_renew_expired_pending_request(tg):
    core.refresh_people(tg)
    run(core.unknown_user(tg, "77", "Guest"))
    core.db.execute("UPDATE requests SET created=?", (time.time() - core.REQ_TTL - 1,))
    core.db.commit()
    before = len(tg.sent("menu"))
    assert run(core.unknown_user(tg, "77", "Guest"))
    assert len(tg.sent("menu")) > before
    assert core.db.execute("SELECT created FROM requests").fetchone()[0] > time.time() - 2


@pytest.mark.parametrize("status", ["deleted", "expired"])
def test_closed_content_is_scrubbed_after_retention(tg, png, llm, status):
    did = run(core.submit(tg, "9", "Author", "Private caption", png))
    core._set(did, status=status)
    core.housekeeping()
    assert core.get(did)["source_text"] == "Private caption"
    core._set(did, closed_at=time.time() - 8 * 86400)
    core.housekeeping()
    d = core.get(did)
    assert d["status"] == status and d["cleaned"] == 2
    assert d["post"] == d["source_text"] == d["author_id"] == d["author_name"] == ""


def test_cleaned_history_is_not_rewritten(tg, png, llm):
    did = run(core.submit(tg, "9", "Author", "Private caption", png))
    run(core.on_action(tg, "1", "okyes", did, 1))
    queries = []
    core.db.set_trace_callback(queries.append)
    try:
        core.housekeeping()
    finally:
        core.db.set_trace_callback(None)
    assert not any(q.startswith("UPDATE drafts SET author_id") for q in queries)
    assert not any(q == "SELECT * FROM drafts" for q in queries)


def test_cleanup_backlog_progresses_in_bounded_batches():
    for _ in range(205):
        core.db.execute("INSERT INTO drafts(platform,status,post,created) VALUES('max','published','private',0)")
    core.db.commit()
    core.housekeeping()
    assert core.db.execute("SELECT COUNT(*) FROM drafts WHERE cleaned=2").fetchone()[0] == 200
    assert core.cleanup_pending()
    core.housekeeping()
    assert core.db.execute("SELECT COUNT(*) FROM drafts WHERE cleaned=2").fetchone()[0] == 205
    assert not core.cleanup_pending()


@pytest.mark.parametrize("name,value", [("REMIND_CHECK_SEC", "0"), ("HOUSEKEEPING_SEC", "-1"),
    ("RATE_LIMIT_PER_HOUR", "-1"), ("SHUTDOWN_DRAIN_SEC", "nan"), ("MAX_VIDEO_MB", "inf"), ("PROBATION_SEC", "oops")])
def test_bad_numeric_settings_are_rejected(name, value, monkeypatch):
    monkeypatch.setenv(name, value)
    assert name in ops.config_problem()


@pytest.mark.parametrize("value", ["--1", "0", "-1", "１２３", "9223372036854775808"])
def test_user_ids_are_ascii_positive_int64(value):
    assert not configuration.user_id(value)


def test_learning_context_is_bounded_and_duplicate_rules_are_not_added(llm):
    for n in range(100):
        core.db.execute("INSERT INTO rules(text,created) VALUES(?,0)", (f"Rule {n} " + "x" * 900,))
    core.db.commit()
    llm.reply = "НЕТ"
    run(core._learn("Use short sentences", "Post"))
    prompt = llm.calls[-1]["text"]
    assert len(prompt) < 14000 and "Rule 0 " not in prompt and "Rule 99 " in prompt
    assert editor_rules.add("same") == "✅ Правило добавлено."
    assert editor_rules.add("same") == "Это правило уже сохранено."
    assert editor_rules.add("x" * 1001).startswith("Пришлите")


def test_removed_message_does_not_stay_in_cleanup_queue(monkeypatch):
    async def scenario():
        ad = max_.Max("token", "-100", [], [], ["1"])
        await ad.http.aclose()
        ad.http = httpx.AsyncClient(base_url=max_.API, transport=httpx.MockTransport(lambda req: httpx.Response(404, json={"code":"message.not.found"})))
        core.db.execute("INSERT INTO max_draft_ui(mid,did,attempt) VALUES('gone',999,1)")
        core.db.commit()
        try:
            await ad.cleanup_stale_buttons()
            assert not core.db.execute("SELECT * FROM max_draft_ui WHERE mid='gone'").fetchone()
        finally:
            await ad.http.aclose()
    run(scenario())


def test_schema_does_not_hide_io_errors():
    class Broken:
        def execute(self, sql, *args):
            if sql.startswith("PRAGMA"):
                return []
            raise sqlite3.OperationalError("disk I/O error")
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        schema.add_columns(Broken(), "drafts", ("new_column TEXT",))


def load_updater():
    spec = importlib.util.spec_from_file_location("updater_under_test", Path(__file__).resolve().parents[1] / "updater.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_update_compatibility_requires_old_read_or_explicit_rollback_migration(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    old.joinpath("compatibility.json").write_text(json.dumps({"read_versions":[1], "write_version":1}))
    new.joinpath("compatibility.json").write_text(json.dumps({"read_versions":[1], "write_version":1}))
    updater = load_updater()
    assert updater.check_compatibility(old, new) == (1, 1)

    # Handoff lifecycle writes v2. A legacy updater requires strict bidirectional
    # readability and therefore rejects this manifest before switching Git code.
    new.joinpath("compatibility.json").write_text(json.dumps({"read_versions":[1,2], "write_version":2}))
    old_meta = updater.compatibility(old)
    new_meta = updater.compatibility(new)
    assert old_meta["write_version"] in new_meta["read_versions"]
    assert new_meta["write_version"] not in old_meta["read_versions"]
    # The new updater may cross this boundary only because its own executable
    # contains a concrete v2 -> v1 rollback migration.
    assert (2, 1) in updater.ROLLBACK_DATA_MIGRATIONS
    assert updater.check_compatibility(old, new) == (2, 1)

    # A future incompatible version without a migration remains blocked.
    new.joinpath("compatibility.json").write_text(json.dumps({"read_versions":[1,2,3], "write_version":3}))
    with pytest.raises(ValueError, match="пересоберите Docker"):
        updater.check_compatibility(old, new)

    new.joinpath("compatibility.json").unlink()
    with pytest.raises(ValueError):
        updater.check_compatibility(old, new)


def test_v2_rollback_migration_converts_handoff_states(tmp_path, monkeypatch):
    updater = load_updater()
    monkeypatch.setattr(updater, "DATA", tmp_path)
    db = sqlite3.connect(tmp_path / "drafts.db")
    db.execute("CREATE TABLE max_actions(id INTEGER PRIMARY KEY,state TEXT,payload TEXT)")
    db.executemany("INSERT INTO max_actions(id,state,payload) VALUES(?,?,?)", [
        (1, "handoff", '{"message":{"_space":2},"_material_to_inbox":true}'),
        (2, "handoff_processing", '{"message":{"_space":2},"_material_to_inbox":true}'),
        (3, "pending", '{}'),
    ])
    db.commit()
    db.close()

    updater.rollback_data(2, 1)

    db = sqlite3.connect(tmp_path / "drafts.db")
    states = [row[0] for row in db.execute("SELECT state FROM max_actions ORDER BY id")]
    payloads = [row[0] for row in db.execute("SELECT payload FROM max_actions ORDER BY id")]
    db.close()
    assert states == ["pending", "pending", "pending"]
    assert '"_space":2' in payloads[0] and '"_material_to_inbox":true' in payloads[0]


def test_repository_declares_handoff_data_version_two():
    meta = json.loads((Path(__file__).resolve().parents[1] / "compatibility.json").read_text())
    assert meta == {"read_versions": [1, 2], "write_version": 2}


def test_repository_locks_match_direct_and_runtime_dependencies():
    load_updater().validate_requirements(Path(__file__).resolve().parents[1])


def test_update_refuses_outdated_lock(tmp_path):
    (tmp_path / "requirements.txt").write_text("pillow==13.0.0\n")
    (tmp_path / "requirements.lock").write_text("pillow==12.1.1\n")
    with pytest.raises(ValueError, match="расходится"):
        load_updater().validate_requirements(tmp_path)


def test_failed_pip_never_starts_main(sandbox):
    code = sandbox.app / "code"
    code.mkdir()
    code.joinpath("main.py").write_text("")
    code.joinpath("requirements.txt").write_text("bad\n")
    sandbox.bin.joinpath("pip").write_text("#!/bin/sh\nexit 1\n")
    result, calls = run_entry(sandbox, [0], {"GITHUB_REPO":"file:///unused", "CODE_DIR":str(code)})
    assert result.returncode != 0 and "main.py" not in calls


def test_failed_workspace_can_be_retried_without_duplicate_registration(tmp_path, monkeypatch, tg):
    ad = FakeAdapter("max", ["1"], admins=["1"])
    ad.channel, tg.channel = "-100", "-200"
    core.ADAPTERS["max"] = ad
    core.db.execute("CREATE TABLE IF NOT EXISTS workspaces (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,owner TEXT UNIQUE,max_channel TEXT,tg_channel TEXT)")
    schema.add_columns(core.db, "workspaces", ("state TEXT DEFAULT 'ready'", "error TEXT DEFAULT ''"))
    core.db.execute("DELETE FROM workspaces")
    core.db.commit()
    monkeypatch.setenv("TG_TOKEN", "123456:ABCDEF")
    monkeypatch.setattr(spaces, "ROOT", SimpleNamespace(db=core.db,folder=tmp_path,adapters={"max":ad,"tg":tg},name="Основное",owner=None))
    monkeypatch.setattr(spaces, "global_admin", lambda uid: True)
    spaces.WAITING_CREATE.add("1")
    def failed(sid):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(spaces, "get", failed)
    try:
        assert run(spaces.create_input(ad, "1", "Company\n77\n-101\n-201"))
        assert core.db.execute("SELECT state FROM workspaces").fetchone()[0] == "failed"
        monkeypatch.setattr(spaces, "get", lambda sid: SimpleNamespace(id=sid,memory={"ADAPTERS":{"max":ad}}))
        assert run(spaces.create_input(ad, "1", "Company\n77\n-101\n-201"))
        assert core.db.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 1
    finally:
        core.db.execute("DELETE FROM workspaces")
        core.db.commit()
        spaces.WAITING_CREATE.clear()


def test_disallowed_image_format_is_rejected_before_decoding():
    image = Image.new("RGB", (4,4), "white")
    buf = io.BytesIO()
    image.save(buf, "PPM")
    with pytest.raises(core.BadImage):
        core.save_photo(buf.getvalue())


def test_fake_members_do_not_implicitly_become_administrators():
    ad = FakeAdapter("max", ["9"])
    assert not core.is_admin(ad, "9")


def test_roles_survive_a_new_process_after_upgrade(tmp_path):
    folder = Path(__file__).resolve().parents[1]
    env = {**os.environ, "DATA_DIR": str(tmp_path / "persistent"), "PYTHONPATH": str(folder)}
    setup = """import asyncio, core
from max_ import Max
ad = Max('token', '-100', [], [], ['1'])
core.ADAPTERS['max'] = ad
core._add_person(ad, '77', 'Member', 'member')
core._add_person(ad, '88', 'Admin', 'adm')
asyncio.run(ad.http.aclose())
"""
    check = """import asyncio, core
from max_ import Max
ad = Max('token', '-100', [], [], ['1'])
assert core.role_of(ad, '77') == 'member'
assert core.role_of(ad, '88') == 'adm'
assert set(ad.admins) == {'1', '88'}
assert ad.allowed == {'1', '77', '88'}
asyncio.run(ad.http.aclose())
"""
    subprocess.run([sys.executable, "-c", setup], cwd=folder, env=env, check=True, capture_output=True, timeout=20)
    subprocess.run([sys.executable, "-c", check], cwd=folder, env=env, check=True, capture_output=True, timeout=20)


def test_reading_legacy_roles_does_not_destroy_original_assignments(tg):
    for uid, role in [("77", "emp"), ("88", "apr")]:
        core.db.execute("INSERT INTO people VALUES(?,?,?,?,0)", (tg.name, uid, uid, role))
    core.db.commit()
    core.refresh_people(tg)
    assert core.role_of(tg, "77") == core.role_of(tg, "88") == "member"
    assert [row[0] for row in core.db.execute("SELECT role FROM people ORDER BY uid")] == ["emp", "apr"]
