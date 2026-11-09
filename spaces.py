"""Изолированные рабочие пространства одного бота. Основная база остаётся пространством 0."""
import asyncio
import contextvars
import json
import sqlite3
import time
from collections.abc import MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import configuration
import dialog_state
import schema

if TYPE_CHECKING:
    from core import Adapter


@dataclass
class Root:
    db: schema.Connection
    folder: Path
    adapters: MutableMapping[str, "Adapter"]
    memories: dict[str, Any]
    name: str = "Основное"
    owner: str | None = None


def root() -> Root:
    if ROOT is None:
        raise RuntimeError("Рабочие пространства ещё не инициализированы")
    return ROOT


CURRENT: contextvars.ContextVar[Any] = contextvars.ContextVar("workspace", default=None)
INSTALLED = False
ROOT: Root | None = None
OPEN: dict[int, Any] = {}
WAITING_CREATE: set[str] = set()
WAITING_MEMBER: set[tuple[int, str]] = set()


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
        return getattr(CURRENT.get().db if CURRENT.get() else root().db, name)


class Folder:
    def __init__(self, child=""): self.child = child
    def value(self):
        base = CURRENT.get().folder if CURRENT.get() else root().folder
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
    ROOT = Root(db=core.db, folder=Path(core.DATA), adapters=core.ADAPTERS, memories={})
    root().db.execute("""CREATE TABLE IF NOT EXISTS workspaces (
        id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,owner TEXT UNIQUE,max_channel TEXT,tg_channel TEXT)""")
    schema.add_columns(root().db, "workspaces", ("state TEXT DEFAULT 'ready'", "error TEXT DEFAULT ''"))
    root().db.execute("CREATE TABLE IF NOT EXISTS workspace_routes(uid TEXT PRIMARY KEY, sid INTEGER)")
    root().db.execute("CREATE UNIQUE INDEX IF NOT EXISTS workspace_max_group ON workspaces(max_channel)")
    root().db.execute("CREATE UNIQUE INDEX IF NOT EXISTS workspace_tg_group ON workspaces(tg_channel)")
    root().db.commit()
    saved = configured_root(root().db)
    root().name = saved["name"] if saved else core.env("COMPANY", "Основное")
    root().owner = saved["owner"] if saved else None
    ad = root().adapters["max"]
    if not hasattr(ad, "global_admins"):
        ad.global_admins = ad.base_admins[:]
    if saved:
        ad.base_admins = list(dict.fromkeys([*ad.global_admins, saved["owner"]]))
        ad.base_approvers, ad.base_employees = [], []
    core.db = Database()
    core.DATA, core.PHOTOS, core.VIDEOS = Folder(), Folder("photos"), Folder("videos")
    for name in ("ADAPTERS", "WAITING", "WAITING_EDIT", "WAITING_REJECT", "_HITS", "_RL_ALERT", "_ALERT_LAST", "LLM_STATE"):
        original = getattr(core, name)
        root().memories[name] = original
        setattr(core, name, Mapping(name, original))
    root().memories["footer_waiting"] = post_footer.WAITING
    post_footer.WAITING = Mapping("footer_waiting", post_footer.WAITING)
    core.WAITING_RULE = Set(core.WAITING_RULE)
    root().adapters["max"].workspace_id = 0
    INSTALLED = True
    for row in root().db.execute("SELECT * FROM workspaces").fetchall():
        try:
            get(row["id"])
        except Exception:
            core.log.exception("не удалось открыть пространство %s; остальные команды продолжают работу", row["id"])
    core.refresh_people(root().adapters["max"])


def queue_db():
    import core
    return root().db if INSTALLED else core.db


def global_admin(uid):
    return INSTALLED and str(uid) in getattr(root().adapters["max"], "global_admins", root().adapters["max"].base_admins)


def _get(sid):
    if not sid:
        return None
    if sid in OPEN:
        return OPEN[sid]
    row = root().db.execute("SELECT * FROM workspaces WHERE id=?", (sid,)).fetchone()
    if not row:
        raise ValueError("Пространство не найдено")
    from types import SimpleNamespace

    import core
    from max_ import Max
    from tg import Telegram
    folder = root().folder / "spaces" / str(sid)
    folder.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(folder / "drafts.db", check_same_thread=False)
    memory: dict[str, Any] = {"ADAPTERS": {}}
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        objects = root().db.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' AND tbl_name NOT IN ('workspaces','workspace_routes') ORDER BY type DESC").fetchall()
        for obj in objects:
            if obj["type"] == "table":
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (obj["name"],)).fetchone():
                    db.execute(obj["sql"])
                existing = {c["name"] for c in db.execute(f'PRAGMA table_info("{obj["name"]}")')}
                for col in root().db.execute(f'PRAGMA table_info("{obj["name"]}")'):
                    if col["name"] not in existing:
                        default = " DEFAULT " + col["dflt_value"] if col["dflt_value"] is not None else ""
                        db.execute(f'ALTER TABLE "{obj["name"]}" ADD COLUMN "{col["name"]}" {col["type"]}{default}')
        for obj in objects:
            if obj["type"] == "index" and not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (obj["name"],)).fetchone():
                db.execute(obj["sql"])
        db.commit()
        schema.audit_tables(db)
        memory = {name: {} for name in root().memories}
        for name, original in root().memories.items():
            if isinstance(original, dialog_state.PendingMap):
                memory[name] = dialog_state.PendingMap(db, original.kind)
        memory["LLM_STATE"] = {"ok": 0., "err": 0., "msg": ""}
        space = SimpleNamespace(id=sid, name=row["name"], folder=folder, db=db, memory=memory,
                                waiting_rule=dialog_state.PendingSet(db), last_housekeeping=time.monotonic())
        memory["ADAPTERS"] = {}
        with enter(space):
            core.PHOTOS.mkdir(parents=True, exist_ok=True)
            core.VIDEOS.mkdir(parents=True, exist_ok=True)
            root_max = root().adapters["max"]
            ad = Max(root_max.http.headers["Authorization"], row["max_channel"], [row["owner"]], [row["owner"]], [row["owner"]])
            ad.workspace_id = sid
            memory["ADAPTERS"]["max"] = ad
            import os
            if row["tg_channel"]:
                if not os.getenv("TG_TOKEN", "").strip():
                    raise ValueError("Для группы Telegram пространства нужен TG_TOKEN в .env.")
                memory["ADAPTERS"]["tg"] = Telegram(os.environ["TG_TOKEN"], row["tg_channel"])
            core.startup()
    except BaseException:
        db.close()
        for adapter in memory.get("ADAPTERS", {}).values():
            close = adapter.http.aclose if adapter.name == "max" else adapter.bot.session.close
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(close())
            else:
                task = loop.create_task(close())
                task.add_done_callback(lambda t: t.cancelled() or t.exception())
        raise
    OPEN[sid] = space
    return space


def get(sid):
    if not sid:
        return None
    if sid in OPEN:
        return OPEN[sid]
    import core
    try:
        space = _get(sid)
    except Exception as exc:
        root().db.execute("UPDATE workspaces SET state='failed',error=? WHERE id=?", (core.err(exc)[:200], sid))
        root().db.commit()
        raise
    root().db.execute("UPDATE workspaces SET state='ready',error='' WHERE id=? AND state!='ready'", (sid,))
    root().db.commit()
    return space


def _root_allows(uid):
    allowed = root().adapters["max"].allowed
    return allowed is None or str(uid) in allowed


def for_user(uid, continue_input=False):
    uid = str(uid)
    owner = root().db.execute("SELECT id FROM workspaces WHERE owner=?", (uid,)).fetchone()
    if not owner and root().owner == uid:
        owner = {"id": 0}
    selected = root().db.execute("SELECT sid FROM workspace_routes WHERE uid=?", (str(uid),)).fetchone()
    if owner:
        if continue_input and selected:
            sid = selected["sid"]
            import core
            if sid == 0:
                memory, rules = root().memories, getattr(core.WAITING_RULE, "root", core.WAITING_RULE)
                allowed = _root_allows(uid)
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
    if selected and selected["sid"] == 0 and _root_allows(uid):
        return 0
    if selected and selected["sid"] in OPEN and str(uid) in OPEN[selected["sid"]].memory["ADAPTERS"]["max"].allowed:
        return selected["sid"]
    for sid, space in OPEN.items():
        if str(uid) in space.memory["ADAPTERS"]["max"].allowed:
            return sid
    return 0


@contextmanager
def notification_target(adapter, uid):
    """Bind system notices to the recipient's space without changing saved routes."""
    if INSTALLED and adapter is root().adapters.get("max"):
        import core
        with enter(get(for_user(uid, continue_input=True))):
            yield core.ADAPTERS["max"]
    else:
        yield adapter


def stamp(update):
    if not INSTALLED or update.get("update_type") != "message_created":
        return update
    message = update.get("message") or {}
    if (message.get("recipient") or {}).get("chat_type", "dialog") != "dialog":
        return update  # сообщение в группе не должно отменять личную правку или настройку
    body = message.get("body") or {}
    text = (body.get("text") or "").strip()
    uid = str((message.get("sender") or {}).get("user_id"))
    if text in ("/menu", "/myspace", "/admin") and (global_admin(uid) or root().owner == uid or root().db.execute("SELECT 1 FROM workspaces WHERE owner=?", (uid,)).fetchone()):
        import core
        key = ("max", uid)
        for memory in [root().memories, *(s.memory for s in OPEN.values())]:
            for name in ("WAITING", "WAITING_EDIT", "WAITING_REJECT", "footer_waiting"):
                memory[name].pop(key, None)
        getattr(core.WAITING_RULE, "root", core.WAITING_RULE).discard(key)
        for space in OPEN.values():
            space.waiting_rule.discard(key)
        WAITING_MEMBER.difference_update({k for k in WAITING_MEMBER if k[1] == uid})
        WAITING_CREATE.discard(uid)
        settings_state().pop(("max", uid), None)
    continuing = bool(text) and not body.get("attachments") and (not text.startswith("/") or text == "/cancel")
    message = {**message, "_space": for_user((message.get("sender") or {}).get("user_id"), continue_input=continuing)}
    if (message.get("body") or {}).get("text", "").strip() == "/admin" and global_admin((message.get("sender") or {}).get("user_id")):
        message["_space"] = 0
        root().db.execute("INSERT OR REPLACE INTO workspace_routes VALUES(?,0)", (str((message.get("sender") or {}).get("user_id")),))
        root().db.commit()
    uid = str((message.get("sender") or {}).get("user_id"))
    if continuing and global_admin(uid) and (uid in WAITING_CREATE or settings_state().get(("max", uid), {}).get("waiting")):
        message["_space"] = 0
    if (message.get("body") or {}).get("text", "").strip() == "/myspace":
        owner = root().db.execute("SELECT id FROM workspaces WHERE owner=?", (uid,)).fetchone()
        if owner:
            message["_space"] = owner["id"]
            root().db.execute("INSERT OR REPLACE INTO workspace_routes VALUES(?,?)", (uid, owner["id"]))
            root().db.commit()
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
            import core
            uid = str(cb["user"]["user_id"])
            home = for_user(uid)
            if home and not core.is_member(adapter, uid):
                space = get(home)
                with enter(space):
                    own = space.memory["ADAPTERS"]["max"]
                    if payload in ("m:admin", "m:menu"):
                        # Only navigation may cross a stale root namespace. Never reinterpret draft IDs.
                        action = "m:admin" if core.is_admin(own, uid) else "m:menu"
                        root().db.execute("INSERT OR REPLACE INTO workspace_routes VALUES(?,?)", (uid, home))
                        root().db.commit()
                        await own.handle({**update, "_routed": True, "callback": {**cb, "payload": action}})
                    else:
                        await own.send_notice_menu(uid, "Эта кнопка относится к основному пространству. Откройте свой пост заново.",
                                                   [[("Моё пространство", "m:admin" if core.is_admin(own, uid) else "m:menu")]])
                return True
            if _root_allows(cb["user"]["user_id"]):
                root().db.execute("INSERT OR REPLACE INTO workspace_routes VALUES(?,0)", (str(cb["user"]["user_id"]),))
                root().db.commit()
            # Разворачиваем namespace основного пространства, не перенаправляя по владельцу.
            await adapter.handle({**update, "_routed": True})
            return True
    else:
        return False
    if sid:
        space = get(sid)
        uid = str((update.get("callback", {}).get("user") or update.get("user") or {}).get("user_id"))
        if uid in space.memory["ADAPTERS"]["max"].allowed:
            root().db.execute("INSERT OR REPLACE INTO workspace_routes VALUES(?,?)", (uid, sid))
            root().db.commit()
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


FIELDS = ("name", "owner", "max_channel", "tg_channel")
INPUT_HELP = ("Отправьте строки:\nНазвание пространства\nID владельца в MAX\nID группы MAX\n"
              "ID или @username группы Telegram (или «-», если Telegram не нужен).\n"
              "Четвёртую строку можно пропустить.")


def configured_root(db):
    """An explicit DB override wins over bootstrap .env values, including empty Telegram."""
    row = db.execute("SELECT value FROM settings WHERE key='root_workspace'").fetchone()
    if row is None:
        return None
    value = json.loads(row[0])

    if (not isinstance(value, dict) or any(field not in value for field in FIELDS) or not isinstance(value.get("name"), str)
            or not 1 <= len(value["name"]) <= 80 or not isinstance(value.get("owner"), str)
            or not configuration.user_id(value["owner"])
            or not isinstance(value.get("max_channel"), str)
            or not configuration.chat_id(value["max_channel"])
            or value.get("tg_channel") is not None and (not isinstance(value["tg_channel"], str) or not configuration.telegram_channel(value["tg_channel"]))):
        raise ValueError("Повреждены настройки основного пространства в базе; восстановите их из резервной копии.")
    return value


def settings_record(sid):
    if sid:
        return root().db.execute("SELECT * FROM workspaces WHERE id=?", (sid,)).fetchone()
    saved = configured_root(root().db)
    if saved:
        return {"id": 0, **saved}
    ad = root().adapters["max"]
    admins = getattr(ad, "global_admins", ad.base_admins)
    return {"id": 0, "name": root().name, "owner": admins[0] if admins else "",
            "max_channel": str(ad.channel), "tg_channel": str(root().adapters["tg"].channel) if "tg" in root().adapters else None}


def root_context():
    from types import SimpleNamespace

    import core
    return SimpleNamespace(id=0, db=root().db, memory=root().memories, name=root().name,
                           waiting_rule=getattr(core.WAITING_RULE, "root", core.WAITING_RULE))


def persist_root(values):
    """Import env members once; later edits must not restore users removed in the menu."""
    ad = root().adapters["max"]
    if configured_root(root().db) is None:
        for uid in dict.fromkeys([*ad.base_approvers, *ad.base_employees]):
            root().db.execute("INSERT OR IGNORE INTO people(platform,uid,name,role,created) VALUES('max',?,?, 'member',?)",
                              (str(uid), str(uid), time.time()))
    root().db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('root_workspace',?)",
                      (json.dumps(dict(zip(FIELDS, values)), ensure_ascii=False),))


def settings_state():
    return dialog_state.PendingMap(root().db, "space_settings")


def parse_settings(text, sid=None):
    parts = [line.strip() for line in text.strip().splitlines()]
    if len(parts) == 3:
        parts.append("")
    if len(parts) != 4:
        raise ValueError(INPUT_HELP)
    if parts[3] == "-":
        parts[3] = ""
    if (not 1 <= len(parts[0]) <= 80 or not configuration.user_id(parts[1])
            or not configuration.chat_id(parts[2])
            or parts[3] and not configuration.telegram_channel(parts[3])):
        raise ValueError(INPUT_HELP)
    # Canonical IDs prevent alternate spellings from bypassing uniqueness checks.
    parts[1:3] = [str(int(value)) for value in parts[1:3]]
    parts[3] = parts[3].lower() if parts[3].startswith("@") else str(int(parts[3])) if parts[3] else ""
    import os
    if parts[3] and not os.getenv("TG_TOKEN", "").strip():
        raise ValueError("Для группы Telegram сначала подключите TG_TOKEN в .env. Без Telegram укажите «-».")
    primary = configured_root(root().db)
    if sid != 0 and primary and parts[1] == primary["owner"]:
        raise ValueError("Этот владелец уже привязан к основному пространству. Укажите другого владельца.")
    if sid == 0 and root().db.execute("SELECT 1 FROM workspaces WHERE owner=? LIMIT 1", (parts[1],)).fetchone():
        raise ValueError("Этот владелец уже привязан к другому пространству.")
    if sid != 0 and (parts[2] == str(root().adapters["max"].channel)
                     or parts[3] and "tg" in root().adapters and parts[3] == str(root().adapters["tg"].channel).lower()):
        raise ValueError("Эта группа уже используется основным пространством. Укажите отдельные группы.")
    if sid == 0 and root().db.execute("SELECT 1 FROM workspaces WHERE max_channel=? OR (? IS NOT NULL AND LOWER(tg_channel)=?) LIMIT 1",
                                     (parts[2], parts[3], parts[3])).fetchone():
        raise ValueError("Эта группа уже привязана к другому пространству.")
    return [*parts[:3], parts[3] or None]


def settings_values(row):
    return [row[field] for field in FIELDS]


def settings_card(sid):
    row = settings_record(sid)
    if not row:
        return "Пространство не найдено.", None
    return (f"Пространство №{sid}: {row['name']}\nВладелец MAX: {row['owner']}\n"
            f"Группа MAX: {row['max_channel']}\nГруппа Telegram: {row['tg_channel'] or 'не подключена'}",
            [[("✏️ Изменить настройки", f"m:space_edit:{sid}")], [("← Пространства", "m:spaces")]])


def update_in_progress():
    try:
        return json.loads((root().folder / "update_state.json").read_text()).get("status") == "probation"
    except (OSError, ValueError, AttributeError):
        return False


async def settings_input(adapter, uid, text):
    import secrets

    import core
    key = (adapter.name, str(uid))
    saved = settings_state().get(key)
    if not saved or not saved.get("waiting"):
        return False
    if active_id() or not global_admin(uid):
        settings_state().pop(key, None)
        return False
    if text.strip() in ("/cancel", "/menu", "/admin"):
        settings_state().pop(key, None)
        await adapter.send_menu(uid, *settings_card(saved["sid"]))
        return True
    try:
        values = parse_settings(text, saved["sid"])
    except ValueError as exc:
        await adapter.notify(uid, core.err(exc))
        return True
    payload = f"m:space_save:{saved['sid']}:{secrets.token_urlsafe(12)}"
    settings_state()[key] = {**saved, "waiting": False, "values": values, "payload": payload}
    name, owner, maximum, telegram = values
    await adapter.send_menu(uid, f"Сохранить настройки?\nНазвание: {name}\nВладелец MAX: {owner}\n"
                            f"Группа MAX: {maximum}\nГруппа Telegram: {telegram or 'не подключена'}\n\n"
                            "При смене владельца прежний владелец потеряет доступ к пространству. Остальные участники сохранят права. "
                            "Главные администраторы из MAX_ADMINS сохраняют полномочия.",
                            [[("✅ Сохранить", payload)], [("Отмена", f"m:space:{saved['sid']}")]])
    return True


def settings_busy(space):
    if root().db.execute("SELECT 1 FROM max_inbox WHERE space_id=? AND state IN ('pending','processing') LIMIT 1", (space.id,)).fetchone():
        return True
    return bool(space.db.execute("SELECT 1 FROM drafts WHERE status IN ('publishing','regenerating') OR "
                                 "(status='pending' AND COALESCE(published_to,'')!='') LIMIT 1").fetchone())


async def settings_save(adapter, uid, payload):
    import core
    from tg import Telegram
    key = (adapter.name, str(uid))
    saved = settings_state().pop(key, None)  # consume before any network operation
    if not saved or saved.get("payload") != payload:
        return "Подтверждение устарело. Откройте настройки пространства заново.", None
    sid, values = saved["sid"], saved["values"]
    row = settings_record(sid)
    if not row or settings_values(row) != saved["before"]:
        return "Настройки уже изменились. Откройте пространство заново.", None
    if update_in_progress():
        return "Обновление ещё проверяется. Повторите изменение через пару минут.", None
    space = get(sid) if sid else root_context()
    if (not sid or values != settings_values(row)) and settings_busy(space):
        return "Есть обработка или незавершённая публикация. Дождитесь обработки, проверьте частичные и неизвестные результаты в каналах и повторите изменение.", None
    # Validate again: another workspace or the root settings may have changed since the prompt.
    try:
        values = parse_settings("\n".join(value or "-" for value in values), sid)
    except ValueError as exc:
        return core.err(exc), None
    old_tg = space.memory["ADAPTERS"].get("tg")
    new_tg = old_tg
    if values[3] and old_tg is None:
        import os
        new_tg = Telegram(os.environ["TG_TOKEN"], values[3])
    try:
        if sid:
            root().db.execute("UPDATE workspaces SET name=?,owner=?,max_channel=?,tg_channel=? WHERE id=?", (*values, sid))
        else:
            persist_root(values)
        if row["owner"] != values[1]:
            space.db.execute("DELETE FROM people WHERE platform='max' AND uid=?", (row["owner"],))
            if sid:
                space.db.commit()
            root().db.execute("DELETE FROM workspace_routes WHERE uid IN (?,?)", (row["owner"], values[1]))
        root().db.commit()
    except sqlite3.IntegrityError:
        root().db.rollback()
        if new_tg is not None and new_tg is not old_tg:
            await new_tg.bot.session.close()
        return "Владелец или одна из групп уже привязаны к другому пространству.", None
    except BaseException:
        root().db.rollback()
        space.db.rollback()
        if new_tg is not None and new_tg is not old_tg:
            await new_tg.bot.session.close()
        raise
    space.name = values[0]
    ad = space.memory["ADAPTERS"]["max"]
    ad.channel = values[2]
    if sid:
        ad.base_admins, ad.base_approvers, ad.base_employees = [values[1]], [values[1]], [values[1]]
    else:
        root().name, root().owner = values[:2]
        ad.base_admins = list(dict.fromkeys([*ad.global_admins, values[1]]))
        ad.base_approvers, ad.base_employees = [], []
    if values[3]:
        new_tg.channel = values[3]
        space.memory["ADAPTERS"]["tg"] = new_tg
    else:
        space.memory["ADAPTERS"].pop("tg", None)
    with enter(space if sid else None):
        core.refresh_people(ad)
        for kind in ("max_confirmation", "confirmation_channels", "people_confirmation"):
            dialog_state.PendingMap(space.db, kind).clear()
        if row["owner"] != values[1]:
            for kind in ("WAITING", "WAITING_EDIT", "WAITING_REJECT", "footer_waiting"):
                space.memory[kind].pop(("max", row["owner"]), None)
            space.waiting_rule.discard(("max", row["owner"]))
    core.refresh_people(root().adapters["max"])
    if old_tg is not None and not values[3]:
        await old_tg.bot.session.close()
    text, rows = settings_card(sid)
    return "Настройки сохранены.\n\n" + text, rows


async def settings_menu(adapter, uid, payload, parts):
    import core
    if active_id() or not global_admin(uid):
        return "⛔ Настройки пространств меняет только главный администратор в основном пространстве.", None
    if parts[1] == "space_save":
        return await settings_save(adapter, uid, payload)
    if len(parts) != 3 or not parts[2].isdigit() or len(parts[2]) > 9:
        return "Пространство не найдено.", None
    sid = int(parts[2])
    if parts[1] == "space":
        return settings_card(sid)
    row = settings_record(sid)
    if not row:
        return "Пространство не найдено.", None
    settings_state()[(adapter.name, str(uid))] = {"sid": sid, "before": settings_values(row), "waiting": True}
    text, _ = settings_card(sid)
    return text + "\n\n" + INPUT_HELP, core.input_rows(adapter, uid)


def view(page=0):
    total = root().db.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
    page = min(max(0, page), max(0, (total - 1) // 12))
    rows = root().db.execute("SELECT * FROM workspaces ORDER BY id LIMIT 12 OFFSET ?", (page * 12,)).fetchall()
    primary = settings_record(0)
    source = "настройки в базе" if configured_root(root().db) else "пока из .env"
    lines = ["Рабочие пространства:", f"0 · {primary['name']} ({source})"]
    buttons = [[(f"⚙ {primary['name'][:40]} · №0", "m:space:0")]]
    for row in rows:
        lines.append(f"{row['id']} · {row['name']} [{row['state']}] · владелец {row['owner']}\n"
                     f"MAX: {row['max_channel']} · Telegram: {row['tg_channel'] or 'не подключена'}")
        buttons.append([(f"⚙ {row['name'][:40]}", f"m:space:{row['id']}")])
    nav = []
    if page:
        nav.append(("← Назад", f"m:spaces:{page - 1}"))
    if (page + 1) * 12 < total:
        nav.append(("Далее →", f"m:spaces:{page + 1}"))
    if nav:
        buttons.append(nav)
    return "\n".join(lines), buttons + [[("➕ Создать пространство", "m:space_add")]]


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
    try:
        parts = parse_settings(text)
    except ValueError as exc:
        await adapter.notify(uid, core.err(exc))
        return True
    if update_in_progress():
        await adapter.notify(uid, "Обновление ещё проверяется. Повторите создание пространства через пару минут.")
        return True
    existing = root().db.execute("SELECT * FROM workspaces WHERE owner=?", (parts[1],)).fetchone()
    if existing and existing["state"] == "failed" and list(existing[k] for k in ("name", "owner", "max_channel", "tg_channel")) == parts:
        sid = existing["id"]
    else:
        try:
            cur = root().db.execute("INSERT INTO workspaces(name,owner,max_channel,tg_channel,state) VALUES(?,?,?,?,'provisioning')", parts)
            root().db.commit()
            sid = cur.lastrowid
        except sqlite3.IntegrityError:
            root().db.rollback()
            await adapter.notify(uid, "Владелец или одна из групп уже привязаны к пространству.")
            return True
    try:
        space = get(sid)
    except Exception as exc:
        root().db.execute("UPDATE workspaces SET state='failed',error=? WHERE id=?", (core.err(exc)[:200], sid))
        root().db.commit()
        await adapter.notify(uid, "Не удалось подготовить пространство. После устранения ошибки отправьте те же четыре строки для повторной попытки.")
        return True
    WAITING_CREATE.discard(str(uid))
    core.refresh_people(root().adapters["max"])
    await adapter.send_menu(uid, "Пространство создано. Бот должен иметь право публикации в каждой указанной группе.\n" + view()[0], view()[1])
    try:
        with enter(space):
            await space.memory["ADAPTERS"]["max"].send_menu(parts[1], f"Для вас создано пространство «{parts[0]}». Отправьте фото с подписью. Пространство выбирается автоматически.", [[("Открыть меню", "m:menu")]])
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
    roles = {"участник", "сотрудник", "проверяющий"}
    if len(parts) not in (1, 2) or not configuration.user_id(parts[0]) or (len(parts) == 2 and parts[1].lower() not in roles):
        await adapter.notify(uid, "Отправьте ID человека в MAX. Он будет добавлен как участник.")
        return True
    if core.role_of(adapter, parts[0]) is not None:
        await adapter.notify(uid, "Этот человек уже добавлен. Изменить его роль можно в разделе «Люди».")
        return True
    core._add_person(adapter, parts[0], parts[0], "member")
    WAITING_MEMBER.discard(key)
    await adapter.send_menu(uid, "Человек добавлен в это пространство.", core.menu_for(adapter, uid))
    try:
        name = CURRENT.get().name if CURRENT.get() else root().name if INSTALLED else "Основное"
        await adapter.send_menu(parts[0], f"Вы добавлены в пространство «{name}». Отправьте материал с подписью.", [[("Открыть меню", "m:menu")]])
    except Exception:
        await adapter.notify(uid, "Приветствие не доставлено. Попросите человека открыть чат с ботом.")
    return True


class Maintenance:
    name = "spaces"

    async def run(self):
        import core
        import ops
        while True:
            for row in root().db.execute("SELECT id FROM workspaces WHERE state!='ready'").fetchall():
                try:
                    get(row["id"])
                except Exception:
                    core.log.warning("пространство %s пока недоступно", row["id"], exc_info=True)
            for space in list(OPEN.values()):
                with enter(space):
                    try:
                        await ops.flush_deferred()
                        await core.remind_once()
                        await core.warn_expiring_once()
                        if time.monotonic() - space.last_housekeeping >= int(core.env("HOUSEKEEPING_SEC", "21600")) or core.cleanup_pending():
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
