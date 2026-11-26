import shutil
import time

import pytest
from conftest import run

import core
import editor_rules
import max_
import ops
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
    assert root.adapters["max"] is ad and ad.workspace_id == 0
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
        root.db.execute("DELETE FROM archived_workspaces")
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


def test_management_panel_has_global_tools_and_separates_workspace_actions(runtime):
    ad, _ = runtime
    child = create(ad)
    text, rows = run(core.on_menu(ad, "1", "m:admin"))
    assert text == "Управление ботом"
    assert {payload for line in rows for _, payload in line} == {
        "m:spaces", "m:status", "m:inbox", "m:upd", "m:root_mode", "m:root_workspace", "m:common_rules",
    }
    with spaces.enter(spaces.get(child["id"])):
        text, rows = run(core.on_menu(core.ADAPTERS["max"], "1", "m:admin"))
        assert any(payload == "m:pending" for line in rows for _, payload in line)
        assert any(payload == "w:0:m:admin" for line in rows for _, payload in line)


def test_disabled_rules_persist_and_do_not_affect_another_workspace(runtime):
    ad, _ = runtime
    first, second = create(ad)["id"], create(ad, 22, -102)["id"]
    for sid in (first, second):
        with spaces.enter(spaces.get(sid)):
            editor_rules.add("Правило команды")

    with spaces.enter(spaces.get(first)):
        adapter = core.ADAPTERS["max"]
        _, rows = run(core.on_menu(adapter, "1", "m:rules"))
        toggle = next(payload for line in rows for _, payload in line if payload.startswith("m:rules_set:"))
        assert "нет прав" in run(core.on_menu(adapter, "22", toggle))[0]
        run(core.on_menu(adapter, "1", toggle))

    run(spaces.close())
    for sid, expected in [(first, []), (second, ["Правило команды"])]:
        with spaces.enter(spaces.get(sid)):
            assert editor_rules.active() == expected


def test_common_rules_apply_in_each_space_and_only_global_admin_changes_them(runtime):
    ad, _ = runtime
    first, second = create(ad)["id"], create(ad, 22, -102)["id"]
    run(core.on_menu(ad, "1", "m:root_mode:enable"))
    text, rows = run(core.on_menu(ad, "1", "m:common_rules"))
    assert "Общие правила" in text
    add = next(payload for line in rows for _, payload in line if payload == "m:common_add")
    run(core.on_menu(ad, "1", add))
    run(core.on_text(ad, "1", "Общее правило для двух команд"))
    assert editor_rules.active() == []

    with spaces.enter(spaces.get(first)):
        adapter = core.ADAPTERS["max"]
        assert "главный администратор" in run(core.on_menu(adapter, "11", add))[0]
        editor_rules.add("Правило только первой команды")
        prompt = core.build_system()
        assert "Общее правило для двух команд" in prompt
        assert "Правило только первой команды" in prompt
        _, rows = run(core.on_menu(adapter, "11", "m:rules"))
        toggle = next(payload for line in rows for _, payload in line if payload.startswith("m:rules_set:"))
        run(core.on_menu(adapter, "11", toggle))
        assert "Общее правило для двух команд" in core.build_system()
        assert "Правило только первой команды" not in core.build_system()

    with spaces.enter(spaces.get(second)):
        assert "Общее правило для двух команд" in core.build_system()
        assert "Правило только первой команды" not in core.build_system()
    _, rows = run(core.on_menu(ad, "1", "m:common_rules"))
    toggle = next(payload for line in rows for _, payload in line if payload.startswith("m:common_rules_set:"))
    run(core.on_menu(ad, "1", toggle))
    with spaces.enter(spaces.get(second)):
        assert "Общее правило для двух команд" not in core.build_system()


def test_management_only_refuses_pending_root_work_then_blocks_new_root_material(runtime):
    ad, messages = runtime
    child = create(ad)
    core._add_person(ad, "77", "Сотрудник", "member")
    with spaces.enter(spaces.get(child["id"])):
        core._add_person(core.ADAPTERS["max"], "77", "Сотрудник", "member")
    spaces.select_for_user("77", 0)
    assert spaces.for_user("77") == 0
    root = spaces.root().db
    root.execute("INSERT INTO drafts(status) VALUES('pending')")
    root.commit()
    text, _ = run(core.on_menu(ad, "1", "m:root_mode:enable"))
    assert "черновики" in text
    root.execute("UPDATE drafts SET status='deleted'")
    root.commit()
    text, rows = run(core.on_menu(ad, "1", "m:root_mode:enable"))
    assert "служебный режим включён" in text.lower()
    assert "m:root_workspace" not in {payload for line in rows for _, payload in line}
    assert "m:space:0" not in {payload for line in spaces.view()[1] for _, payload in line}
    assert "служебный режим" in run(core.on_menu(ad, "1", "m:footer"))[0].lower()

    post = {"sender": {"user_id": 1}, "_space": 0, "body": {
        "text": "Материал с описанием", "attachments": [{"type": "image"}],
    }}
    run(ad.on_message(post))
    assert "рабочее пространство" in messages[-1][1].lower()
    assert spaces.for_user("11") == child["id"]
    assert spaces.for_user("77") == child["id"]
    assert "попросите администратора" in run(core.on_menu(ad, "77", "m:root_mode:disable"))[0]
    assert spaces.management_only()
    assert create(ad, 22, -100)["max_channel"] == "-100"
    assert "занята" in run(core.on_menu(ad, "1", "m:root_mode:disable"))[0]


def test_max_admin_receives_member_drafts_and_publishes_to_each_spaces_groups(runtime, monkeypatch, llm, png):
    ad, _ = runtime
    monkeypatch.setenv("TG_TOKEN", "123456:ABCDEF")
    first = create(ad, 11, -101, "-201")
    second = create(ad, 22, -102, "-202")
    assert spaces.for_user("1") == 0
    deliveries, publications = [], []

    async def approval(adapter, uid, did, photo, post, **kwargs):
        deliveries.append((spaces.active_id(), str(uid), did))

    async def publish(adapter, photo, post, **kwargs):
        publications.append((spaces.active_id(), adapter.name, adapter.channel))

    monkeypatch.setattr(max_.Max, "send_for_approval", approval)
    monkeypatch.setattr(max_.Max, "publish", publish)
    monkeypatch.setattr(tg.Telegram, "publish", publish)
    for row, author in [(first, "31"), (second, "32")]:
        with spaces.enter(spaces.get(row["id"])):
            adapter = core.ADAPTERS["max"]
            core._add_person(adapter, author, "Member", "member")
            did = run(core.submit(adapter, author, "Member", "Material for this team", png))
            assert did == 1  # same draft ID in two different databases
            assert not core.can_manage_draft(adapter, author, core.get(did))
            assert core.can_manage_draft(adapter, "1", core.get(did))
            assert not core.can_manage_draft(ad, "1", core.get(did))
            ops.enqueue(adapter, row["owner"], did)  # legacy queued delivery to the local owner
            assert run(ops.flush_deferred()) == 1
            assert not core.db.execute("SELECT 1 FROM deferred").fetchone()

    # Opening the callback selects its namespace even if the reviewer last used another team.
    async def scenario():
        for row in (second, first):
            assert spaces.INSTALLED and ad.workspace_id == 0
            await ad.handle({"update_type": "message_callback", "callback": {
                "callback_id": f"review-{row['id']}", "user": {"user_id": 1},
                "payload": f"w:{row['id']}:d:show:1:1"}})
            assert spaces.for_user("1") == row["id"]
            with spaces.enter(spaces.get(row["id"])):
                assert (await core.on_action(core.ADAPTERS["max"], "1", "okyes", 1, 1))[0]

    async def call(adapter, *args, **kwargs):
        return {}

    monkeypatch.setattr(max_.Max, "call", call)
    run(scenario())
    assert deliveries == [(first["id"], "1", 1), (first["id"], "1", 1),
                          (second["id"], "1", 1), (second["id"], "1", 1),
                          (second["id"], "1", 1), (first["id"], "1", 1)]
    assert publications == [(second["id"], "max", "-102"), (second["id"], "tg", "-202"),
                            (first["id"], "max", "-101"), (first["id"], "tg", "-201")]


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


@pytest.mark.parametrize("form", ["edit", "create"])
@pytest.mark.parametrize("target", ["root", "child"])
def test_admin_can_edit_and_reject_draft_after_opening_workspace_form(runtime, monkeypatch, llm, png, form, target):
    ad, _ = runtime
    row = create(ad)
    sid = row["id"] if target == "child" else 0
    obsolete_save = preview(ad, row["id"], "Устаревшее название\n11\n-101")

    async def no_network(*args, **kwargs):
        return {}

    async def send_menu(adapter, uid, text, rows=None, **kwargs):
        buttons = [[{"type": "callback", "text": label, "payload": payload}
                    for label, payload in line] for line in (rows or [])]
        await adapter._send({"user_id": uid}, text, buttons=buttons)

    monkeypatch.setattr(max_.Max, "call", no_network)
    monkeypatch.setattr(max_.Max, "send_menu", send_menu)
    monkeypatch.setattr(max_.Max, "send_for_approval", no_network)

    async def scenario():
        async def press(payload):
            await ad.handle({"update_type": "message_callback", "callback": {
                "user": {"user_id": 1}, "callback_id": "test", "payload": payload}})

        async def reply(text):
            await ad.handle(spaces.stamp({"update_type": "message_created", "message": {
                "sender": {"user_id": 1}, "body": {"text": text}}}))

        with spaces.enter(spaces.get(sid)):
            did = await core.submit(core.ADAPTERS["max"], "1", "Автор", "Описание", png)

        form_payload = "m:space_add" if form == "create" else f"m:space_edit:{row['id']}"
        await press(f"w:0:{form_payload}")
        await press(f"w:{sid}:d:edit:{did}:1")
        await press(f"w:0:{obsolete_save}")
        await reply("Исправленный текст")
        with spaces.enter(spaces.get(sid)):
            assert core.get(did)["post"] == "Исправленный текст"
            attempt = core.get(did)["attempts"]

        await press(f"w:0:{form_payload}")
        await press(f"w:{sid}:d:del:{did}:{attempt}")
        with spaces.enter(spaces.get(sid)):
            confirmation = next(p for p in core.ADAPTERS["max"]._confirmations()[("max", "1")]
                                if p.startswith("d:rejectcustom:"))
        await press(f"w:{sid}:{confirmation}")
        await reply("Причина отклонения")
        with spaces.enter(spaces.get(sid)):
            assert core.get(did)["status"] == "deleted"
            assert core.get(did)["rejection_reason"] == "Причина отклонения"

    run(scenario())


def test_telegram_requires_configuration_but_dash_does_not(runtime):
    ad, messages = runtime
    assert create(ad, telegram="-201") is None
    assert "TG_TOKEN" in messages[-1][1]
    assert create(ad, telegram="-") is not None


def test_global_admin_can_open_archive_and_restore_workspace(runtime, monkeypatch):
    ad, messages = runtime
    row = create(ad)
    sid = row["id"]
    space = spaces.get(sid)
    other = spaces.get(create(ad, 22, -102)["id"])
    for candidate in (space, other):
        with spaces.enter(candidate):
            core._add_person(core.ADAPTERS["max"], "33", "Участник", "member")
    assert spaces.for_user("33") == sid
    space.db.execute("INSERT INTO drafts(status,post,created) VALUES('pending','Сохранённый черновик',?)", (time.time(),))
    space.db.commit()

    async def call(*args, **kwargs):
        return {}

    monkeypatch.setattr(max_.Max, "call", call)

    async def scenario():
        async def press(uid, payload):
            await ad.handle({"update_type": "message_callback", "callback": {
                "user": {"user_id": uid}, "callback_id": "test", "payload": payload}})
            return messages[-1][1:]

        _, rows = await press(1, f"w:0:m:space:{sid}")
        open_menu = next(payload for line in rows for _, payload in line if payload == f"w:{sid}:m:admin")
        _, rows = await press(1, open_menu)
        assert any(payload == "m:pending" for line in rows for _, payload in line)
        assert spaces.for_user("1") == sid
        message, _ = await press(11, f"w:{sid}:m:space_archive:{sid}")
        assert "главный администратор" in message

        async def archive():
            _, rows = await press(1, f"w:0:m:space_archive:{sid}")
            return await press(1, f"w:0:{rows[0][0][1]}")

        assert "черновики" in (await archive())[0]
        assert spaces.settings_record(sid)
        space.db.execute("UPDATE drafts SET status='deleted',closed_at=?", (time.time(),))
        space.db.commit()
        assert "перемещено в архив" in (await archive())[0]
        assert spaces.settings_record(sid) is None and sid not in spaces.OPEN
        assert "недоступно" in (await press(1, open_menu))[0]
        assert spaces.for_user("11") == 0
        material = {"update_type": "message_created", "message": {"sender": {"user_id": 33},
                    "body": {"attachments": [{"type": "image"}]}}}
        await ad.handle(spaces.stamp(material))
        message, choices = messages[-1][1:]
        assert "в архиве" in message
        assert choices == [[(other.name, f"w:{other.id}:m:menu")]]
        await press(33, choices[0][0][1])
        assert spaces.stamp(material)["message"]["_space"] == other.id

        await spaces.close()
        _, rows = await press(1, "w:0:m:spaces_archive")
        assert rows[0][0][1] == f"m:space_restore:{sid}"
        _, rows = await press(1, f"w:0:{rows[0][0][1]}")
        restore = f"w:0:{rows[0][0][1]}"
        await press(1, restore)
        assert "устарело" in (await press(1, restore))[0]
        assert spaces.settings_values(spaces.settings_record(sid)) == spaces.settings_values(row)
        assert spaces.archived_record(sid) is None and spaces.for_user("11") == sid
        with spaces.enter(spaces.get(sid)):
            assert core.get(1)["post"] == "Сохранённый черновик"

    run(scenario())


def test_stale_draft_button_cannot_move_active_edit_to_another_workspace(runtime, monkeypatch):
    ad, _ = runtime
    first, second = create(ad)["id"], create(ad, 22, -102)["id"]

    async def no_network(*args, **kwargs):
        return {}

    monkeypatch.setattr(max_.Max, "call", no_network)
    monkeypatch.setattr(max_.Max, "send_for_approval", no_network)
    for sid in (first, second):
        db = spaces.get(sid).db
        db.execute("""INSERT INTO drafts(id,platform,post,status,attempts,created)
                      VALUES(1,'max',?,'pending',2,?)""", (f"Пост {sid}", time.time()))
        db.commit()

    async def scenario():
        for sid, revision in [(first, 2), (second, 2), (first, 1)]:
            await ad.handle({"update_type": "message_callback", "callback": {
                "user": {"user_id": 1}, "callback_id": "test", "payload": f"w:{sid}:d:edit:1:{revision}"}})
        assert spaces.for_user("1", continue_input=True) == second
        await ad.handle(spaces.stamp({"update_type": "message_created", "message": {
            "sender": {"user_id": 1}, "body": {"text": "Правка второй команды"}}}))

    run(scenario())
    assert spaces.get(first).db.execute("SELECT post FROM drafts WHERE id=1").fetchone()[0] == f"Пост {first}"
    assert spaces.get(second).db.execute("SELECT post FROM drafts WHERE id=1").fetchone()[0] == "Правка второй команды"


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
        assert core.is_admin(old_adapter, "1")
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
    assert spaces.get(row["id"]).memory["ADAPTERS"]["max"].admins == ["1", "11"]
    saved = spaces.root().db.execute("SELECT * FROM workspaces WHERE id=?", (row["id"],)).fetchone()
    assert spaces.settings_values(saved) == spaces.settings_values(row)


def test_rename_only_with_existing_main_group_overlap_and_unfinished_publication(runtime):
    ad, _ = runtime
    row = create(ad)
    sid = row["id"]
    space = spaces.get(sid)
    # The bootstrap MAX_CHANNEL can be changed after a child space is created.
    ad.channel = row["max_channel"]
    space.db.execute("INSERT INTO drafts(status,published_to) VALUES('pending','max?')")
    space.db.commit()
    space.memory["ADAPTERS"]["max"]._confirmations()[("max", "1")] = ["existing"]

    _, card = run(core.on_menu(ad, "1", f"m:space:{sid}"))
    _, fields = run(core.on_menu(ad, "1", card[0][0][1]))
    rename = next(payload for line in fields for label, payload in line if "Название" in label)
    run(core.on_menu(ad, "1", rename))
    run(core.on_text(ad, "1", "Новое название"))
    confirmation = spaces.settings_state()[("max", "1")]["payload"]
    assert "сохранены" in run(core.on_menu(ad, "1", confirmation))[0]
    assert spaces.settings_values(spaces.settings_record(sid)) == ["Новое название", "11", "-101", None]
    assert space.name == "Новое название"
    assert space.memory["ADAPTERS"]["max"]._confirmations()[("max", "1")] == ("existing",)


def test_edit_max_group_only_explains_main_conflict_and_keeps_form_open(runtime):
    ad, messages = runtime
    sid = create(ad)["id"]
    root_before = spaces.settings_values(spaces.settings_record(0))
    run(core.on_menu(ad, "1", f"m:space_edit:{sid}:max_channel"))
    run(core.on_text(ad, "1", "-100"))
    assert "основном пространстве №0" in messages[-1][1]
    assert spaces.settings_state()[("max", "1")]["waiting"]

    run(core.on_text(ad, "1", "-102"))
    confirmation = spaces.settings_state()[("max", "1")]["payload"]
    assert "сохранены" in run(core.on_menu(ad, "1", confirmation))[0]
    assert spaces.settings_record(sid)["max_channel"] == "-102"
    assert ad.channel == "-100"

    run(core.on_menu(ad, "1", "m:space_edit:0:max_channel"))
    run(core.on_text(ad, "1", "-103"))
    confirmation = spaces.settings_state()[("max", "1")]["payload"]
    assert "сохранены" in run(core.on_menu(ad, "1", confirmation))[0]
    root_before[2] = "-103"
    assert spaces.settings_values(spaces.settings_record(0)) == root_before

    run(core.on_menu(ad, "1", f"m:space_edit:{sid}:max_channel"))
    run(core.on_text(ad, "1", "-100"))
    confirmation = spaces.settings_state()[("max", "1")]["payload"]
    assert "сохранены" in run(core.on_menu(ad, "1", confirmation))[0]
    assert spaces.settings_record(sid)["max_channel"] == "-100"
    assert ad.channel == "-103"


@pytest.mark.parametrize(("field", "new", "expected"),
                         [("owner", "22", ["Компания 11", "22", "-101", "-201"]),
                          ("tg_channel", "-", ["Компания 11", "11", "-101", None])])
def test_edit_workspace_owner_or_telegram_only(runtime, monkeypatch, field, new, expected):
    monkeypatch.setenv("TG_TOKEN", "123456:ABCDEF")
    ad, _ = runtime
    sid = create(ad, telegram="-201")["id"]
    run(core.on_menu(ad, "1", f"m:space_edit:{sid}:{field}"))
    run(core.on_text(ad, "1", new))
    confirmation = spaces.settings_state()[("max", "1")]["payload"]
    assert "сохранены" in run(core.on_menu(ad, "1", confirmation))[0]
    assert spaces.settings_values(spaces.settings_record(sid)) == expected


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


def test_primary_space_adopts_env_members_without_moving_data_and_keeps_global_control(runtime):
    ad, _ = runtime
    ad.base_employees = ["77"]
    ad.base_approvers = ["78"]
    core.refresh_people(ad)
    core.db.execute("INSERT INTO rules(text,created) VALUES('Исходное правило',0)")
    core.db.execute("INSERT INTO drafts(platform,author_id,status,created,post) VALUES('max','77','pending',?, 'Черновик')", (time.time(),))
    core.db.commit()
    before = core.db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]
    assert any(p == "m:space:0" for row in spaces.view()[1] for _, p in row)
    payload = preview(ad, 0, "Наша компания\n22\n-110\n-")
    assert spaces.configured_root(spaces.root().db) is None
    assert "сохранены" in run(core.on_menu(ad, "1", payload))[0]
    assert spaces.configured_root(spaces.root().db) == {"name": "Наша компания", "owner": "22", "max_channel": "-110", "tg_channel": None}
    assert ad.channel == "-110" and spaces.root().name == "Наша компания"
    assert core.role_of(ad, "77") == core.role_of(ad, "78") == "member"
    assert core.is_admin(ad, "22") and not spaces.global_admin("22") and not core.is_global_admin(ad, "22")
    assert core.is_global_admin(ad, "1") and spaces.global_admin("1")
    assert "только администратор из MAX_ADMINS" in run(core.on_menu(ad, "22", "m:upd"))[0]
    assert "главный" in run(core.on_menu(ad, "22", "m:spaces"))[0]
    assert core.db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == before
    assert "Исходное правило" in core.build_system() and "Наша компания" in core.build_system()
    assert core.db.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 0
    core._remove_person(ad, "77", "Удалённый")
    payload = preview(ad, 0, "Наша компания\n33\n-110\n-")
    assert "сохранены" in run(core.on_menu(ad, "1", payload))[0]
    assert core.role_of(ad, "77") is None and not core.is_member(ad, "22")
    assert core.is_admin(ad, "33") and spaces.global_admin("1")


def test_primary_space_blocks_group_collision_and_partial_publication(runtime):
    ad, _ = runtime
    create(ad, 11, -101)
    run(core.on_menu(ad, "1", "m:space_edit:0"))
    run(core.on_text(ad, "1", "Основное\n1\n-101\n-"))
    assert spaces.settings_state()[("max", "1")]["waiting"]
    assert spaces.configured_root(spaces.root().db) is None
    payload = preview(ad, 0, "Основное\n1\n-100\n-")
    spaces.root().db.execute("INSERT INTO drafts(status,published_to) VALUES('pending','max?')")
    spaces.root().db.commit()
    assert "незавершённая" in run(core.on_menu(ad, "1", payload))[0]
    assert spaces.configured_root(spaces.root().db) is None
    assert ad.channel == "-100"


def test_primary_telegram_can_be_disabled_without_disabling_other_spaces(runtime, monkeypatch):
    ad, _ = runtime
    monkeypatch.setenv("TG_TOKEN", "123456:ABCDEF")
    spaces.root().adapters["tg"] = tg.Telegram("123456:ABCDEF", "-200")
    row = create(ad, telegram="-201")
    payload = preview(ad, 0, "Основное\n1\n-100\n-")
    assert "сохранены" in run(core.on_menu(ad, "1", payload))[0]
    assert "tg" not in spaces.root().adapters
    assert spaces.get(row["id"]).memory["ADAPTERS"]["tg"].channel == "-201"
    run(spaces.close())
    assert spaces.get(row["id"]).memory["ADAPTERS"]["tg"].channel == "-201"
    assert create(ad, 22, -102, "-202")["tg_channel"] == "-202"


def test_primary_owner_routes_to_primary_even_if_previously_used_another_space(runtime):
    ad, _ = runtime
    row = create(ad)
    child = spaces.get(row["id"])
    with spaces.enter(child):
        core._add_person(core.ADAPTERS["max"], "22", "Участник", "member")
    spaces.root().db.execute("INSERT OR REPLACE INTO workspace_routes VALUES('22',?)", (row["id"],))
    spaces.root().db.commit()
    assert spaces.for_user("22") == row["id"]
    payload = preview(ad, 0, "Основное\n22\n-100\n-")
    assert "сохранены" in run(core.on_menu(ad, "1", payload))[0]
    assert spaces.for_user("22") == 0
    assert not spaces.global_admin("22")


def test_restart_in_management_mode_with_only_a_child_telegram_group(runtime, monkeypatch, tmp_path):
    import os
    import sqlite3
    import subprocess
    import sys

    ad, _ = runtime
    monkeypatch.setenv("TG_TOKEN", "123456:ABCDEF")
    child = create(ad, telegram="-201")
    assert "служебный режим включён" in run(core.on_menu(ad, "1", "m:root_mode:enable"))[0].lower()
    target = sqlite3.connect(tmp_path / "drafts.db")
    spaces.root().db.backup(target)
    target.close()
    script = f'''import asyncio
import core, main, spaces
async def quiet(*args): pass
core.announce_start = quiet
main.ops.announce_update_result = quiet
async def pilot(workers):
    assert spaces.management_only()
    assert core.ADAPTERS['max'].channel == ''
    assert 'tg' not in core.ADAPTERS
    assert spaces.get({child["id"]}).memory['ADAPTERS']['tg'].channel == '-201'
main.serve = pilot
asyncio.run(main.main())
'''
    env = {**os.environ, "DATA_DIR": str(tmp_path), "MAX_TOKEN": "test-token", "MAX_CHANNEL": "", "TG_CHANNEL": "",
           "MAX_ADMINS": "1", "TG_TOKEN": "123456:ABCDEF", "LLM_API_KEY": "test-key",
           "LLM_PROVIDER": "openai", "LLM_BASE_URL": "https://llm.example/v1"}
    result = subprocess.run([sys.executable, "-c", script], env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_restart_uses_saved_primary_settings_when_env_channel_ids_are_empty(runtime, monkeypatch, tmp_path):
    import os
    import sqlite3
    import subprocess
    import sys

    ad, _ = runtime
    monkeypatch.setenv("TG_TOKEN", "123456:ABCDEF")
    spaces.root().adapters["tg"] = tg.Telegram("123456:ABCDEF", "-200")
    ad.base_employees = ["77", "78"]
    core.refresh_people(ad)
    child = create(ad, telegram="-201")
    payload = preview(ad, 0, "Название из базы\n22\n-110\n-")
    assert "сохранены" in run(core.on_menu(ad, "1", payload))[0]
    core._remove_person(ad, "77", "Убрали")
    target = sqlite3.connect(tmp_path / "drafts.db")
    spaces.root().db.backup(target)
    target.close()
    script = '''import asyncio
import core, main, spaces
async def quiet(*args): pass
core.announce_start = quiet
main.ops.announce_update_result = quiet
async def pilot(workers):
    ad = core.ADAPTERS['max']
    assert ad.channel == '-110'
    assert spaces.root().name == 'Название из базы'
    assert core.is_admin(ad, '22') and not core.is_global_admin(ad, '22')
    assert core.is_global_admin(ad, '1')
    assert core.role_of(ad, '77') is None  # stale env must not restore a removed member
    assert core.role_of(ad, '78') == 'member'
    assert 'tg' not in core.ADAPTERS
    assert spaces.get(CHILD).memory['ADAPTERS']['tg'].channel == '-201'
    assert core.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
main.serve = pilot
asyncio.run(main.main())
'''.replace('CHILD', str(child["id"]))
    env = {**os.environ, "DATA_DIR": str(tmp_path), "MAX_TOKEN": "test-token", "MAX_CHANNEL": "", "TG_CHANNEL": "",
           "MAX_ADMINS": "1", "MAX_EMPLOYEES": "77", "TG_TOKEN": "123456:ABCDEF", "LLM_API_KEY": "test-key",
           "LLM_PROVIDER": "openai", "LLM_BASE_URL": "https://llm.example/v1"}
    result = subprocess.run([sys.executable, "-c", script], env=env, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_primary_owner_cannot_view_or_retry_foreign_queue_or_see_global_balance(runtime, monkeypatch, llm):
    ad, _ = runtime
    child = create(ad)
    payload = preview(ad, 0, "Основное\n22\n-100\n-")
    assert "сохранены" in run(core.on_menu(ad, "1", payload))[0]
    spaces.root().db.execute("INSERT INTO max_inbox(event_key,state,error,space_id) VALUES('foreign','failed','ЧУЖОЙ_МАТЕРИАЛ',?)", (child["id"],))
    spaces.root().db.commit()
    text, _ = run(core.on_menu(ad, "22", "m:inbox"))
    assert "ЧУЖОЙ_МАТЕРИАЛ" not in text
    run(core.on_menu(ad, "22", "m:inretry:foreign"))
    assert spaces.root().db.execute("SELECT state FROM max_inbox WHERE event_key='foreign'").fetchone()[0] == "failed"
    assert "ЧУЖОЙ_МАТЕРИАЛ" in run(core.on_menu(ad, "1", "m:inbox"))[0]
    balances = []
    async def balance():
        balances.append(True)
        return "Общий баланс"
    monkeypatch.setattr(core, "aitunnel_balance", balance)
    assert "Общий баланс" not in run(core.on_menu(ad, "22", "m:status"))[0]
    assert not balances
    assert "Общий баланс" in run(core.on_menu(ad, "1", "m:status"))[0]


@pytest.mark.parametrize("service_only", [False, True])
def test_status_aggregates_active_spaces_and_scopes_workspace_view(runtime, llm, monkeypatch, service_only):
    ad, _ = runtime
    first = spaces.get(create(ad)["id"])
    monkeypatch.setenv("TG_TOKEN", "123456:ABCDEF")
    second = spaces.get(create(ad, 22, -102, "-202")["id"])
    archived = spaces.get(create(ad, 33, -103)["id"])
    now = time.time()
    monkeypatch.setitem(spaces.root().memories["LLM_STATE"], "ok", 0)
    monkeypatch.setitem(spaces.root().memories["LLM_STATE"], "err", 0)
    first.memory["LLM_STATE"]["ok"] = now - 100
    second.memory["LLM_STATE"].update(err=now - 20, msg="ОШИБКА ВТОРОЙ КОМАНДЫ")
    archived.memory["LLM_STATE"].update(err=now, msg="ОШИБКА АРХИВА")
    for db in (spaces.root().db, first.db, second.db, archived.db):
        db.executemany("INSERT INTO drafts(status,created,published_at) VALUES(?,?,?)", [
            ("pending", now - 48 * 3600, 0), ("published", now - 600, now - 600),
            ("regenerating", now - 1000, 0), ("published", now - 14 * 86400, 0),
        ])
        db.commit()
    # Older/archived records stay on disk but no longer belong to the active registry.
    spaces.root().db.execute("DELETE FROM workspaces WHERE id=?", (archived.id,))
    if service_only:
        spaces.root().db.execute("INSERT INTO settings(key,value) VALUES('root_management_only','1')")
    spaces.root().db.commit()
    expected = 2 if service_only else 3
    whole, rows = run(core.on_menu(ad, "1", "m:status"))
    assert "Мессенджеры: max, tg" in whole
    assert "ОШИБКА ВТОРОЙ КОМАНДЫ" in whole and "ОШИБКА АРХИВА" not in whole
    assert f"На согласовании: {expected}" in whole
    assert "старейший ждёт 2 дн" in whole
    assert f"За сутки создано черновиков: {expected * 2}, опубликовано за неделю: {expected}" in whole
    assert f"Зависших черновиков: {expected}" in whole and f"У {expected} старых публикаций" in whole
    assert any(payload == "m:check_llm" for row in rows for _, payload in row)
    with spaces.enter(first):
        own, _ = run(core.on_menu(core.ADAPTERS["max"], "11", "m:status"))
        assert "Мессенджеры: max\n" in own
        assert "последний пост написан" in own and "ОШИБКА ВТОРОЙ КОМАНДЫ" not in own
        assert "На согласовании: 1" in own and "опубликовано за неделю: 1" in own
    if not service_only:
        _, rows = run(core.on_menu(ad, "1", "m:root_workspace"))
        status = next(payload for row in rows for label, payload in row if label == "📊 Статус")
        work_zero, _ = run(core.on_menu(ad, "1", status))
        assert "На согласовании: 1" in work_zero and "опубликовано за неделю: 1" in work_zero
    assert not llm.calls


def test_primary_space_preserves_telegram_username_and_rejects_corrupt_config(runtime, monkeypatch):
    ad, _ = runtime
    monkeypatch.setenv("TG_TOKEN", "123456:ABCDEF")
    spaces.root().adapters["tg"] = tg.Telegram("123456:ABCDEF", "@CompanyNews")
    payload = preview(ad, 0, "Основное\n1\n-100\n@CompanyNews")
    assert "сохранены" in run(core.on_menu(ad, "1", payload))[0]
    assert spaces.configured_root(spaces.root().db)["tg_channel"] == "@companynews"
    run(core.on_menu(ad, "1", "m:space_add"))
    run(core.on_text(ad, "1", "Дубль\n22\n-101\n@COMPANYNEWS"))
    assert spaces.root().db.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 0
    spaces.root().db.execute("UPDATE settings SET value='{}' WHERE key='root_workspace'")
    spaces.root().db.commit()
    with pytest.raises(ValueError, match="Повреждены"):
        spaces.configured_root(spaces.root().db)


def test_existing_member_becomes_protected_primary_owner_without_global_powers(runtime):
    ad, _ = runtime
    core._add_person(ad, "22", "Будущий владелец", "member")
    payload = preview(ad, 0, "Основное\n22\n-100\n-")
    assert "сохранены" in run(core.on_menu(ad, "1", payload))[0]
    assert core._person(ad, "22")["role"] == "adm"
    assert "👑 Будущий владелец" in core.people_view(ad)[0]
    assert "защищённый" in run(core.on_menu(ad, "1", "m:pdel:22"))[0]
    assert not spaces.global_admin("22") and not core.is_global_admin(ad, "22")


def test_model_picker_selections_are_independent_and_persist_in_each_workspace(runtime, monkeypatch):
    import httpx
    from test_model_picker import INFO, PAIR

    import comparison

    ad, _ = runtime
    first, second = create(ad), create(ad, 22, -102)
    a, b = spaces.get(first["id"]), spaces.get(second["id"])
    monkeypatch.setattr(core, "PROVIDER", "openai")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.aitunnel.ru/v1")
    original = httpx.AsyncClient
    monkeypatch.setattr(comparison.httpx, "AsyncClient", lambda **kw: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={name: INFO for name in PAIR})), **kw))
    with spaces.enter(a):
        own = core.ADAPTERS["max"]
        run(core.on_menu(own, "11", "m:compare_pick"))
        state = comparison.picker_state()[("max", "11")]
        save = f"m:models_save:{state['token']}:{state['revision']}"
        assert "сохранены" in run(core.on_menu(own, "11", save))[0]
        assert comparison.config()["models"] == PAIR
    with spaces.enter(b):
        assert not comparison.config()["enabled"]
        assert "уже закрыт" in run(core.on_menu(core.ADAPTERS["max"], "22", save))[0]
        assert not comparison.config()["enabled"]
    assert not comparison.config()["enabled"]  # root has its own selection
    run(spaces.close())
    with spaces.enter(spaces.get(first["id"])):
        assert comparison.config()["models"] == PAIR
