import pytest
from conftest import run

import core
import post_footer


def settings(**changes):
    return {"enabled": True, "text": "бота", "max": "https://max.ru/bot", "tg": "https://t.me/bot", "marker": "{bot}", **changes}


def test_marker_is_linked_inside_sentence_for_each_channel():
    text = "Для заказа услуг используйте нашего {bot}."
    for channel in ("max", "tg"):
        result = post_footer.render(text, channel, settings())
        assert result == f'Для заказа услуг используйте нашего <a href="{settings()[channel]}">бота</a>.'
        assert result.plain == "Для заказа услуг используйте нашего бота."
        assert result.count('<a href=') == 1


def test_missing_marker_keeps_footer_fallback_and_disabled_is_plain():
    assert post_footer.render("Пост", "max", settings()).plain == "Пост\n\nбота"
    assert post_footer.render("Открыть {bot}", "max", settings(enabled=False)) == "Открыть бота"
    assert post_footer.render("Открыть {bot}", "tg", settings(tg="")) == "Открыть бота"


def test_marker_is_replaced_before_html_escape_and_all_occurrences_are_safe():
    result = post_footer.render("A & * и * <", "max", settings(marker="*", text="<бот>"))
    assert result.count('<a href=') == 2
    assert "&amp;" in result and "&lt;бот&gt;" in result and result.endswith("&lt;")


def test_marker_setting_is_saved_by_admin_and_preserved_in_snapshot(mx):
    mx.admins = ["5"]
    post_footer.save(settings())
    run(core.on_menu(mx, "5", "m:footer_marker"))
    run(core.on_text(mx, "5", "*"))
    assert post_footer.config()["marker"] == "*"
    assert "Маркер: *" in post_footer.view()[0]
    assert "Служебный маркер ссылки: *" in core.build_system()


@pytest.mark.parametrize("marker", ["", "a b", "x" * 33])
def test_invalid_marker_rejected(marker):
    with pytest.raises(ValueError):
        post_footer.validate("marker", marker)


def test_plain_replacement_still_checks_final_length():
    with pytest.raises(ValueError, match="слишком длинный"):
        post_footer.render("*" * 100, "max", settings(enabled=False, marker="*", text="x" * 80))
