import asyncio
import json

import httpx
import pytest
from conftest import FakeAdapter, run
from test_comparison import CATALOG

import comparison
import core
import post_footer


def test_double_enable_and_disable_link_are_idempotent(mx):
    post_footer.save({'enabled': False, 'text': 'Link', 'max': 'https://max.ru/bot', 'tg': ''})
    for _ in range(2):
        run(core.on_menu(mx, '5', 'm:footer_set:1'))
    assert post_footer.config()['enabled']
    run(core.on_menu(mx, '5', 'm:footer_toggle'))
    assert post_footer.config()['enabled']  # an old toggle refreshes controls without mutation
    for _ in range(2):
        run(core.on_menu(mx, '5', 'm:footer_set:0'))
    assert not post_footer.config()['enabled']


def test_double_comparison_enable_only_fetches_catalog_once(mx, monkeypatch):
    monkeypatch.setattr(core, 'PROVIDER', 'openai')
    monkeypatch.setenv('LLM_BASE_URL', 'https://api.aitunnel.ru/v1')
    requests = []
    original = httpx.AsyncClient

    def handler(req):
        requests.append(req)
        return httpx.Response(200, json=CATALOG)

    monkeypatch.setattr(comparison.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    for _ in range(2):
        run(core.on_menu(mx, '5', 'm:compare_set:1'))
    assert comparison.config()['enabled'] and len(requests) == 1
    run(core.on_menu(mx, '5', 'm:compare_toggle'))
    assert comparison.config()['enabled']
    for _ in range(2):
        run(core.on_menu(mx, '5', 'm:compare_set:0'))
    assert not comparison.config()['enabled']


def test_unknown_provider_fails_before_any_generation(monkeypatch):
    monkeypatch.setattr(core, 'PROVIDER', 'opeani')
    assert 'LLM_PROVIDER' in core.llm_config_problem()


def test_partial_publication_cannot_continue_to_a_different_channel(mx, llm, png):
    mx.channel = '-100'
    tg = FakeAdapter('tg', [], fail=[ValueError('denied')], sure=(ValueError,), admins=[])
    tg.publish_only, tg.channel = True, '-200'
    core.ADAPTERS['tg'] = tg
    did = run(core.submit(mx, '9', 'Author', 'Caption', png))
    assert not run(core.on_action(mx, '5', 'okyes', did, 1))[0]
    assert len(mx.sent('publish')) == 1 and not tg.sent('publish')
    tg.channel = '-201'
    assert 'Каналы публикации изменились' in run(core.on_action(mx, '5', 'okyes', did, 1))[1]
    assert not tg.sent('publish') and len(mx.sent('publish')) == 1
    assert core.get(did)['status'] == 'pending'
    tg.channel = '-200'
    assert run(core.on_action(mx, '5', 'okyes', did, 1))[0]
    assert len(mx.sent('publish')) == len(tg.sent('publish')) == 1
    assert core.get(did)['publication_channels'] == '{}'


def test_channel_change_invalidates_existing_publication_prompt(mx, llm, png):
    mx.channel = '-100'
    did = run(core.submit(mx, '9', 'Author', 'Caption', png))
    run(core.on_action(mx, '5', 'ok', did, 1))
    mx.channel = '-101'
    assert 'Каналы публикации изменились' in run(core.on_action(mx, '5', 'okyes', did, 1))[1]
    assert not mx.sent('publish')
    run(core.on_action(mx, '5', 'ok', did, 1))  # explicit reconfirmation for the new destination
    assert run(core.on_action(mx, '5', 'okyes', did, 1))[0]


def test_cancelled_send_is_recovered_as_uncertain_not_repeated(mx, llm, png, monkeypatch):
    async def scenario():
        did = await core.submit(mx, '9', 'Author', 'Caption', png)
        sent = []

        async def lost_reply(*args, **kwargs):
            sent.append(args)
            raise asyncio.CancelledError

        monkeypatch.setattr(mx, 'publish', lost_reply)
        with pytest.raises(asyncio.CancelledError):
            await core.on_action(mx, '5', 'okyes', did, 1)
        assert core.get(did)['status'] == 'publishing'
        assert core.get(did)['published_to'] == 'max?'
        core.startup()
        assert core.get(did)['status'] == 'pending'
        await core.on_action(mx, '5', 'okyes', did, 1)
        assert len(sent) == 1
        assert 'прошлая публикация' in mx.sent('menu')[-1][2].lower()
    run(scenario())


def test_old_partial_draft_without_channel_snapshot_remains_recoverable(mx, llm, png):
    mx.channel = '-100'
    did = run(core.submit(mx, '9', 'Author', 'Caption', png))
    core._set(did, published_to='max?', publication_channels='{}')
    run(core.on_action(mx, '5', 'okyes', did, 1))
    assert not mx.sent('publish')
    assert json.loads(core.get(did)['publication_channels']) == {'max': '-100'}
    assert run(core.on_action(mx, '5', 'seen_max', did, 1))[1] == ''
    assert core.get(did)['status'] == 'published'


def test_unauthorized_replay_cannot_change_idempotent_settings(mx):
    for payload in ('m:compare_set:1', 'm:footer_set:1'):
        assert 'администратор' in run(core.on_menu(mx, '9', payload))[0]
    assert not comparison.config()['enabled'] and not post_footer.config()['enabled']


def test_invalid_setting_payload_has_no_side_effect(mx):
    for payload in ('m:compare_set:2', 'm:footer_set:2', 'm:compare_set', 'm:footer_set'):
        assert 'Неизвестное состояние' in run(core.on_menu(mx, '5', payload))[0]
    assert not comparison.config()['enabled'] and not post_footer.config()['enabled']


def test_concurrent_comparison_changes_preserve_order(mx, monkeypatch):
    async def scenario():
        monkeypatch.setattr(core, 'PROVIDER', 'openai')
        monkeypatch.setenv('LLM_BASE_URL', 'https://api.aitunnel.ru/v1')
        started, release = asyncio.Event(), asyncio.Event()
        original = httpx.AsyncClient

        async def handler(req):
            started.set()
            await release.wait()
            return httpx.Response(200, json=CATALOG)

        monkeypatch.setattr(comparison.httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
        first = asyncio.create_task(core.on_menu(mx, '5', 'm:compare_set:1'))
        await asyncio.wait_for(started.wait(), 2)
        second = asyncio.create_task(core.on_menu(mx, '5', 'm:compare_set:0'))
        tick = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_later(.02, tick.set_result, None)
        await tick
        assert not second.done()
        release.set()
        await asyncio.gather(first, second)
        assert not comparison.config()['enabled']
    run(scenario())
