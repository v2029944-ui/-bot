"""Регрессия по замечаниям аудита: гонка публикаций, альбомы, «показать», залипшие ожидания, лимиты."""
import asyncio
import time
import types

import pytest

import core
import tg as tgmod
from conftest import FakeAdapter, draft_id, run

S = types.SimpleNamespace


class Msg:
    chat = S(id=5)
    message_id = 7
    from_user = S(id=9, full_name="Вася")

    def __init__(self, caption=None, group=None):
        self.caption, self.media_group_id, self.replies = caption, group, []

    async def answer(self, text, **kw):
        self.replies.append(text)


@pytest.fixture
def bot_tg(monkeypatch):
    t = tgmod.Telegram("123456:ABCDEF", "-1", ["1"], ["9"])
    core.ADAPTERS["tg"] = t
    t.called = []

    async def fake_submit(*a, **k):
        t.called.append(a)
        return 1
    monkeypatch.setattr(core, "submit", fake_submit)
    return t


# 1.1 — признак «фото уже ушло» живёт в ошибке, а не в адаптере
def test_partial_flag_is_per_error_not_shared_between_concurrent_publishes(tmp_path):
    t = tgmod.Telegram("123456:ABCDEF", "-1", ["1"], ["9"])
    photo = tmp_path / "p.jpg"
    photo.write_bytes(b"x")
    gate = asyncio.Event()

    class Bot:
        async def send_photo(self, chat, photo, **kw):
            pass

        async def send_message(self, chat, text, **kw):
            if text == "ДЛИННЫЙ":
                await gate.wait()  # фото уже в канале, текст ещё в пути
                raise RuntimeError("обрыв")

    t.bot = Bot()
    long_post = "ДЛИННЫЙ"

    async def scenario():
        orig = tgmod._tg_len
        tgmod._tg_len = lambda s: 5000 if s == long_post else 10  # «длинный» уходит двумя сообщениями
        try:
            first = asyncio.create_task(t.publish(str(photo), long_post))
            await asyncio.sleep(0)
            await t.publish(str(photo), "короткий")  # второй пост ничего не «сбрасывает»
            gate.set()
            with pytest.raises(RuntimeError) as ei:
                await first
            return ei.value
        finally:
            tgmod._tg_len = orig
    err = run(scenario())
    assert getattr(err, "partial", False) and not t.not_sent(err)  # дубль невозможен


def test_utf16_length_counts_emoji_twice():
    assert tgmod._tg_len("ab") == 2 and tgmod._tg_len("😀") == 2


# 1.2 — альбом с короткой подписью не должен молчать
def test_album_photo_with_short_caption_gets_ask_but_without_caption_is_silent(bot_tg):
    short = Msg(caption="ок", group="g")
    run(bot_tg._handle_image(short, object()))
    assert short.replies == [core.CAPTION_ASK]
    none = Msg(caption=None, group="g")
    run(bot_tg._handle_image(none, object()))
    assert none.replies == []


# 2.3 — файл больше 20 МБ
def test_file_over_telegram_limit_is_reported_not_downloaded(bot_tg):
    sent = []

    class Bot:
        async def send_message(self, chat, text, **kw):
            sent.append((chat, text))
    bot_tg.bot = Bot()
    # сотрудник в тихом режиме молчит, а согласующему уходит предупреждение
    m = Msg(caption="Заменили масло")
    run(bot_tg._handle_image(m, S(file_size=21 * 1024 * 1024)))
    assert bot_tg.called == [] and m.replies == []
    assert sent and sent[0][0] == 1 and "20 МБ" in sent[0][1]
    # согласующий-не-тихий получил бы ответ в чат (EMPLOYEE_NOTIFY=1)


# 1.3 — «показать» работает и после пересоздания поста
def test_show_works_after_regeneration(tg, llm, png):
    run(core.submit(tg, "9", "Анна", "Заменили масло и фильтры", png))
    did = draft_id()
    run(core.on_action(tg, "1", "redo", did, 1))  # теперь вариант №2
    before = len(tg.sent("approval"))
    ok, msg = run(core.on_action(tg, "2", "show", did, 1))  # кнопка из старого напоминания
    assert msg == "" and len(tg.sent("approval")) == before + 1
    assert tg.sent("approval")[-1][3] == core.get(did)["post"]  # показан актуальный текст
    assert "устаревший" in run(core.on_action(tg, "2", "ok", did, 1))[1]  # а вот ✅ по старому — нет


# 1.4 — залипшие ожидания
def test_menu_navigation_cancels_waiting_states(tg):
    for label in ("👥 Люди", "📝 Правила стиля"):
        core.WAITING_RULE.add(("tg", "1"))
        core.WAITING_EDIT[("tg", "1")] = (1, 1)
        core.WAITING[("tg", "1")] = 1
        run(core.on_text(tg, "1", label))
        assert not core.WAITING_RULE and not core.WAITING_EDIT and not core.WAITING


def test_commands_are_not_saved_as_a_rule(tg):
    run(core.on_menu(tg, "1", "m:add"))
    assert ("tg", "1") in core.WAITING_RULE
    run(core.on_text(tg, "1", "/rules"))
    assert core.db.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0
    run(core.on_text(tg, "1", "Пиши теплее"))  # а обычный текст по-прежнему становится правилом
    assert core.db.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 1


# 2.4 — непредвиденный сбой не оставляет «publishing»
def test_unexpected_crash_returns_draft_to_pending(tg, llm, png, monkeypatch):
    run(core.submit(tg, "9", "Анна", "Заменили масло и фильтры", png))
    did = draft_id()

    async def boom(*a, **k):
        raise RuntimeError("диск")
    monkeypatch.setattr(core, "_safe_notify", boom)  # сбой уже после публикации
    ok, msg = run(core.on_action(tg, "1", "ok", did, 1))
    assert core.get(did)["status"] in ("published", "pending")  # главное — не «publishing»
    assert "Сбой" in msg or core.get(did)["status"] == "published"

    run(core.submit(tg, "9", "Анна", "Ещё одно описание", png))
    did2 = draft_id()
    monkeypatch.setattr(core, "_set", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sqlite")))
    ok, msg = run(core.on_action(tg, "1", "ok", did2, 1))
    monkeypatch.undo()
    assert not ok and "Сбой при публикации" in msg and core.get(did2)["status"] == "pending"


# 2.5 — обучение можно отключить
def test_learning_can_be_disabled(tg, llm, png, monkeypatch):
    run(core.submit(tg, "9", "Анна", "Заменили масло и фильтры", png))
    did = draft_id()
    llm.reply = lambda system, text: "Всегда пиши теплее" if "Редактор оставил комментарий" in text else "Новый текст."
    monkeypatch.setenv("LEARN_RULES", "0")
    run(core.on_action(tg, "1", "redoc", did, 1))
    run(core.on_text(tg, "1", "Сделай теплее"))
    assert core.db.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0


# мелочи: протухшие заявки и «сиротские» токены
def test_stale_pending_requests_do_not_block_new_people(monkeypatch):
    a = FakeAdapter("tg", ["1"])
    a.admins = ["1"]
    core.ADAPTERS["tg"] = a
    core.refresh_people(a)
    monkeypatch.setattr(core, "MAX_PENDING_REQUESTS", 1)
    assert run(core.unknown_user(a, "100", "Старая")) is True
    assert run(core.unknown_user(a, "101", "Новая")) is False  # очередь полна
    core.db.execute("UPDATE requests SET created=?", (time.time() - core.REQ_TTL - 5,))
    core.db.commit()
    assert run(core.unknown_user(a, "101", "Новая")) is True  # старая заявка очистилась


def test_no_orphan_invite_token_when_links_unsupported():
    a = FakeAdapter("max", ["5"])
    a.admins = ["5"]
    core.ADAPTERS["max"] = a
    core.refresh_people(a)
    run(core.on_menu(a, "5", "m:invn:emp"))
    assert core.db.execute("SELECT COUNT(*) FROM invites").fetchone()[0] == 0


# устаревшее нажатие кнопки не мешает обработке
def test_expired_callback_answer_does_not_block_action(bot_tg, monkeypatch):
    seen = []

    async def action(*a, **k):
        seen.append(a[2])
        return True, ""
    monkeypatch.setattr(core, "on_action", action)

    class CB:
        data = "d:ok:1:1"
        from_user = S(id=1)
        message = None

        async def answer(self):
            raise RuntimeError("query is too old")
    run(bot_tg.on_cb(CB()))
    assert seen == ["ok"]
