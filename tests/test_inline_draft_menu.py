import core
from conftest import run
from test_admin_menu_button import Api, make, buttons


def test_change_length_and_back_replace_buttons_under_post(monkeypatch, png, llm):
    async def scenario():
        api = Api()
        adapter = make(api)
        core.ADAPTERS["max"] = adapter

        async def upload(path):
            return "photo-token"

        monkeypatch.setattr(adapter, "_upload_image", upload)
        did = await core.submit(adapter, "9", "Анна", "Описание работы", png)
        # Последняя отправка согласующему — исходный пост.
        original = api.of("POST")[-1][3]
        mid = f"m{api.n}"
        count = len(api.of("POST"))
        for action, expected in [("change", "redo"), ("length", "len_short"), ("back", "ok")]:
            await adapter.on_callback({"user": {"user_id": 1}, "callback_id": action,
                                       "payload": f"d:{action}:{did}:1", "message": {"body": {"mid": mid}}})
            edit = api.of("PUT")[-1]
            assert edit[2]["message_id"] == mid
            assert api.messages[mid]["text"] == original["text"]
            assert "text" not in edit[3]
            assert any(b["payload"] == f"d:{expected}:{did}:1" for b in buttons(edit[3]))
            assert edit[3]["attachments"][0] == original["attachments"][0]
        assert len(api.of("POST")) == count and not api.of("DELETE")
        await adapter.http.aclose()
    run(scenario())
