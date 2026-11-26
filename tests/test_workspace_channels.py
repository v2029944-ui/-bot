from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.types import ChatFullInfo, ChatMemberAdministrator, ChatMemberMember, ChatPermissions
from conftest import run
from test_workspace_settings import create
from test_workspace_settings import runtime as runtime

import core
import max_
import spaces
import tg


def test_card_checks_only_the_selected_workspace_channels(runtime, monkeypatch):
    adapter, messages = runtime
    monkeypatch.setenv("TG_TOKEN", "123456:ABCDEF")
    first = create(adapter, 11, -101, "@first_channel")["id"]
    second = create(adapter, 22, -102)["id"]
    spaces.select_for_user("1", second)
    _, rows = run(core.on_menu(adapter, "1", f"m:space:{first}"))
    button = next((payload for row in rows for label, payload in row if "Проверить каналы" in label), None)
    assert button is not None

    api_calls = []

    async def max_api(self, method, path, **kwargs):
        api_calls.append((method, path))
        if path == "/answers":
            return {"success": True}
        if path.endswith("/members/me"):
            return {"is_admin": True, "is_owner": False, "permissions": ["read_all_messages", "write"]}
        return {"chat_id": -101, "title": "MAX команды", "status": "active", "type": "channel"}

    chat = ChatFullInfo.model_construct(id=-100123, title="Telegram команды", type="channel")
    member = ChatMemberAdministrator.model_construct(can_post_messages=False)
    bot = spaces.get(first).memory["ADAPTERS"]["tg"].bot
    get_chat = AsyncMock(return_value=chat)
    get_chat_member = AsyncMock(return_value=member)
    monkeypatch.setattr(max_.Max, "call", max_api)
    monkeypatch.setattr(bot, "get_chat", get_chat)
    monkeypatch.setattr(bot, "get_chat_member", get_chat_member)
    monkeypatch.setattr(bot, "send_message", AsyncMock(side_effect=AssertionError("posting during channel check")))

    run(adapter.handle({"update_type": "message_callback", "callback": {
        "callback_id": "channel-check", "user": {"user_id": 1}, "payload": f"w:0:{button}",
    }}))
    uid, text, rows = messages[-1]
    assert uid == "1"
    assert "MAX команды" in text and "-101" in text and "write" in text
    assert "Telegram команды" in text and "-100123" in text and "нет права публикации" in text
    assert f"m:space:{first}" in {payload for row in rows for _, payload in row}
    assert api_calls == [("POST", "/answers"), ("GET", "/chats/-101"), ("GET", "/chats/-101/members/me")]
    assert get_chat.await_args.args == ("@first_channel",)
    assert get_chat_member.await_args.args == (-100123, 123456)


@pytest.mark.parametrize("case", ["owner", "wrong_context", "disabled_root", "unknown_sid", "malformed_sid"])
def test_stale_or_unauthorized_channel_checks_make_no_requests(runtime, monkeypatch, case):
    adapter, _ = runtime
    sid = create(adapter)["id"]
    api = AsyncMock(side_effect=AssertionError("unauthorized network request"))
    monkeypatch.setattr(max_.Max, "call", api)
    if case == "owner":
        text, _ = run(core.on_menu(adapter, "11", f"m:space_check:{sid}"))
        assert "прав" in text or "главный" in text
    elif case == "wrong_context":
        space = spaces.get(sid)
        with spaces.enter(space):
            text, _ = run(core.on_menu(space.memory["ADAPTERS"]["max"], "1", f"m:space_check:{sid}"))
        assert "главный" in text
    elif case == "disabled_root":
        run(core.on_menu(adapter, "1", "m:root_mode:enable"))
        text, _ = run(core.on_menu(adapter, "1", "m:space_check:0"))
        assert "служебном режиме" in text
    else:
        target = "99999999" if case == "unknown_sid" else f"{sid}:extra"
        text, _ = run(core.on_menu(adapter, "1", f"m:space_check:{target}"))
        assert "не найдено" in text
    api.assert_not_awaited()


def test_max_chat_details_survive_a_failed_membership_lookup(runtime, monkeypatch):
    adapter, _ = runtime
    sid = create(adapter)["id"]

    async def max_api(self, method, path, **kwargs):
        if path.endswith("/members/me"):
            raise httpx.ReadTimeout("dummy secret")
        return {"chat_id": -101, "title": "Рабочая группа", "status": "active", "type": "chat"}

    monkeypatch.setattr(max_.Max, "call", max_api)
    telegram_check = AsyncMock(side_effect=AssertionError("unconfigured Telegram"))
    monkeypatch.setattr(tg.Telegram, "__init__", telegram_check)
    text, _ = run(core.on_menu(adapter, "1", f"m:space_check:{sid}"))
    assert "Рабочая группа" in text and "-101" in text
    assert "не удалось проверить права" in text.lower()
    assert "Telegram: не подключён" in text
    assert "dummy secret" not in text
    telegram_check.assert_not_called()


def test_malformed_max_permissions_get_reported_as_unknown(runtime, monkeypatch):
    adapter, _ = runtime
    sid = create(adapter)["id"]
    monkeypatch.setattr(max_.Max, "call", AsyncMock(side_effect=[
        {"chat_id": -101, "title": "Канал", "status": "active", "type": "channel"},
        {"is_admin": True, "is_owner": False, "permissions": [{"nested": "value"}, "\n" * 4000]},
    ]))

    text, _ = run(core.on_menu(adapter, "1", f"m:space_check:{sid}"))
    assert "MAX не сообщил право публикации" in text
    assert "nested" not in text
    assert len(text) < 1000


def test_telegram_group_shows_reported_and_missing_media_permissions(runtime, monkeypatch):
    adapter, _ = runtime
    monkeypatch.setenv("TG_TOKEN", "123456:ABCDEF")
    sid = create(adapter, telegram="-100123")["id"]
    monkeypatch.setattr(max_.Max, "call", AsyncMock(side_effect=httpx.ReadTimeout("unreachable")))
    bot = spaces.get(sid).memory["ADAPTERS"]["tg"].bot
    chat = ChatFullInfo.model_construct(
        id=-100123, title="Группа", type="supergroup",
        permissions=ChatPermissions(can_send_messages=True, can_send_photos=False),
    )
    monkeypatch.setattr(bot, "get_chat", AsyncMock(return_value=chat))
    monkeypatch.setattr(bot, "get_chat_member", AsyncMock(return_value=ChatMemberMember.model_construct()))

    text, _ = run(core.on_menu(adapter, "1", f"m:space_check:{sid}"))
    assert "MAX (ID -101): не удалось" in text
    assert "текст — да, фото — нет, видео — нет данных" in text
