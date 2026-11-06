import json

import httpx
import pytest
from conftest import run

import comparison
import core
import ops

BANNED = ["grok-4.20", "grok-4.7", "claude-haiku-4.5", "claude-sonnet-5.5", "claude-opus-5.5",
          "gpt-6-astra", "gpt-6-astra-pro", "gpt-5.5-pro", "gpt-5.4-mini", "gemini-2.5-pro",
          "gemini-4-argon", "gemini-3-flash-preview", "deepseek-v4-flash-vision-exp", "gpt-audio",
          "codestral-2508", "sonar-deep-research"]
INFO = {"modalities": {"input": ["text", "image"], "output": ["text"]}, "prompt_cost": 1, "completion_cost": 1}


def test_picker_only_accepts_curated_small_models_even_if_large_ones_are_cheap():
    catalog = {name: INFO for name in [*comparison.BUDGET_MODELS, *BANNED]}
    assert set(comparison.eligible(catalog)) == comparison.BUDGET_MODELS
    assert comparison.select(catalog) == ["gpt-4.1-mini", "gemini-2.5-flash-lite"]
    with pytest.raises(ValueError):
        comparison.select({name: INFO for name in BANNED})


def test_saved_expensive_set_is_pruned_and_persisted_without_losing_valid_choices():
    ops.set_setting("model_comparison", json.dumps({"enabled": True, "models": [BANNED[0], "gpt-4o-mini", BANNED[5]]}))
    assert comparison.config() == {"enabled": True, "models": ["gpt-4o-mini"]}
    assert json.loads(ops.get_setting("model_comparison"))["models"] == ["gpt-4o-mini"]


def test_fully_expensive_configuration_falls_back_to_one_budget_model(mx, llm, png):
    ops.set_setting("model_comparison", json.dumps({"enabled": True, "models": BANNED[:4]}))
    llm.reply = lambda system, text: "Пост: " + comparison.MODEL.get()
    did = run(core.submit(mx, "9", "Автор", "Описание", png))
    assert len(llm.calls) == 1 and core.get(did)["selected_model"] == "gpt-4.1-mini"
    assert core.get(did)["candidates"] == "[]"
    assert comparison.config()["models"] == ["gpt-4.1-mini"]


def test_old_picker_snapshot_cannot_reintroduce_expensive_model(mx):
    old = {"token": "old-token", "revision": 0, "names": [BANNED[0]], "selected": [BANNED[0]],
           "page": 0, "before": comparison.config()}
    comparison.picker_state()[("max", "5")] = old
    text, _ = run(core.on_menu(mx, "5", "m:models_save:old-token:0"))
    assert "уже закрыт" in text and not comparison.config()["enabled"]
    assert not comparison.picker_state()


def test_aitunnel_env_and_pinned_context_cannot_bypass_budget_policy(monkeypatch):
    monkeypatch.setattr(core, "PROVIDER", "openai")
    monkeypatch.setattr(core, "MODEL", "gpt-6-astra-pro")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.aitunnel.ru/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    calls = []
    original = httpx.AsyncClient
    def handle(request):
        calls.append(json.loads(request.content)["model"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    token = comparison.MODEL.set("claude-opus-5.5")
    try:
        assert run(core._openai("test", "test", None)) == "OK"
    finally:
        comparison.MODEL.reset(token)
    assert calls == ["gpt-4.1-mini"] and comparison.default_model() == "gpt-4.1-mini"


def test_old_draft_regeneration_replaces_expensive_pinned_model_but_keeps_existing_text(mx, llm, png, monkeypatch):
    monkeypatch.setattr(core, "PROVIDER", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.aitunnel.ru/v1")
    did = run(core.submit(mx, "9", "Автор", "Описание", png))
    core._set(did, selected_model="grok-4.20")
    before = core.get(did)["post"]
    ops.set_setting("model_comparison", json.dumps({"enabled": True, "models": ["grok-4.20"]}))
    comparison.config()  # selection migration must not rewrite or delete an existing draft
    assert core.get(did)["post"] == before and core.get(did)["selected_model"] == "grok-4.20"
    seen = []
    llm.reply = lambda system, text: seen.append(comparison.MODEL.get()) or "Новый текст"
    assert run(core.on_action(mx, "5", "len_short", did, 1))[0]
    assert seen == ["gpt-4.1-mini"] and core.get(did)["selected_model"] == "gpt-4.1-mini"


def test_non_aitunnel_provider_model_is_unchanged(monkeypatch):
    monkeypatch.setattr(core, "PROVIDER", "anthropic")
    monkeypatch.setattr(core, "MODEL", "claude-sonnet-4.6")
    assert comparison.default_model() == "claude-sonnet-4.6"
    assert comparison.request_model("claude-opus-4.6") == "claude-opus-4.6"
