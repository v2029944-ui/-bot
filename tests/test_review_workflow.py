import time
from pathlib import Path

import pytest

import core
from conftest import FakeAdapter, run


def draft(mx, png, text="Установили новый насос"):
    return run(core.submit(mx, "9", "Анна", text, png))


def test_pending_list_paginates_and_opens_current_version(mx, llm, png):
    ids = [draft(mx, png, f"Работа номер {n}") for n in range(10)]
    core._set(ids[0], status="published")
    core._set(ids[1], attempts=3, author_name="Иван")
    text, rows = run(core.on_menu(mx, "5", "m:pending"))
    assert "На проверке: 9" in text and "Иван" in text
    assert f"d:show:{ids[1]}:3" in [b[1] for row in rows for b in row]
    second, rows2 = run(core.on_menu(mx, "5", "m:pending:1"))
    assert f"№{ids[-1]}" in second and "Страница 2" in second
    assert "нет прав" in run(core.on_menu(mx, "9", "m:pending"))[0]
    run(core.on_action(mx, "5", "show", ids[1], 1))
    assert mx.sent("approval")[-1][2] == ids[1]


@pytest.mark.parametrize("action,reason", [("rejectphoto", "фотографии"), ("rejecttext", "описание")])
def test_reject_reason_reaches_quiet_employee_and_removes_media(mx, llm, png, action, reason):
    did = draft(mx, png)
    paths = core.draft_files(core.get(did))
    assert core.quiet(mx, "9")
    run(core.on_action(mx, "5", "del", did, 1))
    assert core.get(did)["status"] == "pending"
    assert run(core.on_action(mx, "5", action, did, 1))[0]
    assert reason in core.get(did)["rejection_reason"]
    assert any(o[1] == "9" and reason in o[2] for o in mx.sent("notify"))
    assert all(not Path(path).exists() for path in paths)


def test_custom_rejection_cancel_length_and_stale_version(mx, llm, png):
    did = draft(mx, png)
    run(core.on_action(mx, "5", "rejectcustom", did, 1))
    run(core.on_text(mx, "5", "x" * 1001))
    assert ("max", "5") in core.WAITING_REJECT
    run(core.on_text(mx, "5", "/cancel"))
    assert core.get(did)["status"] == "pending"
    run(core.on_action(mx, "5", "rejectcustom", did, 1))
    core._set(did, attempts=2)
    run(core.on_text(mx, "5", "Нужна фотография результата"))
    assert core.get(did)["status"] == "pending"
    run(core.on_action(mx, "5", "rejectcustom", did, 2))
    run(core.on_text(mx, "5", "Нужна фотография результата"))
    assert core.get(did)["status"] == "deleted"
    assert core.get(did)["rejection_reason"] == "Нужна фотография результата"


def test_partly_published_post_cannot_be_rejected(mx, llm, png):
    did = draft(mx, png)
    core._set(did, published_to="max")
    assert "Публикация уже началась" in run(core.on_action(mx, "5", "rejectphoto", did, 1))[1]
    assert core.get(did)["status"] == "pending"


def test_expiry_warning_once_no_extension_and_repeat_after_new_version(mx, llm, png):
    did = draft(mx, png)
    now = time.time()
    last = now - 6.5 * 86400
    core._set(did, created=last, last_sent=last)
    assert run(core.warn_expiring_once(now)) == 1
    assert core.get(did)["last_sent"] == last
    assert "будет удалён" in mx.sent("menu")[-1][2]
    assert run(core.warn_expiring_once(now)) == 0
    core._set(did, last_sent=now)
    assert run(core.warn_expiring_once(now)) == 0
    assert run(core.warn_expiring_once(now + 6.5 * 86400)) == 1


def test_expiry_warning_skips_terminal_and_disabled_ttl(mx, llm, png, monkeypatch):
    now = time.time()
    did = draft(mx, png)
    core._set(did, last_sent=now - 6.5 * 86400, status="published")
    assert run(core.warn_expiring_once(now)) == 0
    core._set(did, status="pending")
    monkeypatch.setenv("DRAFT_TTL_DAYS", "0")
    assert run(core.warn_expiring_once(now)) == 0


def test_per_channel_status_retry_and_manual_confirmation(mx, llm, png):
    tg = FakeAdapter("tg", [], fail=[TimeoutError("Потерян ответ")])
    tg.publish_only = True
    core.ADAPTERS["tg"] = tg
    did = draft(mx, png)
    paths = core.draft_files(core.get(did))
    run(core.on_action(mx, "5", "okyes", did, 1))
    text, rows = mx.sent("menu")[-1][2:]
    assert "MAX — ✅ опубликовано" in text and "Telegram — ❓ результат неизвестен" in text
    assert f"d:seen_tg:{did}:1" in [b[1] for row in rows for b in row]
    run(core.on_action(mx, "5", "seen_tg", did, 1))
    assert core.get(did)["status"] == "published"
    assert len(mx.sent("publish")) == 1 and not tg.sent("publish")
    assert all(not Path(p).exists() for p in paths)


def test_definite_failure_status_and_retry_only_remaining_channel(mx, llm, png):
    tg = FakeAdapter("tg", [], fail=[ValueError("Нет прав")], sure=(ValueError,))
    tg.publish_only = True
    core.ADAPTERS["tg"] = tg
    did = draft(mx, png)
    run(core.on_action(mx, "5", "okyes", did, 1))
    assert "Telegram — ❌ ошибка" in mx.sent("menu")[-1][2]
    run(core.on_action(mx, "5", "okyes", did, 1))
    assert len(mx.sent("publish")) == len(tg.sent("publish")) == 1
    assert "Telegram — ✅ опубликовано" in mx.sent("menu")[-1][2]
