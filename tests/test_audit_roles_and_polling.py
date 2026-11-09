"""Regression checks from the role and MAX API compatibility audit."""
import asyncio
import gc

import pytest
from conftest import FakeAdapter, run

import core
import max_
import ops
from services import submissions
import spaces


def test_promoting_root_admin_does_not_grant_global_process_control():
    ad = FakeAdapter('max', ['1'], admins=['1'])
    core.ADAPTERS['max'] = ad
    core.refresh_people(ad)
    core._add_person(ad, '2', 'Team admin', 'adm')
    assert core.is_admin(ad, '2') and not core.is_global_admin(ad, '2')
    assert core.is_global_admin(ad, '1')
    assert not any(p == 'm:upd' for row in core.menu_for(ad, '2') for _, p in row)
    for payload in ('m:upd', 'm:updgo'):
        assert 'MAX_ADMINS' in run(core.on_menu(ad, '2', payload))[0]
    assert 'MAX_ADMINS' in ops.upd_go(ad, '2')[0]
    assert ops.RESTART_CODE is None


def test_global_admin_uses_configured_ids_not_mutable_admin_roles(monkeypatch):
    from types import SimpleNamespace
    ad = FakeAdapter('max', ['1'], admins=['1'])
    core.ADAPTERS['max'] = ad
    core.refresh_people(ad)
    core._add_person(ad, '2', 'Team admin', 'adm')
    monkeypatch.setattr(spaces, 'INSTALLED', True)
    monkeypatch.setattr(spaces, 'ROOT', SimpleNamespace(adapters={'max': ad}))
    assert spaces.global_admin('1') and not spaces.global_admin('2')


def test_fast_polling_obeys_official_minimum_interval(monkeypatch):
    async def scenario():
        ad = max_.Max('token', '-1', ['1'], [], ['1'])
        delays, calls = [], []

        async def call(method, path, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return {'updates': [], 'marker': 42}
            assert calls[-1]['params']['marker'] == 42
            raise asyncio.CancelledError

        async def sleep(delay):
            delays.append(delay)

        ticks = iter([10., 10.05, 10.3])
        # Patch only the adapter's time facade, preserving asyncio's own monotonic clock.
        from types import SimpleNamespace
        monkeypatch.setattr(max_, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
        monkeypatch.setattr(ad, 'call', call)
        monkeypatch.setattr(asyncio, 'sleep', sleep)
        try:
            with pytest.raises(asyncio.CancelledError):
                await ad.run()
            assert delays == pytest.approx([.25])
        finally:
            await ad.http.aclose()
    run(scenario())


def test_media_upload_locks_release_after_use_without_cleanup(monkeypatch, tmp_path):
    async def scenario():
        ad = max_.Max('token', '-1', [], [], [])
        path = tmp_path / 'photo.jpg'
        path.write_bytes(b'file')

        async def upload(path):
            return 'photo-token'

        monkeypatch.setattr(ad, '_upload_image', upload)
        try:
            assert await ad._cached_upload(path, 'image') == 'photo-token'
            gc.collect()
            assert not ad._upload_locks
            assert len(ad._upload_cache) == 1
        finally:
            await ad.http.aclose()
    run(scenario())


def test_send_draft_rechecks_stale_recipient_list_before_delivery(monkeypatch, png, llm):
    async def scenario():
        approval = FakeAdapter("max", ["9"], admins=[])
        core.ADAPTERS["max"] = approval
        core.refresh_people(approval)
        core._add_person(approval, "5", "Admin", "adm")
        did = await core.submit(approval, "9", "Author", "Description", png)
        d = core.get(did)
        sent_before = len(approval.sent("approval"))

        def stale_recipients(_draft, _adapter):
            core._remove_person(approval, "5", "Admin")
            return ["5"]

        monkeypatch.setattr(core, "draft_recipients", stale_recipients)
        sent, last = await submissions._send_draft(d, did, d["post"], approval)
        assert sent == 0 and last is None
        assert len(approval.sent("approval")) == sent_before
        assert not core.can_manage_draft(approval, "5", core.get(did))

    run(scenario())


def test_max_rechecks_draft_rights_after_media_upload_before_post(monkeypatch, png, llm):
    async def scenario():
        seed = FakeAdapter("max", ["9"], admins=[])
        core.ADAPTERS["max"] = seed
        did = await core.submit(seed, "9", "Author", "Description", png)
        d = core.get(did)

        ad = max_.Max("token", "-1", [], ["9"], [])
        core.ADAPTERS["max"] = ad
        core.refresh_people(ad)
        core._add_person(ad, "5", "Admin", "adm")
        assert core.can_manage_draft(ad, "5", core.get(did))
        calls = []

        async def upload(_path, _kind, force=False):
            core._remove_person(ad, "5", "Admin")
            return "token-after-slow-upload"

        async def call(method, path, **kwargs):
            calls.append((method, path))
            return {"message": {"body": {"mid": "unexpected"}}}

        monkeypatch.setattr(ad, "_cached_upload", upload)
        monkeypatch.setattr(ad, "call", call)
        try:
            with pytest.raises(PermissionError, match="больше не имеет доступа"):
                await ad.send_for_approval("5", did, d["photo"], d["post"])
            assert ("POST", "/messages") not in calls
            assert not core.can_manage_draft(ad, "5", core.get(did))
        finally:
            await ad.http.aclose()

    run(scenario())
