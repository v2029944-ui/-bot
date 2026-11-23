import shutil
import time

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


def test_shared_administrator_reviews_members_and_publishes_to_each_spaces_groups(runtime, monkeypatch, llm, png):
    ad, _ = runtime
    monkeypatch.setenv("TG_TOKEN", "123456:ABCDEF")
    first = create(ad, 11, -101, "-201")
    second = create(ad, 22, -102, "-202")
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
            core._add_person(adapter, "99", "Shared administrator", "adm")
            core._add_person(adapter, author, "Member", "member")
            did = run(core.submit(adapter, author, "Member", "Material for this team", png))
            assert did == 1  # same draft ID in two different databases
            assert not core.can_manage_draft(adapter, author, core.get(did))
            assert core.can_manage_draft(adapter, "99", core.get(did))
            assert not core.can_manage_draft(ad, "1", core.get(did))

    # Opening the callback selects its namespace even if the reviewer last used another team.
    async def scenario():
        for row in (second, first):
            assert spaces.INSTALLED and ad.workspace_id == 0
            await ad.handle({"update_type": "message_callback", "callback": {
                "callback_id": f"review-{row['id']}", "user": {"user_id": 99},
                "payload": f"w:{row['id']}:d:show:1:1"}})
            assert spaces.for_user("99") == row["id"]
            with spaces.enter(spaces.get(row["id"])):
                assert (await core.on_action(core.ADAPTERS["max"], "99", "okyes", 1, 1))[0]

    async def call(adapter, *args, **kwargs):
        return {}

    monkeypatch.setattr(max_.Max, "call", call)
    run(scenario())
    assert {uid for _, uid, _ in deliveries} == {"11", "22", "99"}
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
