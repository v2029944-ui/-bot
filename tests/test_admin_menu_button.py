import json

import httpx
import pytest

import core
import max_
from conftest import run


def buttons(body):
    return [button for attachment in body["attachments"] if attachment["type"] == "inline_keyboard"
            for row in attachment["payload"]["buttons"] for button in row]


@pytest.mark.parametrize("kind", ["notify", "menu", "approval"])
def test_every_admin_reply_keeps_menu_button(kind, png, llm, monkeypatch):
    async def scenario():
        adapter = max_.Max("token", "-1", ["5"], ["9"], admins=["1"])
        requests = []

        async def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={})

        async def upload(path):
            return "photo-token"

        await adapter.http.aclose()
        adapter.http = httpx.AsyncClient(base_url=max_.API, transport=httpx.MockTransport(handler))
        monkeypatch.setattr(adapter, "_upload_image", upload)
        if kind == "approval":
            did = await core.submit(adapter, "9", "Анна", "Почистили форсунки", png)
            await adapter.send_for_approval("1", did, core.get(did)["photo"], core.get(did)["post"])
            assert [b["payload"].split(":")[1] for b in buttons(requests[-1])[:-1]] == ["del", "ok", "change"]
        elif kind == "menu":
            await adapter.send_menu("1", "Правка", [[("Править самому", "d:edit:1:1")]])
            assert buttons(requests[-1])[0]["payload"] == "d:edit:1:1"
        else:
            await adapter.notify("1", "Готово")
        assert buttons(requests[-1])[-1] == {"type": "callback", "text": "⚙️ Меню админа", "payload": "m:admin"}
        await adapter.notify("1", "Следующий ответ")
        assert buttons(requests[-1])[-1]["payload"] == "m:admin"
        await adapter.http.aclose()
    run(scenario())


def test_employees_approvers_and_publications_do_not_get_admin_button():
    async def scenario():
        adapter = max_.Max("token", "-1", ["5"], ["9"], admins=["1"])
        bodies = []

        async def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={})

        await adapter.http.aclose()
        adapter.http = httpx.AsyncClient(base_url=max_.API, transport=httpx.MockTransport(handler))
        await adapter.notify("9", "Принято")
        await adapter.notify("5", "Опубликовано")
        await adapter.publish(None, "Пост в канал")
        adapter.channel = "user:1"
        await adapter.publish(None, "Публикация в тестовую личку")
        assert all(not buttons(body) for body in bodies)
        await adapter.http.aclose()
    run(scenario())


def test_admin_menu_callback_opens_controls_and_checks_current_role():
    async def scenario():
        adapter = max_.Max("token", "-1", ["5"], [], admins=["1"])
        bodies = []

        async def handler(request):
            if request.url.path == "/messages":
                bodies.append(json.loads(request.content))
            return httpx.Response(200, json={})

        await adapter.http.aclose()
        adapter.http = httpx.AsyncClient(base_url=max_.API, transport=httpx.MockTransport(handler))
        key = ("max", "1")
        core.WAITING[key] = 1
        core.WAITING_EDIT[key] = (1, 1)
        core.WAITING_RULE.add(key)
        callback = {"callback_id": "cb", "user": {"user_id": 1}, "payload": "m:admin"}
        await adapter.on_callback(callback)
        assert bodies[-1]["text"] == "Меню администратора:"
        payloads = [b["payload"] for b in buttons(bodies[-1])]
        assert {"m:people", "m:inv", "m:status", "m:pause", "m:upd", "m:admin"} <= set(payloads)
        assert payloads.count("m:admin") == 1
        assert key not in core.WAITING and key not in core.WAITING_EDIT and key not in core.WAITING_RULE
        adapter.admins = []  # старая кнопка после снятия прав; согласующим человек остался
        await adapter.on_callback(callback)
        assert "только администратору" in bodies[-1]["text"] and not buttons(bodies[-1])
        await adapter.http.aclose()
    run(scenario())
