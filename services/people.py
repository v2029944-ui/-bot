"""Роли, заявки и участники."""
import core
import time
import spaces
import ops


def is_admin(adapter: core.Adapter, uid) -> bool:
    return str(uid) in (getattr(adapter, "admins", None) or [])


def requests_enabled(adapter: core.Adapter) -> bool:
    """Заявки работают, только если у мессенджера есть администратор."""
    return bool(getattr(adapter, "admins", None)) and core.env("ACCESS_REQUESTS", "1") != "0"


def _base(adapter: core.Adapter) -> tuple[list[str], list[str]]:
    """Списки из настроек (.env). Запоминаем их один раз, дальше к ним добавляются люди из базы."""
    if "base_approvers" not in adapter.__dict__:
        adapter.base_approvers = list(adapter.approvers)
    if "base_employees" not in adapter.__dict__:
        adapter.base_employees = []
    return adapter.base_approvers, adapter.base_employees


def _db_people(adapter: core.Adapter) -> list[dict]:
    rows = core.db.execute("SELECT * FROM people WHERE platform=? ORDER BY created", (adapter.name,)).fetchall()
    return [dict(r) for r in rows]


def _base_admins(adapter: core.Adapter) -> list[str]:
    """Администраторы из настроек (.env): кнопками их не меняют."""
    if "base_admins" not in adapter.__dict__:
        adapter.base_admins = [str(x) for x in (getattr(adapter, "admins", None) or [])]
    return adapter.base_admins


def refresh_people(adapter: core.Adapter):
    """Пересобирает adapter.approvers и adapter.allowed: настройки + люди, добавленные кнопками."""
    if adapter.publish_only:
        # Старые роли Telegram остаются в БД, но больше не участвуют в работе.
        adapter.admins, adapter.approvers, adapter.allowed = [], [], set()
        return
    base_apr, base_emp = core._base(adapter)
    people = core._db_people(adapter)
    adm = list(dict.fromkeys([*core._base_admins(adapter), *(p["uid"] for p in people if p["role"] == "adm")]))
    adapter.admins = adm  # администратор — это ещё и согласующий
    apr = list(dict.fromkeys([*map(str, base_apr), *adm, *(p["uid"] for p in people if p["role"] == "apr")]))
    emp = [*map(str, base_emp), *(p["uid"] for p in people if p["role"] == "emp")]
    adapter.approvers = apr
    if spaces.INSTALLED and spaces.active_id() == 0:
        owners = {r[0] for r in spaces.ROOT.db.execute("SELECT owner FROM workspaces")}
        adapter.approvers = [uid for uid in adapter.approvers if uid not in owners or uid in adapter.admins]
    # пустой список сотрудников = писать может кто угодно (как раньше), кроме режима с заявками
    adapter.allowed = (set(emp) | set(apr)) if (emp or core.requests_enabled(adapter)) else None


def role_of(adapter: core.Adapter, uid) -> str | None:
    uid = str(uid)
    if uid in (getattr(adapter, "admins", None) or []):
        return "adm"
    if uid in adapter.approvers:
        return "apr"
    if adapter.allowed is not None and uid in adapter.allowed:
        return "emp"
    return None


def _add_person(adapter: core.Adapter, uid: str, name: str, role: str):
    core.db.execute("INSERT OR REPLACE INTO people(platform,uid,name,role,created) VALUES(?,?,?,?,?)",
               (adapter.name, str(uid), name, role, time.time()))
    core.db.execute("DELETE FROM requests WHERE platform=? AND uid=?", (adapter.name, str(uid)))
    core.db.commit()
    core.refresh_people(adapter)


def _remove_person(adapter: core.Adapter, uid: str, name: str):
    core.db.execute("DELETE FROM people WHERE platform=? AND uid=?", (adapter.name, str(uid)))
    core.db.execute("INSERT OR REPLACE INTO requests(platform,uid,name,username,status,created) VALUES(?,?,?,?,?,?)",
               (adapter.name, str(uid), name, None, "rejected", time.time()))  # чтобы не вернулся заявкой сам
    core.db.commit()
    core.refresh_people(adapter)


async def _tell_admins(adapter: core.Adapter, text: str, rows=None):
    for a in adapter.admins:
        try:
            await adapter.send_menu(a, text, rows) if rows else await adapter.notify(a, text)
        except Exception:
            core.log.warning("не удалось написать администратору %s", a, exc_info=True)


def _who(name, username, uid) -> str:
    return f"{name or 'Без имени'}" + (f" (@{username})" if username else "") + f", id {uid}"


def _request_rows(uid: str, short: str = "") -> list:
    return [[("👷 Сотрудник", f"m:acc:emp:{uid}"), ("✅ Согласующий", f"m:acc:apr:{uid}")],
            [("🚫 Отклонить", f"m:acc:no:{uid}")]]


async def unknown_user(adapter: core.Adapter, uid, name: str, username: str | None = None) -> bool:
    """Незнакомый человек написал боту: создаём заявку и спрашиваем администраторов.
    True = заявка принята (или уже ждёт решения). Незнакомцу бот ничего не отвечает."""
    if not core.requests_enabled(adapter):
        return False
    uid = str(uid)
    r = core.db.execute("SELECT status, created FROM requests WHERE platform=? AND uid=?", (adapter.name, uid)).fetchone()
    if r:
        if r["status"] == "pending":
            return True
        if time.time() - r["created"] < core.REQ_TTL:
            return False
    core.db.execute("DELETE FROM requests WHERE status='pending' AND created < ?", (time.time() - core.REQ_TTL,))  # протухшие
    n = core.db.execute("SELECT COUNT(*) FROM requests WHERE platform=? AND status='pending'", (adapter.name,)).fetchone()[0]
    if n >= core.MAX_PENDING_REQUESTS:
        core.log.warning("слишком много заявок на доступ, новая от %s пропущена", uid)
        return False
    core.db.execute("INSERT OR REPLACE INTO requests(platform,uid,name,username,status,created) VALUES(?,?,?,?,?,?)",
               (adapter.name, uid, name, username, "pending", time.time()))
    core.db.commit()
    await core._tell_admins(adapter, f"🔔 {core._who(name, username, uid)} просит доступ к боту.\nКем добавить?",
                       core._request_rows(uid))
    return True


def menu_for(adapter: core.Adapter, uid) -> list:
    rows = [list(r) for r in core.MENU]
    rows.append([("📋 На проверке", "m:pending")])
    if core.is_admin(adapter, uid):
        rows.append([("➕ Добавить человека", "m:member")])
        if spaces.INSTALLED and spaces.active_id() == 0 and spaces.global_admin(uid):
            rows.append([("🏢 Рабочие пространства", "m:spaces")])
        rows.append([("👥 Люди", "m:people"), ("📊 Статус", "m:status")])
        if spaces.active_id():
            rows.append([("⏯ Пауза пространства", "m:pause")])
        else:
            rows.append([("⏯ Пауза", "m:pause"), ("🔄 Обновить", "m:upd")])
        rows.append([("📥 Обработка материалов", "m:inbox")])
        rows.append([("🔗 Ссылка в постах", "m:footer")])
        rows.append([("🧪 Сравнение моделей", "m:compare")])
    return rows


def people_view(adapter: core.Adapter) -> tuple[str, list]:
    people = core._db_people(adapter)
    in_db = {p["uid"] for p in people}
    base_apr, base_emp = core._base(adapter)
    base_adm = core._base_admins(adapter)

    def line(name, uid, note=""):
        return f"• {name or 'Без имени'} (id {uid}){note}"
    adm = [line(None, u, " — из настроек") for u in base_adm if u not in in_db]
    adm += [line(p["name"], p["uid"]) for p in people if p["role"] == "adm"]
    apr = [line(None, u, " — из настроек") for u in dict.fromkeys(map(str, base_apr))
           if u not in in_db and u not in base_adm]
    apr += [line(p["name"], p["uid"]) for p in people if p["role"] == "apr"]
    emp = [line(None, u, " — из настроек") for u in map(str, base_emp) if u not in in_db]
    emp += [line(p["name"], p["uid"]) for p in people if p["role"] == "emp"]
    text = ("👥 Люди\n\n👑 Администраторы:\n" + ("\n".join(adm) or "—") +
            "\n\n✅ Согласующие:\n" + ("\n".join(apr) or "—") +
            "\n\n👷 Сотрудники:\n" + ("\n".join(emp) or "—"))
    if len(text) > 3500:
        text = text[:3500] + "\n…"
    btns = [(f"⚙ {(p['name'] or p['uid'])[:18]}", f"m:pset:{p['uid']}") for p in people[:20]]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    if people:
        text += "\n\nНажмите ⚙, чтобы сменить роль или убрать человека. Люди «из настроек» меняются в файле .env."
    n = core.db.execute("SELECT COUNT(*) FROM requests WHERE platform=? AND status='pending'", (adapter.name,)).fetchone()[0]
    if n:
        rows.append([(f"📬 Заявки ({n})", "m:reqs")])
    rows.append([("➕ Добавить человека", "m:member")])
    return text, rows


def _person(adapter: core.Adapter, uid: str) -> dict | None:
    return next((x for x in core._db_people(adapter) if x["uid"] == uid), None)


def reqs_view(adapter: core.Adapter) -> tuple[str, list]:
    rs = core.db.execute("SELECT * FROM requests WHERE platform=? AND status='pending' ORDER BY created LIMIT 5",
                    (adapter.name,)).fetchall()
    if not rs:
        return "Новых заявок нет.", [[("👥 Люди", "m:people")]]
    text = "📬 Заявки на доступ:\n" + "\n".join(f"• {core._who(r['name'], r['username'], r['uid'])}" for r in rs)
    rows = []
    for r in rs:
        short = (r["name"] or r["uid"])[:10]
        rows.append([(f"👷 {short}", f"m:acc:emp:{r['uid']}"), (f"✅ {short}", f"m:acc:apr:{r['uid']}"),
                     (f"🚫 {short}", f"m:acc:no:{r['uid']}")])
    return text + "\n\n👷 — сотрудник, ✅ — согласующий, 🚫 — отклонить.", rows


async def people_menu(adapter: core.Adapter, act: str, parts: list[str], uid: str = "") -> tuple[str, list | None]:
    arg = lambda i: parts[i] if len(parts) > i else ""
    if act == "people":
        return core.people_view(adapter)
    if act == "reqs":
        return core.reqs_view(adapter)
    if act == "acc":
        role, uid = arg(2), arg(3)
        r = core.db.execute("SELECT * FROM requests WHERE platform=? AND uid=? AND status='pending'",
                       (adapter.name, uid)).fetchone()
        if not r:
            return "Эта заявка уже обработана.", None
        who = r["name"] or uid
        if role == "no":
            core.db.execute("UPDATE requests SET status='rejected', created=? WHERE platform=? AND uid=?",
                       (time.time(), adapter.name, uid))
            core.db.commit()
            return f"🚫 Заявка отклонена: {who}.", None
        if role not in core.ROLE_NAME:
            return "Неизвестная роль.", None
        core._add_person(adapter, uid, r["name"], role)
        try:  # единственное сообщение незнакомцу: доступ открыт
            await adapter.notify(uid, core.ACCESS_WELCOME[role])
        except Exception:
            core.log.warning("не удалось сообщить %s о доступе", uid, exc_info=True)
        return f"✅ {who} добавлен: {core.ROLE_NAME[role]}.", None
    if act == "pause":
        return ops.pause_view()
    if act == "pauseset":
        ops.set_paused(arg(2) == "1")
        text, rows = ops.pause_view()
        return ("⏸ Поставлено на паузу.\n\n" if arg(2) == "1" else "▶️ Бот снова принимает фото.\n\n") + text, rows
    if act == "upd":
        return await ops.upd_view()
    if act == "updgo":
        return ops.upd_go(adapter, uid)
    if act == "pset":
        p = core._person(adapter, arg(2))
        if not p:
            return core.people_view(adapter)
        who = p["name"] or p["uid"]
        rows = [[((("✓ " if p["role"] == r else "") + f"{core.ROLE_ICON[r]} {core.ROLE_NAME[r].capitalize()}"),
                  f"m:prole:{r}:{p['uid']}") for r in ("emp", "apr", "adm")],
                [("🗑 Убрать", f"m:pdel:{p['uid']}"), ("← Назад", "m:people")]]
        return f"{who} (id {p['uid']}) — сейчас: {core.ROLE_NAME[p['role']]}.\nВыберите новую роль:", rows
    if act in ("prole", "proleok"):
        role, uid = arg(2), arg(3)
        p = core._person(adapter, uid)
        if not p or role not in core.ROLE_NAME:
            return core.people_view(adapter)
        who = p["name"] or uid
        if p["role"] == role:
            return f"{who} уже {core.ROLE_NAME[role]}.", [[("← Назад", f"m:pset:{uid}")]]
        if p["role"] == "adm" and len(adapter.admins) <= 1:
            return "⛔ Нельзя снять права с последнего администратора.", [[("← Назад", f"m:pset:{uid}")]]
        if act == "prole":
            warn = ("\nАдминистратор добавляет и убирает людей, обновляет и ставит на паузу бота."
                    if role == "adm" else "")
            return (f"Сменить роль: {who} — {core.ROLE_NAME[p['role']]} → {core.ROLE_NAME[role]}?{warn}",
                    [[("Да, сменить", f"m:proleok:{role}:{uid}"), ("Отмена", f"m:pset:{uid}")]])
        core._add_person(adapter, uid, p["name"], role)
        try:
            await adapter.notify(uid, f"Ваша роль изменена: {core.ROLE_NAME[role]}. Нажмите /menu, чтобы обновить кнопки.")
        except Exception:
            core.log.warning("не удалось сообщить %s о новой роли", uid, exc_info=True)
        return f"✅ {who}: теперь {core.ROLE_NAME[role]}.", [[("← К списку", "m:people")]]
    if act == "pdel":
        p = next((x for x in core._db_people(adapter) if x["uid"] == arg(2)), None)
        if not p:
            return core.people_view(adapter)
        if p["role"] == "adm" and len(adapter.admins) <= 1:
            return "⛔ Нельзя убрать последнего администратора.", [[("← Назад", f"m:pset:{p['uid']}")]]
        return (f"Убрать {p['name'] or p['uid']} из списка? Он перестанет присылать фото.",
                [[("Да, убрать", f"m:pdelok:{p['uid']}"), ("Отмена", "m:people")]])
    if act == "pdelok":
        p = next((x for x in core._db_people(adapter) if x["uid"] == arg(2)), None)
        if p and p["role"] == "adm" and len(adapter.admins) <= 1:  # иначе бот остался бы без админа и открылся всем
            return "⛔ Нельзя убрать последнего администратора.", [[("← Назад", f"m:pset:{p['uid']}")]]
        if p:
            core._remove_person(adapter, p["uid"], p["name"])
        text, rows = core.people_view(adapter)
        return ("🗑 Убран.\n\n" if p else "") + text, rows
    return "Выберите действие:", core.menu_for(adapter, "")
