from conftest import run
from test_admin_menu_button import Api, make

import core
import post_footer


def test_preview_contains_link_and_publication_uses_same_snapshot(monkeypatch, png, llm):
    llm.reply = "Для заказа используйте нашего {bot}"
    async def scenario():
        api = Api()
        adapter = make(api)
        core.ADAPTERS["max"] = adapter

        async def upload(path):
            return "photo-token"

        monkeypatch.setattr(adapter, "_upload_image", upload)
        settings = {"enabled": True, "text": "Записаться", "max": "https://max.ru/first", "tg": ""}
        post_footer.save(settings)
        did = await core.submit(adapter, "9", "Анна", "Описание работы", png)
        assert api.of("POST")[-1][3]["text"].endswith('<a href="https://max.ru/first">Записаться</a>')
        assert api.of("POST")[-1][3]["format"] == "html"
        settings["max"] = "https://max.ru/second"
        post_footer.save(settings)
        await core.on_action(adapter, "1", "okyes", did, 1)
        publication = [c for c in api.of("POST") if "chat_id" in c[2]][0][3]["text"]
        assert publication.count('<a href=') == 1 and "https://max.ru/first" in publication
        await adapter.http.aclose()
    run(scenario())


def test_editing_keyboard_does_not_rewrite_formatted_text(monkeypatch):
    async def scenario():
        api = Api()
        adapter = make(api)
        api.messages["formatted"] = {"text": "Пост\n\nЗаписаться", "markup": [{"type": "link", "url": "https://max.ru/bot", "from": 6, "length": 10}], "attachments": []}
        await adapter._strip_admin_button({"mid": "formatted"}, replacement=[])
        assert "text" not in api.of("PUT")[-1][3]
        await adapter.http.aclose()
    run(scenario())
