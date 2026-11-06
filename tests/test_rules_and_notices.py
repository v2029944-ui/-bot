from conftest import run
from test_admin_menu_button import Api, make

import core


def test_deleted_rules_do_not_leave_gaps_and_old_buttons_keep_identity(tg):
    for text in ("Первое", "Удалить", "Третье"):
        core.rules_command(tg, "1", "/rule_add " + text)
    ids = [r[0] for r in core.db.execute("SELECT id FROM rules ORDER BY id")]
    run(core.on_menu(tg, "1", f"m:del:{ids[1]}"))
    text, rows = core.rules_view()
    assert "1. Первое" in text and "2. Третье" in text
    assert rows[0][1] == ("🗑 2", f"m:del:{ids[2]}")
    core.rules_command(tg, "1", "/rule_add Новое")
    assert "3. Новое" in core.rules_view()[0]
    core.rules_command(tg, "1", "/rule_del 2")
    assert "Третье" not in core.rules_view()[0] and "2. Новое" in core.rules_view()[0]
    run(core.on_menu(tg, "1", f"m:del:{ids[2]}"))
    assert "Новое" in core.rules_view()[0]


def test_stale_notice_removed_after_three_successful_bot_messages_in_same_chat():
    async def scenario():
        api = Api()
        adapter = make(api)
        await adapter.notify("1", "Это устаревший вариант. Используйте последнее сообщение с постом.")
        await adapter.notify("5", "Другой пользователь")
        await adapter.notify("1", "Первый ответ")
        await adapter.notify("1", "Второй ответ")
        assert not api.of("DELETE")
        await adapter.notify("1", "Третий ответ")
        assert [c[2]["message_id"] for c in api.of("DELETE")] == ["m1"]
        assert not core.db.execute("SELECT * FROM max_notices").fetchall()
        await adapter.http.aclose()
    run(scenario())
