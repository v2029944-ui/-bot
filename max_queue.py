"""Очередь материалов: ограниченный параллелизм между пространствами, порядок внутри каждого."""
import asyncio
import hashlib
import json
import logging
import time
from collections import Counter

import core
import ops
import spaces

log = logging.getLogger("max_queue")


def cache_dir(key, space_id=None):
    base = spaces.root().folder / "spaces" / str(space_id) if spaces.INSTALLED and space_id else core.DATA
    return base / "inbox" / hashlib.sha256(key.encode()).hexdigest()


async def download(adapter, attachment, limit, kind, key, index):
    if not key:
        return await adapter._download(attachment, limit, kind)
    folder = cache_dir(key)
    path = folder / str(index)
    if path.exists():
        if path.stat().st_size > limit:
            raise core.BadImage("Превышен допустимый размер материала")
        return await asyncio.to_thread(path.read_bytes)
    data = await adapter._download(attachment, limit, kind)
    folder.mkdir(parents=True, exist_ok=True)
    temp = folder / f"{index}.part"
    await asyncio.to_thread(temp.write_bytes, data)
    temp.replace(path)
    return data


def clear_cache(key, space_id=None):
    import shutil
    shutil.rmtree(cache_dir(key, space_id), ignore_errors=True)


def is_material(update):
    message = update.get("message") or {}
    return (update.get("update_type") == "message_created"
            and message.get("recipient", {}).get("chat_type", "dialog") == "dialog"
            and any(a.get("type") in ("image", "video")
                    for a in (message.get("body", {}).get("attachments") or [])))


def enqueue(update):
    payload = json.dumps(update, ensure_ascii=False, sort_keys=True)
    message = update["message"]
    mid = message.get("body", {}).get("mid")
    identity = f"{message.get('sender', {}).get('user_id')}:{mid}" if mid else payload
    key = hashlib.sha256(identity.encode()).hexdigest()
    core.db.execute("INSERT OR IGNORE INTO max_inbox(event_key,payload,created,space_id) VALUES(?,?,?,?)",
                    (key, payload, time.time(), message.get("_space", 0)))
    core.db.commit()  # только после этой записи polling может подтвердить marker
    return key


def recover():
    core.db.execute("UPDATE max_inbox SET state='pending' WHERE state='processing'")
    core.db.commit()


def view():
    queue = spaces.queue_db()
    clause = " WHERE space_id=?" if spaces.active_id() else ""
    args = (spaces.active_id(),) if spaces.active_id() else ()
    counts = dict(queue.execute("SELECT state,COUNT(*) FROM max_inbox" + clause + " GROUP BY state", args).fetchall())
    text = (f"Обработка материалов: в очереди {counts.get('pending', 0)}, "
            f"в работе {counts.get('processing', 0)}, с ошибкой {counts.get('failed', 0)}.")
    rows = []
    condition = " AND space_id=?" if args else ""
    for row in queue.execute("SELECT event_key,error FROM max_inbox WHERE state='failed'" + condition + " ORDER BY created LIMIT 5", args):
        key = row["event_key"]
        text += f"\n• {key[:8]}: {row['error'][:120]}"
        rows.append([(f"🔁 Повторить {key[:8]}", f"m:inretry:{key}")])
    return text, rows


class Worker:
    name = "max_queue"

    def __init__(self, adapter):
        self.adapter = adapter

    async def process_once(self, now=None):
        now = time.time() if now is None else now
        # Ссылки и подписи не храним бесконечно даже при недоступной нейросети.
        expired_keys = list(core.db.execute(
            "SELECT event_key,space_id FROM max_inbox WHERE state IN ('pending','failed') AND created<?", (now - 7 * 86400,)))
        expired = core.db.execute(
            "UPDATE max_inbox SET state='expired',payload='',error='Истёк срок обработки' "
            "WHERE state IN ('pending','failed') AND created<?", (now - 7 * 86400,)).rowcount
        core.db.commit()
        for key, sid in expired_keys:
            clear_cache(key, sid)
        if expired:
            for sid, count in Counter(sid for _, sid in expired_keys).items():
                with spaces.enter(spaces.get(sid) if spaces.INSTALLED else None):
                    adapter = core.ADAPTERS["max"] if spaces.INSTALLED else self.adapter
                    await core.alert_staff(adapter, f"⚠️ Истёк срок обработки {count} материалов в очереди. Попросите сотрудников отправить их заново.")
        if core.SHUTTING_DOWN or (not spaces.INSTALLED and ops.is_paused()):
            return False
        row = core.db.execute("SELECT a.* FROM max_inbox a WHERE a.state='pending' AND a.next_try<=? "
                              "AND NOT EXISTS (SELECT 1 FROM max_inbox busy WHERE busy.space_id=a.space_id AND busy.state='processing') "
                              "ORDER BY a.created LIMIT 1",
                              (now,)).fetchone()
        if not row:
            return False
        if spaces.INSTALLED:
            with spaces.enter(spaces.get(row["space_id"])):
                paused = ops.is_paused()
            if paused:
                core.db.execute("UPDATE max_inbox SET next_try=? WHERE event_key=?", (now + 60, row["event_key"]))
                core.db.commit()
                return True
        key, attempt = row["event_key"], row["attempts"] + 1
        changed = core.db.execute("UPDATE max_inbox SET state='processing',attempts=? WHERE event_key=? AND state='pending'",
                                  (attempt, key)).rowcount
        core.db.commit()
        if not changed:
            return False
        try:
            message = json.loads(row["payload"])["message"]
            if spaces.INSTALLED:
                message = {"_space": row["space_id"], **message}
            await self.adapter.on_message(message, source_key=key)
        except asyncio.CancelledError:
            core.db.execute("UPDATE max_inbox SET state='pending' WHERE event_key=? AND state='processing'", (key,))
            core.db.commit()
            raise
        except core.RateLimited:
            core.db.execute("UPDATE max_inbox SET state='pending',attempts=?,next_try=?,error=? WHERE event_key=? AND state='processing'",
                            (row["attempts"], now + 3600, "Лимит материалов: повтор через час", key))
            core.db.commit()
        except Exception as exc:
            state = "failed" if attempt >= 8 else "pending"
            changed = core.db.execute("UPDATE max_inbox SET state=?,next_try=?,error=? WHERE event_key=? AND state='processing'",
                                      (state, now + min(30 * 2 ** min(attempt - 1, 7), 3600), core.err(exc)[:300], key)).rowcount
            core.db.commit()
            if not changed:
                return True  # пост уже полностью опубликован; очищенную очередь не оживляем
            log.warning("материал %s: попытка %s не удалась: %s", key[:8], attempt, core.err(exc))
            if state == "failed":
                if spaces.INSTALLED and row["space_id"]:
                    with spaces.enter(spaces.get(row["space_id"])):
                        text, rows = view()
                        await core._tell_admins(core.ADAPTERS["max"], "⚠️ Материал не обработан после 8 попыток.\n" + text, rows)
                else:
                    text, rows = view()
                    if self.adapter.admins:
                        await core._tell_admins(self.adapter, "⚠️ Материал не обработан после 8 попыток.\n" + text, rows)
                    else:
                        await core.alert_staff(self.adapter, "⚠️ Материал не обработан после 8 попыток.\n" + text)
        else:
            clear_cache(key, row["space_id"])
            core.db.execute("UPDATE max_inbox SET state='done',payload='',error='' WHERE event_key=?", (key,))
            core.db.commit()
        return True

    async def run(self):
        recover()  # также после перезапуска фоновой задачи супервизором
        count = max(1, min(2, int(core.env("MATERIAL_WORKERS", "2"))))
        tasks = [asyncio.create_task(self.run_lane()) for _ in range(count)]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run_lane(self):
        while not core.SHUTTING_DOWN:
            if not await core._guard(self.process_once()):
                await asyncio.sleep(2)
