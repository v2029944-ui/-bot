import json

import httpx
import pytest
from conftest import run

import comparison
import core
import ops

MODELS = ["gpt-4.1-mini", "gpt-4.1-nano", "gemini-3.1-flash-lite", "gpt-4o-mini"]
CATALOG = {m: {"modalities": {"input": ["text", "image"], "output": ["text"]}, "prompt_cost": 10, "completion_cost": 20} for m in MODELS}


def enable():
    ops.set_setting("model_comparison", json.dumps({"enabled": True, "models": MODELS}))


def test_catalog_selects_recommended_pair_and_rejects_text_only():
    assert comparison.select(CATALOG) == [MODELS[0], MODELS[2]]
    bad = {name: {"modalities": {"input": ["text"], "output": ["text"]}} for name in MODELS}
    with pytest.raises(ValueError):
        comparison.select(bad)


def test_admin_enable_validates_public_catalog_without_key(mx, monkeypatch):
    mx.admins = ["5"]
    monkeypatch.setattr(core, "PROVIDER", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.aitunnel.ru/v1")
    original = httpx.AsyncClient
    calls = []

    def handler(request):
        calls.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(200, json=CATALOG)

    monkeypatch.setattr(comparison.httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    assert "только администратору" in run(core.on_menu(mx, "9", "m:compare_set:1"))[0]
    assert not calls
    text, _ = run(core.on_menu(mx, "5", "m:compare_set:1"))
    assert all(m in text for m in (MODELS[0], MODELS[2])) and not comparison.config()["enabled"]
    state = comparison.picker_state()[("max", "5")]
    run(core.on_menu(mx, "5", f"m:models_save:{state['token']}:{state['revision']}"))
    assert comparison.config() == {"enabled": True, "models": [MODELS[0], MODELS[2]]}


@pytest.mark.parametrize("album", [False, True])
def test_four_generations_one_draft_selection_and_publication(mx, png, llm, album):
    enable()
    llm.reply = lambda system, text: "Пост от " + comparison.MODEL.get()
    if album:
        did = run(core.submit_album(mx, "9", "Анна", "Описание работы", [{"type": "image", "data": png}] * 2))
    else:
        did = run(core.submit(mx, "9", "Анна", "Описание работы", png))
    assert len(llm.calls) == 4 and core.db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1
    assert [o["model"] for o in json.loads(core.get(did)["candidates"])] == MODELS
    assert "выберите" in run(core.on_action(mx, "5", "okyes", did, 1))[1]
    assert not mx.sent("publish")
    assert run(core.on_action(mx, "5", "choose_1", did, 1))[0]
    assert core.get(did)["selected_model"] == MODELS[1]
    assert "устаревший" in run(core.on_action(mx, "5", "choose_0", did, 1))[1]
    assert comparison.MODEL.get() is None
    run(core.on_action(mx, "5", "regen", did, 2))
    assert len(llm.calls) == 8 and len(json.loads(core.get(did)["candidates"])) == 4
    run(core.on_action(mx, "5", "choose_1", did, 3))
    run(core.on_action(mx, "5", "okyes", did, 4))
    assert mx.sent("publish")[0][1] == "Пост от " + MODELS[1]
    assert core.get(did)["candidates"] == "[]" and core.get(did)["selected_model"] == ""


def test_redo_before_selection_replaces_all_candidates(mx, png, llm):
    enable()
    llm.reply = lambda system, text: "Вариант " + comparison.MODEL.get()
    did = run(core.submit(mx, "9", "Анна", "Описание работы", png))
    assert run(core.on_action(mx, "5", "regen", did, 1))[0]
    assert len(llm.calls) == 8 and core.get(did)["attempts"] == 2
    assert "устаревший" in run(core.on_action(mx, "5", "choose_0", did, 1))[1]
    assert "выберите" in run(core.on_action(mx, "5", "okyes", did, 2))[1]


def test_failed_regeneration_keeps_old_candidates(mx, png, llm):
    enable()
    did = run(core.submit(mx, "9", "Анна", "Описание работы", png))
    before = core.get(did)
    llm.reply = RuntimeError("Unavailable")
    assert not run(core.on_action(mx, "5", "regen", did, 1))[0]
    assert core.get(did)["candidates"] == before["candidates"] and core.get(did)["attempts"] == 1


def test_disabled_comparison_regenerates_only_selected_model(mx, png, llm):
    enable()
    llm.reply = lambda system, text: "Пост " + comparison.MODEL.get()
    did = run(core.submit(mx, "9", "Анна", "Описание работы", png))
    run(core.on_action(mx, "5", "choose_1", did, 1))
    ops.set_setting("model_comparison", json.dumps({"enabled": False, "models": MODELS}))
    run(core.on_action(mx, "5", "regen", did, 2))
    assert len(llm.calls) == 5 and core.get(did)["post"] == "Пост " + MODELS[1]


def test_failed_model_does_not_discard_other_candidates(mx, png, llm):
    enable()
    llm.reply = lambda system, text: RuntimeError("unavailable") if comparison.MODEL.get() == MODELS[0] else "Готовый пост"
    did = run(core.submit(mx, "9", "Анна", "Описание работы", png))
    assert "недоступен" in run(core.on_action(mx, "5", "choose_0", did, 1))[1]
    assert run(core.on_action(mx, "5", "choose_2", did, 1))[0]


def test_real_max_shows_labels_and_selection_buttons(monkeypatch, png, llm):
    from test_admin_menu_button import Api, buttons, make

    async def scenario():
        enable()
        llm.reply = lambda system, text: "Пост: " + comparison.MODEL.get()
        api = Api()
        ad = make(api)
        core.ADAPTERS["max"] = ad

        async def upload(path):
            return "token"

        monkeypatch.setattr(ad, "_upload_image", upload)
        await core.submit(ad, "9", "Анна", "Описание работы", png)
        cards = [c[3] for c in api.of("POST") if c[3]["text"].startswith("Модель:")]
        assert len(cards) == 4  # четыре варианта только автору
        assert all(any("choose_" in b["payload"] for b in buttons(card)) for card in cards)
        assert all(any(m in card["text"] for m in MODELS) for card in cards)
        await ad.http.aclose()
    run(scenario())


def test_recommended_pair_replaces_old_models_only_after_save(mx, monkeypatch):
    mx.admins = ["5"]
    monkeypatch.setattr(core, "PROVIDER", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.aitunnel.ru/v1")
    ops.set_setting("model_comparison", json.dumps({"enabled": True, "models": MODELS[:3]}))
    original = httpx.AsyncClient
    monkeypatch.setattr(comparison.httpx, "AsyncClient", lambda **kw: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=CATALOG)), **kw))
    text, _ = run(core.on_menu(mx, "5", "m:compare_refresh"))
    assert comparison.config()["models"] == MODELS[:3]
    state = comparison.picker_state()[("max", "5")]
    assert state["selected"] == [MODELS[0], MODELS[2]]
    run(core.on_menu(mx, "5", f"m:models_save:{state['token']}:{state['revision']}"))
    assert comparison.config()["models"] == [MODELS[0], MODELS[2]]


def test_failed_refresh_keeps_existing_models_and_allows_retry(mx, monkeypatch):
    mx.admins = ["5"]
    monkeypatch.setattr(core, "PROVIDER", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.aitunnel.ru/v1")
    previous = {"enabled": True, "models": MODELS[:3]}
    ops.set_setting("model_comparison", json.dumps(previous))
    original = httpx.AsyncClient
    monkeypatch.setattr(comparison.httpx, "AsyncClient", lambda **kw: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(503)), **kw))
    text, rows = run(core.on_menu(mx, "5", "m:compare_refresh"))
    assert "Не удалось" in text and comparison.config() == previous
    assert rows[0][0][1] == "m:compare_pick"


def test_recommended_pair_prefers_flash_lite_without_requiring_claude_or_grok():
    catalog = {MODELS[0]: CATALOG[MODELS[0]], "gemini-2.5-flash-lite": CATALOG[MODELS[2]]}
    assert comparison.select(catalog) == [MODELS[0], "gemini-2.5-flash-lite"]


@pytest.mark.parametrize("cost", [None, -1, "NaN", "Infinity", "not-a-price"])
def test_fallback_prefers_known_prices_to_invalid_or_unknown_prices(cost):
    catalog = {MODELS[0]: CATALOG[MODELS[0]],
               "gpt-4.1-nano": {**CATALOG[MODELS[0]], "prompt_cost": cost},
               "gpt-4o-mini": {**CATALOG[MODELS[0]], "prompt_cost": 1}}
    assert comparison.select(catalog) == [MODELS[0], "gpt-4o-mini"]


@pytest.mark.parametrize("enabled", [False, True])
def test_redo_opens_menu_without_charges_then_regenerates_when_requested(mx, png, llm, enabled):
    if enabled:
        enable()
    did = run(core.submit(mx, "9", "Анна", "Описание", png))
    count = len(llm.calls)
    before = core.get(did)
    run(core.on_action(mx, "5", "redo", did, 1))
    assert len(llm.calls) == count and core.get(did) == before
    payloads = [p for row in mx.sent("menu")[-1][3] for _, p in row]
    assert all(f"d:{action}:{did}:1" in payloads for action in ("regen", "length", "redoc", "back"))
    assert run(core.on_action(mx, "5", "regen", did, 1))[0]
    assert len(llm.calls) == count * 2


def test_comparison_length_and_comment_regenerate_all_candidates_before_selection(mx, png, llm, monkeypatch):
    enable()
    monkeypatch.setenv("LEARN_RULES", "0")
    seen = []

    def answer(system, text):
        model = comparison.MODEL.get()
        seen.append((model, system, text))
        return "Вариант модели " + model

    llm.reply = answer
    did = run(core.submit(mx, "9", "Анна", "Описание", png))
    run(core.on_action(mx, "5", "length", did, 1))
    assert mx.sent("menu")[-1][3][0][0][1] == f"d:len_short:{did}:1"
    assert run(core.on_action(mx, "5", "len_short", did, 1))[0]
    assert core.get(did)["post_length"] == "short"
    assert len(json.loads(core.get(did)["candidates"])) == 4 and core.get(did)["attempts"] == 2
    seen.clear()
    run(core.on_action(mx, "5", "redoc", did, 2))
    assert ("max", "5") in core.WAITING
    run(core.on_text(mx, "5", "Добавь конкретики"))
    assert len(seen) == 4 and {model for model, _, _ in seen} == set(MODELS)
    for model, system, text in seen:
        assert "150–300" in system
        assert "Добавь конкретики" in text and "Текущий вариант поста:\nВариант модели " + model in text
    assert len(json.loads(core.get(did)["candidates"])) == 4 and core.get(did)["attempts"] == 3
    assert "выберите" in run(core.on_action(mx, "5", "okyes", did, 3))[1]
    assert "устаревший" in run(core.on_action(mx, "5", "len_long", did, 2))[1]


def test_selected_comparison_variant_length_and_comment_use_only_selected_model(mx, png, llm, monkeypatch):
    enable()
    monkeypatch.setenv("LEARN_RULES", "0")
    llm.reply = lambda system, text: "Пост " + comparison.MODEL.get()
    did = run(core.submit(mx, "9", "Анна", "Описание", png))
    run(core.on_action(mx, "5", "choose_1", did, 1))
    assert run(core.on_action(mx, "5", "len_long", did, 2))[0]
    assert len(llm.calls) == 5 and core.get(did)["selected_model"] == MODELS[1]
    run(core.on_action(mx, "5", "redoc", did, 3))
    run(core.on_text(mx, "5", "Убери вводную фразу"))
    assert len(llm.calls) == 6 and core.get(did)["selected_model"] == MODELS[1]
    assert core.get(did)["candidates"] == "[]"


def test_real_max_redo_opens_options_on_existing_card_without_requesting_models(monkeypatch, png, llm):
    from test_admin_menu_button import Api, buttons, make

    async def scenario():
        enable()
        api = Api()
        ad = make(api)
        core.ADAPTERS["max"] = ad

        async def upload(path):
            return "token"

        monkeypatch.setattr(ad, "_upload_image", upload)
        did = await core.submit(ad, "9", "Автор", "Описание", png)
        mid = f"m{api.n}"
        count = len(api.of("POST"))
        await ad.on_callback({"user": {"user_id": 1}, "callback_id": "redo-options",
                              "payload": f"d:redo:{did}:1", "message": {"body": {"mid": mid}}})
        payloads = [b["payload"] for b in buttons(api.of("PUT")[-1][3])]
        assert all(f"d:{action}:{did}:1" in payloads for action in ("regen", "length", "redoc", "back")), payloads
        assert len(llm.calls) == 4 and len(api.of("POST")) == count
        await ad.on_callback({"user": {"user_id": 1}, "callback_id": "length-options",
                              "payload": f"d:length:{did}:1", "message": {"body": {"mid": mid}}})
        assert len(llm.calls) == 4
        await ad.on_callback({"user": {"user_id": 1}, "callback_id": "new-length", "payload": f"d:len_long:{did}:1"})
        assert len(llm.calls) == 8 and core.get(did)["post_length"] == "long"
        assert len(json.loads(core.get(did)["candidates"])) == 4
        await ad.http.aclose()
    run(scenario())


@pytest.mark.parametrize("failure", ["llm", "delivery"])
def test_comparison_length_failure_restores_candidates_version_and_length(mx, png, llm, monkeypatch, failure):
    enable()
    did = run(core.submit(mx, "9", "Автор", "Описание", png))
    before = core.get(did)
    if failure == "llm":
        llm.reply = RuntimeError("Unavailable")
    else:
        async def fail(*args, **kwargs):
            raise RuntimeError("Cannot send")
        monkeypatch.setattr(mx, "send_for_approval", fail)
    assert not run(core.on_action(mx, "5", "len_long", did, 1))[0]
    after = core.get(did)
    assert all(after[key] == before[key] for key in ("post", "candidates", "attempts", "post_length", "selected_model"))
    assert after["status"] == "pending"
