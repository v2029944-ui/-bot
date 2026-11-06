"""Проверка подписи, «Править самому», напоминания, статус и сообщения о сбоях. Без сети."""
import time
import types

import pytest
from conftest import FakeAdapter, draft_id, run

import core
import main
import max_

S = types.SimpleNamespace


# ---------- подпись ----------
def test_caption_ok_rules(monkeypatch):
    assert not core.caption_ok(None) and not core.caption_ok("  ") and not core.caption_ok("ок")
    assert core.caption_ok("Заменили масло")
    monkeypatch.setenv("REQUIRE_CAPTION", "0")
    assert core.caption_ok("")
    monkeypatch.setenv("REQUIRE_CAPTION", "1")
    monkeypatch.setenv("MIN_CAPTION_CHARS", "3")
    assert core.caption_ok("ТО-2")


def test_max_photo_without_caption_asks_for_it(monkeypatch):
    m = max_.Max("token", "-1", ["1"], ["9"])
    sent = []

    async def notify(uid, text):
        sent.append((uid, text))
    monkeypatch.setattr(m, "notify", notify)
    core.ADAPTERS["max"] = m
    msg = {"recipient": {"chat_type": "dialog"}, "sender": {"user_id": 9},
           "body": {"attachments": [{"type": "image", "payload": {"url": "http://x"}}]}}
    run(m.on_message(msg))
    assert sent == [(9, core.CAPTION_ASK)]


# ---------- Править самому ----------
def new_draft(tg, llm, png):
    run(core.submit(tg, "9", "Анна", "Заменили масло и фильтры", png))
    return draft_id()


def test_edit_replaces_text_without_llm_and_resends_to_everyone(tg, llm, png):
    did = new_draft(tg, llm, png)
    calls = len(llm.calls)
    ok, msg = run(core.on_action(tg, "1", "edit", did, 1))
    assert not ok and "Пришлите исправленный текст" in msg
    assert run(core.on_text(tg, "1", "Мой точный текст поста.")) is True
    d = core.get(did)
    assert d["post"] == "Мой точный текст поста." and d["attempts"] == 2 and d["status"] == "pending"
    assert len(llm.calls) == calls  # нейросеть не вызывалась
    assert [o for o in tg.sent("approval") if o[3] == "Мой точный текст поста."].__len__() == 2  # обоим согласующим
    assert "заменил черновик" in tg.sent("notify")[-1][2]
    assert "устаревший" in run(core.on_action(tg, "1", "okyes", did, 1))[1]  # старые кнопки погашены
    assert not core.WAITING_EDIT


def test_edit_then_publish_does_not_retain_a_style_example(tg, llm, png):
    did = new_draft(tg, llm, png)
    run(core.on_action(tg, "1", "edit", did, 1))
    run(core.on_text(tg, "1", "Эталонный текст."))
    run(core.on_action(tg, "1", "okyes", did, 2))
    assert tg.sent("publish")[-1][1] == "Эталонный текст."
    assert "Эталонный текст." not in core.build_system()
    assert core.get(did)["post"] == ""


def test_edit_cancel_and_too_long_keep_state(tg, llm, png, monkeypatch):
    did = new_draft(tg, llm, png)
    run(core.on_action(tg, "1", "edit", did, 1))
    monkeypatch.setattr(core, "MAX_POST_CHARS", 20)
    run(core.on_text(tg, "1", "x" * 50))
    assert core.get(did)["attempts"] == 1 and ("tg", "1") in core.WAITING_EDIT  # ждём текст дальше
    run(core.on_text(tg, "1", "/cancel"))
    assert not core.WAITING_EDIT and core.get(did)["post"] == "Почистили форсунки на стенде."


def test_edit_not_applied_if_draft_changed_meanwhile(tg, llm, png):
    did = new_draft(tg, llm, png)
    run(core.on_action(tg, "1", "edit", did, 1))
    run(core.on_action(tg, "2", "regen", did, 1))  # второй согласующий пересоздал вариант
    run(core.on_text(tg, "1", "Мой текст"))
    assert core.get(did)["post"] != "Мой текст" and "уже изменён" in tg.sent("notify")[-1][2]


def test_rules_command_does_not_eat_waiting_edit(tg, llm, png):
    did = new_draft(tg, llm, png)
    run(core.on_action(tg, "1", "edit", did, 1))
    run(core.on_text(tg, "1", "/rules"))
    assert ("tg", "1") in core.WAITING_EDIT  # команда ответила, но ожидание правки осталось


def test_only_approver_can_edit(tg, llm, png):
    did = new_draft(tg, llm, png)
    assert "нет прав" in run(core.on_action(tg, "99", "edit", did, 1))[1]


def test_max_approval_has_three_main_buttons(tg, llm, png, monkeypatch):
    did = new_draft(tg, llm, png)
    sent = []

    async def scenario():
        adapter = max_.Max("token", "-1", ["1"], [])

        async def send(target, post, photo, buttons, **kwargs):
            sent.extend(button for row in buttons for button in row)

        monkeypatch.setattr(adapter, "_send", send)
        await adapter.send_for_approval("1", did, core.get(did)["photo"], "Текст")
        await adapter.http.aclose()

    run(scenario())
    assert [b["text"] for b in sent] == ["❌ Отклонить", "✅ Опубликовать", "✏️ Изменить"]
    assert [b["payload"] for b in sent] == [f"d:{a}:{did}:1" for a in ("del", "ok", "change")]


# ---------- напоминания ----------
@pytest.fixture
def window(monkeypatch):
    monkeypatch.setenv("NOTIFY_HOURS", "0-24")
    monkeypatch.setenv("NOTIFY_DAYS", "1-7")


def age_draft(did, hours):
    t = time.time() - hours * 3600
    core.db.execute("UPDATE drafts SET created=?, last_sent=? WHERE id=?", (t, t, did))
    core.db.commit()


def test_reminder_once_with_show_button(tg, llm, png, window):
    did = new_draft(tg, llm, png)
    assert run(core.remind_once()) == 0  # свежий черновик
    age_draft(did, 4)
    assert run(core.remind_once()) == 1
    menus = tg.sent("menu")
    assert {m[1] for m in menus} == {"9"} and "Ждут решения: 1" in menus[0][2]
    assert menus[0][3][0][0][1] == f"d:show:{did}:1"
    assert run(core.remind_once()) == 0  # второй раз не напоминаем


def test_show_button_resends_draft(tg, llm, png, window):
    did = new_draft(tg, llm, png)
    before = len(tg.sent("approval"))
    run(core.on_action(tg, "2", "show", did, 1))
    last = tg.sent("approval")[-1]
    assert len(tg.sent("approval")) == before + 1 and last[1] == "2" and last[2] == did


def test_reminder_respects_off_window_and_published(tg, llm, png, monkeypatch):
    did = new_draft(tg, llm, png)
    age_draft(did, 5)
    monkeypatch.setenv("NOTIFY_TZ", "UTC")
    monkeypatch.setenv("NOTIFY_HOURS", "9-20")
    monkeypatch.setenv("NOTIFY_DAYS", "1-7")
    night = 1_800_000_000 - (1_800_000_000 % 86400) + 3 * 3600  # 03:00 UTC
    assert run(core.remind_once(now=night)) == 0
    monkeypatch.setenv("NOTIFY_HOURS", "0-24")
    monkeypatch.setenv("NOTIFY_DAYS", "1-7")
    monkeypatch.setenv("REMIND_AFTER_MIN", "0")
    assert run(core.remind_once()) == 0  # выключено
    monkeypatch.setenv("REMIND_AFTER_MIN", "180")
    run(core.on_action(tg, "1", "okyes", did, 1))
    assert run(core.remind_once()) == 0  # опубликованный не напоминаем


def test_regenerated_variant_restarts_the_clock(tg, llm, png, window):
    did = new_draft(tg, llm, png)
    age_draft(did, 4)
    run(core.on_action(tg, "1", "regen", did, 1))
    assert run(core.remind_once()) == 0  # новый вариант только что отправлен


def test_failed_reminder_is_retried_later(tg, llm, png, window):
    did = new_draft(tg, llm, png)
    age_draft(did, 4)

    async def broken(*a, **k):
        raise RuntimeError("нет сети")
    orig = tg.send_menu
    tg.send_menu = broken
    assert run(core.remind_once()) == 0
    tg.send_menu = orig
    assert run(core.remind_once()) == 1


# ---------- статус и сообщения о сбоях ----------
def admin_tg():
    a = FakeAdapter("tg", ["1", "2"], admins=["1", "2"])
    a.admins = ["1"]
    core.ADAPTERS["tg"] = a
    core.refresh_people(a)
    return a


def test_status_text_and_access(llm, png):
    a = admin_tg()
    run(core.submit(a, "2", "Анна", "Заменили масло", png))
    assert run(core.on_text(a, "1", "/status")) is True
    text = a.sent("notify")[-1][2]
    assert "Бот работает" in text and "✅ отвечает" in text and "На согласовании: 1" in text
    run(core.on_text(a, "2", "/status"))  # обычный согласующий при заданном админе: отказ, а не молчание
    assert len(a.sent("notify")) == 2 and "администратору" in a.sent("notify")[-1][2]
    assert "администратору" in run(core.on_menu(a, "2", "m:status"))[0]
    assert "Бот работает" in run(core.on_menu(a, "1", "m:status"))[0]


def test_status_shows_llm_failure(llm):
    a = admin_tg()
    llm.reply = RuntimeError("insufficient funds")
    run(core.on_text(a, "1", "/status"))
    assert "❌" in a.sent("notify")[-1][2] and "insufficient funds" in a.sent("notify")[-1][2]


def test_status_for_approver_when_no_admins_configured(llm):
    a = FakeAdapter("tg", ["1"], admins=["1"])
    core.ADAPTERS["tg"] = a
    run(core.on_text(a, "1", "/status"))
    assert "Бот работает" in a.sent("notify")[-1][2]


def test_generation_failure_is_tracked(tg, llm, png):
    llm.reply = RuntimeError("quota")
    with pytest.raises(RuntimeError):
        run(core.submit(tg, "9", "Анна", "Заменили масло", png))
    assert core.LLM_STATE["err"] > 0 and "quota" in core.LLM_STATE["msg"]


def test_announce_start_goes_to_admins_only():
    a = admin_tg()
    run(core.announce_start())
    assert [o[1] for o in a.sent("notify")] == ["1"] and "запущен" in a.sent("notify")[0][2]


def test_alert_staff_throttled_and_tries_other_messenger():
    a = admin_tg()
    m = FakeAdapter("max", ["5"], admins=["5"])
    m.admins = ["5"]
    core.ADAPTERS["max"] = m
    run(core.alert_staff(a, "⚠️ tg: сбой"))
    run(core.alert_staff(a, "⚠️ tg: сбой"))  # повтор подавлен
    assert [o[1] for o in a.sent("notify")] == ["1"] and [o[1] for o in m.sent("notify")] == ["5"]


def test_supervise_alerts_admin_on_crash():
    admin_tg()
    calls = {"n": 0}

    class Flaky:
        name = "tg"
        admins = ["1"]
        approvers = ["1"]
        out = []

        async def run(self):
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("сеть")

        async def notify(self, uid, text):
            Flaky.out.append((uid, text))
    f = Flaky()
    core.ADAPTERS["tg"] = f
    run(main.supervise(f))
    assert Flaky.out and "сбой" in Flaky.out[0][1] and Flaky.out[0][0] == "1"
