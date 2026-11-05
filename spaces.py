"""Изолированные рабочие пространства одного бота. Основная база остаётся пространством 0."""
import asyncio
from collections.abc import MutableMapping
from contextlib import contextmanager
import contextvars
import json
from pathlib import Path
import sqlite3
import time

CURRENT = contextvars.ContextVar("workspace", default=None)
INSTALLED = False
ROOT = None
OPEN = {}
WAITING_CREATE = set()
WAITING_MEMBER = set()


class Mapping(MutableMapping):
    def __init__(self, name, root):
        self.name, self.root = name, root

    def value(self):
        space = CURRENT.get()
        return space.memory[self.name] if space else self.root

    def __getitem__(self, key): return self.value()[key]
    def __setitem__(self, key, value): self.value()[key] = value
    def __delitem__(self, key): del self.value()[key]
    def __iter__(self): return iter(self.value())
    def __len__(self): return len(self.value())


class Set:
    def __init__(self, root): self.root = root
    def value(self): return CURRENT.get().waiting_rule if CURRENT.get() else self.root
    def __contains__(self, key): return key in self.value()
    def __len__(self): return len(self.value())
    def __iter__(self): return iter(self.value())
    def __getattr__(self, name): return getattr(self.value(), name)


class Database:
    def __getattr__(self, name):
        return getattr(CURRENT.get().db if CURRENT.get() else ROOT.db, name)


class Folder:
    def __init__(self, child=""): self.child = child
    def value(self):
        base = CURRENT.get().folder if CURRENT.get() else ROOT.folder
        return base / self.child if self.child else base
    def __truediv__(self, other): return self.value() / other
    def __fspath__(self): return str(self.value())
    def __str__(self): return str(self.value())
    def __getattr__(self, name): return getattr(self.value(), name)


def active_id():
    return CURRENT.get().id if CURRENT.get() else 0


@contextmanager
def enter(space):
    token = CURRENT.set(space)
    try:
        yield
    finally:
        CURRENT.reset(token)


def install():
    global INSTALLED, ROOT
    if INSTALLED:
        return
    import core
    import post_footer
    from types import SimpleNamespace
    ROOT = SimpleNamespace(db=core.db, folder=Path(core.DATA), adapters=core.ADAPTERS, memories={})
    ROOT.db.execute("""CREATE TABLE IF NOT EXISTS workspaces (
        id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,owner TEXT UNIQUE,max_channel TEXT,tg_channel TEXT)""")
    ROOT.db.execute("CREATE TABLE IF NOT EXISTS workspace_routes(uid TEXT PRIMARY KEY, sid INTEGER)")
    ROOT.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS workspace_max_group ON workspaces(max_channel)")
    ROOT.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS workspace_tg_group ON workspaces(tg_channel)")
    ROOT.db.commit()
    core.db = Database()
    core.DATA, core.PHOTOS, core.VIDEOS = Folder(), Folder("photos"), Folder("videos")
    for name in ("ADAPTERS", "WAITING", "WAITING_EDIT", "WAITING_REJECT", "_HITS", "_RL_ALERT", "_ALERT_LAST", "LLM_STATE"):
        original = getattr(core, name)
        ROOT.memories[name] = original
        setattr(core, name, Mapping(name, original))
    ROOT.memories["footer_waiting"] = post_footer.WAITING
    post_footer.WAITING = Mapping("footer_waiting", post_footer.WAITING)
    core.WAITING_RULE = Set(core.WAITING_RULE)
    ROOT.adapters["max"].workspace_id = 0
    INSTALLED = True
    for row in ROOT.db.execute("SELECT * FROM workspaces"):
        get(row["id"])
    core.refresh_people(ROOT.adapters["max"])


def queue_db():
    import core
    return ROOT.db if INSTALLED else core.db


def global_admin(uid):
    return INSTALLED and str(uid) in ROOT.adapters["max"].admins


def get(sid):
    if not sid:
        return None
    if sid in OPEN:
        return OPEN[sid]
    row = ROOT.db.execute("SELECT * FROM workspaces WHERE id=?", (sid,)).fetchone()
    if not row:
        raise ValueError("Пространство не найдено")
    from types import SimpleNamespace
    import core
    from max_ import Max
    from tg import Telegram
    folder = ROOT.folder / "spaces" / str(sid)
    folder.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(folder / "drafts.db", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    schema = ROOT.db.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' AND tbl_name NOT IN ('workspaces','workspace_routes') ORDER BY type DESC").fetchall()
    for obj in schema:
        if obj["type"] == "table":
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (obj["name"],)).fetchone():
                db.execute(obj["sql"])
            existing = {c["name"] for c in db.execute(f'PRAGMA table_info("{obj["name"]}")')}
            for col in ROOT.db.execute(f'PRAGMA table_info("{obj["name"]}")'):
                if col["name"] not in existing:
                    default = " DEFAULT " + col["dflt_value"] if col["dflt_value"] is not None else ""
                    db.execute(f'ALTER TABLE "{obj["name"]}" ADD COLUMN "{col["name"]}" {col["type"]}{default}')
    for obj in schema:
        if obj["type"] == "index" and not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (obj["name"],)).fetchone():
            db.execute(obj["sql"])
    db.commit()
    memory = {name: {} for name in ROOT.memories}
    memory["LLM_STATE"] = {"ok": 0., "err": 0., "msg": ""}
    space = SimpleNamespace(id=sid, name=row["name"], folder=folder, db=db, memory=memory, waiting_rule=set(), last_housekeeping=time.monotonic())
    memory["ADAPTERS"] = {}
    with enter(space):
        core.PHOTOS.mkdir(parents=True, exist_ok=True)
        core.VIDEOS.mkdir(parents=True, exist_ok=True)
        root_max = ROOT.adapters["max"]
        ad = Max(root_max.http.headers["Authorization"], row["max_channel"], [row["owner"]], [row["owner"]], [row["owner"]])
        ad.workspace_id = sid
        memory["ADAPTERS"]["max"] = ad
        import os
        if "tg" in ROOT.adapters:
            memory["ADAPTERS"]["tg"] = Telegram(os.environ["TG_TOKEN"], row["tg_channel"])
        core.startup()
    OPEN[sid] = space
    return space


def for_user(uid, continue_input=False):
    uid = str(uid)
    owner = ROOT.db.execute("SELECT id FROM workspaces WHERE owner=?", (uid,)).fetchone()
    selected = ROOT.db.execute("SELECT sid FROM workspace_routes WHERE uid=?", (str(uid),)).fetchone()
    if owner:
        if continue_input and selected:
            sid = selected["sid"]
            import core
            if sid == 0:
                memory, rules = ROOT.memories, core.WAITING_RULE.root
                allowed = uid in ROOT.adapters["max"].approvers
            elif sid in OPEN:
                memory, rules = OPEN[sid].memory, OPEN[sid].waiting_rule
                allowed = uid in memory["ADAPTERS"]["max"].allowed
            else:
                memory, rules, allowed = {}, set(), False
            key = ("max", uid)
            pending = any(key in memory.get(name, {}) for name in ("WAITING", "WAITING_EDIT", "WAITING_REJECT", "footer_waiting"))
            if allowed and (pending or key in rules or (sid, uid) in WAITING_MEMBER or sid == 0 and uid in WAITING_CREATE):
                return sid
        return owner["id"]
    if selected and selected["sid"] == 0 and str(uid) in ROOT.adapters["max"].approvers:
        return 0
    if selected and selected["sid"] in OPEN and str(uid) in OPEN[selected["sid"]].memory["ADAPTERS"]["max"].allowed:
        return selected["sid"]
    for sid, space in OPEN.items():
        if str(uid) in space.memory["ADAPTERS"]["max"].allowed:
            return sid
    return 0


def stamp(update):
    if not INSTALLED or update.get("update_type") != "message_created":
        return update
    message = update.get("message") or {}
    if message.get("recipient", {}).get("chat_type", "dialog") != "dialog":
        return update  # сообщение в группе не должно отменять личную правку или настройку
    body = message.get("body") or {}
    text = (body.get("text") or "").strip()
    uid = str((message.get("sender") or {}).get("user_id"))
    if text in ("/menu", "/myspace", "/admin") and (global_admin(uid) or ROOT.db.execute("SELECT 1 FROM workspaces WHERE owner=?", (uid,)).fetchone()):
        import core
        key = ("max", uid)
        for memory in [ROOT.memories, *(s.memory for s in OPEN.values())]:
            for name in ("WAITING", "WAITING_EDIT", "WAITING_REJECT", "footer_waiting"):
                memory[name].pop(key, None)
        core.WAITING_RULE.root.discard(key)
        for space in OPEN.values():
            space.waiting_rule.discard(key)
        WAITING_MEMBER.difference_update({k for k in WAITING_MEMBER if k[1] == uid})
        WAITING_CREATE.discard(uid)
    continuing = bool(text) and not body.get("attachments") and (not text.startswith("/") or text == "/cancel")
    message = {**message, "_space": for_user((message.get("sender") or {}).get("user_id"), continue_input=continuing)}
    if (message.get("body") or {}).get("text", "").strip() == "/admin" and global_admin((message.get("sender") or {}).get("user_id")):
        message["_space"] = 0
        ROOT.db.execute("INSERT OR REPLACE INTO workspace_routes VALUES(?,0)", (str((message.get("sender") or {}).get("user_id")),))
        ROOT.db.commit()
    uid = str((message.get("sender") or {}).get("user_id"))
    if continuing and uid in WAITING_CREATE and global_admin(uid):
        message["_space"] = 0
    if (message.get("body") or {}).get("text", "").strip() == "/myspace":
        owner = ROOT.db.execute("SELECT id FROM workspaces WHERE owner=?", (uid,)).fetchone()
        if owner:
            message["_space"] = owner["id"]
            ROOT.db.execute("INSERT OR REPLACE INTO workspace_routes VALUES(?,?)", (uid, owner["id"]))
            ROOT.db.commit()
    return {**update, "message": message}


async def route_message(adapter, message, source_key):
    if not INSTALLED or getattr(adapter, "workspace_id", None) != 0 or message.get("_routed"):
        return False
    sid = message.get("_space", for_user((message.get("sender") or {}).get("user_id")))
    if sid:
        space = get(int(sid))
        with enter(space):
            await space.memory["ADAPTERS"]["max"].on_message({**message, "_routed": True}, source_key=source_key)
        return True
    return False


async def route_update(adapter, update):
    if not INSTALLED or getattr(adapter, "workspace_id", None) != 0 or update.get("_routed"):
        return False
    kind = update.get("update_type")
    if kind == "message_created":
        message = {"_space": 0, **update["message"]}  # старые команды относятся к основному пространству
        if not message["_space"]:
            await adapter.on_message({**message, "_routed": True})
            return True
        return await route_message(adapter, message, None)
    if kind == "bot_started":
        sid = for_user(update["user"]["user_id"])
    elif kind == "message_callback":
        cb = update["callback"]
        payload = cb.get("payload") or ""
        sid = 0
        if payload.startswith("w:"):
            _, number, payload = payload.split(":", 2)
            sid = int(number)
            update = {**update, "callback": {**cb, "payload": payload}}
        if sid == 0:
            if ROOT.db.execute("SELECT 1 FROM workspaces WHERE owner=?", (str(cb["user"]["user_id"]),)).fetchone() and not global_admin(cb["user"]["user_id"]):
                await adapter.notify(cb["user"]["user_id"], "Эта кнопка относится к основному пространству. Откройте /myspace.")
                return True
            if str(cb["user"]["user_id"]) in ROOT.adapters["max"].approvers:
                ROOT.db.execute("INSERT OR REPLACE INTO workspace_routes VALUES(?,0)", (str(cb["user"]["user_id"]),))
                ROOT.db.commit()
            # Разворачиваем namespace основного пространства, не перенаправляя по владельцу.
            await adapter.handle({**update, "_routed": True})
            return True
    else:
        return False
    if sid:
        space = get(sid)
        uid = str((update.get("callback", {}).get("user") or update.get("user") or {}).get("user_id"))
        if uid in space.memory["ADAPTERS"]["max"].allowed:
            ROOT.db.execute("INSERT OR REPLACE INTO workspace_routes VALUES(?,?)", (uid, sid))
            ROOT.db.commit()
        with enter(space):
            await space.memory["ADAPTERS"]["max"].handle({**update, "_routed": True})
        return True
    return False


def namespace(buttons, sid):
    if sid is None:
        return buttons
    return [[{**b, "payload": f"w:{sid}:{b['payload']}"} if b.get("type") == "callback" and not b.get("payload", "").startswith("w:") else b for b in row] for row in buttons]


def payload(button):
    value = button.get("payload", "")
    return value.split(":", 2)[2] if value.startswith("w:") else value


def view():
    lines = ["Рабочие пространства:", "0 · Основное (существующий бот)"]
    for row in ROOT.db.execute("SELECT * FROM workspaces ORDER BY id"):
        lines.append(f"{row['id']} · {row['name']} · владелец {row['owner']}\nMAX: {row['max_channel']} · Telegram: {row['tg_channel']}")
    text = "\n".join(lines).encode("utf-16-le")[:7000].decode("utf-16-le", errors="ignore")
    return text, [[("➕ Создать пространство", "m:space_add")]]


async def create_input(adapter, uid, text):
    import core
    if str(uid) not in WAITING_CREATE:
        return False
    if not global_admin(uid) or active_id() != 0:
        WAITING_CREATE.discard(str(uid))
        return False
    if text.strip() in ("/cancel", "/admin", "/menu"):
        WAITING_CREATE.discard(str(uid))
        await adapter.send_menu(uid, *view())
        return True
    parts = [line.strip() for line in text.strip().splitlines()]
    import re
    if len(parts) != 4 or not 1 <= len(parts[0]) <= 80 or not re.fullmatch(r"[1-9][0-9]{0,18}", parts[1]) or not all(re.fullmatch(r"-?[1-9][0-9]{0,18}", p) for p in parts[2:]):
        await adapter.notify(uid, "Нужны четыре строки: название, ID владельца MAX, ID группы MAX, ID группы Telegram. Отмена: /cancel")
        return True
    if "tg" not in ROOT.adapters:
        await adapter.notify(uid, "Сначала подключите Telegram в .env основного бота.")
        return True
    try:
        updating = json.loads((ROOT.folder / "update_state.json").read_text()).get("status") == "probation"
    except (OSError, ValueError, AttributeError):
        updating = False
    if updating:
        await adapter.notify(uid, "Обновление ещё проверяется. Повторите создание пространства через пару минут.")
        return True
    if parts[2] == str(ROOT.adapters["max"].channel) or parts[3] == str(ROOT.adapters["tg"].channel):
        await adapter.notify(uid, "Эта группа уже используется основным пространством. Укажите отдельные группы.")
        return True
    try:
        cur = ROOT.db.execute("INSERT INTO workspaces(name,owner,max_channel,tg_channel) VALUES(?,?,?,?)", parts)
        ROOT.db.commit()
        space = get(cur.lastrowid)
    except sqlite3.IntegrityError:
        await adapter.notify(uid, "Владелец или одна из групп уже привязаны к пространству.")
        return True
    WAITING_CREATE.discard(str(uid))
    core.refresh_people(ROOT.adapters["max"])
    await adapter.send_menu(uid, "Пространство создано. Бот должен быть добавлен в обе группы с правом публикации.\n" + view()[0], view()[1])
    try:
        with enter(space):
            await space.memory["ADAPTERS"]["max"].notify(parts[1], f"Для вас создано пространство «{parts[0]}». Отправьте фото с подписью или /menu. Пространство выбирается автоматически.")
    except Exception:
        await adapter.notify(uid, "Пространство создано, но приветствие не доставлено. Владелец должен открыть чат с ботом в MAX.")
    return True


async def member_input(adapter, uid, text):
    key = (active_id(), str(uid))
    if key not in WAITING_MEMBER:
        return False
    import core
    if not core.is_admin(adapter, uid):
        WAITING_MEMBER.discard(key)
        return False
    if text.strip() in ("/cancel", "/menu", "/myspace"):
        WAITING_MEMBER.discard(key)
        await adapter.send_menu(uid, "Добавление отменено.", core.menu_for(adapter, uid))
        return True
    parts = [line.strip() for line in text.strip().splitlines()]
    roles = {"сотрудник": "emp", "проверяющий": "apr"}
    if len(parts) != 2 or not parts[0].isdigit() or len(parts[0]) > 19 or parts[1].lower() not in roles:
        await adapter.notify(uid, "Отправьте две строки: ID человека в MAX и роль — сотрудник или проверяющий. Отмена: /cancel")
        return True
    if parts[0] in adapter.admins:
        await adapter.notify(uid, "Этот человек уже администратор пространства.")
        return True
    core._add_person(adapter, parts[0], parts[0], roles[parts[1].lower()])
    WAITING_MEMBER.discard(key)
    await adapter.send_menu(uid, "Человек добавлен в это пространство.", core.menu_for(adapter, uid))
    try:
        await adapter.notify(parts[0], f"Вы добавлены в пространство «{CURRENT.get().name}». Отправьте /menu или материал с подписью.")
    except Exception:
        await adapter.notify(uid, "Приветствие не доставлено. Попросите человека открыть чат с ботом.")
    return True


class Maintenance:
    name = "spaces"

    async def run(self):
        import core
        import ops
        while True:
            for space in list(OPEN.values()):
                with enter(space):
                    try:
                        await ops.flush_deferred()
                        await core.remind_once()
                        await core.warn_expiring_once()
                        if time.monotonic() - space.last_housekeeping >= int(core.env("HOUSEKEEPING_SEC", "21600")):
                            core.housekeeping()
                            space.last_housekeeping = time.monotonic()
                        await core.ADAPTERS["max"].cleanup_stale_buttons()
                    except Exception:
                        core.log.exception("ошибка обслуживания пространства %s", space.id)
            await asyncio.sleep(30)


async def close():
    for space in OPEN.values():
        for adapter in space.memory["ADAPTERS"].values():
            if adapter.name == "max":
                await adapter.http.aclose()
            else:
                await adapter.bot.session.close()
        space.db.close()
    OPEN.clear()
