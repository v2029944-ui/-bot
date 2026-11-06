from conftest import run

import core


def test_first_click_and_cancel_do_not_publish(mx, llm, png):
    did = run(core.submit(mx, "9", "Анна", "Описание работы", png))
    run(core.on_action(mx, "5", "ok", did, 1))
    assert not mx.sent("publish") and core.get(did)["status"] == "pending"
    text, rows = mx.sent("menu")[-1][2:]
    assert "Опубликовать" in text and "MAX" in text
    assert [b[1] for row in rows for b in row] == [f"d:okyes:{did}:1", f"d:okno:{did}:1"]
    run(core.on_action(mx, "5", "okno", did, 1))
    assert not mx.sent("publish") and core.get(did)["status"] == "pending"


def test_confirmation_publishes_once_and_rechecks_permissions(mx, llm, png):
    did = run(core.submit(mx, "9", "Анна", "Описание работы", png))
    run(core.on_action(mx, "5", "ok", did, 1))
    assert "нет прав" in run(core.on_action(mx, "8", "okyes", did, 1))[1]
    assert not mx.sent("publish")
    assert run(core.on_action(mx, "5", "okyes", did, 1))[0]
    run(core.on_action(mx, "5", "okyes", did, 1))
    assert len(mx.sent("publish")) == 1


def test_edit_invalidates_confirmation(mx, llm, png):
    did = run(core.submit(mx, "9", "Анна", "Описание работы", png))
    run(core.on_action(mx, "5", "ok", did, 1))
    run(core._apply_edit(mx, "5", did, 1, "Новый текст"))
    assert "устаревший" in run(core.on_action(mx, "5", "okyes", did, 1))[1]
    assert not mx.sent("publish")
    run(core.on_action(mx, "5", "ok", did, 2))
    assert not mx.sent("publish")
    run(core.on_action(mx, "5", "okyes", did, 2))
    assert mx.sent("publish") == [("publish", "Новый текст")]
