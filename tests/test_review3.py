"""Числовые id в настройках, длинный список правил, ответы нейросети, повторы запросов."""
import types

import pytest

import core
import main
from conftest import run

S = types.SimpleNamespace


def test_ids_reject_usernames(monkeypatch):
    monkeypatch.setenv("TG_APPROVERS", "123, 456")
    assert main.ids("TG_APPROVERS") == ["123", "456"]
    monkeypatch.setenv("TG_APPROVERS", "123,@ivan")
    with pytest.raises(SystemExit, match="числовые id"):
        main.ids("TG_APPROVERS")
    monkeypatch.delenv("TG_APPROVERS")
    assert main.ids("TG_APPROVERS") == []


def test_long_rules_list_fits_in_one_message():
    for i in range(120):
        core.db.execute("INSERT INTO rules(text,created) VALUES(?,0)", (f"Правило {i} " + "очень длинное " * 20,))
    core.db.commit()
    text, rows = core.rules_view()
    assert len(text) < 4096 and "Показаны последние" in text
    assert sum(len(r) for r in rows[:-1]) <= 40  # кнопок удаления не больше 40 (лимит Telegram — 100)


def test_claude_reply_collects_text_blocks(monkeypatch):
    class Block(S):
        pass

    class Client:
        class messages:
            @staticmethod
            async def create(**kw):
                return S(content=[Block(type="thinking", thinking="..."), Block(type="text", text="Готовый пост")])
    monkeypatch.setattr(core, "_client", Client)
    assert run(core._claude("sys", "text", None)) == "Готовый пост"


def test_openai_param_switch_is_not_a_retry(monkeypatch):
    calls = []
    answers = [S(status_code=400, text="use max_completion_tokens instead of max_tokens"),
               S(status_code=429, text="slow"), S(status_code=429, text="slow"), S(status_code=429, text="slow"),
               S(status_code=200, text="", json=lambda: {"choices": [{"message": {"content": "Пост"}}]})]

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, headers, json):
            calls.append(sorted(k for k in json if k.startswith("max")))
            return answers[len(calls) - 1]
    monkeypatch.setattr(core.httpx, "AsyncClient", Client)
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    assert run(core._openai("sys", "text", None)) == "Пост"
    assert calls[0] == ["max_tokens"] and all(c == ["max_completion_tokens"] for c in calls[1:])
    assert len(calls) == 5  # смена параметра + три повтора после 429 + успех
