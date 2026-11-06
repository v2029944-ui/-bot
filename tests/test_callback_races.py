import asyncio

import pytest
from conftest import run
from test_admin_menu_button import Api, buttons, make

import core


@pytest.mark.parametrize('fails', [False, True])
def test_edit_delivery_blocks_publication_and_cannot_rollback_published_post(mx, llm, png, monkeypatch, fails):
    async def scenario():
        did = await core.submit(mx, '9', 'Author', 'Original', png)
        started, release = asyncio.Event(), asyncio.Event()

        async def delivery(*args, **kwargs):
            started.set()
            await release.wait()
            return (0, RuntimeError('offline')) if fails else (1, None)

        monkeypatch.setattr(core, '_send_draft', delivery)
        edit = asyncio.create_task(core._apply_edit(mx, '5', did, 1, 'Edited text'))
        await asyncio.wait_for(started.wait(), 2)
        assert core.get(did)['status'] == 'regenerating'
        assert 'обрабатывается' in (await core.on_action(mx, '9', 'okyes', did, 2))[1]
        assert not mx.sent('publish')
        release.set()
        await edit
        assert core.get(did)['status'] == 'pending'
        assert core.get(did)['post'] == (llm.reply if fails else 'Edited text')
    run(scenario())


def test_max_confirmation_cancel_reopen_and_double_click(monkeypatch, png, llm):
    async def scenario():
        api = Api()
        ad = make(api)
        core.ADAPTERS['max'] = ad
        sent = []

        async def upload(path):
            return 'photo-token'

        async def publish(*args, **kwargs):
            sent.append(args)

        monkeypatch.setattr(ad, '_upload_image', upload)
        monkeypatch.setattr(ad, 'publish', publish)
        did = await core.submit(ad, '9', 'Author', 'Original', png)

        async def click(payload):
            await ad.on_callback({'callback_id': payload, 'user': {'user_id': 1}, 'payload': payload})

        def confirm(action):
            bodies = [entry[3] for entry in api.of('POST')]
            return next(b['payload'] for body in reversed(bodies) for b in buttons(body)
                        if b.get('payload', '').startswith(f'd:{action}:'))

        try:
            await click(f'd:ok:{did}:1')
            old = confirm('okyes')
            assert len(old.split(':')) == 5
            await click(f'd:okno:{did}:1')
            await click(old)
            assert not sent and core.get(did)['status'] == 'pending'
            await click(f'd:ok:{did}:1')
            new = confirm('okyes')
            assert new != old
            await click(old)
            assert not sent
            await asyncio.gather(click(new), click(new))
            assert len(sent) == 1 and core.get(did)['status'] == 'published'
        finally:
            await ad.http.aclose()
    run(scenario())


def test_legacy_confirmation_cannot_reject_post(monkeypatch, png, llm):
    async def scenario():
        api = Api()
        ad = make(api)
        core.ADAPTERS['max'] = ad

        async def upload(path):
            return 'photo-token'

        monkeypatch.setattr(ad, '_upload_image', upload)
        try:
            did = await core.submit(ad, '9', 'Author', 'Original', png)
            for action in ('okyes', 'okforce', 'delyes', 'rejectphoto', 'rejecttext'):
                await ad.on_callback({'callback_id': action, 'user': {'user_id': 1}, 'payload': f'd:{action}:{did}:1'})
            assert core.get(did)['status'] == 'pending'
            assert any('подтверждение больше не действует' in entry[3].get('text', '') for entry in api.of('POST'))
        finally:
            await ad.http.aclose()
    run(scenario())


def test_uneditable_old_buttons_do_not_starve_later_cleanup(monkeypatch):
    async def scenario():
        ad = make(Api())
        for i in range(21):
            core.db.execute('INSERT INTO max_draft_ui VALUES(?,?,?)', (f'z{i:02}', 999999, 1))
        core.db.commit()
        visited = []

        async def edit(prev, **kwargs):
            visited.append(prev['mid'])
            return prev['mid'] == 'z20'

        monkeypatch.setattr(ad, '_strip_admin_button', edit)
        try:
            await ad.cleanup_stale_buttons()
            assert visited == [f'z{i:02}' for i in range(20)]
            await ad.cleanup_stale_buttons()
            assert visited[-1] == 'z20'
            assert core.db.execute("SELECT 1 FROM max_draft_ui WHERE mid='z20'").fetchone() is None
            await ad.cleanup_stale_buttons()
            assert visited[-1] == 'z19'  # the retry cycle returns to the failed messages
        finally:
            core.db.execute("DELETE FROM max_draft_ui WHERE mid LIKE 'z%'")
            core.db.commit()
            await ad.http.aclose()
    run(scenario())
