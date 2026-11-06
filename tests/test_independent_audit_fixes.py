"""Regression coverage for the independent audit, excluding documentation-only F12."""
import asyncio
import json
import sys
import time

import httpx
import pytest
from conftest import run
from test_workspace_settings import create, preview
from test_workspace_settings import runtime as runtime

import configuration
import core
import max_
import ops
import spaces
import updater


def test_space_settings_work_inside_production_guard(runtime):
    ad,_ = runtime
    payload = preview(ad,0,'Название\n22\n-100\n-')
    result = run(core._guard(core.on_menu(ad,'1',payload)))
    assert 'сохранены' in result[0]


def test_primary_owner_cannot_be_owner_of_another_space(runtime):
    ad,_ = runtime
    payload = preview(ad,0,'Название\n22\n-100\n-')
    assert 'сохранены' in run(core.on_menu(ad,'1',payload))[0]
    assert create(ad,owner=22,maximum=-101) is None


def test_rejection_retention_starts_when_rejected(mx,llm,png):
    cursor = core.db.execute("INSERT INTO drafts(platform,author_id,author_name,source_text,photo,post,created,status) VALUES('max','9','Автор','Описание работы','','Текст',?,'pending')",(time.time(),))
    core.db.commit()
    did = cursor.lastrowid
    core._set(did,created=time.time()-8*86400,last_sent=time.time())
    assert run(core.reject_post(mx,'5',did,1,'Нужно описание'))[0]
    core.housekeeping()
    assert core.get(did)['rejection_reason'] == 'Нужно описание'


def test_zero_reminder_interval_is_valid_disabled_setting(monkeypatch):
    monkeypatch.setenv('REMIND_AFTER_MIN','0')
    assert configuration.validate() is None


def test_truncated_generation_is_not_accepted(monkeypatch):
    monkeypatch.setenv('LLM_BASE_URL','https://llm.example/v1')
    monkeypatch.setenv('LLM_API_KEY','dummy')
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'choices':[{'message':{'content':'Незавершённый текст'},'finish_reason':'length'}]})),**kw))
    with pytest.raises(RuntimeError):
        run(core._openai('system','text',None))


def test_update_timeout_stops_descendant_process(tmp_path):
    marker = tmp_path/'orphan.txt'
    child = f'import time,pathlib;time.sleep(.4);pathlib.Path({str(marker)!r}).write_text("alive")'
    parent = f'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",{child!r}]);time.sleep(5)'
    rc,_ = updater.run([sys.executable,'-c',parent],tmp_path,timeout=.1)
    assert rc == 1
    time.sleep(.6)
    assert not marker.exists()


def test_failed_update_notice_retains_delivery_record(mx,monkeypatch,tmp_path):
    monkeypatch.setenv('DATA_DIR',str(tmp_path))
    result = tmp_path/'update_result.json'
    result.write_text(json.dumps({'status':'ok','platform':'max','uid':'5','commit':'audit'}))
    async def broken(*args,**kw):raise RuntimeError('Simulated network failure')
    monkeypatch.setattr(mx,'notify',broken)
    run(ops.announce_update_result())
    assert result.exists()


def test_member_ids_are_int64(runtime):
    ad,_ = runtime
    run(core.on_menu(ad,'1','m:member'))
    run(core.on_text(ad,'1','9999999999999999999'))
    assert core.role_of(ad,'9999999999999999999') is None


def test_channel_ids_are_int64(runtime):
    with pytest.raises(ValueError):
        spaces.parse_settings('Название\n33\n9999999999999999999\n-')


def test_weekly_statistics_count_publication_time(mx,llm,png):
    cursor = core.db.execute("INSERT INTO drafts(platform,author_id,author_name,source_text,photo,post,created,status) VALUES('max','9','Автор','Описание работы','','Текст',?,'pending')",(time.time(),))
    core.db.commit()
    did = cursor.lastrowid
    core._set(did,created=time.time()-8*86400,last_sent=time.time())
    assert run(core.on_action(mx,'5','okyes',did,1))[0]
    assert 'опубликовано за неделю: 1' in run(core.status_text(mx))


def test_empty_midnight_working_window_is_rejected(monkeypatch):
    monkeypatch.setenv('NOTIFY_HOURS','24-0')
    assert ops.config_problem() is not None


def test_confirmation_off_suppresses_material_confirmation(monkeypatch):
    async def scenario():
        ad = max_.Max('dummy','-100',[],['9'],['1'])
        core.ADAPTERS['max'] = ad
        notices = []
        async def notify(uid,text):notices.append(text)
        async def download(*args,**kwargs):return b'dummy'
        async def submit(*args,**kwargs):return 1
        monkeypatch.setenv('EMPLOYEE_CONFIRM','off')
        monkeypatch.setattr(ad,'notify',notify)
        monkeypatch.setattr(ad,'_download',download)
        monkeypatch.setattr(core,'submit',submit)
        try:
            await ad.on_message({'sender':{'user_id':9},'body':{'text':'Описание работы','attachments':[{'type':'image'}]}})
            assert notices == []
        finally:
            await ad.http.aclose()
    run(scenario())


def test_settings_save_runs_through_command_worker_while_other_space_is_busy(runtime):
    import max_actions

    ad, messages = runtime
    child = create(ad)
    spaces.root().db.execute("INSERT INTO max_inbox(event_key,state,space_id) VALUES('other','processing',?)", (child['id'],))
    spaces.root().db.commit()
    payload = preview(ad, 0, 'Новое название\n22\n-100\n-')

    async def scenario():
        async def call(*args, **kwargs):
            return {}
        ad.call = call
        background = asyncio.create_task(asyncio.Event().wait())
        core.INFLIGHT.add(background)
        try:
            max_actions.enqueue({'update_type': 'message_callback', 'callback': {
                'callback_id': 'save-settings', 'user': {'user_id': 1}, 'payload': 'w:0:' + payload}})
            worker = max_actions.Worker(ad)
            worker.schedule()
            await asyncio.gather(*worker.active.values())
            assert spaces.configured_root(spaces.root().db)['name'] == 'Новое название'
            assert any('сохранены' in text for _, text, _ in messages)
        finally:
            background.cancel()
            await asyncio.gather(background, return_exceptions=True)
            core.INFLIGHT.discard(background)
    run(scenario())


def test_primary_owner_collision_is_checked_on_preview_and_confirmation(runtime):
    ad, _ = runtime
    child = create(ad)
    run(core.on_menu(ad, '1', 'm:space_edit:0'))
    run(core.on_text(ad, '1', 'Основное\n11\n-100\n-'))
    assert spaces.settings_state()[('max', '1')]['waiting']
    payload = preview(ad, 0, 'Основное\n22\n-100\n-')
    spaces.root().db.execute('UPDATE workspaces SET owner=? WHERE id=?', ('22', child['id']))
    spaces.root().db.commit()
    assert 'владелец' in run(core.on_menu(ad, '1', payload))[0].lower()
    assert spaces.configured_root(spaces.root().db) is None


def test_legacy_closed_rows_receive_retention_once():
    now = time.time()
    cursor = core.db.execute("INSERT INTO drafts(status,post,created,closed_at) VALUES('deleted','Сохранить',?,0)", (now - 30 * 86400,))
    core.db.commit()
    core.startup()
    before = core.get(cursor.lastrowid)
    assert before['post'] == 'Сохранить' and before['closed_at'] >= now
    core.startup()
    assert core.get(cursor.lastrowid)['closed_at'] == before['closed_at']


@pytest.mark.parametrize('reason', ['max_tokens', 'model_context_window_exceeded', 'pause_turn'])
def test_anthropic_incomplete_text_is_rejected(monkeypatch, reason):
    from types import SimpleNamespace

    async def create(**kwargs):
        return SimpleNamespace(stop_reason=reason, content=[SimpleNamespace(type='text', text='Оборванный ответ')])
    monkeypatch.setattr(core, '_client', SimpleNamespace(messages=SimpleNamespace(create=create)))
    with pytest.raises(core.IncompleteGeneration):
        run(core._claude('system', 'text', None))


def test_incomplete_material_requires_manual_retry_and_keeps_cached_download(monkeypatch, png):
    from test_durable_queue import event

    import max_queue

    async def scenario():
        ad = max_.Max('dummy', '-100', [], ['9'], ['1'])
        core.ADAPTERS['max'] = ad
        calls, notices = [], []
        async def download(*args):
            return png
        async def notify(uid, text, rows=None, **kwargs):
            notices.append(text)
        async def incomplete(*args):
            calls.append('incomplete')
            raise core.IncompleteGeneration('Ответ оборван')
        monkeypatch.setattr(ad, '_download', download)
        monkeypatch.setattr(ad, 'notify', notify)
        monkeypatch.setattr(ad, 'send_menu', notify)
        monkeypatch.setattr(core, '_complete', incomplete)
        key = max_queue.enqueue(event())
        worker = max_queue.Worker(ad)
        try:
            assert await worker.process_once()
            row = core.db.execute('SELECT state,attempts FROM max_inbox WHERE event_key=?', (key,)).fetchone()
            assert tuple(row) == ('failed', 1) and len(calls) == 1
            assert max_queue.cache_dir(key).exists()
            assert not await worker.process_once(time.time() + 4000)
            assert any('платные повторы остановлены' in text for text in notices)
            async def complete(*args):
                calls.append('complete')
                return 'Готовый пост'
            async def approval(*args, **kwargs):
                pass
            monkeypatch.setattr(core, '_complete', complete)
            monkeypatch.setattr(ad, 'send_for_approval', approval)
            await core.on_menu(ad, '1', 'm:inretry:' + key)
            assert await worker.process_once()
            assert len(calls) == 2 and not max_queue.cache_dir(key).exists()
            assert core.db.execute('SELECT state FROM max_inbox WHERE event_key=?', (key,)).fetchone()[0] == 'done'
        finally:
            await ad.http.aclose()
    run(scenario())


def test_truncated_comparison_does_not_retry_failed_models_automatically(mx, png, monkeypatch):
    from test_comparison import enable

    enable()
    async def incomplete(*args):
        raise core.IncompleteGeneration('Ответ оборван')
    monkeypatch.setattr(core, '_complete', incomplete)
    with pytest.raises(core.IncompleteGeneration):
        run(core.submit(mx, '9', 'Автор', 'Описание', png))
    assert not core.db.execute('SELECT 1 FROM drafts').fetchone()


def test_update_notice_retries_only_undelivered_recipients(mx, tg, monkeypatch, tmp_path):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    record = tmp_path / 'update_result.json'
    record.write_text(json.dumps({'status': 'ok', 'commit': 'audit'}))
    original = tg.notify
    async def broken(*args):
        raise RuntimeError('offline')
    monkeypatch.setattr(tg, 'notify', broken)
    run(ops.announce_update_result())
    saved = json.loads(record.read_text())
    assert saved['notice_remaining'] == [['tg', '1'], ['tg', '2']]
    assert len(mx.sent('notify')) == 1
    run(ops.announce_update_result())  # backoff survives reloading from the record
    assert len(mx.sent('notify')) == 1
    monkeypatch.setattr(tg, 'notify', original)
    monkeypatch.setattr(ops.time, 'time', lambda: saved['next_try'] + 1)
    run(ops.announce_update_result())
    assert not record.exists() and len(mx.sent('notify')) == 1
    assert len(tg.sent('notify')) == 2


def test_concurrent_update_notices_deliver_once(mx, monkeypatch, tmp_path):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    record = tmp_path / 'update_result.json'
    record.write_text(json.dumps({'status': 'ok', 'platform': 'max', 'uid': '5', 'commit': 'audit'}))
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def notify(*args):
            calls.append(args)
            entered.set()
            await release.wait()
        monkeypatch.setattr(mx, 'notify', notify)
        first = asyncio.create_task(ops.announce_update_result())
        await entered.wait()
        second = asyncio.create_task(ops.announce_update_result())
        release.set()
        await asyncio.gather(first, second)
        assert len(calls) == 1 and not record.exists()
    run(scenario())


def test_pending_page_limits_sql_and_filters_authors(mx):
    core.refresh_people(mx)
    core._add_person(mx, '9', 'Автор', 'member')
    core._add_person(mx, '8', 'Другой', 'member')
    core.db.executemany("INSERT INTO drafts(platform,author_id,author_name,source_text,post,created) VALUES('max',?,?,?,'Пост',?)",
                        [(str(8 + n % 2), 'Автор', 'Подпись', n) for n in range(1000)])
    core.db.commit()
    queries = []
    core.db.set_trace_callback(queries.append)
    try:
        text, rows = core.pending_view(mx, 2, '9')
    finally:
        core.db.set_trace_callback(None)
    assert 'На проверке: 500' in text
    assert len([p for row in rows for _, p in row if p.startswith('d:show:')]) == 8
    pages = [sql for sql in queries if sql.startswith('SELECT * FROM drafts')]
    assert len(pages) == 1 and 'LIMIT 8 OFFSET 16' in pages[0] and "author_id='9'" in pages[0]
    shown = [p for row in rows for _, p in row if p.startswith('d:show:')]
    assert all(core.get(int(p.split(':')[2]))['author_id'] == '9' for p in shown)


def test_pending_admin_page_preserves_old_telegram_and_fallback_platforms(mx, tg):
    tg.publish_only = True
    for platform in ('max', 'tg', 'retired'):
        core.db.execute("INSERT INTO drafts(platform,author_id,author_name,source_text,post,created) VALUES(?,'9','Автор','Описание','Пост',0)", (platform,))
    core.db.commit()
    assert 'На проверке: 3' in core.pending_view(mx, uid='5')[0]
    assert 'На проверке: 1' in core.pending_view(mx, uid='9')[0]


def test_person_lookup_is_targeted_and_keeps_legacy_roles(mx, monkeypatch):
    core.db.execute("INSERT INTO people VALUES('max','9','Автор','apr',0)")
    core.db.commit()
    def full_list(*args):
        pytest.fail('single-person lookup must not load all people')
    monkeypatch.setattr(core, '_db_people', full_list)
    assert core._person(mx, '9')['role'] == 'member'


@pytest.mark.parametrize('value,expected', [('0-24', True), ('24-0', False), ('0-0', False), ('24-24', False),
                                           ('20-6', True), ('24-6', True), ('6-24', True)])
def test_working_window_boundaries(monkeypatch, value, expected):
    monkeypatch.setenv('NOTIFY_HOURS', value)
    assert (ops.config_problem() is None) is expected


def test_disabled_reminders_do_not_prevent_process_startup(tmp_path):
    import os
    import subprocess

    script = '''import asyncio
import main, core, ops
entered = False
async def serve(workers):
    global entered
    entered = True
    assert ops.config_problem() is None
    assert await core.remind_once() == 0
main.serve = serve
asyncio.run(main.main())
assert entered
'''
    env = {**os.environ, 'DATA_DIR': str(tmp_path / 'startup'), 'REMIND_AFTER_MIN': '0',
           'MAX_TOKEN': 'dummy', 'MAX_ADMINS': '1', 'MAX_CHANNEL': '-100', 'TG_TOKEN': '',
           'LLM_PROVIDER': 'openai', 'LLM_API_KEY': 'dummy', 'LLM_BASE_URL': 'https://llm.example/v1',
           'STARTUP_NOTIFY': '0'}
    subprocess.run([sys.executable, '-c', script], env=env, check=True, capture_output=True, text=True, timeout=20)


def test_unknown_old_publication_date_is_not_fabricated(mx, llm):
    cursor = core.db.execute("INSERT INTO drafts(status,created,post) VALUES('published',?,'')", (time.time(),))
    core.db.commit()
    core.startup()
    assert core.get(cursor.lastrowid)['published_at'] == 0
    text = run(core.status_text(mx))
    assert 'опубликовано за неделю: 0' in text and 'дата отправки неизвестна' in text


@pytest.mark.parametrize('value,allowed', [('9223372036854775807', True), ('9223372036854775808', False),
                                         ('-9223372036854775808', True), ('-9223372036854775809', False),
                                         ('0', False), ('-0', False), ('001', False), ('１２３', False)])
def test_chat_id_bounds(value, allowed):
    assert configuration.chat_id(value) is allowed


def test_cancelled_publication_prompt_does_not_block_new_space_settings(runtime):
    ad, _ = runtime
    cursor = core.db.execute("INSERT INTO drafts(platform,author_id,author_name,source_text,post,created) VALUES('max','1','Автор','Описание','Пост',?)", (time.time(),))
    core.db.commit()
    did = cursor.lastrowid
    core._set(did, publication_channels=json.dumps({'max': '-100'}))
    ad._confirmations()[('max', '1')] = ['old-confirmation']
    payload = preview(ad, 0, 'Основное\n22\n-110\n-')
    assert 'сохранены' in run(core._guard(core.on_menu(ad, '1', payload)))[0]
    assert not ad._confirmations()
    run(core.on_action(ad, '1', 'ok', did, 1))
    assert json.loads(core.get(did)['publication_channels']) == {'max': '-110'}
