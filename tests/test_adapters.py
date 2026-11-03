"""Telegram- и MAX-адаптеры: фильтры, тихий режим, классификация сбоев. Без сети."""
import types

import httpx
import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendPhoto

import core
import main
import max_
import tg
from conftest import run

S = types.SimpleNamespace


# ---------- Telegram ----------


def test_telegram_failure_classification(bot_tg):
    method = SendPhoto(chat_id=1, photo="x")
    assert bot_tg.not_sent(TelegramBadRequest(method=method, message="chat not found"))
    assert bot_tg.not_sent(TelegramRetryAfter(method=method, message="flood", retry_after=3))
    assert not bot_tg.not_sent(TelegramNetworkError(method=method, message="timeout"))
    partial = TelegramBadRequest(method=method, message="text too long")
    partial.partial = True  # фото длинного поста уже в канале
    assert not bot_tg.not_sent(partial)


# ---------- MAX ----------
def http_error(code):
    req = httpx.Request("POST", "https://platform-api.max.ru/messages")
    return httpx.HTTPStatusError("err", request=req, response=httpx.Response(code, request=req))


def test_max_failure_classification():
    m = max_.Max("token", "-1", ["1"], [])
    assert m.not_sent(http_error(403)) and m.not_sent(http_error(404)) and m.not_sent(http_error(429))
    assert not m.not_sent(http_error(408)) and not m.not_sent(http_error(500))
    assert not m.not_sent(RuntimeError("timeout"))


def test_max_ids_become_numbers():
    assert max_._id("123") == 123 and max_._id("-45") == -45 and max_._id("@chan") == "@chan"


def test_max_ignores_group_and_stays_quiet_for_employee(monkeypatch):
    m = max_.Max("token", "-1", ["1"], ["9"])
    sent = []

    async def notify(uid, text):
        sent.append((uid, text))
    monkeypatch.setattr(m, "notify", notify)
    core.ADAPTERS["max"] = m
    run(m.on_message({"recipient": {"chat_type": "chat"}, "sender": {"user_id": 9}, "body": {"text": "привет"}}))
    run(m.on_message({"recipient": {"chat_type": "dialog"}, "sender": {"user_id": 9}, "body": {"text": "привет"}}))
    assert sent == []  # в группе — игнор, сотруднику в личке — тишина


# ---------- main ----------
def test_need_rejects_empty_setting(monkeypatch):
    monkeypatch.delenv("SOME_SETTING", raising=False)
    with pytest.raises(SystemExit, match="SOME_SETTING"):
        main.need("SOME_SETTING")


def test_supervise_restarts_only_the_failed_adapter():
    calls = {"n": 0}

    class Flaky:
        name = "flaky"

        async def run(self):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("сеть")
    run(main.supervise(Flaky()))
    assert calls["n"] == 3


@pytest.fixture
def bot_tg():
    return tg.Telegram("123456:ABCDEF", "-1")
