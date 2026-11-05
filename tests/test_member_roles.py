"""Author ownership, legacy role migration and delayed delivery access checks."""
import time

import pytest
import core
import ops
from conftest import FakeAdapter, run


def members():
    ad = FakeAdapter('max', ['1', '2'])
    ad.admins = ['1']
    ad.base_employees = ['9', '8']
    core.ADAPTERS['max'] = ad
    core.refresh_people(ad)
    return ad


@pytest.mark.parametrize('action', ['show', 'change', 'edit', 'redoc', 'redo', 'len_short',
    'choose_0', 'del', 'delyes', 'rejectcustom', 'pubstatus', 'seen_max', 'ok', 'okyes', 'okforce'])
def test_other_member_cannot_use_even_old_buttons(action, llm, png):
    ad = members()
    did = run(core.submit(ad, '9', 'Author', 'Private caption', png))
    before = core.get(did)
    ad.out.clear()
    ok, text = run(core.on_action(ad, '2', action, did, 1))
    assert not ok and 'нет прав' in text
    assert core.get(did) == before and not ad.out


def test_author_receives_and_publishes_own_draft(llm, png):
    ad = members()
    did = run(core.submit(ad, '9', 'Author', 'Private caption', png))
    assert [o[1] for o in ad.sent('approval')] == ['9']
    assert core.role_of(ad, '9') == 'member' and not core.is_admin(ad, '9')
    assert run(core.on_action(ad, '9', 'edit', did, 1))[0] is False
    assert run(core.on_text(ad, '9', 'My edited post'))
    assert core.get(did)['post'] == 'My edited post'
    assert run(core.on_action(ad, '9', 'okyes', did, 2))[0]
    assert core.get(did)['status'] == 'published'


def test_restored_input_and_direct_mutations_check_owner(llm, png):
    ad = members()
    did = run(core.submit(ad, '9', 'Author', 'Private caption', png))
    before = core.get(did)
    core.WAITING_EDIT[('max', '2')] = (did, 1)
    run(core.on_text(ad, '2', 'Stolen edit'))
    for coro in [core._regenerate(ad, '2', did), core._publish(ad, '2', did, 1), core.reject_post(ad, '2', did, 1, 'No')]:
        assert 'нет прав' in run(coro)[1]
    assert core.get(did) == before
    assert ('max', '2') not in core.WAITING_EDIT


def test_lists_filter_and_admin_can_recover_orphan(llm, png):
    ad = members()
    own = run(core.submit(ad, '9', 'Author', 'Private caption', png))
    other = run(core.submit(ad, '8', 'Other', 'Other caption', png))
    text, rows = run(core.on_menu(ad, '9', 'm:pending'))
    assert f'№{own}' in text and 'Other caption' not in text
    assert [p for row in rows for _, p in row if p.startswith('d:show')] == [f'd:show:{own}:1']
    assert 'На проверке: 2' in run(core.on_menu(ad, '1', 'm:pending'))[0]
    ad.allowed.remove('9')
    assert core.draft_recipients(core.get(own), ad) == ['1']
    assert run(core.on_action(ad, '1', 'okyes', own, 1))[0]
    assert core.get(other)['status'] == 'pending'


def test_legacy_roles_migrate_without_changing_drafts_or_pending_input(llm, png):
    ad = members()
    did = run(core.submit(ad, '9', 'Author', 'Private caption', png))
    core.WAITING_EDIT[('max', '9')] = (did, 1)
    for uid, role in [('77', 'emp'), ('78', 'apr'), ('79', 'adm')]:
        core.db.execute('INSERT INTO people VALUES(?,?,?,?,?)', ('max', uid, uid, role, time.time()))
    core.db.commit()
    core.refresh_people(ad)
    core.refresh_people(ad)
    assert core.role_of(ad, '77') == core.role_of(ad, '78') == 'member'
    assert core.is_admin(ad, '79') and not core.is_admin(ad, '78')
    assert core.WAITING_EDIT[('max', '9')] == (did, 1)
    assert core.get(did)['source_text'] == 'Private caption'


def test_reminders_and_deferred_do_not_leak(llm, png, monkeypatch):
    ad = members()
    monkeypatch.setenv('NOTIFY_HOURS', '00:00-23:59')
    monkeypatch.setenv('NOTIFY_DAYS', '1,2,3,4,5,6,7')
    did = run(core.submit(ad, '9', 'Author', 'Private caption', png))
    core._set(did, created=time.time()-20000, last_sent=time.time()-20000)
    ad.out.clear()
    assert run(core.remind_once()) == 1
    assert {o[1] for o in ad.sent('menu')} == {'9'}
    ad.out.clear()
    ops.enqueue(ad, '2', did)  # legacy approver must lose delivery access
    ops.enqueue(ad, '9', did)
    run(ops.flush_deferred())
    assert {o[1] for o in ad.sent('approval')} == {'9'}
    core._set(did, last_sent=time.time()-6.5*86400)
    ad.out.clear()
    assert run(core.warn_expiring_once()) == 1
    assert {o[1] for o in ad.sent('menu')} == {'9'}


def test_old_telegram_author_is_not_same_max_user(llm, png):
    ad = members()
    did = run(core.submit(ad, '9', 'Author', 'Private caption', png))
    core._set(did, platform='tg')
    assert not core.can_manage_draft(ad, '9', core.get(did))
    assert core.draft_recipients(core.get(did), ad) == ['1']


def test_member_cannot_change_rules_even_via_restored_input():
    ad = members()
    for payload in ['m:add', 'm:del:1', 'm:rules', 'm:member', 'm:compare', 'm:footer', 'm:status']:
        assert 'администратор' in run(core.on_menu(ad, '9', payload))[0]
    core.WAITING_RULE.add(('max', '9'))
    run(core.on_text(ad, '9', 'Unauthorized rule'))
    assert core.db.execute('SELECT COUNT(*) FROM rules').fetchone()[0] == 0


def test_legacy_deferred_delivery_is_reassigned_without_losing_draft(llm, png):
    ad = members()
    did = run(core.submit(ad, '9', 'Author', 'Private caption', png))
    ad.out.clear()
    ops.enqueue(ad, '2', did)
    assert run(ops.flush_deferred()) == 1
    assert [o[1] for o in ad.sent('approval')] == ['9']
    assert core.get(did)['status'] == 'pending'
    assert core.db.execute('SELECT COUNT(*) FROM deferred').fetchone()[0] == 0
    assert run(ops.flush_deferred()) == 0


def test_author_delivery_failure_keeps_draft_for_admin(llm, png, monkeypatch):
    ad = members()
    original = ad.send_for_approval

    async def send(uid, *args, **kwargs):
        if uid == '9':
            raise RuntimeError('Author blocked the bot')
        return await original(uid, *args, **kwargs)

    monkeypatch.setattr(ad, 'send_for_approval', send)
    did = run(core.submit(ad, '9', 'Author', 'Private caption', png))
    assert [o[1] for o in ad.sent('approval')] == ['1']
    assert core.get(did)['status'] == 'pending'
    assert core.can_manage_draft(ad, '1', core.get(did))


def test_legacy_approvers_are_members_without_administrative_privileges():
    import max_
    ad = max_.Max('test-token', '-100', ['2'], ['9'], [])
    core.ADAPTERS['max'] = ad
    try:
        assert ad.allowed == {'2', '9'} and ad.admins == []
        assert core.role_of(ad, '2') == 'member'
        assert not core.is_admin(ad, '2') and not core.can_status(ad, '2')
        assert not core.is_member(ad, '99')
    finally:
        run(ad.http.aclose())
