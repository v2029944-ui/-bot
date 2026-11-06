import asyncio
import io
import json

import pytest
from conftest import run
from PIL import Image
from test_admin_menu_button import Api, make

import core


def test_official_max_update_passes_original_message_to_callback(monkeypatch):
    async def scenario():
        api = Api()
        adapter = make(api)
        received = []

        async def callback(data):
            received.append(data)

        monkeypatch.setattr(adapter, "on_callback", callback)
        message = {"body": {"mid": "m1"}}
        await adapter.handle({"update_type": "message_callback", "callback": {"callback_id": "c", "user": {"user_id": 1}}, "message": message})
        assert received[0]["message"] == message
        await adapter.http.aclose()
    run(scenario())


def test_menu_state_does_not_retain_post_text_or_media():
    async def scenario():
        api = Api()
        adapter = make(api)
        await adapter.notify("1", "Содержимое поста")
        stored = core.db.execute("SELECT btn FROM max_ui WHERE uid='1'").fetchone()[0]
        assert json.loads(stored) == {"mid": "m1"}
        await adapter.notify("1", "Следующее сообщение")
        assert api.of("PUT")[0][3]["text"] == "Содержимое поста"
        await adapter.http.aclose()
    run(scenario())


def test_concurrent_notifications_leave_one_admin_button():
    async def scenario():
        api = Api()
        adapter = make(api)
        await asyncio.gather(*(adapter.notify("1", str(n)) for n in range(5)))
        assert len(api.of("PUT")) == 4
        assert adapter._ui("1")["btn"] == {"mid": "m5"}
        await adapter.http.aclose()
    run(scenario())


@pytest.mark.parametrize("size", [(1600, 5), (5, 1600)])
def test_panoramic_photo_obeys_telegram_aspect_limit(size):
    buffer = io.BytesIO()
    Image.new("RGB", size, "red").save(buffer, "PNG")
    path = core.save_photo(buffer.getvalue())
    with Image.open(path) as photo:
        assert max(photo.size) <= 20 * min(photo.size)
        assert sum(photo.size) <= 10000
