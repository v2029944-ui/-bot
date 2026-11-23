"""Mandatory administrator review, including state persisted by the author workflow."""
import asyncio
import json
import time

import pytest
from conftest import FakeAdapter, run
from test_admin_menu_button import Api, make

import core
import ops


def team():
    ad = FakeAdapter("max", ["1"], admins=["1"])
    ad.base_employees = ["9", "8"]
    core.ADAPTERS["max"] = ad
    core.refresh_people(ad)
    return ad


def test_materials_from_multiple_members_go_only_to_administrator(llm, png):
    ad = team()
    first = run(core.submit(ad, "9", "First", "First material", png))
    second = run(core.submit(ad, "8", "Second", "Second material", png))
    assert [(o[1], o[2]) for o in ad.sent("approval")] == [("1", first), ("1", second)]
    assert core.get(first)["author_id"] == "9" and core.get(second)["author_id"] == "8"
    assert run(core.on_action(ad, "1", "okyes", first, 1))[0]
    assert core.get(first)["status"] == "published"
    assert core.get(second)["status"] == "pending"


@pytest.mark.parametrize("action", ["show", "edit", "regen", "redoc", "len_short", "choose_0",
                                     "del", "delyes", "pubstatus", "seen_max", "ok", "okyes", "okforce"])
def test_author_cannot_use_old_draft_buttons(action, llm, png):
    ad = team()
    did = run(core.submit(ad, "9", "Author", "Material", png))
    before = core.get(did)
    ad.out.clear()
    ok, text = run(core.on_action(ad, "9", action, did, 1))
    assert not ok and "нет прав" in text
    assert core.get(did) == before and not ad.out


def test_restored_author_inputs_cannot_edit_reject_or_generate(llm, png):
    ad = team()
    did = run(core.submit(ad, "9", "Author", "Material", png))
    before, calls = core.get(did), len(llm.calls)
    for state in (core.WAITING_EDIT, core.WAITING, core.WAITING_REJECT):
        state[("max", "9")] = (did, 1)
        assert run(core.on_text(ad, "9", "Changed by former author"))
        assert ("max", "9") not in state
    assert core.get(did) == before and len(llm.calls) == calls


def test_member_has_no_draft_menu_or_pending_list(llm, png):
    ad = team()
    did = run(core.submit(ad, "9", "Author", "Material", png))
    assert "m:pending" not in [p for row in core.menu_for(ad, "9") for _, p in row]
    text, rows = run(core.on_menu(ad, "9", "m:pending"))
    assert "администратор" in text and not rows
    assert not any(p.startswith("d:") for row in core.pending_view(ad, uid="9")[1] for _, p in row)
    assert core.get(did)["status"] == "pending"


def test_old_deferred_author_delivery_and_reminders_go_to_admin(llm, png, monkeypatch):
    ad = team()
    monkeypatch.setenv("NOTIFY_HOURS", "00:00-23:59")
    monkeypatch.setenv("NOTIFY_DAYS", "1,2,3,4,5,6,7")
    did = run(core.submit(ad, "9", "Author", "Material", png))
    ops.enqueue(ad, "9", did)
    ad.out.clear()
    assert run(ops.flush_deferred()) == 1
    assert [o[1] for o in ad.sent("approval")] == ["1"]
    core._set(did, last_sent=time.time() - 6.5 * 86400)
    ad.out.clear()
    assert run(core.remind_once()) == 1
    assert run(core.warn_expiring_once()) == 1
    assert {o[1] for o in ad.sent("menu")} == {"1"}


def test_startup_requeues_existing_pending_draft_for_administrator(llm, png):
    ad = team()
    did = run(core.submit(ad, "9", "Author", "Material", png))
    core.startup()
    core.startup()
    assert [tuple(row) for row in core.db.execute("SELECT uid,did FROM deferred")] == [("1", did)]
    ad.out.clear()
    assert run(ops.flush_deferred()) == 1
    assert [o[1] for o in ad.sent("approval")] == ["1"]


@pytest.mark.parametrize("options,slots,want_queued", [([], [-1], 0),
    ([{"post": "First"}, {"post": "Second"}], [0, 1], 0),
    ([{"post": "First"}, {"post": "Second"}], [0], 1)])
def test_startup_preserves_delivered_previews_and_only_recovers_incomplete_sets(options, slots, want_queued, llm, png):
    ad = team()
    did = run(core.submit(ad, "9", "Author", "Material", png))
    ad.supports_approval_resume = True
    core._set(did, candidates=json.dumps(options), last_sent=time.time() - 86400)
    before = core.get(did)["last_sent"]
    for slot in slots:
        core.db.execute("INSERT INTO max_approval_delivery VALUES(?,?,?,?,?,?)", (did, 1, "1", slot, f"mid{slot}", time.time()))
    core.db.commit()
    core.startup()
    core.startup()
    assert core.db.execute("SELECT COUNT(*) FROM deferred").fetchone()[0] == want_queued
    if not want_queued:
        assert run(ops.flush_deferred()) == 0
        assert core.get(did)["last_sent"] == before


def test_revoked_admin_and_foreign_adapter_cannot_manage_draft(llm, png):
    ad = team()
    did = run(core.submit(ad, "9", "Author", "Material", png))
    foreign = FakeAdapter("max", ["1"], admins=["1"])
    assert not core.can_manage_draft(foreign, "1", core.get(did))
    ad.admins.clear()
    assert not core.can_manage_draft(ad, "1", core.get(did))
    assert core.draft_recipients(core.get(did), ad) == []
    assert "нет прав" in run(core._publish(ad, "1", did, 1))[1]


@pytest.mark.parametrize("queue_state,want_queued", [("pending", 0), ("processing", 0), ("failed", 1), (None, 1)])
def test_startup_only_leaves_actively_queued_source_drafts_to_material_worker(queue_state, want_queued, llm, png):
    ad = team()
    did = run(core.submit(ad, "9", "Author", "Material", png, source_key="material-job"))
    core._set(did, last_sent=0)
    if queue_state:
        core.db.execute("INSERT INTO max_inbox(event_key,payload,state,created) VALUES(?,?,?,?)",
                        ("material-job", "{}", queue_state, time.time()))
        core.db.commit()
    core.startup()
    assert core.db.execute("SELECT COUNT(*) FROM deferred").fetchone()[0] == want_queued
    assert run(core.resume_submission(ad, "material-job")) == did


def test_legacy_telegram_draft_reaches_admin_in_max_only_space(llm, png):
    ad = team()
    did = run(core.submit(ad, "9", "Author", "Material", png))
    core._set(did, platform="tg")
    assert core._approval_adapter(core.get(did), None) is ad
    core.startup()
    assert [tuple(r) for r in core.db.execute("SELECT uid,did FROM deferred")] == [("1", did)]


def test_failed_admin_delivery_is_retried_without_resending_to_successful_admin(llm, png, monkeypatch):
    ad = team()
    core._add_person(ad, "2", "Second administrator", "adm")
    original = ad.send_for_approval

    async def fail_first(uid, *args, **kwargs):
        if uid == "1":
            raise RuntimeError("Temporarily offline")
        await original(uid, *args, **kwargs)

    monkeypatch.setattr(ad, "send_for_approval", fail_first)
    did = run(core.submit(ad, "9", "Author", "Material", png))
    assert [o[1] for o in ad.sent("approval")] == ["2"]
    assert [tuple(r) for r in core.db.execute("SELECT uid,did FROM deferred")] == [("1", did)]
    monkeypatch.setattr(ad, "send_for_approval", original)
    ad.out.clear()
    assert run(ops.flush_deferred()) == 1
    assert [o[1] for o in ad.sent("approval")] == ["1"]


@pytest.mark.parametrize("notice", ["reminder", "expiry"])
def test_revoked_admin_is_skipped_between_notice_recipients(notice, llm, png, monkeypatch):
    ad = team()
    core._add_person(ad, "2", "Second administrator", "adm")
    monkeypatch.setenv("NOTIFY_HOURS", "0-24")
    monkeypatch.setenv("NOTIFY_DAYS", "1-7")
    did = run(core.submit(ad, "9", "Author", "Material", png))
    core._set(did, last_sent=time.time() - 6.5 * 86400)
    original = ad.send_menu

    async def demote_after_first(uid, *args, **kwargs):
        await original(uid, *args, **kwargs)
        if uid == "1":
            core._add_person(ad, "2", "Former administrator", "member")

    monkeypatch.setattr(ad, "send_menu", demote_after_first)
    ad.out.clear()
    run(core.remind_once() if notice == "reminder" else core.warn_expiring_once())
    assert [o[1] for o in ad.sent("menu")] == ["1"]


@pytest.mark.parametrize("notice", ["reminder", "expiry"])
@pytest.mark.parametrize("wait_at", ["lock", "rate"])
def test_real_max_notice_rechecks_admin_after_waiting(notice, wait_at, llm, png, monkeypatch):
    ad = team()
    did = run(core.submit(ad, "9", "Author", "Material", png))
    core._set(did, last_sent=time.time() - 6.5 * 86400)
    monkeypatch.setenv("NOTIFY_HOURS", "0-24")
    monkeypatch.setenv("NOTIFY_DAYS", "1-7")

    async def scenario():
        api = Api()
        real = make(api)
        core.ADAPTERS["max"] = real
        core._add_person(real, "2", "Second administrator", "adm")
        entered, release = asyncio.Event(), asyncio.Event()

        class Gate:
            async def wait(self):
                entered.set()
                await release.wait()

            async def __aenter__(self):
                await self.wait()

            async def __aexit__(self, *args):
                pass

        gate = Gate()
        if wait_at == "lock":
            real._send_locks["2"] = gate
        else:
            real._rates().messages["user_id:2"] = gate
        try:
            task = asyncio.create_task(core.remind_once() if notice == "reminder" else core.warn_expiring_once())
            await asyncio.wait_for(entered.wait(), 2)
            core._add_person(real, "2", "Former administrator", "member")
            release.set()
            await task
            assert [c[2]["user_id"] for c in api.of("POST")] == ["1"]
        finally:
            release.set()
            await real.http.aclose()

    run(scenario())


def test_delivered_comparison_with_oversized_option_is_not_requeued(llm, png, monkeypatch):
    async def scenario():
        api = Api()
        ad = make(api)
        core.ADAPTERS["max"] = ad

        async def upload(path):
            return "photo-token"

        monkeypatch.setattr(ad, "_upload_image", upload)
        try:
            did = await core.submit(ad, "9", "Author", "Material", png)
            core._set(did, attempts=2, revision_seq=2, candidates=json.dumps([
                {"model": "first", "post": "X" * 3990}, {"model": "second", "post": "Short post"}]))
            d = core.get(did)
            await ad.send_for_approval("1", did, d["photo"], d["post"], resume=True)
            core._set(did, last_sent=time.time() - 86400, reminded=1)
            before = core.get(did)
            messages = len(api.of("POST"))
            for _ in range(2):
                core.startup()
                assert await ops.flush_deferred() == 0
            assert len(api.of("POST")) == messages
            assert core.get(did)["last_sent"] == before["last_sent"]
            assert core.get(did)["reminded"] == 1
        finally:
            await ad.http.aclose()

    run(scenario())
