import shutil

import pytest
from conftest import run

import core
import max_
import spaces
import tg


@pytest.fixture
def runtime(monkeypatch):
    messages = []

    async def send(adapter, uid, text, rows=None, **kwargs):
        messages.append((str(uid), text, rows))

    monkeypatch.setattr(max_.Max, "send_menu", send)
    monkeypatch.setattr(max_.Max, "notify", send)
    ad = max_.Max("token", "-100", ["1"], [], ["1"])
    core.ADAPTERS["max"] = ad
    spaces.install()
    root = spaces.root()
    waiting_rule = core.WAITING_RULE.root
    try:
        yield ad, messages
    finally:
        run(spaces.close())
        for name, original in root.memories.items():
            if name != "footer_waiting":
                setattr(core, name, original)
        core.post_footer.WAITING = root.memories["footer_waiting"]
        core.WAITING_RULE = waiting_rule
        core.db = root.db
        core.DATA, core.PHOTOS, core.VIDEOS = root.folder, root.folder / "photos", root.folder / "videos"
        root.db.execute("DELETE FROM workspaces")
        root.db.execute("DELETE FROM workspace_routes")
        root.db.commit()
        spaces.INSTALLED, spaces.ROOT = False, None
        spaces.WAITING_CREATE.clear()
        spaces.WAITING_MEMBER.clear()
        shutil.rmtree(root.folder / "spaces", ignore_errors=True)
        for adapter in list(root.adapters.values()):
            run(adapter.http.aclose() if adapter.name == "max" else adapter.bot.session.close())


def create(ad, owner=11, maximum=-101, telegram=None):
    run(core.on_menu(ad, "1", "m:space_add"))
    text = f"Компания {owner}\n{owner}\n{maximum}"
    if telegram is not None:
        text += "\n" + telegram
    run(core.on_text(ad, "1", text))
    row = spaces.root().db.execute("SELECT * FROM workspaces WHERE owner=?", (str(owner),)).fetchone()
    return row


def preview(ad, sid, text, uid="1"):
    run(core.on_menu(ad, uid, f"m:space_edit:{sid}"))
    run(core.on_text(ad, uid, text))
    return spaces.settings_state()[("max", uid)]["payload"]


@pytest.mark.parametrize("telegram", [None, "", "-"])
def test_create_max_only_with_omitted_blank_or_dash_telegram(runtime, telegram):
    ad, _ = runtime
    row = create(ad, telegram=telegram)
    assert row is not None and row["tg_channel"] is None
    space = spaces.get(row["id"])
    assert set(space.memory["ADAPTERS"]) == {"max"}
    assert create(ad, 22, -102, "-")["tg_channel"] is None  # multiple NULLs remain unique-safe
    run(spaces.close())
    assert set(spaces.get(row["id"]).memory["ADAPTERS"]) == {"max"}


def test_telegram_requires_configuration_but_dash_does_not(runtime):
    ad, messages = runtime
    assert create(ad, telegram="-201") is None
    assert "TG_TOKEN" in messages[-1][1]
    assert create(ad, telegram="-") is not None


def test_edit_preview_confirmation_replay_and_owner_revocation(runtime):
    ad, _ = runtime
    row = create(ad)
    sid = row["id"]
    space = spaces.get(sid)
    old_adapter = space.memory["ADAPTERS"]["max"]
    with spaces.enter(space):
        core._add_person(old_adapter, "11", "Прежний владелец", "adm")
        core._add_person(old_adapter, "33", "Участник", "member")
        core.WAITING_EDIT[("max", "11")] = (10, 1)
    payload = preview(ad, sid, "Новое название\n22\n-102\n-")
    assert spaces.settings_values(spaces.root().db.execute("SELECT * FROM workspaces WHERE id=?", (sid,)).fetchone()) == spaces.settings_values(row)
    assert "сохранены" in run(core.on_menu(ad, "1", payload))[0]
    assert "устарело" in run(core.on_menu(ad, "1", payload))[0]
    assert space.name == "Новое название" and old_adapter.channel == "-102"
    assert space.memory["ADAPTERS"]["max"] is old_adapter
    with spaces.enter(space):
        assert not core.is_member(old_adapter, "11")
        assert core.is_admin(old_adapter, "22")
        assert core.role_of(old_adapter, "33") == "member"
        assert ("max", "11") not in core.WAITING_EDIT
        assert "нет прав" in run(core.on_menu(old_adapter, "11", "m:people"))[0]
    assert spaces.for_user("22") == sid
    run(spaces.close())
    assert spaces.get(sid).memory["ADAPTERS"]["max"].channel == "-102"


def test_enable_and_disable_telegram_for_individual_space(runtime, monkeypatch):
    ad, _ = runtime
    monkeypatch.setenv("TG_TOKEN", "123456:ABCDEF")
    spaces.root().adapters["tg"] = tg.Telegram("123456:ABCDEF", "-200")
    row = create(ad, telegram="-")
    sid = row["id"]
    payload = preview(ad, sid, "Компания\n11\n-101\n-201")
    assert "сохранены" in run(core.on_menu(ad, "1", payload))[0]
    assert spaces.get(sid).memory["ADAPTERS"]["tg"].channel == "-201"
    payload = preview(ad, sid, "Компания\n11\n-101\n-")
    assert "сохранены" in run(core.on_menu(ad, "1", payload))[0]
    assert "tg" not in spaces.get(sid).memory["ADAPTERS"]
    assert "tg" in spaces.root().adapters


@pytest.mark.parametrize("mark", ["max", "max?"])
def test_partial_or_unknown_publication_blocks_destination_change(runtime, mark):
    ad, _ = runtime
    row = create(ad)
    space = spaces.get(row["id"])
    space.db.execute("INSERT INTO drafts(status,published_to) VALUES('pending',?)", (mark,))
    space.db.commit()
    payload = preview(ad, row["id"], "Компания\n11\n-102\n-")
    assert "незавершённая" in run(core.on_menu(ad, "1", payload))[0]
    assert space.memory["ADAPTERS"]["max"].channel == "-101"


def test_duplicate_owner_or_group_does_not_change_runtime_or_database(runtime):
    ad, _ = runtime
    row = create(ad)
    create(ad, 22, -102)
    payload = preview(ad, row["id"], "Дубль\n22\n-103\n-")
    assert "привязаны" in run(core.on_menu(ad, "1", payload))[0]
    assert spaces.get(row["id"]).memory["ADAPTERS"]["max"].admins == ["11"]
    saved = spaces.root().db.execute("SELECT * FROM workspaces WHERE id=?", (row["id"],)).fetchone()
    assert spaces.settings_values(saved) == spaces.settings_values(row)


def test_stale_confirmation_and_navigation_cannot_overwrite_settings(runtime):
    ad, _ = runtime
    row = create(ad)
    payload = preview(ad, row["id"], "Изменено\n11\n-102\n-")
    spaces.root().db.execute("UPDATE workspaces SET name='Чужая правка' WHERE id=?", (row["id"],))
    spaces.root().db.commit()
    assert "уже изменились" in run(core.on_menu(ad, "1", payload))[0]
    payload = preview(ad, row["id"], "Изменено\n11\n-102\n-")
    run(core.on_menu(ad, "1", "m:spaces"))
    assert "устарело" in run(core.on_menu(ad, "1", payload))[0]


def test_only_global_admin_can_edit_and_new_material_routes_to_updated_owner(runtime):
    ad, _ = runtime
    row = create(ad)
    space = spaces.get(row["id"])
    with spaces.enter(space):
        assert "главный" in run(core.on_menu(space.memory["ADAPTERS"]["max"], "11", f"m:space_edit:{row['id']}"))[0]
    assert not spaces.settings_state()
    spaces.root().db.execute("INSERT INTO workspaces(name,owner,max_channel) VALUES('Админ','1','-103')")
    spaces.root().db.commit()
    spaces.get(spaces.root().db.execute("SELECT id FROM workspaces WHERE owner='1'").fetchone()[0])
    run(core.on_menu(ad, "1", f"m:space_edit:{row['id']}"))
    event = {"update_type": "message_created", "message": {"sender": {"user_id": 1}, "body": {"text": "Название\n11\n-102\n-"}}}
    assert spaces.stamp(event)["message"]["_space"] == 0
    event["message"]["body"]["attachments"] = [{"type": "image"}]
    assert spaces.stamp(event)["message"]["_space"] != 0


def test_queue_processing_blocks_edit(runtime):
    ad, _ = runtime
    row = create(ad)
    spaces.root().db.execute("INSERT INTO max_inbox(event_key,state,space_id) VALUES('busy','processing',?)", (row["id"],))
    spaces.root().db.commit()
    payload = preview(ad, row["id"], "Название\n11\n-102\n-")
    assert "обработка" in run(core.on_menu(ad, "1", payload))[0]
    assert spaces.get(row["id"]).memory["ADAPTERS"]["max"].channel == "-101"


def test_max_only_space_publishes_without_using_root_telegram(runtime, monkeypatch, png, llm):
    ad, _ = runtime
    monkeypatch.setenv("TG_TOKEN", "123456:ABCDEF")
    spaces.root().adapters["tg"] = tg.Telegram("123456:ABCDEF", "-200")
    row = create(ad)
    space = spaces.get(row["id"])
    destinations = []

    async def approval(*args, **kwargs):
        return None

    async def publish(adapter, *args, **kwargs):
        destinations.append((spaces.active_id(), adapter.name, adapter.channel))

    monkeypatch.setattr(max_.Max, "send_for_approval", approval)
    monkeypatch.setattr(max_.Max, "publish", publish)
    monkeypatch.setattr(tg.Telegram, "publish", publish)
    with spaces.enter(space):
        target = core.ADAPTERS["max"]
        did = run(core.submit(target, "11", "Владелец", "Описание", png))
        assert run(core.on_action(target, "11", "okyes", did, 1))[0]
        assert core.get(did)["status"] == "published"
    assert destinations == [(row["id"], "max", "-101")]


def test_create_and_settings_inputs_have_working_cancel_buttons(runtime):
    ad, _ = runtime
    _, rows = run(core.on_menu(ad, "1", "m:space_add"))
    assert rows and rows[0][0][1].startswith("m:cancel:")
    run(core.on_menu(ad, "1", rows[0][0][1]))
    assert "1" not in spaces.WAITING_CREATE
    row = create(ad)
    _, rows = run(core.on_menu(ad, "1", f"m:space_edit:{row['id']}"))
    assert spaces.settings_state()[("max", "1")]["waiting"]
    run(core.on_menu(ad, "1", rows[0][0][1]))
    assert not spaces.settings_state()
    assert spaces.settings_values(spaces.root().db.execute("SELECT * FROM workspaces WHERE id=?", (row["id"],)).fetchone()) == spaces.settings_values(row)


def test_cancel_is_scoped_to_workspace_and_does_not_clear_other_input(runtime):
    ad, _ = runtime
    first, second = create(ad), create(ad, 22, -102)
    a, b = spaces.get(first["id"]), spaces.get(second["id"])
    for space in (a, b):
        with spaces.enter(space):
            core._add_person(core.ADAPTERS["max"], "33", "Админ", "adm")
    with spaces.enter(a):
        _, rows = run(core.on_menu(core.ADAPTERS["max"], "33", "m:add"))
        cancel_a = rows[0][0][1]
    with spaces.enter(b):
        _, rows = run(core.on_menu(core.ADAPTERS["max"], "33", "m:add"))
        cancel_b = rows[0][0][1]
        assert "устарела" in run(core.on_menu(core.ADAPTERS["max"], "33", cancel_a))[0]
        assert ("max", "33") in core.WAITING_RULE
        run(core.on_menu(core.ADAPTERS["max"], "33", cancel_b))
        assert ("max", "33") not in core.WAITING_RULE
    with spaces.enter(a):
        assert ("max", "33") in core.WAITING_RULE
        run(core.on_menu(core.ADAPTERS["max"], "33", cancel_a))
        assert ("max", "33") not in core.WAITING_RULE
