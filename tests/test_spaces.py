import asyncio
import shutil
import time

from conftest import run

import core
import max_
import max_actions
import max_queue
import ops
import post_footer
import spaces
import tg


def test_two_spaces_isolate_data_callbacks_settings_and_both_destinations(monkeypatch, png, llm):
    async def scenario():
        root_max = max_.Max("token", "-100", ["1"], [], ["1"])
        root_tg = tg.Telegram("123456:ABCDEF", "-200")
        core.ADAPTERS.update(max=root_max, tg=root_tg)
        monkeypatch.setenv("TG_TOKEN", "123456:ABCDEF")
        calls, publications = [], []
        messages = {}

        async def call(adapter, method, path, **kw):
            calls.append((spaces.active_id(), method, path, kw))
            if method == "POST" and path == "/messages":
                mid = f"m{len(calls)}"
                messages[mid] = kw["json"]
                return {"message": {"body": {"mid": mid}}}
            if method == "GET" and path.startswith("/messages/"):
                return {"body": messages[path.rsplit("/", 1)[-1]]}
            return {}

        async def upload(adapter, path):
            return "image-token"

        async def download(adapter, *args):
            return png

        async def publish(adapter, photo, post, **kwargs):
            publications.append((spaces.active_id(), adapter.channel, str(post)))

        monkeypatch.setattr(max_.Max, "call", call)
        monkeypatch.setattr(max_.Max, "_upload_image", upload)
        monkeypatch.setattr(max_.Max, "_download", download)
        monkeypatch.setattr(tg.Telegram, "publish", publish)
        spaces.install()
        root = spaces.ROOT
        waiting_rule = core.WAITING_RULE.root
        try:
            root.db.execute("INSERT INTO workspaces(name,owner,max_channel,tg_channel) VALUES('Компания А','11','-101','-201')")
            aid = root.db.execute("SELECT last_insert_rowid()").fetchone()[0]
            root.db.execute("INSERT INTO workspaces(name,owner,max_channel,tg_channel) VALUES('Компания Б','22','-102','-202')")
            bid = root.db.execute("SELECT last_insert_rowid()").fetchone()[0]
            root.db.commit()
            a, b = spaces.get(aid), spaces.get(bid)

            async def test_version():
                return "test"
            monkeypatch.setattr(ops, "version", test_version)
            now = time.time()
            root.db.execute("""INSERT INTO max_handoff_failures
                               (event_key,uid,source_mid,space_id,created,failed_at,attempts,reason)
                               VALUES('ha','11','ma',?,?,?,?,?)""",
                            (aid, now - 10, now, 4, "ошибка А"))
            root.db.execute("""INSERT INTO max_handoff_failures
                               (event_key,uid,source_mid,space_id,created,failed_at,attempts,reason)
                               VALUES('hb','22','mb',?,?,?,?,?)""",
                            (bid, now - 10, now, 5, "ошибка Б"))
            root.db.commit()
            with spaces.enter(a):
                status_a = await core.status_text(core.ADAPTERS["max"], include_global=False)
                assert "ошибка А" in status_a and "ошибка Б" not in status_a
            with spaces.enter(b):
                status_b = await core.status_text(core.ADAPTERS["max"], include_global=False)
                assert "ошибка Б" in status_b and "ошибка А" not in status_b
            global_status = await core.status_text(root_max, include_global=True)
            assert "ошибка А" in global_status and "ошибка Б" in global_status
            root.db.execute("DELETE FROM max_handoff_failures")
            root.db.commit()

            # Durable staff notices re-check current workspace rights immediately
            # before delivery. A removed administrator must not receive stale details.
            with spaces.enter(b):
                bmax = core.ADAPTERS["max"]
                core._add_person(bmax, "55", "Временный администратор", "adm")
                assert "55" in core.staff(bmax)
            root.db.execute("""INSERT INTO max_handoff_notices
                               (event_key,kind,uid,space_id,text,state,created)
                               VALUES('revoked-staff','staff','55',?,'secret','sending',?)""",
                            (bid, time.time()))
            root.db.commit()
            with spaces.enter(b):
                bmax = core.ADAPTERS["max"]
                core._remove_person(bmax, "55", "Временный администратор")
                assert "55" not in core.staff(bmax)
            before = len(calls)
            notice = dict(root.db.execute(
                "SELECT * FROM max_handoff_notices WHERE event_key='revoked-staff'").fetchone())
            await max_actions.Worker(root_max).process_notice(notice)
            skipped = root.db.execute(
                "SELECT state,last_error FROM max_handoff_notices WHERE event_key='revoked-staff'").fetchone()
            assert skipped["state"] == "skipped" and "отозваны" in skipped["last_error"]
            assert len(calls) == before

            # If the workspace cannot be opened, terminal diagnostics still persist
            # and the warning is queued only for immutable global administrators.
            original_get = spaces.get

            def fail_space(value):
                if value == bid:
                    raise RuntimeError("workspace unavailable")
                return original_get(value)

            monkeypatch.setattr(spaces, "get", fail_space)
            now = time.time()
            root.db.execute("""INSERT INTO max_actions
                               (event_key,uid,payload,state,created,attempts,next_try,space_id)
                               VALUES('fallback-global','9','{}','handoff_processing',?,6,0,?)""",
                            (now - 8 * 86400, bid))
            action = dict(root.db.execute(
                "SELECT * FROM max_actions WHERE event_key='fallback-global'").fetchone())
            max_actions.Worker(root_max)._terminal_handoff(
                action, {"message": {"_space": bid, "body": {"mid": "m-fallback"}}},
                7, now, "workspace unavailable")
            fallback = root.db.execute(
                """SELECT kind,uid,space_id,state FROM max_handoff_notices
                   WHERE event_key='fallback-global' ORDER BY kind,uid""").fetchall()
            assert [tuple(row) for row in fallback] == [
                ("global_staff", "1", bid, "pending"),
                ("user", "9", bid, "pending"),
            ]
            assert not root.db.execute(
                "SELECT 1 FROM max_handoff_notices WHERE event_key='fallback-global' AND uid='22'").fetchone()
            monkeypatch.setattr(spaces, "get", original_get)

            # Preparation failures happen before Max.notify(), so they are safe to
            # retry. Once the workspace is available again the notice is sent once.
            root.db.execute("""INSERT INTO max_handoff_notices
                               (event_key,kind,uid,space_id,text,state,created)
                               VALUES('prepare-retry','user','33',?,'retry-me','sending',?)""",
                            (bid, time.time()))
            root.db.commit()
            original_get = spaces.get

            def fail_prepare(value):
                if value == bid:
                    raise RuntimeError("workspace temporarily unavailable")
                return original_get(value)

            monkeypatch.setattr(spaces, "get", fail_prepare)
            notice = dict(root.db.execute(
                "SELECT * FROM max_handoff_notices WHERE event_key='prepare-retry'").fetchone())
            before = len(calls)
            notice_worker = max_actions.Worker(root_max)
            await notice_worker.process_notice(notice)
            pending = root.db.execute(
                "SELECT state,attempts,next_try FROM max_handoff_notices WHERE event_key='prepare-retry'").fetchone()
            assert pending["state"] == "pending" and pending["attempts"] == 1 and pending["next_try"] > time.time()
            assert len(calls) == before

            monkeypatch.setattr(spaces, "get", original_get)
            root.db.execute(
                "UPDATE max_handoff_notices SET state='sending',next_try=0 WHERE event_key='prepare-retry'")
            root.db.commit()
            notice = dict(root.db.execute(
                "SELECT * FROM max_handoff_notices WHERE event_key='prepare-retry'").fetchone())
            await notice_worker.process_notice(notice)
            done = root.db.execute(
                "SELECT state,attempts FROM max_handoff_notices WHERE event_key='prepare-retry'").fetchone()
            assert done["state"] == "done" and done["attempts"] == 1
            sent_retry = [kw for _, method, path, kw in calls[before:]
                          if method == "POST" and path == "/messages"
                          and kw.get("json", {}).get("text") == "retry-me"]
            assert len(sent_retry) == 1

            for space, rule in [(a, "ТОЛЬКО_ПРАВИЛО_А"), (b, "ТОЛЬКО_ПРАВИЛО_Б")]:
                with spaces.enter(space):
                    ad = core.ADAPTERS["max"]
                    core.rules_command(ad, ad.admins[0], "/rule_add " + rule)
                    post_footer.save({"enabled": True, "text": space.name, "marker": "{bot}",
                                      "max": f"https://max.ru/bot{space.id}", "tg": f"https://t.me/bot{space.id}"})
                    assert rule in core.build_system()
            assert root.db.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0

            def material(uid):
                return {"update_type": "message_created", "message": {"sender": {"user_id": uid, "first_name": str(uid)},
                        "recipient": {"chat_type": "dialog"}, "body": {"mid": "same-mid", "text": "Описание работы",
                        "attachments": [{"type": "image", "payload": {"url": "https://example.com/photo"}}]}}}

            for uid in (11, 22):
                max_queue.enqueue(spaces.stamp(material(uid)))
            worker = max_queue.Worker(root_max)
            ops.set_setting("paused", "1")  # stale pause flags must not block any workspace
            with spaces.enter(a):
                ops.set_setting("paused", "1")
            assert await worker.process_once()
            assert await worker.process_once()
            assert root.db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0
            for space in (a, b):
                with spaces.enter(space):
                    assert core.get(1)["status"] == "pending"
                    assert str(core.PHOTOS).startswith(str(space.folder))
                    assert core.db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1
                    assert space.name == post_footer.config()["text"]
                    assert "ТОЛЬКО_ПРАВИЛО_" + ("Б" if space is a else "А") not in core.build_system()
                assert not list((space.folder / "inbox").glob("*"))
            assert any("ТОЛЬКО_ПРАВИЛО_А" in call["system"] for call in llm.calls)
            assert any("ТОЛЬКО_ПРАВИЛО_Б" in call["system"] for call in llm.calls)

            async def callback(uid, sid, action):
                await root_max.handle({"update_type": "message_callback", "callback": {"callback_id": "c", "user": {"user_id": uid},
                    "payload": f"w:{sid}:d:{action}:1:1"}})

            await callback(11, bid, "okyes")  # чужие кнопки с совпадающим ID поста
            assert not publications
            with spaces.enter(b):
                assert core.get(1)["status"] == "pending"

            await callback(11, aid, "ok")
            with spaces.enter(a):
                confirmation = core.ADAPTERS["max"]._confirmations()[("max", "11")][0]
            # A valid prompt from A cannot authorize publication of the same draft ID in B.
            await root_max.handle({"update_type": "message_callback", "callback": {"callback_id": "foreign-confirm",
                "user": {"user_id": 11}, "payload": f"w:{bid}:{confirmation}"}})
            assert not publications
            await root_max.handle({"update_type": "message_callback", "callback": {"callback_id": "own-confirm",
                "user": {"user_id": 11}, "payload": f"w:{aid}:{confirmation}"}})
            assert publications[0][:2] == (aid, "-201")
            destinations = [(sid, kw.get("params", {}).get("chat_id")) for sid, method, path, kw in calls if method == "POST" and path == "/messages" and "chat_id" in kw.get("params", {})]
            assert destinations == [(aid, -101)]
            with spaces.enter(a):
                assert core.get(1)["status"] == "published" and not core.get(1)["post"]
            with spaces.enter(b):
                assert core.get(1)["status"] == "pending" and core.get(1)["post"]
                ad = core.ADAPTERS["max"]
                assert "главному" in (await core.on_menu(ad, "22", "m:upd"))[0]
                assert "главный" in (await core.on_menu(ad, "22", "m:spaces"))[0]

            # Один и тот же пользователь может быть проверяющим в двух пространствах:
            # ожидание комментария и правил при этом хранится раздельно.
            with spaces.enter(a):
                core.WAITING[("max", "33")] = 7
                core.WAITING_RULE.add(("max", "33"))
            with spaces.enter(b):
                assert ("max", "33") not in core.WAITING
                assert ("max", "33") not in core.WAITING_RULE
                core.WAITING[("max", "33")] = 8
            with spaces.enter(a):
                assert core.WAITING[("max", "33")] == 7

            async def submit_in(space, uid, text):
                with spaces.enter(space):
                    return await core.submit(core.ADAPTERS["max"], uid, uid, text, png)

            results = await asyncio.gather(submit_in(a, "11", "Параллельно А"), submit_in(b, "22", "Параллельно Б"))
            assert results == [2, 2]
            with spaces.enter(a):
                assert core.get(2)["source_text"] == "Параллельно А"

            # Общая очередь действительно обрабатывает две команды одновременно,
            # сохраняя ContextVar, базу, каталог и получателей каждого материала.
            downloads_started = set()
            both_downloading = asyncio.Event()

            async def simultaneous_download(adapter, *args):
                downloads_started.add(spaces.active_id())
                if len(downloads_started) == 2:
                    both_downloading.set()
                await asyncio.wait_for(both_downloading.wait(), 2)
                return png

            monkeypatch.setattr(max_.Max, "_download", simultaneous_download)
            for uid in (11, 22):
                item = material(uid)
                item["message"]["body"]["mid"] = "parallel-inbox"
                max_queue.enqueue(spaces.stamp(item))
            assert await asyncio.gather(worker.process_once(), worker.process_once()) == [True, True]
            assert downloads_started == {aid, bid}
            for space in (a, b):
                with spaces.enter(space):
                    assert core.get(3)["source_text"] == "Описание работы"
                    assert core.get(3)["photo"].startswith(str(space.folder))
            monkeypatch.setattr(max_.Max, "_download", download)

            with spaces.enter(b):
                assert core.get(2)["source_text"] == "Параллельно Б"
                ad = core.ADAPTERS["max"]
                await core.on_menu(ad, "22", "m:space_member")
                await core.on_text(ad, "22", "33\nпроверяющий")
                assert "33" in ad.allowed and "33" not in ad.approvers
                # A migrated reviewer cannot edit someone else's post, even with matching draft IDs.
                assert "нет прав" in (await core.on_action(ad, "33", "edit", 2, 1))[1]
                assert not core.can_manage_draft(root_max, "1", core.get(2))
                core._add_person(ad, "33", "Администратор команды Б", "adm")
            with spaces.enter(a):
                assert "33" not in core.ADAPTERS["max"].approvers
                core._add_person(core.ADAPTERS["max"], "33", "Участник двух команд", "member")

            # Workspace switch and immediately following media are accepted in MAX order.
            # Even after a restart between receipt and processing, the callback must run
            # before material routing and the photo must land in the newly selected space.
            root.db.execute("INSERT OR REPLACE INTO workspace_routes VALUES(?,?)", ("33", aid))
            root.db.commit()
            rapid_callback = {"update_type": "message_callback", "callback": {
                "callback_id": "rapid-switch", "user": {"user_id": 33}, "payload": f"w:{bid}:m:menu"}}
            rapid_material = material(33)
            rapid_material["message"]["body"]["mid"] = "rapid-photo"
            await root_max._handle_limited(rapid_callback)
            await root_max._handle_limited(rapid_material)
            queued = root.db.execute("""SELECT id,state,payload FROM max_actions
                                        WHERE state='pending' ORDER BY id""").fetchall()
            assert len(queued) >= 2
            assert "rapid-switch" in queued[-2]["payload"] and "rapid-photo" in queued[-1]["payload"]
            root.db.execute("UPDATE max_actions SET state='processing' WHERE id=?", (queued[-2]["id"],))
            root.db.commit()
            max_actions.recover()
            actions = max_actions.Worker(root_max)
            ordered = root.db.execute("SELECT * FROM max_actions WHERE state='pending' ORDER BY id").fetchall()
            callback_row = next(row for row in ordered if "rapid-switch" in row["payload"])
            material_row = next(row for row in ordered if "rapid-photo" in row["payload"])
            await actions.process(callback_row)
            assert spaces.for_user("33") == bid

            original_enqueue = max_queue.enqueue

            def fail_handoff(_update):
                raise RuntimeError("temporary inbox failure")

            root.db.execute("UPDATE max_actions SET state='processing' WHERE id=?", (material_row["id"],))
            root.db.commit()
            await actions.process(dict(material_row))
            frozen = root.db.execute("SELECT * FROM max_actions WHERE id=?", (material_row["id"],)).fetchone()
            assert __import__("json").loads(frozen["payload"])["message"]["_space"] == bid
            assert frozen["state"] == "handoff" and frozen["space_id"] == bid

            monkeypatch.setattr(max_queue, "enqueue", fail_handoff)
            root.db.execute("UPDATE max_actions SET state='handoff_processing' WHERE id=?", (material_row["id"],))
            root.db.commit()
            await actions.process_handoff(dict(frozen))
            waiting = root.db.execute("SELECT * FROM max_actions WHERE id=?", (material_row["id"],)).fetchone()
            assert waiting["state"] == "handoff" and waiting["next_try"] > 0

            # A later real callback may switch the user back to A while B's material
            # is waiting for handoff. It must not be blocked and must not re-route the photo.
            switch_back = {"update_type": "message_callback", "callback": {
                "callback_id": "rapid-switch-back", "user": {"user_id": 33}, "payload": f"w:{aid}:m:menu"}}
            await root_max._handle_limited(switch_back)
            back_row = root.db.execute(
                "SELECT * FROM max_actions WHERE state='pending' AND payload LIKE '%rapid-switch-back%'").fetchone()
            root.db.execute("UPDATE max_actions SET state='processing' WHERE id=?", (back_row["id"],))
            root.db.commit()
            await actions.process(dict(back_row))
            assert spaces.for_user("33") == aid

            root.db.execute("UPDATE max_actions SET next_try=0,state='handoff_processing' WHERE id=?", (material_row["id"],))
            root.db.commit()
            monkeypatch.setattr(max_queue, "enqueue", original_enqueue)
            await actions.process_handoff(dict(root.db.execute("SELECT * FROM max_actions WHERE id=?", (material_row["id"],)).fetchone()))
            routed = root.db.execute("SELECT space_id FROM max_inbox WHERE payload LIKE '%rapid-photo%'").fetchone()
            assert routed and routed["space_id"] == bid

            await root_max.handle({"update_type": "message_callback", "callback": {"callback_id": "edit", "user": {"user_id": 33},
                                  "payload": f"w:{bid}:d:edit:2:1"}})
            reply = {"update_type": "message_created", "message": {"sender": {"user_id": 33}, "body": {"text": "Исправленный текст {bot}"}}}
            await root_max.handle(spaces.stamp(reply))
            with spaces.enter(b):
                assert core.get(2)["post"] == "Исправленный текст {bot}"
            with spaces.enter(a):
                assert core.get(2)["post"] != "Исправленный текст {bot}"
            with spaces.enter(b):
                core._add_person(core.ADAPTERS["max"], "11", "Администратор другой команды", "adm")
            await root_max.handle({"update_type": "message_callback", "callback": {"callback_id": "edit-owner", "user": {"user_id": 11},
                                  "payload": f"w:{bid}:d:edit:2:2"}})
            assert spaces.for_user("11") == aid  # последнее нажатие не меняет пространство по умолчанию
            text_reply = {"update_type": "message_created", "message": {"sender": {"user_id": 11}, "body": {"text": "Ответ на открытую правку {bot}"}}}
            assert spaces.stamp(text_reply)["message"]["_space"] == bid
            assert spaces.stamp(material(11))["message"]["_space"] == aid
            menu_reply = {"update_type": "message_created", "message": {"sender": {"user_id": 11}, "body": {"text": "/menu"}}}
            assert spaces.stamp(menu_reply)["message"]["_space"] == aid
            assert spaces.stamp(text_reply)["message"]["_space"] == aid

            # Polling принимает кнопку и ответ раньше, чем фоновый обработчик
            # успевает открыть правку в другой команде владельца.
            await root_max._handle_limited({"update_type": "message_callback", "callback": {
                "callback_id": "queued-edit", "user": {"user_id": 11}, "payload": f"w:{bid}:d:edit:2:2"}})
            await root_max._handle_limited(text_reply)
            actions = max_actions.Worker(root_max)
            for row in root.db.execute("SELECT * FROM max_actions WHERE state='pending' ORDER BY id").fetchall():
                await actions.process(row)
            with spaces.enter(b):
                assert core.get(2)["post"] == "Ответ на открытую правку {bot}"
                core.WAITING_EDIT[("max", "11")] = (2, 3)
            # /menu в общей группе не отменяет приватный ввод.
            group_menu = {"update_type": "message_created", "message": {"sender": {"user_id": 11},
                "recipient": {"chat_type": "chat"}, "body": {"text": "/menu"}}}
            assert "_space" not in spaces.stamp(group_menu)["message"]
            with spaces.enter(b):
                assert ("max", "11") in core.WAITING_EDIT
                core.WAITING_EDIT.clear()

            # Истёкшие материалы уведомляют только свою команду.
            root.db.execute("INSERT INTO max_inbox(event_key,state,created,space_id) VALUES('expired-a','pending',?,?)", (time.time() - 8 * 86400, aid))
            root.db.commit()
            calls.clear()
            await worker.process_once()
            expiry_notices = root.db.execute(
                """SELECT kind,uid,space_id,state,text FROM max_handoff_notices
                   WHERE event_key='expired-a' ORDER BY kind,uid""").fetchall()
            assert [(r["kind"], r["uid"], r["space_id"], r["state"]) for r in expiry_notices] == [
                ("staff", "11", aid, "pending")]
            assert "Истёк срок обработки" in expiry_notices[0]["text"]
            assert not root.db.execute(
                "SELECT 1 FROM max_handoff_notices WHERE event_key='expired-a' AND uid='22'").fetchone()
            assert not [x for x in calls
                        if x[1] == "POST" and x[2] == "/messages"
                        and "Истёк срок обработки" in x[3].get("json", {}).get("text", "")]

            root.db.execute("INSERT INTO max_inbox(event_key,state,error,space_id) VALUES('job-a','failed','ОШИБКА_А',?)", (aid,))
            root.db.execute("INSERT INTO max_inbox(event_key,state,error,space_id) VALUES('job-b','failed','ОШИБКА_Б',?)", (bid,))
            root.db.commit()
            with spaces.enter(a):
                text, _ = max_queue.view()
                assert "ОШИБКА_А" in text and "ОШИБКА_Б" not in text
                await core.on_menu(core.ADAPTERS["max"], "11", "m:inretry:job-b")
            assert root.db.execute("SELECT state FROM max_inbox WHERE event_key='job-b'").fetchone()[0] == "failed"

            await core.on_menu(root_max, "1", "m:space_add")
            await core.on_text(root_max, "1", "Компания В\n44\n-103\n-203")
            assert root.db.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 3
            await core.on_menu(root_max, "1", "m:space_add")
            await core.on_text(root_max, "1", "Дубль\n55\n-103\n-204")
            assert root.db.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 3
            spaces.WAITING_CREATE.clear()
            spaces.WAITING_MEMBER.clear()

            # Даже главный администратор с собственным пространством возвращается
            # в него по умолчанию после /admin, кроме ожидаемого ввода настроек.
            root.db.execute("INSERT INTO workspaces(name,owner,max_channel,tg_channel) VALUES('Админская команда','1','-104','-204')")
            own = root.db.execute("SELECT last_insert_rowid()").fetchone()[0]
            root.db.commit()
            spaces.get(own)
            admin_event = {"update_type": "message_created", "message": {"sender": {"user_id": 1}, "body": {"text": "/admin"}}}
            assert spaces.stamp(admin_event)["message"]["_space"] == 0
            assert spaces.for_user("1") == own
            assert spaces.stamp(material(1))["message"]["_space"] == own
            await core.on_menu(root_max, "1", "m:footer_text")
            admin_input = {"update_type": "message_created", "message": {"sender": {"user_id": 1}, "body": {"text": "Подпись для основного пространства"}}}
            assert spaces.stamp(admin_input)["message"]["_space"] == 0
            assert spaces.stamp(material(1))["message"]["_space"] == own
            post_footer.WAITING.clear()

            await root_max.handle({"update_type": "message_callback", "callback": {"callback_id": "before-restart",
                "user": {"user_id": 11}, "payload": f"w:{bid}:d:edit:2:3"}})

            # Повторное открытие использует сохранённые базы и настройки.
            await spaces.close()
            reopened_a, reopened_b = spaces.get(aid), spaces.get(bid)
            with spaces.enter(reopened_a):
                assert core.get(1)["status"] == "published" and core.get(2)["source_text"] == "Параллельно А"
            with spaces.enter(reopened_b):
                assert core.get(1)["status"] == "pending" and "ТОЛЬКО_ПРАВИЛО_Б" in core.build_system()
                assert core.WAITING_EDIT[("max", "11")] == (2, 3)
            resumed = {"update_type": "message_created", "message": {"sender": {"user_id": 11},
                "body": {"text": "Правка после перезапуска {bot}"}}}
            assert spaces.stamp(resumed)["message"]["_space"] == bid
            await root_max.handle(spaces.stamp(resumed))
            with spaces.enter(reopened_b):
                assert core.get(2)["post"] == "Правка после перезапуска {bot}"
                assert ("max", "11") not in core.WAITING_EDIT
        finally:
            await spaces.close()
            for name, original in root.memories.items():
                if name != "footer_waiting":
                    setattr(core, name, original)
            post_footer.WAITING = root.memories["footer_waiting"]
            core.WAITING_RULE = waiting_rule
            core.db = root.db
            core.DATA, core.PHOTOS, core.VIDEOS = root.folder, root.folder / "photos", root.folder / "videos"
            root.db.execute("DELETE FROM workspaces")
            root.db.execute("DELETE FROM workspace_routes")
            root.db.commit()
            spaces.INSTALLED, spaces.ROOT = False, None
            spaces.WAITING_CREATE.clear()
            shutil.rmtree(root.folder / "spaces", ignore_errors=True)
            await root_max.http.aclose()
            await root_tg.bot.session.close()
    run(scenario())
