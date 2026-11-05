import json

import pytest

import core
import post_footer
from conftest import run


def settings():
    return {"enabled": True, "text": "бота", "marker": "{bot}", "max": "https://max.ru/bot", "tg": "https://t.me/bot"}


def test_copied_footer_is_restored_without_duplicate_link(mx, llm, png):
    post_footer.save(settings())
    llm.reply = "Старый текст.\n\nДля заказа используйте нашего {bot}."
    did = run(core.submit(mx, "9", "Анна", "Описание работы", png))
    core._set(did, footer=json.dumps(settings()))
    changed = settings()
    changed.update(marker="*", text="другого бота", max="https://max.ru/other")
    post_footer.save(changed)
    run(core._apply_edit(mx, "5", did, 1, "Исправленный текст.\n\nДля заказа используйте нашего бота."))
    d = core.get(did)
    assert d["post"] == "Исправленный текст.\n\nДля заказа используйте нашего {bot}."
    final = post_footer.render(d["post"], "max", json.loads(d["footer"]))
    assert final.count('<a href=') == 1 and "https://max.ru/other" not in final


def test_copied_standalone_footer_is_not_appended_twice():
    restored = post_footer.restore_copied_marker("Исправлено.\n\nбота", "Старый текст.", settings())
    assert restored == "Исправлено.\n\n{bot}"
    assert post_footer.render(restored, "max", settings()).count('<a href=') == 1


def test_changed_footer_requests_marker_without_losing_edit(mx, llm, png):
    post_footer.save(settings())
    llm.reply = "Текст.\n\nИспользуйте нашего {bot}."
    did = run(core.submit(mx, "9", "Анна", "Описание работы", png))
    waiting, message = run(core._apply_edit(mx, "5", did, 1, "Новый текст. Используйте помощника."))
    assert waiting and "{bot}" in message and core.get(did)["attempts"] == 1
    run(core._apply_edit(mx, "5", did, 1, "Новый текст. Используйте {bot}."))
    assert core.get(did)["attempts"] == 2
