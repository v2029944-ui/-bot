"""Основной сценарий: фото -> пост -> согласование -> публикация / правка / удаление."""
from pathlib import Path

from conftest import run

import core


def test_submit_sends_draft_only_to_author(tg, llm, png):
    did = run(core.submit(tg, "9", "Вася", "форсунки", png))
    assert [o[1] for o in tg.sent("approval")] == ["9"]
    assert core.get(did)["status"] == "pending"
    call = llm.calls[0]
    assert "форсунки" in call["text"] and call["b64"]  # описание и картинка ушли в нейросеть


def test_without_vision_photo_is_not_sent_to_llm(tg, llm, png, monkeypatch):
    monkeypatch.setenv("LLM_VISION", "0")
    run(core.submit(tg, "9", "Вася", "форсунки", png))
    assert llm.calls[0]["b64"] is None


def test_publish_and_author_notification_modes(tg, llm, png, monkeypatch):
    did = run(core.submit(tg, "9", "Вася", "x", png))
    assert run(core.on_action(tg, "1", "okyes", did, 1)) == (True, "✅ Опубликовано.")
    assert tg.sent("publish") and core.get(did)["status"] == "published"
    assert [o for o in tg.sent("notify") if o[1] == "9"]  # тихий режим: сотрудник ничего не получает
    monkeypatch.setenv("EMPLOYEE_NOTIFY", "1")
    did2 = run(core.submit(tg, "9", "Вася", "y", png))
    run(core.on_action(tg, "1", "okyes", did2, 1))
    assert [o for o in tg.sent("notify") if o[1] == "9"]


def test_only_approvers_can_press_buttons(tg, llm, png):
    did = run(core.submit(tg, "9", "Вася", "x", png))
    ok, text = run(core.on_action(tg, "7", "okyes", did, 1))
    assert not ok and "нет прав" in text and core.get(did)["status"] == "pending"


def test_regenerate_sends_to_author_and_acting_admin_and_old_buttons_expire(tg, llm, png):
    did = run(core.submit(tg, "9", "Вася", "x", png))
    llm.reply = "Новый вариант"
    run(core.on_action(tg, "1", "regen", did, 1))
    assert core.get(did)["attempts"] == 2 and core.get(did)["post"] == "Новый вариант"
    assert len(tg.sent("approval")) == 3  # первоначальный автору, новый — автору и действующему администратору
    ok, text = run(core.on_action(tg, "1", "okyes", did, 1))  # кнопка под старым вариантом
    assert "устаревший" in text and core.get(did)["status"] == "pending"


def test_failed_regenerate_rolls_back(tg, llm, png):
    did = run(core.submit(tg, "9", "Вася", "x", png))
    llm.reply = RuntimeError("нет баланса")
    ok, text = run(core.on_action(tg, "1", "regen", did, 1))
    d = core.get(did)
    assert not ok and "нет баланса" in text and d["status"] == "pending" and d["attempts"] == 1


def test_busy_message_keeps_buttons(tg, llm, png):
    did = run(core.submit(tg, "9", "Вася", "x", png))
    core.db.execute("UPDATE drafts SET status='publishing' WHERE id=?", (did,))
    core.db.commit()
    ok, text = run(core.on_action(tg, "1", "okyes", did, 1))
    assert ok is False and "обрабатывается" in text  # ok=False: кнопки у черновика не убираются


def test_comment_regenerates_and_learns_general_rule(tg, llm, png):
    did = run(core.submit(tg, "9", "Вася", "x", png))
    run(core.on_action(tg, "1", "redoc", did, 1))
    assert ("tg", "1") in core.WAITING

    def reply(system, text):
        return "Писать короче 400 символов" if "свод правил" in system else "Короткий пост"
    llm.reply = reply
    assert run(core.on_text(tg, "1", "сделай короче")) is True
    assert core.get(did)["post"] == "Короткий пост"
    assert any("сделай короче" in c["text"] for c in llm.calls)
    assert [r["text"] for r in core.db.execute("SELECT text FROM rules")] == ["Писать короче 400 символов"]


def test_one_off_comment_is_not_learned(tg, llm, png):
    did = run(core.submit(tg, "9", "Вася", "x", png))
    run(core.on_action(tg, "1", "redoc", did, 1))
    llm.reply = lambda system, text: "НЕТ" if "свод правил" in system else "Пост"
    run(core.on_text(tg, "1", "исправь название улицы"))
    assert core.db.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0


def test_delete_needs_confirmation(tg, llm, png):
    did = run(core.submit(tg, "9", "Вася", "x", png))
    photo = core.get(did)["photo"]
    assert run(core.on_action(tg, "1", "del", did, 1)) == (False, "")
    assert core.get(did)["status"] == "pending" and Path(photo).exists()
    assert [b[0] for b in tg.sent("menu")[-1][3][-1]] == ["❌ Без причины", "↩️ Отмена"]
    run(core.on_action(tg, "1", "delno", did, 1))
    assert core.get(did)["status"] == "pending"
    assert "нет прав" in run(core.on_action(tg, "7", "delyes", did, 1))[1]
    assert "устаревший" in run(core.on_action(tg, "1", "delyes", did, 99))[1]
    assert run(core.on_action(tg, "1", "delyes", did, 1)) == (True, "❌ Пост отклонён.")
    assert core.get(did)["status"] == "deleted" and not Path(photo).exists()
    assert "уже обработан" in run(core.on_action(tg, "1", "delyes", did, 1))[1]


def test_approval_platform_setting(tg, mx, llm, png, monkeypatch):
    monkeypatch.setenv("APPROVAL_PLATFORM", "max")
    run(core.submit(tg, "9", "Вася", "x", png))  # написал в Telegram, согласует MAX
    assert mx.sent("approval") and not tg.sent("approval")
