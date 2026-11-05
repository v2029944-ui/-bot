"""Постоянная очередь команд MAX: до четырёх пользователей, по одной команде на пользователя."""
import asyncio
import hashlib
import json
import logging
import time

import core

core.db.execute("""CREATE TABLE IF NOT EXISTS max_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE, uid TEXT,
    payload TEXT, state TEXT DEFAULT 'pending')""")
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
    key = callback.get("callback_id") or hashlib.sha256(payload.encode()).hexdigest()
    core.db.execute("INSERT OR IGNORE INTO max_actions(event_key,uid,payload) VALUES(?,?,?)", (key, uid, payload))
    core.db.commit()


class Worker:
    name = "max_actions"

    def __init__(self, adapter):
        self.adapter = adapter
        self.active = {}
        self.clean_task = None

    async def process(self, row):
        try:
            await self.adapter.handle(json.loads(row["payload"]))
        except asyncio.CancelledError:
            core.db.execute("UPDATE max_actions SET state='pending' WHERE id=?", (row["id"],))
            core.db.commit()
            raise
        else:
            core.db.execute("UPDATE max_actions SET state='done',payload='' WHERE id=?", (row["id"],))
            core.db.commit()

    def schedule(self):
        self.active = {uid: task for uid, task in self.active.items() if not task.done()}
        for row in core.db.execute("SELECT * FROM max_actions WHERE state='pending' ORDER BY id").fetchall():
            if len(self.active) >= 4 or core.SHUTTING_DOWN:
                break
            if row["uid"] in self.active:
                continue
            core.db.execute("UPDATE max_actions SET state='processing' WHERE id=?", (row["id"],))
            core.db.commit()
            self.active[row["uid"]] = asyncio.create_task(core._guard(self.process(dict(row))))
            self.active[row["uid"]].add_done_callback(lambda t: t.cancelled() or t.exception())

    async def run(self):
        core.db.execute("UPDATE max_actions SET state='pending' WHERE state='processing'")
        core.db.commit()
        while not core.SHUTTING_DOWN:
            self.schedule()
            if self.clean_task is None or self.clean_task.done():
                self.clean_task = asyncio.create_task(core._guard(self.adapter.cleanup_stale_buttons()))
                self.clean_task.add_done_callback(lambda t: t.cancelled() or t.exception())
            await asyncio.sleep(1)
