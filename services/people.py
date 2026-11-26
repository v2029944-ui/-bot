"""Роли, заявки и участники."""
import secrets
import time

import core
import dialog_state
import ops
import spaces


def is_admin(adapter: core.Adapter, uid) -> bool:
    return str(uid) in (getattr(adapter, "admins", None) or [])


def is_global_admin(adapter, uid):
    if adapter.publish_only or spaces.active_id():
        return False
    root = spaces.root().adapters.get("max") if spaces.INSTALLED else adapter
    return root is adapter and str(uid) in getattr(root, "global_admins", core._base_admins(root))


def is_member(adapter, uid):
    if adapter.publish_only:
        return False
    uid = str(uid)
    if spaces.INSTALLED and spaces.active_id() == 0:
        if spaces.root().db.execute("SELECT 1 FROM workspaces WHERE owner=?", (uid,)).fetchone() and not is_admin(adapter, uid):
            return False
    return is_admin(adapter, uid) or adapter.allowed is None or uid in adapter.allowed


def can_manage_draft(adapter, uid, draft):
    # Resolve against the active workspace's adapter, never a caller-supplied foreign adapter.
    if not draft or core.ADAPTERS.get(adapter.name) is not adapter or not is_member(adapter, uid):
        return False
    if spaces.INSTALLED and getattr(adapter, "workspace_id", None) != spaces.active_id():
        return False
    if spaces.in_management_only():
        return False
    return adapter is not None and is_admin(adapter, uid)


def draft_recipients(draft, adapter):
    # Route automatic review to the designated supervisors of the whole bot.
    reviewers = spaces.global_admin_ids() if adapter.name == "max" else []
    recipients = reviewers or getattr(adapter, "admins", [])
    return [uid for uid in recipients if can_manage_draft(adapter, uid, draft)]


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


def _normalized_person(adapter, row):
    return {**dict(row), "role": "adm" if row["uid"] in core._base_admins(adapter) else
            "member" if row["role"] in ("emp", "apr") else row["role"]}


def _db_people(adapter: core.Adapter) -> list[dict]:
    rows = core.db.execute("SELECT * FROM people WHERE platform=? ORDER BY created", (adapter.name,)).fetchall()
    return [_normalized_person(adapter, r) for r in rows]


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
    members = set(map(str, [*base_apr, *base_emp])) | {p["uid"] for p in people if p["role"] == "member"} | set(adm)
    adapter.admins = adm
    adapter.approvers = adm[:]  # legacy field; never use it as draft authorization
    adapter.allowed = members if (base_emp or members or core.requests_enabled(adapter)) else None


def role_of(adapter: core.Adapter, uid) -> str | None:
    uid = str(uid)
    if uid in (getattr(adapter, "admins", None) or []):
        return "adm"
    if adapter.allowed is not None and uid in adapter.allowed:
        return "member"
    return None


def _add_person(adapter: core.Adapter, uid: str, name: str, role: str):
    role = "member" if role in ("emp", "apr") else role
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
    return [[("✅ Добавить участника", f"m:acc:member:{uid}")],
            [("🚫 Отклонить", f"m:acc:no:{uid}")]]


async def unknown_user(adapter: core.Adapter, uid, name: str, username: str | None = None) -> bool:
    """Незнакомый человек написал боту: создаём заявку и спрашиваем администраторов.
    True = заявка принята (или уже ждёт решения). Незнакомцу бот ничего не отвечает."""
    if not core.requests_enabled(adapter):
        return False
    uid = str(uid)
    r = core.db.execute("SELECT status, created FROM requests WHERE platform=? AND uid=?", (adapter.name, uid)).fetchone()
    if r and time.time() - r["created"] < core.REQ_TTL:
        return r["status"] == "pending"
    core.db.execute("DELETE FROM requests WHERE status='pending' AND created < ?", (time.time() - core.REQ_TTL,))  # протухшие
    n = core.db.execute("SELECT COUNT(*) FROM requests WHERE platform=? AND status='pending'", (adapter.name,)).fetchone()[0]
    if n >= core.MAX_PENDING_REQUESTS:
        core.log.warning("слишком много заявок на доступ, новая от %s пропущена", uid)
        return False
    core.db.execute("INSERT OR REPLACE INTO requests(platform,uid,name,username,status,created) VALUES(?,?,?,?,?,?)",
               (adapter.name, uid, name, username, "pending", time.time()))
    core.db.commit()
    await core._tell_admins(adapter, f"🔔 {core._who(name, username, uid)} просит доступ к боту.\nДобавить участника?",
                       core._request_rows(uid))
    return True


def menu_for(adapter: core.Adapter, uid) -> list:
    if not core.is_admin(adapter, uid):
        return []
    if spaces.INSTALLED and core.is_global_admin(adapter, uid):
        rows = [[("🏢 Рабочие пространства", "m:spaces")],
                [("📊 Общий статус", "m:status"), ("📥 Общая очередь", "m:inbox")],
                [("🔄 Обновить", "m:upd"), ("⚙️ Режим №0", "m:root_mode")]]
        if not spaces.management_only():
            rows.append([("📂 Рабочее меню №0", "m:root_workspace")])
        return rows
    return workspace_menu(adapter, uid)


def workspace_menu(adapter, uid):
    rows = [list(r) for r in core.MENU]
    rows.append([("📋 На проверке", "m:pending")])
    if spaces.INSTALLED and spaces.active_id() and spaces.global_admin(uid):
        rows.append([("🏠 Главное управление", "w:0:m:admin")])
    if spaces.INSTALLED and not spaces.active_id() and core.is_global_admin(adapter, uid):
        rows.append([("🏠 Управление ботом", "m:admin")])
    rows.append([("👥 Люди", "m:people"), ("📊 Статус", "m:status")])
    if not spaces.INSTALLED and core.is_global_admin(adapter, uid):
        rows.append([("🔄 Обновить", "m:upd")])
    rows.append([("📥 Обработка материалов", "m:inbox")])
    rows.append([("🔗 Ссылка в постах", "m:footer")])
    rows.append([("🤖 Модели генерации", "m:compare")])
    return rows


def people_view(adapter: core.Adapter, page: int = 0) -> tuple[str, list]:
    total = core.db.execute("SELECT COUNT(*) FROM people WHERE platform=?", (adapter.name,)).fetchone()[0]
    page = max(0, min(page, max(0, (total - 1) // 12)))
    people = [dict(r) for r in core.db.execute("SELECT * FROM people WHERE platform=? ORDER BY created,uid LIMIT 12 OFFSET ?", (adapter.name, page * 12))]
    base_apr, base_emp = core._base(adapter)
    base_adm = core._base_admins(adapter)
    lines = [f"👥 Люди · страница {page + 1} из {max(1, (total + 11) // 12)}", "👑 Администраторы / 👤 Участники"]
    if page == 0:
        initial = list(dict.fromkeys([*base_adm, *base_apr, *base_emp]))
        placeholders = ",".join("?" for _ in initial)
        stored = {r[0] for r in core.db.execute(f"SELECT uid FROM people WHERE platform=? AND uid IN ({placeholders})",
                                               (adapter.name, *initial))} if initial else set()
        for uid in initial:
            if uid not in stored:
                icon = "👑" if uid in base_adm else "👤"
                lines.append(f"{icon} Без имени (id {uid}) — из настроек")
    for p in people:
        role = "adm" if p["uid"] in base_adm else "member" if p["role"] in ("emp", "apr") else p["role"]
        lines.append(f"{core.ROLE_ICON.get(role, '👤')} {(p['name'] or 'Без имени')[:60]} (id {p['uid']})")
    suffix = "\n\nНажмите ⚙, чтобы сменить роль или убрать человека. Защищённый владелец меняется в настройках пространства, главные администраторы — в .env."
    # Reserve the footer and budget UTF-16 consistently with the rest of the UI.
    body = "\n".join(lines).encode("utf-16-le")[:6000].decode("utf-16-le", errors="ignore")
    btns = [(f"⚙ {(p['name'] or p['uid'])[:18]}", f"m:pset:{p['uid']}") for p in people]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    nav = []
    if page:
        nav.append(("← Назад", f"m:people:{page - 1}"))
    if (page + 1) * 12 < total:
        nav.append(("Далее →", f"m:people:{page + 1}"))
    if nav:
        rows.append(nav)
    n = core.db.execute("SELECT COUNT(*) FROM requests WHERE platform=? AND status='pending'", (adapter.name,)).fetchone()[0]
    if n:
        rows.append([(f"📬 Заявки ({n})", "m:reqs")])
    rows.append([("➕ Добавить человека", "m:member")])
    return body + suffix, rows


def _people_confirmation(adapter, actor, payload, person):
    payload += ":" + secrets.token_urlsafe(12)
    dialog_state.PendingMap(core.db, "people_confirmation")[(adapter.name, str(actor))] = {
        "payload": payload, "revision": [person["role"], person["created"]]}
    return payload


def _person(adapter: core.Adapter, uid: str) -> dict | None:
    row = core.db.execute("SELECT * FROM people WHERE platform=? AND uid=?", (adapter.name, uid)).fetchone()
    return _normalized_person(adapter, row) if row else None


def reqs_view(adapter: core.Adapter) -> tuple[str, list]:
    rs = core.db.execute("SELECT * FROM requests WHERE platform=? AND status='pending' ORDER BY created LIMIT 5",
                    (adapter.name,)).fetchall()
    if not rs:
        return "Новых заявок нет.", [[("👥 Люди", "m:people")]]
    text = "📬 Заявки на доступ:\n" + "\n".join(f"• {core._who(r['name'], r['username'], r['uid'])}" for r in rs)
    rows = []
    for r in rs:
        short = (r["name"] or r["uid"])[:10]
        rows.append([(f"✅ {short}", f"m:acc:member:{r['uid']}"),
                     (f"🚫 {short}", f"m:acc:no:{r['uid']}")])
    return text + "\n\n✅ — добавить участника, 🚫 — отклонить.", rows


async def people_menu(adapter: core.Adapter, act: str, parts: list[str], uid: str = "") -> tuple[str, list | None]:
    actor = uid
    def arg(i):
        return parts[i] if len(parts) > i else ""
    if (spaces.INSTALLED and not spaces.active_id() and spaces.configured_root(spaces.root().db)
            and act in ("pset", "prole", "proleok", "pdel", "pdelok")):
        target = arg(3) if act in ("prole", "proleok") else arg(2)
        if target in core._base_admins(adapter):
            return "Это защищённый владелец или главный администратор. Владелец меняется в настройках пространства, MAX_ADMINS — в .env.", [[("← Люди", "m:people")]]
    if act == "people":
        page = int(arg(2)) if arg(2).isascii() and arg(2).isdigit() and len(arg(2)) < 8 else 0
        return core.people_view(adapter, page)
    if act == "reqs":
        return core.reqs_view(adapter)
    if act == "acc":
        role, uid = arg(2), arg(3)
        role = "member" if role in ("emp", "apr") else role
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
            await adapter.send_menu(uid, core.ACCESS_WELCOME[role], [[("Открыть меню", "m:menu")]])
        except Exception:
            core.log.warning("не удалось сообщить %s о доступе", uid, exc_info=True)
        return f"✅ {who} добавлен: {core.ROLE_NAME[role]}.", None
    if act in ("pause", "pauseset"):
        return "Пауза больше не поддерживается. Бот принимает материалы.", None
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
                  f"m:prole:{r}:{p['uid']}") for r in ("member", "adm")],
                [("🗑 Убрать", f"m:pdel:{p['uid']}"), ("← Назад", "m:people")]]
        return f"{who} (id {p['uid']}) — сейчас: {core.ROLE_NAME[p['role']]}.\nВыберите новую роль:", rows
    if act in ("prole", "proleok"):
        role, uid = arg(2), arg(3)
        role = "member" if role in ("emp", "apr") else role
        p = core._person(adapter, uid)
        if not p or role not in core.ROLE_NAME:
            return core.people_view(adapter)
        who = p["name"] or uid
        if p["role"] == role:
            return f"{who} уже {core.ROLE_NAME[role]}.", [[("← Назад", f"m:pset:{uid}")]]
        if p["role"] == "adm" and len(adapter.admins) <= 1:
            return "⛔ Нельзя снять права с последнего администратора.", [[("← Назад", f"m:pset:{uid}")]]
        if act == "prole":
            warn = ("\nАдминистратор добавляет и убирает людей, управляет настройками и всеми постами своего пространства."
                    if role == "adm" else "")
            return (f"Сменить роль: {who} — {core.ROLE_NAME[p['role']]} → {core.ROLE_NAME[role]}?{warn}",
                    [[("Да, сменить", _people_confirmation(adapter, actor, f"m:proleok:{role}:{uid}", p)), ("Отмена", f"m:pset:{uid}")]])
        core._add_person(adapter, uid, p["name"], role)
        try:
            await adapter.send_menu(uid, f"Ваша роль изменена: {core.ROLE_NAME[role]}.", [[("Открыть меню", "m:menu")]])
        except Exception:
            core.log.warning("не удалось сообщить %s о новой роли", uid, exc_info=True)
        return f"✅ {who}: теперь {core.ROLE_NAME[role]}.", [[("← К списку", "m:people")]]
    if act == "pdel":
        p = core._person(adapter, arg(2))
        if not p:
            return core.people_view(adapter)
        if p["role"] == "adm" and len(adapter.admins) <= 1:
            return "⛔ Нельзя убрать последнего администратора.", [[("← Назад", f"m:pset:{p['uid']}")]]
        return (f"Убрать {p['name'] or p['uid']} из списка? Он перестанет присылать фото.",
                [[("Да, убрать", _people_confirmation(adapter, actor, f"m:pdelok:{p['uid']}", p)), ("Отмена", "m:people")]])
    if act == "pdelok":
        p = core._person(adapter, arg(2))
        if p and p["role"] == "adm" and len(adapter.admins) <= 1:  # иначе бот остался бы без админа и открылся всем
            return "⛔ Нельзя убрать последнего администратора.", [[("← Назад", f"m:pset:{p['uid']}")]]
        if p:
            core._remove_person(adapter, p["uid"], p["name"])
        text, rows = core.people_view(adapter)
        return ("🗑 Убран.\n\n" if p else "") + text, rows
    return "Выберите действие:", core.menu_for(adapter, "")
