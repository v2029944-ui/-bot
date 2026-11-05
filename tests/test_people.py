"""Добавление людей кнопками: заявки, добавление по ID, удаление, права. Без сети."""
import time

import core
import pytest
from conftest import FakeAdapter, run


def make(admins=("1",), approvers=("1", "2")):
    a = FakeAdapter("tg", list(approvers))
    a.admins = list(admins)
    core.ADAPTERS["tg"] = a
    core.refresh_people(a)
    return a


def menu(a, uid, payload):
    return run(core.on_menu(a, uid, payload))


def test_stranger_creates_one_request_and_gets_no_reply():
    a = make()
    assert a.allowed is not None and "77" not in a.allowed
    assert run(core.unknown_user(a, "77", "Иван", "ivan")) is True
    assert run(core.unknown_user(a, "77", "Иван", "ivan")) is True  # повтор: второй заявки нет
    menus = a.sent("menu")
    assert len(menus) == 1 and menus[0][1] == "1" and "Иван" in menus[0][2] and "@ivan" in menus[0][2]
    payloads = [p for row in menus[0][3] for _, p in row]
    assert payloads == ["m:acc:emp:77", "m:acc:apr:77", "m:acc:no:77"]
    assert a.sent("notify") == []  # незнакомцу ничего не пишем


def test_admin_approves_as_employee():
    a = make()
    run(core.unknown_user(a, "77", "Иван"))
    text, _ = menu(a, "1", "m:acc:emp:77")
    assert "добавлен" in text and "77" in a.allowed and "77" not in a.approvers
    assert a.sent("notify") and a.sent("notify")[-1][1] == "77"  # единственное сообщение: доступ открыт
    assert "уже обработана" in menu(a, "1", "m:acc:emp:77")[0]  # повторное нажатие безвредно
    assert core.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0


def test_admin_approves_as_approver_and_draft_reaches_him(llm, png):
    a = make()
    run(core.unknown_user(a, "77", "Пётр"))
    menu(a, "1", "m:acc:apr:77")
    assert "77" in a.approvers
    run(core.submit(a, "2", "Анна", "заменили масло", png))
    assert {o[1] for o in a.sent("approval")} == {"1", "2", "77"}


def test_reject_is_silent_for_a_week_then_allowed_again():
    a = make()
    run(core.unknown_user(a, "77", "Иван"))
    assert "отклонена" in menu(a, "1", "m:acc:no:77")[0]
    assert run(core.unknown_user(a, "77", "Иван")) is False
    assert len(a.sent("menu")) == 1
    core.db.execute("UPDATE requests SET created=?", (time.time() - core.REQ_TTL - 5,))
    core.db.commit()
    assert run(core.unknown_user(a, "77", "Иван")) is True and len(a.sent("menu")) == 2


def test_only_admin_manages_people():
    a = make()
    for payload in ("m:people", "m:inv", "m:invn:apr", "m:acc:apr:77", "m:pdelok:5"):
        assert "только администратор" in menu(a, "2", payload)[0]  # "2" — обычный согласующий
    assert "нет прав" in menu(a, "99", "m:people")[0]


def test_people_menu_only_for_admin_buttons():
    a = make()
    assert ("👥 Люди", "m:people") in [b for r in core.menu_for(a, "1") for b in r]
    assert ("👥 Люди", "m:people") not in [b for r in core.menu_for(a, "2") for b in r]


def test_remove_needs_confirm_and_blocks_reappearing_request():
    a = make()
    run(core.unknown_user(a, "77", "Иван"))
    menu(a, "1", "m:acc:emp:77")
    text, rows = menu(a, "1", "m:pdel:77")
    assert "Убрать Иван" in text and ("Да, убрать", "m:pdelok:77") in rows[0]
    assert "77" in a.allowed  # пока не подтвердили — человек на месте
    menu(a, "1", "m:pdelok:77")
    assert "77" not in a.allowed
    assert run(core.unknown_user(a, "77", "Иван")) is False  # сам заявкой не вернётся


def test_people_list_shows_settings_and_added():
    a = make()
    a.base_employees = ["9"]
    run(core.unknown_user(a, "77", "Иван"))
    menu(a, "1", "m:acc:emp:77")
    text, rows = menu(a, "1", "m:people")
    assert "Иван" in text and "id 9" in text and "Администраторы" in text and "id 1) — из настроек" in text
    assert ("⚙ Иван", "m:pset:77") in [b for r in rows for b in r]


def test_pending_requests_are_capped(monkeypatch):
    a = make()
    monkeypatch.setattr(core, "MAX_PENDING_REQUESTS", 2)
    results = [run(core.unknown_user(a, str(100 + i), f"Гость{i}")) for i in range(4)]
    assert results == [True, True, False, False] and len(a.sent("menu")) == 2


def test_without_admins_nothing_changes():
    a = FakeAdapter("tg", ["1"])
    core.refresh_people(a)
    assert a.allowed is None  # как раньше: список сотрудников пуст = пишет кто угодно
    assert run(core.unknown_user(a, "77", "Иван")) is False and a.out == []


@pytest.mark.parametrize("button", ["m:member", "m:inv", "m:invn:emp"])
def test_add_by_id_replaces_unsupported_invites(button):
    a = make()
    assert "две строки" in menu(a, "1", button)[0]
    assert run(core.on_text(a, "1", "77\nсотрудник"))
    assert "77" in a.allowed and "77" not in a.approvers
    assert any("Основное" in o[2] for o in a.sent("notify") if o[1] == "77")


@pytest.mark.parametrize("uid", ["0", "001", "１２３", "12345678901234567890", "-77"])
def test_add_by_id_rejects_invalid_ids(uid):
    a = make()
    menu(a, "1", "m:member")
    assert run(core.on_text(a, "1", uid + "\nпроверяющий"))
    assert uid not in a.allowed
    assert not core.db.execute("SELECT 1 FROM people").fetchone()


def test_add_existing_user_does_not_silently_demote_or_change_admin():
    a = make()
    for uid in ("1", "2"):
        menu(a, "1", "m:member")
        run(core.on_text(a, "1", uid + "\nсотрудник"))
    assert a.admins == ["1"] and "2" in a.approvers
    assert "уже добавлен" in a.sent("notify")[-1][2]


def test_revoked_admin_cannot_complete_pending_addition():
    a = make()
    menu(a, "1", "m:member")
    a.admins = []
    run(core.on_text(a, "1", "77\nпроверяющий"))
    assert "77" not in a.approvers
