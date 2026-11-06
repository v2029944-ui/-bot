"""Неизвестные команды в режиме ожидания, закрытие старых черновиков, события MAX на подставных данных."""
import time
import types

import pytest
from conftest import draft_id, run

import core
import max_

S = types.SimpleNamespace


# ---------- неизвестная команда не становится данными ----------
def test_unknown_command_is_not_taken_as_post_rule_or_comment(tg, llm, png):
    run(core.submit(tg, "9", "Анна", "Заменили масло и фильтры", png))
    did = draft_id()
    calls = len(llm.calls)

    run(core.on_action(tg, "1", "edit", did, 1))
    assert run(core.on_text(tg, "1", "/help")) is True
    assert core.get(did)["attempts"] == 1 and ("tg", "1") in core.WAITING_EDIT  # ждём текст дальше
    assert "Неизвестная команда" in tg.sent("notify")[-1][2]
    core.WAITING_EDIT.clear()

    run(core.on_menu(tg, "1", "m:add"))
    run(core.on_text(tg, "1", "/help"))
    assert core.db.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0 and ("tg", "1") in core.WAITING_RULE
    core.WAITING_RULE.clear()

    run(core.on_action(tg, "1", "redoc", did, 1))
    run(core.on_text(tg, "1", "/help"))
    assert len(llm.calls) == calls and ("tg", "1") in core.WAITING  # нейросеть не вызывалась


def test_cancel_and_known_commands_still_work_in_waiting_state(tg, llm, png):
    run(core.submit(tg, "9", "Анна", "Заменили масло и фильтры", png))
    did = draft_id()
    run(core.on_action(tg, "1", "edit", did, 1))
    run(core.on_text(tg, "1", "/cancel"))
    assert not core.WAITING_EDIT
    run(core.on_menu(tg, "1", "m:add"))
    run(core.on_text(tg, "1", "/rules"))  # команда отвечает, ожидание правила остаётся
    assert ("tg", "1") in core.WAITING_RULE


def test_unknown_command_outside_waiting_is_unchanged(tg):
    run(core.on_text(tg, "1", "/help"))
    assert not any("Неизвестная команда" in o[2] for o in tg.sent("notify"))


# ---------- закрытие брошенных черновиков ----------
def test_stale_pending_drafts_are_closed_at_startup(tg, llm, png, monkeypatch):
    run(core.submit(tg, "9", "Анна", "Заменили масло и фильтры", png))
    old = draft_id()
    run(core.submit(tg, "9", "Анна", "Заменили масло, второй раз", png))
    fresh = draft_id()
    photo = core.get(old)["photo"]
    t = time.time() - 31 * 86400
    core.db.execute("UPDATE drafts SET created=?, last_sent=? WHERE id=?", (t, t, old))
    core.db.commit()
    core.startup()
    assert core.get(old)["status"] == "expired" and core.get(fresh)["status"] == "pending"
    from pathlib import Path
    assert not Path(photo).exists() and core.LAST_EXPIRED == 1
    assert "уже обработан" in run(core.on_action(tg, "1", "okyes", old, 1))[1]
    tg.admins = ["1"]
    run(core.announce_start())
    assert "Закрыто неразобранных" in tg.sent("notify")[-1][2]


def test_draft_ttl_zero_disables_closing(tg, llm, png, monkeypatch):
    run(core.submit(tg, "9", "Анна", "Заменили масло и фильтры", png))
    did = draft_id()
    core.db.execute("UPDATE drafts SET created=?, last_sent=0", (time.time() - 90 * 86400,))
    core.db.commit()
    monkeypatch.setenv("DRAFT_TTL_DAYS", "0")
    core.startup()
    assert core.get(did)["status"] == "pending"


# ---------- MAX: события на подставных данных ----------
@pytest.fixture
def mx_bot(monkeypatch):
    m = max_.Max("token", "-1", ["5"], ["9"], ["5"])
    core.ADAPTERS["max"] = m
    m.out = []

    async def notify(uid, text):
        m.out.append(("notify", str(uid), text))

    async def send_menu(uid, text, rows):
        m.out.append(("menu", str(uid), text, rows))

    async def call(method, path, **kw):
        m.out.append(("call", path))
        return {}
    monkeypatch.setattr(m, "notify", notify)
    monkeypatch.setattr(m, "send_menu", send_menu)
    monkeypatch.setattr(m, "call", call)
    return m


def test_max_bot_started_approver_gets_admin_menu(mx_bot):
    run(mx_bot.handle({"update_type": "bot_started", "user": {"user_id": 5}}))
    menu = [o for o in mx_bot.out if o[0] == "menu"][0]
    assert menu[1] == "5" and ("👥 Люди", "m:people") in [b for r in menu[3] for b in r]


def test_max_stranger_bot_started_creates_request_for_admin(mx_bot):
    run(mx_bot.handle({"update_type": "bot_started", "user": {"user_id": 77, "name": "Иван"}}))
    menus = [o for o in mx_bot.out if o[0] == "menu"]
    assert len(menus) == 1 and menus[0][1] == "5" and "Иван" in menus[0][2]
    assert not [o for o in mx_bot.out if o[0] == "notify" and o[1] == "77"]  # незнакомцу тишина


def test_max_photo_with_caption_goes_to_submit(mx_bot, monkeypatch):
    got = []

    async def fake_submit(adapter, uid, name, text, data):
        got.append((uid, name, text, data))
        return 1
    monkeypatch.setattr(core, "submit", fake_submit)

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        def stream(self, method, url):  # MAX-адаптер скачивает вложение потоком
            class Resp:
                headers = {}

                async def __aenter__(self_inner):
                    return self_inner

                async def __aexit__(self_inner, *a):
                    pass

                def raise_for_status(self_inner):
                    pass

                async def aiter_bytes(self_inner):
                    yield b"IMG"
            return Resp()
    monkeypatch.setattr(max_.httpx, "AsyncClient", Client)
    msg = {"recipient": {"chat_type": "dialog"}, "sender": {"user_id": 9, "name": "Вася"},
           "body": {"text": "Заменили масло и фильтры", "attachments": [{"type": "image", "payload": {"url": "http://x"}}]}}
    run(mx_bot.handle({"update_type": "message_created", "message": msg}))
    assert got == [("9", "Вася", "Заменили масло и фильтры", b"IMG")]
    assert ("notify", "9", "Пост готов к редактированию и публикации ✅") in mx_bot.out  # подтверждение вместо реакции


def test_max_callbacks_route_menu_and_draft_actions(mx_bot, monkeypatch):
    run(mx_bot.handle({"update_type": "message_callback",
                       "callback": {"callback_id": "c1", "user": {"user_id": 5}, "payload": "m:people"}}))
    assert any(o[0] == "menu" and "Люди" in o[2] for o in mx_bot.out)
    assert ("call", "/answers") in mx_bot.out
    seen = []

    async def action(adapter, uid, act, did, attempt):
        seen.append((uid, act, did, attempt))
        return True, "ок"
    monkeypatch.setattr(core, "on_action", action)
    run(mx_bot.handle({"update_type": "message_callback",
                       "callback": {"callback_id": "c2", "user": {"user_id": 5}, "payload": "d:edit:3:2"}}))
    assert seen == [(5, "edit", 3, 2)] and ("notify", "5", "ок") in mx_bot.out


def test_max_photo_without_caption_asks_even_for_quiet_employee(mx_bot):
    msg = {"recipient": {"chat_type": "dialog"}, "sender": {"user_id": 9},
           "body": {"attachments": [{"type": "image", "payload": {"url": "http://x"}}]}}
    run(mx_bot.handle({"update_type": "message_created", "message": msg}))
    assert ("notify", "9", core.CAPTION_ASK) in mx_bot.out
