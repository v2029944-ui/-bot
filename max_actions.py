"""Постоянная очередь команд MAX: до четырёх пользователей, по одной команде на пользователя."""
import asyncio
import hashlib
import json
import logging
import time

import command_context
import core
import schema
import spaces

core.db.execute("""CREATE TABLE IF NOT EXISTS max_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE, uid TEXT,
    payload TEXT, state TEXT DEFAULT 'pending', created REAL DEFAULT 0,
    attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0)""")
schema.add_columns(core.db, "max_actions", ("created REAL DEFAULT 0", "attempts INTEGER DEFAULT 0", "next_try REAL DEFAULT 0"))
core.db.execute("UPDATE max_actions SET created=? WHERE created=0", (time.time(),))
core.db.execute("CREATE INDEX IF NOT EXISTS max_actions_pending ON max_actions(state,uid,id)")
core.db.commit()


def enqueue(update):
    if update.get("update_type") == "message_callback":
        # Для нажатия нужен только ID исходного сообщения, а не копия поста/медиа.
        mid = ((update.get("message") or {}).get("body") or {}).get("mid")
        update = {"update_type": "message_callback", "callback": update["callback"],
                  "message": {"body": {"mid": mid}} if mid else None}
    payload = json.dumps(update, ensure_ascii=False, sort_keys=True)
    callback = update.get("callback") or {}
    message = update.get("message") or {}
    uid = str((callback.get("user") or message.get("sender") or update.get("user") or {}).get("user_id", ""))
    mid = (message.get("body") or {}).get("mid")
    identity = f"message:{uid}:{mid}" if update.get("update_type") == "message_created" and mid else payload
    key = callback.get("callback_id") or hashlib.sha256(identity.encode()).hexdigest()
    core.db.execute("INSERT OR IGNORE INTO max_actions(event_key,uid,payload,created) VALUES(?,?,?,?)", (key, uid, payload, time.time()))
    core.db.commit()


def recover():
    """A process crash cannot leave per-user command ordering stuck in processing."""
    core.db.execute("UPDATE max_actions SET state='pending' WHERE state='processing'")
    core.db.commit()


class Worker:
    name = "max_actions"

    def __init__(self, adapter):
        self.adapter = adapter
        self.active = {}
        self.clean_task = None
        self.clean_requested = True
        self.last_cleanup = 0.0

    def finished(self, task):
        self.clean_requested = True
        if not task.cancelled():
            task.exception()

    async def process(self, row):
        material_transfer = False
        try:
            update = json.loads(row["payload"])
            material_transfer = bool(update.pop("_material_to_inbox", False))
            if update.pop("_route_on_process", False):
                update = spaces.stamp(update)
            if material_transfer:
                import max_queue
                max_queue.enqueue(update)
            else:
                token = command_context.EVENT.set(row["event_key"])
                try:
                    await self.adapter.handle(update)
                finally:
                    command_context.EVENT.reset(token)
        except asyncio.CancelledError:
            core.db.execute("UPDATE max_actions SET state='pending' WHERE id=?", (row["id"],))
            core.db.commit()
            raise
        except Exception:
            if material_transfer:
                # Transfer to max_inbox is idempotent and has no external side effect.
                # Keep the original payload until the durable handoff succeeds, but
                # back off and respect the same 7-day retention as max_inbox.
                now = time.time()
                attempt = int(row["attempts"] or 0) + 1
                if now - float(row["created"] or now) >= 7 * 86400:
                    core.db.execute("UPDATE max_actions SET state='failed',payload='',attempts=? WHERE id=?",
                                    (attempt, row["id"]))
                    core.db.commit()
                    logging.getLogger("max_actions").error(
                        "материал %s не удалось перенести в очередь за 7 дней; запись закрыта", row["id"])
                    try:
                        await self.adapter.notify(
                            row["uid"], "⚠️ Материал не удалось поставить в очередь обработки. Отправьте его заново.")
                    except Exception:
                        logging.getLogger("max_actions").warning("не удалось сообщить об истёкшем материале")
                    return
                delay = min(30 * 2 ** min(attempt - 1, 7), 3600)
                core.db.execute("""UPDATE max_actions SET state='pending',attempts=?,next_try=?
                                   WHERE id=?""", (attempt, now + delay, row["id"]))
                core.db.commit()
                logging.getLogger("max_actions").exception(
                    "не удалось перенести материал %s в постоянную очередь; повтор через %.0f с", row["id"], delay)
                return
            # Не повторяем неизвестный побочный эффект команды автоматически.
            core.db.execute("UPDATE max_actions SET state='failed',payload='' WHERE id=?", (row["id"],))
            core.db.commit()
            logging.getLogger("max_actions").exception("ошибка фоновой команды %s", row["id"])
            try:
                await self.adapter.notify(row["uid"], "⚠️ Не удалось выполнить действие. Проверьте результат и повторите его при необходимости.")
            except Exception:
                logging.getLogger("max_actions").warning("не удалось сообщить об ошибке команды")
        else:
            core.db.execute("UPDATE max_actions SET state='done',payload='' WHERE id=?", (row["id"],))
            core.db.commit()

    def schedule(self):
        self.active = {uid: task for uid, task in self.active.items() if not task.done()}
        # Только первая команда каждого пользователя; тела всей очереди не нужны.
        now = time.time()
        rows = core.db.execute("""SELECT a.* FROM max_actions a JOIN (
            SELECT MIN(id) AS id FROM max_actions
            WHERE state='pending' AND next_try<=? GROUP BY uid
            ) first ON first.id=a.id ORDER BY a.id LIMIT 8""", (now,)).fetchall()
        for row in rows:
            if len(self.active) >= 4 or core.SHUTTING_DOWN:
                break
            if row["uid"] in self.active:
                continue
            core.db.execute("UPDATE max_actions SET state='processing' WHERE id=?", (row["id"],))
            core.db.commit()
            self.active[row["uid"]] = asyncio.create_task(core._guard(self.process(dict(row))))
            self.active[row["uid"]].add_done_callback(self.finished)

    async def run(self):
        recover()
        while not core.SHUTTING_DOWN:
            self.schedule()
            if (self.clean_requested or time.monotonic() - self.last_cleanup >= 30) and (self.clean_task is None or self.clean_task.done()):
                self.clean_requested = False
                self.last_cleanup = time.monotonic()
                self.clean_task = asyncio.create_task(core._guard(self.adapter.cleanup_stale_buttons()))
                self.clean_task.add_done_callback(lambda t: t.cancelled() or t.exception())
            await asyncio.sleep(1)
