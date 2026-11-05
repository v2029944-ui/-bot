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
        assert {"m:people", "m:inv", "m:status", "m:pause", "m:upd"} <= set(payloads)
        assert "m:admin" not in payloads  # в самом меню админа кнопка не нужна
        assert key not in core.WAITING and key not in core.WAITING_EDIT and key not in core.WAITING_RULE
        adapter.admins = []  # старая кнопка после снятия прав; согласующим человек остался
        await adapter.on_callback(callback)
        assert "только администратору" in bodies[-1]["text"] and not buttons(bodies[-1])
        await adapter.http.aclose()
    run(scenario())


class Api:
    """Подставной MAX: считает id сообщений и записывает все запросы."""

    def __init__(self):
        self.calls, self.n = [], 0

    async def handler(self, request):
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, dict(request.url.params), body))
        if request.method == "POST" and request.url.path == "/messages":
            self.n += 1
            return httpx.Response(200, json={"message": {"body": {"mid": f"m{self.n}"}}})
        return httpx.Response(200, json={})

    def of(self, method):
        return [c for c in self.calls if c[0] == method and c[1] == "/messages"]


def make(api):
    adapter = max_.Max("token", "-1", ["5"], [], admins=["1"])
    adapter.http = httpx.AsyncClient(base_url=max_.API, transport=httpx.MockTransport(api.handler))
    core.db.execute("DELETE FROM max_ui")
    return adapter


def test_admin_button_only_under_last_message():
    async def scenario():
        api = Api()
        adapter = make(api)
        await adapter.notify("1", "Первое")
        await adapter.notify("1", "Второе")
        await adapter.notify("1", "Третье")
        puts = api.of("PUT")
        assert [c[2]["message_id"] for c in puts] == ["m1", "m2"]  # кнопка ушла из прежних сообщений
        assert all(not buttons(c[3]) for c in puts) and puts[0][3]["text"] == "Первое"
        assert buttons(api.of("POST")[-1][3])[-1]["payload"] == "m:admin"
        await adapter.http.aclose()
    run(scenario())


def test_old_button_stripped_keeps_other_keyboard_and_media():
    async def scenario():
        api = Api()
        adapter = make(api)
        await adapter._send({"user_id": "1"}, "Пост", None, [[{"type": "callback", "text": "Да", "payload": "d:ok:1:1"}]],
                            media=None)
        await adapter.notify("1", "Дальше")
        edit = api.of("PUT")[0][3]
        assert edit["text"] == "Пост"
        assert [b["payload"] for b in buttons(edit)] == ["d:ok:1:1"]  # свои кнопки поста остались
        await adapter.http.aclose()
    run(scenario())


def test_only_one_menu_and_old_steps_deleted():
    async def scenario():
        api = Api()
        adapter = make(api)
        cb = lambda payload, mid: {"callback_id": "c", "user": {"user_id": 1}, "payload": payload,
                                   "message": {"body": {"mid": mid}}}
        await adapter.notify("1", "Привет")                    # m1, с кнопкой
        await adapter.on_callback(cb("m:admin", "m1"))         # m2 — меню; m1 не удаляется
        assert not api.of("DELETE")
        assert "m:admin" not in [b["payload"] for b in buttons(api.of("POST")[-1][3])]
        await adapter.on_callback(cb("m:rules", "m2"))         # m3 вместо m2
        assert [c[2]["message_id"] for c in api.of("DELETE")] == ["m2"]
        await adapter.on_callback(cb("m:admin", "m1"))         # второе открытие меню: m4 вместо m3
        assert [c[2]["message_id"] for c in api.of("DELETE")] == ["m2", "m3"]
        assert adapter._ui("1")["menu"] == "m4"
        await adapter.http.aclose()
    run(scenario())


def test_post_message_is_never_deleted_by_its_buttons():
    async def scenario():
        api = Api()
        adapter = make(api)
        did = core.db.execute("SELECT COALESCE(MAX(id),0)+1 FROM drafts").fetchone()[0]
        await adapter.on_callback({"callback_id": "c", "user": {"user_id": 1}, "payload": f"d:show:{did}:1",
                                   "message": {"body": {"mid": "post-1"}}})
        assert not api.of("DELETE")
        await adapter.http.aclose()
    run(scenario())
