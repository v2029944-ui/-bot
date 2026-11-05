import pytest

import core
from conftest import run


@pytest.mark.parametrize("action", ["redo", "len_short", "len_long"])
def test_max_new_draft_does_not_get_redundant_reply(mx, llm, png, action):
    did = run(core.submit(mx, "9", "Анна", "Описание работы", png))
    llm.reply = "Новый пост"
    ok, text = run(core.on_action(mx, "5", action, did, 1))
    assert ok and text == ""
    assert mx.sent("approval")[-1][3] == "Новый пост"


def test_max_regeneration_error_is_still_reported(mx, llm, png):
    did = run(core.submit(mx, "9", "Анна", "Описание работы", png))
    llm.reply = RuntimeError("Сервис недоступен")
    ok, text = run(core.on_action(mx, "5", "redo", did, 1))
    assert not ok and "Не удалось пересоздать" in text
