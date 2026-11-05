import json

import httpx
import pytest

import comparison
import core
import ops
from conftest import run

MODELS = ["gpt-4.1-mini", "claude-sonnet-4.6", "gemini-2.5-flash"]
CATALOG = {m: {"modalities": {"input": ["text", "image"], "output": ["text"]}, "prompt_cost": 10, "completion_cost": 20} for m in MODELS}


def enable():
    ops.set_setting("model_comparison", json.dumps({"enabled": True, "models": MODELS}))


def test_catalog_selects_three_vision_models_and_rejects_text_only():
    assert comparison.select(CATALOG) == MODELS
    bad = {**CATALOG, "gemini-2.5-flash": {"modalities": {"input": ["text"], "output": ["text"]}}}
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
    assert "только администратору" in run(core.on_menu(mx, "9", "m:compare_toggle"))[0]
    assert not calls
    text, _ = run(core.on_menu(mx, "5", "m:compare_toggle"))
    assert all(m in text for m in MODELS) and comparison.config()["enabled"]


@pytest.mark.parametrize("album", [False, True])
def test_three_generations_one_draft_selection_and_publication(mx, png, llm, album):
    enable()
    llm.reply = lambda system, text: "Пост от " + comparison.MODEL.get()
    if album:
        did = run(core.submit_album(mx, "9", "Анна", "Описание работы", [{"type": "image", "data": png}] * 2))
    else:
        did = run(core.submit(mx, "9", "Анна", "Описание работы", png))
    assert len(llm.calls) == 3 and core.db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1
    assert [o["model"] for o in json.loads(core.get(did)["candidates"])] == MODELS
    assert "выберите" in run(core.on_action(mx, "5", "okyes", did, 1))[1]
    assert not mx.sent("publish")
    assert run(core.on_action(mx, "5", "choose_1", did, 1))[0]
    assert core.get(did)["selected_model"] == MODELS[1]
    assert "устаревший" in run(core.on_action(mx, "5", "choose_0", did, 1))[1]
    assert comparison.MODEL.get() is None
    run(core.on_action(mx, "5", "redo", did, 2))
    assert len(llm.calls) == 6 and len(json.loads(core.get(did)["candidates"])) == 3
    run(core.on_action(mx, "5", "choose_1", did, 3))
    run(core.on_action(mx, "5", "okyes", did, 4))
    assert mx.sent("publish")[0][1] == "Пост от " + MODELS[1]
    assert core.get(did)["candidates"] == "[]" and core.get(did)["selected_model"] == ""


def test_redo_before_selection_replaces_all_candidates(mx, png, llm):
    enable()
    llm.reply = lambda system, text: "Вариант " + comparison.MODEL.get()
    did = run(core.submit(mx, "9", "Анна", "Описание работы", png))
    assert run(core.on_action(mx, "5", "redo", did, 1))[0]
    assert len(llm.calls) == 6 and core.get(did)["attempts"] == 2
    assert "устаревший" in run(core.on_action(mx, "5", "choose_0", did, 1))[1]
    assert "выберите" in run(core.on_action(mx, "5", "okyes", did, 2))[1]


def test_failed_regeneration_keeps_old_candidates(mx, png, llm):
    enable()
    did = run(core.submit(mx, "9", "Анна", "Описание работы", png))
    before = core.get(did)
    llm.reply = RuntimeError("Unavailable")
    assert not run(core.on_action(mx, "5", "redo", did, 1))[0]
    assert core.get(did)["candidates"] == before["candidates"] and core.get(did)["attempts"] == 1


def test_disabled_comparison_regenerates_only_selected_model(mx, png, llm):
    enable()
    llm.reply = lambda system, text: "Пост " + comparison.MODEL.get()
    did = run(core.submit(mx, "9", "Анна", "Описание работы", png))
    run(core.on_action(mx, "5", "choose_1", did, 1))
    ops.set_setting("model_comparison", json.dumps({"enabled": False, "models": MODELS}))
    run(core.on_action(mx, "5", "redo", did, 2))
    assert len(llm.calls) == 4 and core.get(did)["post"] == "Пост " + MODELS[1]


def test_failed_model_does_not_discard_other_candidates(mx, png, llm):
    enable()
    llm.reply = lambda system, text: RuntimeError("unavailable") if comparison.MODEL.get() == MODELS[0] else "Готовый пост"
    did = run(core.submit(mx, "9", "Анна", "Описание работы", png))
    assert "недоступен" in run(core.on_action(mx, "5", "choose_0", did, 1))[1]
    assert run(core.on_action(mx, "5", "choose_2", did, 1))[0]


def test_real_max_shows_labels_and_selection_buttons(monkeypatch, png, llm):
    from test_admin_menu_button import Api, make, buttons

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
        assert len(cards) == 3  # три варианта только автору
        assert all(any("choose_" in b["payload"] for b in buttons(card)) for card in cards)
        assert all(any(m in card["text"] for m in MODELS) for card in cards)
        await ad.http.aclose()
    run(scenario())
