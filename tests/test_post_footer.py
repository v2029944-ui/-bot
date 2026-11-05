import json

import pytest

import core
import post_footer
from conftest import FakeAdapter, run


def test_admin_can_configure_enable_and_disable_link(mx):
    mx.admins = ["5"]
    assert "администратору" in run(core.on_menu(mx, "9", "m:footer"))[0] or "нет прав" in run(core.on_menu(mx, "9", "m:footer"))[0]
    run(core.on_menu(mx, "5", "m:footer_text"))
    run(core.on_text(mx, "5", "Записаться"))
    run(core.on_menu(mx, "5", "m:footer_max"))
    run(core.on_text(mx, "5", "https://max.ru/examplebot"))
    run(core.on_menu(mx, "5", "m:footer_toggle"))
    assert post_footer.config()["enabled"] and post_footer.config()["text"] == "Записаться"
    assert "Записаться" in post_footer.view()[0]
    run(core.on_menu(mx, "5", "m:footer_toggle"))
    assert not post_footer.config()["enabled"]


@pytest.mark.parametrize("value", ["javascript:alert(1)", "http://example.com", "https://user:password@example.com", "не ссылка"])
def test_unsafe_addresses_rejected(value):
    with pytest.raises(ValueError):
        post_footer.validate("max", value)


def test_footer_snapshot_does_not_change_during_partial_retry(mx, llm, png):
    tg = FakeAdapter("tg", [], fail=[ValueError("Отказ")], sure=(ValueError,))
    tg.publish_only = True
    core.ADAPTERS["tg"] = tg
    settings = {"enabled": True, "text": "A & B", "max": "https://max.ru/bot", "tg": "https://t.me/bot"}
    post_footer.save(settings)
    did = run(core.submit(mx, "9", "Анна", "Описание работы", png))
    run(core.on_action(mx, "5", "okyes", did, 1))
    assert '<a href="https://max.ru/bot">A &amp; B</a>' in mx.sent("publish")[0][1]
    settings["text"] = "Другой текст"
    post_footer.save(settings)
    run(core.on_action(mx, "5", "okyes", did, 1))
    assert "A &amp; B" in tg.sent("publish")[0][1] and "Другой текст" not in tg.sent("publish")[0][1]
    assert core.get(did)["footer"] is None


def test_render_escapes_text_and_keeps_disabled_posts_plain():
    settings = {"enabled": True, "text": "Открыть", "max": "https://max.ru/bot"}
    result = post_footer.render("Текст <b> &", "max", settings)
    assert "&lt;b&gt; &amp;" in result and result.plain == "Текст <b> &\n\nОткрыть"
    assert post_footer.render("Текст", "tg", settings) == "Текст"


def test_real_adapters_enable_html_for_linked_posts(monkeypatch, tmp_path):
    import max_
    import tg

    async def scenario():
        mx = max_.Max("token", "-1", ["5"], [])
        messages = []

        async def call(method, path, **kwargs):
            messages.append(kwargs["json"])
            return {}

        async def upload(path):
            return "token"

        monkeypatch.setattr(mx, "call", call)
        monkeypatch.setattr(mx, "_upload_image", upload)
        text = post_footer.render("Пост & текст", "max", {"enabled": True, "text": "Бот", "max": "https://max.ru/bot"})
        path = tmp_path / "photo.jpg"
        path.write_bytes(b"image")
        await mx.publish(str(path), text)
        assert messages[0]["format"] == "html" and '<a href=' in messages[0]["text"]
        await mx.http.aclose()
        telegram = tg.Telegram("123456:ABCDEF", "-1")
        calls = []

        class Bot:
            async def send_photo(self, *args, **kwargs):
                calls.append(kwargs)

        telegram.bot = Bot()
        path = tmp_path / "photo.jpg"
        path.write_bytes(b"image")
        await telegram.publish(str(path), text)
        assert calls[0]["parse_mode"] == "HTML" and '<a href=' in calls[0]["caption"]
    run(scenario())
