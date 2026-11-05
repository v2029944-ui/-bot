import json

import httpx

import core
from conftest import run
from test_admin_menu_button import Api, make


def test_cached_edit_uses_no_get_and_terminal_cleanup_removes_cache(monkeypatch, png, llm):
    async def scenario():
        api = Api()
        adapter = make(api)
        core.ADAPTERS["max"] = adapter

        async def upload(path):
            return "image-token"

        monkeypatch.setattr(adapter, "_upload_image", upload)
        did = await core.submit(adapter, "9", "Анна", "Исходная подпись", png)
        mid = f"m{api.n}"
        raw = core.db.execute("SELECT attachments FROM max_button_cache WHERE mid=?", (mid,)).fetchone()[0]
        assert "Исходная подпись" not in raw and llm.reply not in raw
        before = len([c for c in api.calls if c[0] == "GET"])
        await adapter.on_callback({"user": {"user_id": 1}, "callback_id": "c", "payload": f"d:change:{did}:1", "message": {"body": {"mid": mid}}})
        assert len([c for c in api.calls if c[0] == "GET"]) == before
        assert "text" not in api.of("PUT")[-1][3]
        await core.on_action(adapter, "1", "okyes", did, 1)
        assert not core.db.execute("SELECT * FROM max_button_cache").fetchall()
        await adapter.http.aclose()
    run(scenario())


def test_invalid_cache_falls_back_to_api_without_losing_text():
    async def scenario():
        api = Api()
        adapter = make(api)
        await adapter.notify("1", "Текст")
        core.db.execute("INSERT INTO max_button_cache VALUES('m1','not-json')")
        core.db.commit()
        assert await adapter._strip_admin_button({"mid": "m1"})
        assert api.messages["m1"]["text"] == "Текст"
        assert any(c[0] == "GET" for c in api.calls)
        assert not core.db.execute("SELECT * FROM max_button_cache").fetchall()
        await adapter.http.aclose()
    run(scenario())
