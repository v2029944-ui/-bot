import json
import time

import httpx
from conftest import run
from test_admin_menu_button import Api, buttons
from test_workspace_settings import create
from test_workspace_settings import runtime as runtime

import core
import max_
import ops
import spaces

SEND_MENU, NOTIFY = max_.Max.send_menu, max_.Max.notify


def connect(monkeypatch, api, adapters):
    monkeypatch.setattr(max_.Max, "send_menu", SEND_MENU)
    monkeypatch.setattr(max_.Max, "notify", NOTIFY)
    for ad in adapters:
        run(ad.http.aclose())
        ad.http = httpx.AsyncClient(base_url=max_.API, transport=httpx.MockTransport(api.handler))


def callback(uid, payload, mid=None):
    return {"update_type": "message_callback", "callback": {"callback_id": payload, "user": {"user_id": uid}, "payload": payload},
            "message": {"body": {"mid": mid}} if mid else None}


def test_restart_and_update_notice_buttons_stay_in_owners_space(runtime, monkeypatch, tmp_path):
    root_ad, _ = runtime
    row = create(root_ad, 1)
    space = spaces.get(row["id"])
    own = space.memory["ADAPTERS"]["max"]
    for _ in range(7):
        spaces.root().db.execute("INSERT INTO drafts(status,created) VALUES('pending',?)", (time.time(),))
    space.db.execute("INSERT INTO drafts(status,created) VALUES('pending',?)", (time.time(),))
    space.db.commit()
    spaces.root().db.execute("INSERT OR REPLACE INTO workspace_routes VALUES('1',0)")
    spaces.root().db.commit()
    api = Api()
    connect(monkeypatch, api, [root_ad, own])
    run(core.announce_start())
    notice = api.of("POST")[-1][3]
    assert "Ждут согласования: 1." in notice["text"] and "7." not in notice["text"]
    assert [b["payload"] for b in buttons(notice)] == [f"w:{space.id}:m:admin"]
    # Sending notices must not rewrite routing or close persisted input.
    assert spaces.root().db.execute("SELECT sid FROM workspace_routes WHERE uid='1'").fetchone()[0] == 0
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "update_result.json").write_text(json.dumps({"status": "ok", "platform": "max", "uid": "1", "commit": "abc123"}))
    run(ops.announce_update_result())
    result = api.of("POST")[-1][3]
    assert "Бот обновлён" in result["text"]
    assert [b["payload"] for b in buttons(result)] == [f"w:{space.id}:m:admin"]
    run(root_ad.handle(callback(1, f"w:{space.id}:m:admin")))
    menu = api.of("POST")[-1][3]
    assert "Меню администратора" in menu["text"] and space.name in menu["text"]
    assert not any("Эта кнопка относится" in call[3]["text"] for call in api.of("POST"))


def test_old_root_admin_buttons_open_own_menu_without_warning_duplicates(runtime, monkeypatch):
    root_ad, _ = runtime
    row = create(root_ad)
    space = spaces.get(row["id"])
    own = space.memory["ADAPTERS"]["max"]
    api = Api()
    connect(monkeypatch, api, [root_ad, own])
    mids = []
    for payload in ("m:admin", "w:0:m:admin", "w:0:m:menu", "w:0:m:admin"):
        run(root_ad.handle(callback(11, payload)))
        mids.append(f"m{api.n}")
        assert space.name in api.of("POST")[-1][3]["text"]
    assert not any("Эта кнопка относится" in call[3]["text"] for call in api.of("POST"))
    assert [c[2]["message_id"] for c in api.calls if c[0] == "DELETE"] == mids[:-1]
    with spaces.enter(space):
        assert own._ui("11")["menu"] == mids[-1]
    assert spaces.root().db.execute("SELECT sid FROM workspace_routes WHERE uid='11'").fetchone()[0] == space.id


def test_foreign_root_post_action_is_not_reinterpreted_and_warning_is_single_menu(runtime, monkeypatch):
    root_ad, _ = runtime
    row = create(root_ad)
    space = spaces.get(row["id"])
    own = space.memory["ADAPTERS"]["max"]
    for db in (spaces.root().db, space.db):
        db.execute("INSERT INTO drafts(id,platform,author_id,status,post,created) VALUES(42,'max','11','pending','Сохранить',?)", (time.time(),))
        db.commit()
    with spaces.enter(space):
        core.WAITING_EDIT[("max", "11")] = (42, 1)
        own._confirmations()[("max", "11")] = ["pending-publication"]
    api = Api()
    connect(monkeypatch, api, [root_ad, own])
    mids = []
    for _ in range(3):
        run(root_ad.handle(callback(11, "w:0:d:rejecttext:42:1", "old-root-post")))
        mids.append(f"m{api.n}")
    deleted = [c[2]["message_id"] for c in api.calls if c[0] == "DELETE"]
    assert deleted == mids[:-1] and "old-root-post" not in deleted
    for db in (spaces.root().db, space.db):
        assert db.execute("SELECT status FROM drafts WHERE id=42").fetchone()[0] == "pending"
    with spaces.enter(space):
        assert core.WAITING_EDIT[("max", "11")] == (42, 1)
        assert own._confirmations()[("max", "11")] == ("pending-publication",)
        assert own._ui("11")["menu"] == mids[-1]
    assert not any(c for c in api.of("POST") if c[2].get("chat_id"))


def test_notifications_preserve_open_dialog_in_other_space(runtime, monkeypatch):
    root_ad, _ = runtime
    a, b = create(root_ad, 1), create(root_ad, 22, -102)
    first, second = spaces.get(a["id"]), spaces.get(b["id"])
    with spaces.enter(second):
        ad = core.ADAPTERS["max"]
        core._add_person(ad, "1", "Администратор", "adm")
        core.WAITING_EDIT[("max", "1")] = (10, 2)
    spaces.root().db.execute("INSERT OR REPLACE INTO workspace_routes VALUES('1',?)", (second.id,))
    spaces.root().db.commit()
    api = Api()
    connect(monkeypatch, api, [root_ad, first.memory["ADAPTERS"]["max"], second.memory["ADAPTERS"]["max"]])
    run(core.announce_start())
    assert any(b["payload"] == f"w:{second.id}:m:admin" for b in buttons(api.of("POST")[-1][3]))
    with spaces.enter(second):
        assert core.WAITING_EDIT[("max", "1")] == (10, 2)
    assert spaces.for_user("1") == first.id
    assert spaces.for_user("1", continue_input=True) == second.id
