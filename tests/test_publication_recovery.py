import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from conftest import run
from test_admin_menu_button import Api, buttons

import core
import max_
from tg import Telegram


class TelegramMessages:
    def __init__(self, *errors):
        self.errors = list(errors)
        self.media = []
        self.text = []

    async def send_photo(self, chat, *args, **kwargs):
        self.media.append(chat)
        return SimpleNamespace(chat=SimpleNamespace(id=-200))

    async def send_message(self, chat, text, **kwargs):
        self.text.append((chat, text))
        if self.errors:
            raise self.errors.pop(0)


def telegram(*errors):
    sender = Telegram.__new__(Telegram)
    sender.channel = "-200"
    sender.bot = TelegramMessages(*errors)
    sender.admins, sender.approvers, sender.allowed = [], [], set()
    core.ADAPTERS["tg"] = sender
    return sender


def refused():
    return TelegramForbiddenError(method=SendMessage(chat_id=-200, text="Текст"), message="Forbidden")


def partial(mx, llm, png, *errors, telegram_first=False):
    mx.channel = "-100"
    if telegram_first:
        core.ADAPTERS.pop("max")
    sender = telegram(*errors)
    if telegram_first:
        core.ADAPTERS["max"] = mx
    llm.reply = "Длинный текст. " * 90
    did = run(core.submit(mx, "9", "Автор", "Описание", png))
    assert not run(core.on_action(mx, "5", "okyes", did, 1))[0]
    return did, sender


def test_send_text_in_telegram_without_generic_retry(mx, llm, png):
    did, sender = partial(mx, llm, png, refused())
    rows = mx.sent("menu")[-1][3]
    assert any(label == "Дослать текст" and data == f"d:sendtext:{did}:1" for row in rows for label, data in row)
    assert run(core.on_action(mx, "5", "sendtext", did, 1))[0]
    assert core.get(did)["status"] == "published"
    assert sender.bot.media == [-200]
    assert len(sender.bot.text) == 2 and sender.bot.text[0] == sender.bot.text[1]
    assert len(mx.sent("publish")) == 1


def test_telegram_text_timeout_requires_explicit_retry(mx, llm, png):
    did, sender = partial(mx, llm, png, refused(), TimeoutError("ответ потерян"), refused())
    run(core.on_action(mx, "5", "sendtext", did, 1))
    assert len(sender.bot.text) == 2
    run(core.on_action(mx, "5", "sendtext", did, 1))  # stale safe-text button
    assert len(sender.bot.text) == 2
    assert "подтвердите именно текст" in run(core.on_action(mx, "5", "seen_tg", did, 1))[1]
    assert core.get(did)["status"] == "pending"
    assert any(data == f"d:seen_tg_text:{did}:1" for row in mx.sent("menu")[-1][3] for _, data in row)
    run(core.on_action(mx, "5", "okyes", did, 1))
    assert len(sender.bot.text) == 2
    assert any(data == f"d:okforce:{did}:1" for row in mx.sent("menu")[-1][3] for _, data in row)
    assert not run(core.on_action(mx, "5", "okforce", did, 1))[0]
    assert json.loads(core.get(did)["publish_progress"])["tg"]["text_started"]
    assert run(core.on_action(mx, "5", "okforce", did, 1))[0]
    assert sender.bot.media == [-200]
    assert len(sender.bot.text) == 4


def test_admin_can_confirm_observed_telegram_text_without_resending(mx, llm, png):
    did, sender = partial(mx, llm, png, TimeoutError("ответ потерян"))
    run(core.on_action(mx, "5", "seen_tg_text", did, 1))
    assert core.get(did)["status"] == "published"
    assert sender.bot.media == [-200] and len(sender.bot.text) == 1


def test_send_text_never_redirects_saved_telegram_destination(mx, llm, png):
    did, sender = partial(mx, llm, png, refused())
    sender.channel = "-201"
    run(core.on_action(mx, "5", "sendtext", did, 1))
    assert len(sender.bot.text) == 1 and core.get(did)["status"] == "pending"
    text, rows = core.publication_view(core.get(did), mx)
    assert "-200" in text and "-201" not in text
    assert any(data == f"d:pubclose:{did}:1" for row in rows for _, data in row)


def test_telegram_resume_skips_already_sent_media_even_with_short_text():
    sender = telegram()
    stages = []
    run(sender.publish("already-sent.jpg", "Короткий текст", resume={"media": True}, progress=stages.append))
    assert sender.bot.media == []
    assert sender.bot.text == [(-200, "Короткий текст")]
    assert stages == ["text_started", "text"]


def test_telegram_alias_text_is_pinned_to_chat_that_received_media(mx, llm, png):
    mx.channel = "-100"
    sender = telegram(refused())
    sender.channel = "@old_group"
    llm.reply = "Длинный текст. " * 90
    did = run(core.submit(mx, "9", "Автор", "Описание", png))
    assert not run(core.on_action(mx, "5", "okyes", did, 1))[0]
    assert json.loads(core.get(did)["publish_progress"])["tg"] == {"media": True, "media_chat_id": -200}
    assert run(core.on_action(mx, "5", "sendtext", did, 1))[0]
    assert sender.bot.media == ["@old_group"]
    assert [chat for chat, _ in sender.bot.text] == [-200, -200]


def test_legacy_telegram_alias_without_pinned_chat_cannot_resume(mx, llm, png):
    did, sender = partial(mx, llm, png, refused())
    sender.channel = "@old_group"
    core._set(did, publication_channels='{"max":"-100","tg":"@old_group"}')
    for action in ("sendtext", "okyes", "okforce"):
        assert not run(core.on_action(mx, "5", action, did, 1))[0]
    assert sender.bot.media == [-200] and len(sender.bot.text) == 1
    assert core.get(did)["status"] == "pending"


def test_legacy_telegram_media_without_destination_snapshot_cannot_resume(mx, llm, png):
    did, sender = partial(mx, llm, png, refused())
    core._set(did, publication_channels="{}")
    assert not run(core.on_action(mx, "5", "okforce", did, 1))[0]
    assert sender.bot.media == [-200] and len(sender.bot.text) == 1


def test_legacy_unknown_telegram_cannot_be_skipped_when_adapter_is_missing(mx, llm, png):
    did = run(core.submit(mx, "9", "Автор", "Описание", png))
    core._set(did, published_to="max,tg?")
    assert not run(core.on_action(mx, "5", "okyes", did, 1))[0]
    assert core.get(did)["status"] == "pending" and not mx.sent("publish")
    _, rows = core.publication_view(core.get(did), mx)
    assert any(data == f"d:pubclose:{did}:1" for row in rows for _, data in row)


def test_text_resume_leaves_other_unsent_channels_pending(mx, llm, png, monkeypatch):
    monkeypatch.setenv("PUBLISH_TO_ALL", "1")
    did, sender = partial(mx, llm, png, refused(), telegram_first=True)
    assert run(core.on_action(mx, "5", "sendtext", did, 1))[0]
    assert not mx.sent("publish")
    assert sender.bot.media == [-200] and len(sender.bot.text) == 2
    assert core.get(did)["status"] == "pending"


def test_close_partial_publication_requires_one_use_admin_confirmation(llm, png, monkeypatch):
    async def scenario():
        adapter = max_.Max("token", "-1", ["5"], ["9"], admins=["5"])
        core.ADAPTERS["max"] = adapter
        api = Api()
        async def upload(path):
            return "photo-token"

        monkeypatch.setattr(adapter, "_upload_image", upload)
        await adapter.http.aclose()
        async with httpx.AsyncClient(base_url=max_.API, transport=httpx.MockTransport(api.handler)) as client:
            adapter.http = client
            did = await core.submit(adapter, "9", "Автор", "Описание", png, source_key="material")
            photo = Path(core.get(did)["photo"])
            core._set(did, published_to="max,tg?", publication_channels='{"max":"-1","tg":"-200"}')

            async def click(payload, uid=5):
                await adapter.on_callback({"callback_id": "test", "user": {"user_id": uid}, "payload": payload})

            await click(f"d:pubcloseyes:{did}:1")
            assert core.get(did)["status"] == "pending" and photo.exists()

            await click(f"d:pubclose:{did}:1")
            menu = api.of("POST")[-1][3]
            confirm = next(b["payload"] for b in buttons(menu) if b["payload"].startswith(f"d:pubcloseyes:{did}:1:"))
            core._set(did, publish_progress='{"tg":{"media":true,"text_started":true}}')
            await click(confirm)
            assert core.get(did)["status"] == "pending" and photo.exists()
            assert "Результат изменился" in api.of("POST")[-1][3]["text"]
            core._set(did, publish_progress="{}")

            await click(f"d:pubclose:{did}:1")
            menu = api.of("POST")[-1][3]
            confirm = next(b["payload"] for b in buttons(menu) if b["payload"].startswith(f"d:pubcloseyes:{did}:1:"))
            await click(confirm, 9)
            assert core.get(did)["status"] == "pending" and photo.exists()
            await click(confirm)
            closed = core.get(did)
            assert closed["status"] == "expired" and closed["closed_at"] > 0 and closed["published_at"] == 0
            assert closed["source_key"] == "material" and not photo.exists()
            assert closed["post"] == "" and closed["publication_channels"] == "{}"
            report = api.of("POST")[-1][3]["text"]
            assert "MAX — ✅ опубликовано (канал -1)" in report
            assert "Telegram — ❓ результат неизвестен (канал -200)" in report
            await click(confirm)
            assert core.get(did)["closed_at"] == closed["closed_at"]
            assert await core.resume_submission(adapter, "material") == did
            assert len(llm.calls) == 1

    run(scenario())


def test_startup_scrubs_interrupted_partial_closure(mx, llm, png):
    did, _ = partial(mx, llm, png, refused())
    path = Path(core.get(did)["photo"])
    assert core._claim(did, "pending", "expired")
    core.startup()
    d = core.get(did)
    assert d["status"] == "expired" and d["closed_at"] > 0
    assert d["cleaned"] == 2 and d["post"] == "" and d["published_to"] == "" and not path.exists()
