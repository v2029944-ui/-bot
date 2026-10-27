"""Telegram- и MAX-адаптеры: фильтры, тихий режим, классификация сбоев. Без сети."""
import io
import json
import types

import httpx
import pytest
from aiogram import F
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendPhoto

import core
import main
import max_
import tg
from conftest import run

S = types.SimpleNamespace


class FakeBot:
    def __init__(self):
        self.log = []

    async def download(self, f):
        return io.BytesIO(b"img")

    async def set_message_reaction(self, **kw):
        self.log.append(("reaction", kw["message_id"]))

    async def send_message(self, chat, text, **kw):
        self.log.append(("to_approver", chat, text[:50]))


class Msg:
    chat = S(id=5)
    message_id = 7
    caption = "Заменили масло"
    from_user = S(id=9, full_name="Вася")

    def __init__(self):
        self.replies = []

    async def answer(self, text, **kw):
        self.replies.append(text)


@pytest.fixture
def bot_tg():
    t = tg.Telegram("123456:ABCDEF", "-1", ["1"], ["9"])
    t.bot = FakeBot()
    core.ADAPTERS["tg"] = t
    return t


# ---------- Telegram ----------
def test_filters_private_chats_and_image_documents():
    private = F.chat.type == "private"
    assert private.resolve(S(chat=S(type="private"))) and not private.resolve(S(chat=S(type="supergroup")))


def test_success_gives_reaction_and_no_reply(bot_tg, monkeypatch):
    async def ok(*a, **k):
        return 1
    monkeypatch.setattr(core, "submit", ok)
    m = Msg()
    run(bot_tg._handle_image(m, object()))
    assert bot_tg.bot.log == [("reaction", 7)] and m.replies == []


def test_confirm_modes(bot_tg, monkeypatch):
    m = Msg()
    monkeypatch.setenv("EMPLOYEE_CONFIRM", "text")
    run(bot_tg._confirm(m))
    assert m.replies == ["✅ Принято"]
    monkeypatch.setenv("EMPLOYEE_CONFIRM", "off")
    run(bot_tg._confirm(m))
    assert m.replies == ["✅ Принято"] and bot_tg.bot.log == []


def test_reaction_failure_falls_back_to_text(bot_tg):
    async def boom(**kw):
        raise RuntimeError("no")
    bot_tg.bot.set_message_reaction = boom
    m = Msg()
    run(bot_tg._confirm(m))
    assert m.replies == ["✅ Принято"]


def test_bad_file_is_silent_but_llm_failure_reaches_approvers(bot_tg, monkeypatch):
    async def bad(*a, **k):
        raise core.BadImage("не изображение")
    monkeypatch.setattr(core, "submit", bad)
    m = Msg()
    run(bot_tg._handle_image(m, object()))
    assert m.replies == [] and bot_tg.bot.log == []

    async def broken_llm(*a, **k):
        raise json.JSONDecodeError("bad", "", 0)
    monkeypatch.setattr(core, "submit", broken_llm)
    run(bot_tg._handle_image(m, object()))
    assert bot_tg.bot.log and bot_tg.bot.log[0][0] == "to_approver" and m.replies == []


def test_rate_limit_is_silent_for_employee(bot_tg, monkeypatch):
    async def limited(*a, **k):
        raise core.RateLimited("x")
    monkeypatch.setattr(core, "submit", limited)
    m = Msg()
    run(bot_tg._handle_image(m, object()))
    assert m.replies == [] and bot_tg.bot.log == []


def test_stranger_is_ignored_silently(bot_tg):
    m = Msg()
    m.from_user = S(id=77, full_name="Чужой")
    run(bot_tg._handle_image(m, object()))
    assert m.replies == [] and bot_tg.bot.log == []


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
