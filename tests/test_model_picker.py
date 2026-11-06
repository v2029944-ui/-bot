import json

import httpx
import pytest
from conftest import run

import comparison
import core
import dialog_state

PAIR = ["gpt-4.1-mini", "gemini-2.5-flash-lite"]
NAMES = [*PAIR, "claude-haiku-4.5", "grok-4.20", "grok-4.3", "deepseek-v4-flash-vision-exp",
         "qwen3.8-flash", "gemini-3.1-flash-lite", "kimi-k2.5", "glm-4.6v", "mistral-small-3.2-24b-instruct"]
CLIENT = httpx.AsyncClient
INFO = {"modalities": {"input": ["text", "image"], "output": ["text"]}, "prompt_cost": 1, "completion_cost": 2}


@pytest.fixture
def catalog(mx, monkeypatch):
    mx.admins = ["5", "6"]
    monkeypatch.setattr(core, "PROVIDER", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.aitunnel.ru/v1")
    current = {name: INFO for name in NAMES}
    current["text-only"] = {"modalities": {"input": ["text"], "output": ["text"]}}
    calls = []
    original = CLIENT
    def handle(request):
        calls.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(200, json=current)
    monkeypatch.setattr(comparison.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    return current, calls


def state(uid="5"):
    return comparison.picker_state()[("max", uid)]


def open_picker(mx, uid="5", recommended=False):
    return run(core.on_menu(mx, uid, "m:compare_refresh" if recommended else "m:compare_pick"))


def check(mx, name, enabled, uid="5"):
    c = state(uid)
    payload = f"m:models_set:{c['token']}:{c['revision']}:{c['names'].index(name)}:{1 if enabled else 0}"
    result = run(core.on_menu(mx, uid, payload))
    return payload, result


def save(mx, uid="5"):
    c = state(uid)
    payload = f"m:models_save:{c['token']}:{c['revision']}"
    return payload, run(core.on_menu(mx, uid, payload))


def test_picker_shows_recommended_checks_and_full_paginated_catalog_without_generation(mx, catalog, llm):
    text, rows = open_picker(mx)
    assert state()["selected"] == PAIR and "text-only" not in state()["names"]
    assert rows[0][0][0] == "☑ gpt-4.1-mini" and rows[1][0][0] == "☑ gemini-2.5-flash-lite"
    assert any(p.startswith("m:models_page:") for row in rows for _, p in row)
    assert "grok-4.20" in state()["names"]  # no requirement for retired Grok Fast names
    assert not comparison.config()["enabled"] and not llm.calls
    page = next(p for row in rows for _, p in row if p.startswith("m:models_page:"))
    run(core.on_menu(mx, "5", page))
    assert state()["page"] == 1
    assert len(catalog[1]) == 1  # pagination uses the same snapshot


def test_save_single_model_uses_one_request_no_candidate_selection_and_persists_for_redo(mx, catalog, llm, png):
    open_picker(mx)
    check(mx, PAIR[1], False)
    payload, result = save(mx)
    assert "сохранены" in result[0] and comparison.config() == {"enabled": True, "models": PAIR[:1]}
    assert comparison.default_model() == PAIR[0]
    llm.reply = lambda system, text: "Пост " + comparison.MODEL.get()
    did = run(core.submit(mx, "9", "Автор", "Описание", png))
    assert len(llm.calls) == 1 and core.get(did)["candidates"] == "[]"
    assert core.get(did)["selected_model"] == PAIR[0]
    assert run(core.on_action(mx, "5", "len_short", did, 1))[0]
    assert len(llm.calls) == 2 and core.get(did)["selected_model"] == PAIR[0]
    assert "уже закрыт" in run(core.on_menu(mx, "5", payload))[0]


@pytest.mark.parametrize("album", [False, True])
def test_two_or_more_models_generate_exactly_the_selected_set(mx, catalog, llm, png, album):
    open_picker(mx)
    check(mx, "grok-4.20", True)
    _, result = save(mx)
    assert "сохранены" in result[0]
    expected = [*PAIR, "grok-4.20"]
    llm.reply = lambda system, text: comparison.MODEL.get()
    if album:
        did = run(core.submit_album(mx, "9", "Автор", "Описание", [{"type": "image", "data": png}] * 2))
    else:
        did = run(core.submit(mx, "9", "Автор", "Описание", png))
    assert len(llm.calls) == 3
    assert [o["model"] for o in json.loads(core.get(did)["candidates"])] == expected
    assert "выберите" in run(core.on_action(mx, "5", "okyes", did, 1))[1]


def test_duplicate_and_stale_checks_do_not_toggle_selection_back(mx, catalog):
    open_picker(mx)
    payload, _ = check(mx, "grok-4.20", True)
    before = state()
    text, _ = run(core.on_menu(mx, "5", payload))
    assert "устарела" in text and state() == before
    check(mx, "grok-4.20", False)
    run(core.on_menu(mx, "5", payload))
    assert "grok-4.20" not in state()["selected"]


def test_limits_empty_save_and_cancellation_preserve_saved_configuration(mx, catalog):
    open_picker(mx)
    check(mx, "grok-4.20", True)
    check(mx, "claude-haiku-4.5", True)
    _, result = check(mx, "grok-4.3", True)
    assert "не больше четырёх" in result[0] and len(state()["selected"]) == 4
    for name in list(state()["selected"]):
        check(mx, name, False)
    _, result = save(mx)
    assert "хотя бы одну" in result[0] and not comparison.config()["enabled"]
    check(mx, PAIR[0], True)
    c = state()
    old_save = f"m:models_save:{c['token']}:{c['revision']}"
    run(core.on_menu(mx, "5", "m:compare"))
    assert "уже закрыт" in run(core.on_menu(mx, "5", old_save))[0]
    assert not comparison.config()["enabled"]


def test_unauthorized_user_or_other_admin_cannot_use_foreign_picker(mx, catalog):
    text, _ = run(core.on_menu(mx, "9", "m:compare_pick"))
    assert "только администратору" in text and not catalog[1]
    open_picker(mx)
    c = state()
    action = f"m:models_save:{c['token']}:{c['revision']}"
    assert "уже закрыт" in run(core.on_menu(mx, "6", action))[0]
    assert not comparison.config()["enabled"] and state() == c


def test_other_admin_update_is_not_overwritten_by_older_picker(mx, catalog):
    open_picker(mx, "5")
    open_picker(mx, "6")
    check(mx, PAIR[1], False, "6")
    save(mx, "6")
    c = state("5")
    action = f"m:models_save:{c['token']}:{c['revision']}"
    assert "другим администратором" in run(core.on_menu(mx, "5", action))[0]
    assert comparison.config()["models"] == PAIR[:1]


def test_removed_model_is_rechecked_before_save_and_previous_configuration_kept(mx, catalog):
    open_picker(mx)
    del catalog[0][PAIR[1]]
    _, result = save(mx)
    assert "больше не доступна" in result[0] and not comparison.config()["enabled"]
    assert state()["selected"] == PAIR


def test_saved_picker_survives_state_reload_and_navigation_invalidates_old_buttons(mx, catalog, monkeypatch):
    open_picker(mx)
    c = state()
    monkeypatch.setattr(comparison, "picker_state", lambda: dialog_state.PendingMap(core.db, "model_picker"))
    payload = f"m:models_set:{c['token']}:{c['revision']}:0:0"
    run(core.on_menu(mx, "5", payload))
    assert state()["selected"] == PAIR[1:]
    c = state()
    save_action = f"m:models_save:{c['token']}:{c['revision']}"
    run(core.on_menu(mx, "5", "m:rules"))
    assert "уже закрыт" in run(core.on_menu(mx, "5", save_action))[0]


def test_text_only_models_are_available_when_vision_disabled(mx, catalog, monkeypatch):
    monkeypatch.setenv("LLM_VISION", "0")
    open_picker(mx)
    assert "text-only" in state()["names"]


def test_default_model_mode_can_be_restored(mx, catalog):
    open_picker(mx)
    save(mx)
    run(core.on_menu(mx, "5", "m:compare_set:0"))
    assert not comparison.config()["enabled"] and comparison.default_model() == core.MODEL


def test_general_redo_switches_existing_post_to_new_single_model(mx, catalog, llm, png):
    open_picker(mx)
    check(mx, PAIR[1], False)
    save(mx)
    llm.reply = lambda system, text: comparison.MODEL.get()
    did = run(core.submit(mx, "9", "Автор", "Описание", png))
    open_picker(mx)
    check(mx, PAIR[0], False)
    check(mx, "grok-4.20", True)
    save(mx)
    assert run(core.on_action(mx, "5", "regen", did, 1))[0]
    assert core.get(did)["selected_model"] == "grok-4.20"
    assert core.get(did)["post"] == "grok-4.20" and core.get(did)["candidates"] == "[]"


def test_failed_catalog_validation_does_not_apply_unsaved_changes(mx, catalog, monkeypatch):
    open_picker(mx)
    check(mx, PAIR[1], False)
    async def unavailable():
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(comparison, "fetch_catalog", unavailable)
    _, result = save(mx)
    assert "Выбор не сохранён" in result[0] and not comparison.config()["enabled"]
    assert state()["selected"] == PAIR[:1]


def test_single_model_album_does_not_require_comparison_selection(mx, catalog, llm, png):
    open_picker(mx)
    check(mx, PAIR[1], False)
    save(mx)
    did = run(core.submit_album(mx, "9", "Автор", "Описание", [{"type": "image", "data": png}] * 2))
    assert len(llm.calls) == 1 and core.get(did)["selected_model"] == PAIR[0]
    assert core.get(did)["candidates"] == "[]"
    assert run(core.on_action(mx, "5", "okyes", did, 1))[0]


def test_provider_uses_space_selected_model_for_unscoped_requests(mx, catalog, monkeypatch):
    open_picker(mx)
    check(mx, PAIR[0], False)
    save(mx)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    calls = []
    original = CLIENT
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    monkeypatch.setattr(comparison.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    assert run(core._openai("test", "test", None)) == "OK"
    assert calls[0]["model"] == PAIR[1]


def test_models_with_unknown_prices_can_still_be_selected_explicitly(mx, catalog):
    catalog[0]["grok-4.20"] = {"modalities": INFO["modalities"]}
    open_picker(mx)
    check(mx, "grok-4.20", True)
    assert "сохранены" in save(mx)[1][0]
    assert "grok-4.20" in comparison.config()["models"]


def test_real_max_checkboxes_survive_callbacks_and_save_without_generating_posts(monkeypatch, llm):
    from test_admin_menu_button import Api, buttons, make

    async def scenario():
        api = Api()
        ad = make(api)
        core.ADAPTERS["max"] = ad
        async def available():
            return {name: INFO for name in NAMES}
        monkeypatch.setattr(comparison, "fetch_catalog", available)
        await ad.on_callback({"user": {"user_id": 1}, "callback_id": "open", "payload": "m:compare_pick"})
        first = api.of("POST")[-1][3]
        action = next(b["payload"] for b in buttons(first) if b["text"] == "☑ gemini-2.5-flash-lite")
        await ad.on_callback({"user": {"user_id": 1}, "callback_id": "uncheck", "payload": action,
                              "message": {"body": {"mid": f"m{api.n}"}}})
        body = api.of("POST")[-1][3]
        assert any(b["text"] == "☐ gemini-2.5-flash-lite" for b in buttons(body))
        assert comparison.picker_state()[("max", "1")]["selected"] == PAIR[:1]
        save_action = next(b["payload"] for b in buttons(body) if b["payload"].startswith("m:models_save:"))
        await ad.on_callback({"user": {"user_id": 1}, "callback_id": "save", "payload": save_action,
                              "message": {"body": {"mid": f"m{api.n}"}}})
        assert comparison.config() == {"enabled": True, "models": PAIR[:1]}
        assert not llm.calls and len([call for call in api.calls if call[0] == "DELETE"]) == 2
        await ad.http.aclose()
    run(scenario())
