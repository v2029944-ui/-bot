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
schema.add_columns(core.db, "max_actions", ("created REAL DEFAULT 0", "attempts INTEGER DEFAULT 0", "next_try REAL DEFAULT 0", "space_id INTEGER DEFAULT 0"))
core.db.execute("UPDATE max_actions SET created=? WHERE created=0", (time.time(),))
core.db.execute("CREATE INDEX IF NOT EXISTS max_actions_pending ON max_actions(state,uid,id)")
core.db.execute("""CREATE TABLE IF NOT EXISTS max_handoff_failures (
    event_key TEXT PRIMARY KEY, uid TEXT, source_mid TEXT, space_id INTEGER DEFAULT 0,
    created REAL, failed_at REAL, attempts INTEGER, reason TEXT)""")
core.db.execute("""CREATE TABLE IF NOT EXISTS max_handoff_notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT, kind TEXT, uid TEXT,
    space_id INTEGER DEFAULT 0, text TEXT, state TEXT DEFAULT 'pending',
    attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0, created REAL,
    last_error TEXT DEFAULT '', UNIQUE(event_key,kind,uid))""")
core.db.execute("CREATE INDEX IF NOT EXISTS max_handoff_notice_due ON max_handoff_notices(state,next_try,id)")
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
    """Recover ordered commands and independent handoffs without re-routing frozen materials."""
    core.db.execute("UPDATE max_actions SET state='pending' WHERE state='processing'")
    core.db.execute("UPDATE max_actions SET state='handoff' WHERE state='handoff_processing'")
    core.db.execute("UPDATE max_handoff_notices SET state='pending' WHERE state='sending'")
    core.db.commit()


class Worker:
    name = "max_actions"

    def __init__(self, adapter):
        self.adapter = adapter
        self.active = {}
        self.handoffs = {}
        self.notices = {}
        self.clean_task = None
        self.clean_requested = True
        self.last_cleanup = 0.0

    def finished(self, task):
        self.clean_requested = True
        if not task.cancelled():
            task.exception()

    async def process(self, row):
        try:
            update = json.loads(row["payload"])
            material_transfer = bool(update.pop("_material_to_inbox", False))
            route_now = bool(update.pop("_route_on_process", False))
            if route_now:
                update = spaces.stamp(update)
            if material_transfer:
                # Ordered phase ends here. Persist the selected workspace and remove
                # the routing flag atomically with state='handoff'. From this commit
                # onward later commands of the same user may proceed independently.
                message = update.get("message") or {}
                space_id = int(message.get("_space") or 0)
                payload = json.dumps({**update, "_material_to_inbox": True},
                                     ensure_ascii=False, sort_keys=True)
                core.db.execute("""UPDATE max_actions
                                   SET payload=?,state='handoff',space_id=?,attempts=0,next_try=0
                                   WHERE id=? AND state='processing'""",
                                (payload, space_id, row["id"]))
                core.db.commit()
                return
            token = command_context.EVENT.set(row["event_key"])
            try:
                await self.adapter.handle(update)
            finally:
                command_context.EVENT.reset(token)
        except asyncio.CancelledError:
            core.db.execute("UPDATE max_actions SET state='pending' WHERE id=? AND state='processing'", (row["id"],))
            core.db.commit()
            raise
        except Exception:
            # Unknown side effects of ordinary commands are never replayed automatically.
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

    async def process_handoff(self, row):
        try:
            update = json.loads(row["payload"])
            update.pop("_material_to_inbox", None)
            import max_queue
            max_queue.enqueue(update)
        except asyncio.CancelledError:
            core.db.execute("UPDATE max_actions SET state='handoff' WHERE id=? AND state='handoff_processing'", (row["id"],))
            core.db.commit()
            raise
        except Exception as exc:
            now = time.time()
            attempt = int(row["attempts"] or 0) + 1
            created = float(row["created"] or now)
            if now - created >= 7 * 86400:
                self._terminal_handoff(row, update if 'update' in locals() else {}, attempt, now, core.err(exc))
                return
            delay = min(30 * 2 ** min(attempt - 1, 7), 3600)
            core.db.execute("""UPDATE max_actions SET state='handoff',attempts=?,next_try=?
                               WHERE id=? AND state='handoff_processing'""",
                            (attempt, now + delay, row["id"]))
            core.db.commit()
            logging.getLogger("max_actions").exception(
                "handoff материала %s не удался; повтор через %.0f с", row["id"], delay)
            return
        core.db.execute("UPDATE max_actions SET state='done',payload='',next_try=0 WHERE id=? AND state='handoff_processing'",
                        (row["id"],))
        core.db.commit()

    def _terminal_handoff(self, row, update, attempt, now, reason):
        message = update.get("message") or {}
        mid = ((message.get("body") or {}).get("mid") or "")
        sid = int(row["space_id"] or message.get("_space") or 0)
        safe_reason = (reason or "Не удалось передать материал в очередь")[:240]
        core.db.execute("""INSERT OR REPLACE INTO max_handoff_failures
                           (event_key,uid,source_mid,space_id,created,failed_at,attempts,reason)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (row["event_key"], row["uid"], str(mid), sid, row["created"], now, attempt, safe_reason))
        core.db.execute("""UPDATE max_actions SET state='failed',payload='',attempts=?,next_try=0,space_id=?
                           WHERE id=?""", (attempt, sid, row["id"]))
        user_text = "⚠️ Материал не удалось поставить в очередь обработки. Отправьте его заново."
        staff_text = (f"⚠️ Материал пользователя {row['uid']} не удалось передать в очередь за 7 дней. "
                      f"mid={mid or 'неизвестен'}, попыток={attempt}. Попросите отправить материал заново.")
        self._queue_notice(row["event_key"], "user", row["uid"], sid, user_text, now)
        with spaces.enter(spaces.get(sid) if spaces.INSTALLED and sid else None):
            for admin_uid in core.staff(core.ADAPTERS["max"]):
                self._queue_notice(row["event_key"], "staff", admin_uid, sid, staff_text, now)
        core.db.commit()
        logging.getLogger("max_actions").error("terminal handoff failure event=%s space=%s", row["event_key"][:8], sid)

    def _queue_notice(self, event_key, kind, uid, sid, text, now=None):
        now = time.time() if now is None else now
        core.db.execute("""INSERT OR IGNORE INTO max_handoff_notices
                           (event_key,kind,uid,space_id,text,created)
                           VALUES(?,?,?,?,?,?)""",
                        (event_key, kind, str(uid or ""), int(sid or 0), text[:500], now))

    async def process_notice(self, row):
        try:
            with spaces.enter(spaces.get(row["space_id"]) if spaces.INSTALLED and row["space_id"] else None):
                adapter = core.ADAPTERS["max"]
                await adapter.notify(row["uid"], row["text"])
        except asyncio.CancelledError:
            core.db.execute("UPDATE max_handoff_notices SET state='pending' WHERE id=? AND state='sending'", (row["id"],))
            core.db.commit()
            raise
        except Exception as exc:
            self._notice_failed(row, exc)
            return
        core.db.execute("UPDATE max_handoff_notices SET state='done',last_error='' WHERE id=? AND state='sending'", (row["id"],))
        core.db.commit()

    def _notice_failed(self, row, exc):
        import httpx
        attempts = int(row["attempts"] or 0) + 1
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        safe_network = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
        if status == 429 or safe_network:
            state, delay = "pending", min(30 * 2 ** min(attempts - 1, 7), 3600)
        elif status is not None and 400 <= status < 500 and status != 408:
            state, delay = "failed", 0
        else:
            # The request may have reached MAX (408/5xx/read-write failure). Do not
            # automatically retry an ambiguous notification and risk duplicates.
            state, delay = "unknown", 0
        core.db.execute("""UPDATE max_handoff_notices SET state=?,attempts=?,next_try=?,last_error=?
                           WHERE id=?""",
                        (state, attempts, time.time() + delay if delay else 0, core.err(exc)[:240], row["id"]))
        core.db.commit()

    def schedule(self):
        self.active = {uid: task for uid, task in self.active.items() if not task.done()}
        rows = core.db.execute("""SELECT a.* FROM max_actions a JOIN (
            SELECT MIN(id) AS id FROM max_actions WHERE state='pending' GROUP BY uid
            ) first ON first.id=a.id ORDER BY a.id LIMIT 8""").fetchall()
        for row in rows:
            if len(self.active) >= 4 or core.SHUTTING_DOWN:
                break
            if row["uid"] in self.active:
                continue
            changed = core.db.execute("UPDATE max_actions SET state='processing' WHERE id=? AND state='pending'", (row["id"],)).rowcount
            core.db.commit()
            if not changed:
                continue
            self.active[row["uid"]] = asyncio.create_task(core._guard(self.process(dict(row))))
            self.active[row["uid"]].add_done_callback(self.finished)

    def schedule_handoffs(self):
        self.handoffs = {rid: task for rid, task in self.handoffs.items() if not task.done()}
        slots = max(0, 2 - len(self.handoffs))
        if not slots or core.SHUTTING_DOWN:
            return
        now = time.time()
        rows = core.db.execute("""SELECT * FROM max_actions
                                  WHERE state='handoff' AND next_try<=?
                                  ORDER BY id LIMIT ?""", (now, slots)).fetchall()
        for row in rows:
            changed = core.db.execute("""UPDATE max_actions SET state='handoff_processing'
                                         WHERE id=? AND state='handoff'""", (row["id"],)).rowcount
            core.db.commit()
            if not changed:
                continue
            task = asyncio.create_task(core._guard(self.process_handoff(dict(row))))
            self.handoffs[row["id"]] = task
            task.add_done_callback(self.finished)

    def schedule_notices(self):
        self.notices = {nid: task for nid, task in self.notices.items() if not task.done()}
        slots = max(0, 2 - len(self.notices))
        if not slots or core.SHUTTING_DOWN:
            return
        rows = core.db.execute("""SELECT * FROM max_handoff_notices
                                  WHERE state='pending' AND next_try<=?
                                  ORDER BY id LIMIT ?""", (time.time(), slots)).fetchall()
        for row in rows:
            changed = core.db.execute("""UPDATE max_handoff_notices SET state='sending'
                                         WHERE id=? AND state='pending'""", (row["id"],)).rowcount
            core.db.commit()
            if not changed:
                continue
            task = asyncio.create_task(core._guard(self.process_notice(dict(row))))
            self.notices[row["id"]] = task
            task.add_done_callback(self.finished)

    async def run(self):
        recover()
        while not core.SHUTTING_DOWN:
            self.schedule()
            self.schedule_handoffs()
            self.schedule_notices()
            if (self.clean_requested or time.monotonic() - self.last_cleanup >= 30) and (self.clean_task is None or self.clean_task.done()):
                self.clean_requested = False
                self.last_cleanup = time.monotonic()
                self.clean_task = asyncio.create_task(core._guard(self.adapter.cleanup_stale_buttons()))
                self.clean_task.add_done_callback(lambda t: t.cancelled() or t.exception())
            await asyncio.sleep(1)
