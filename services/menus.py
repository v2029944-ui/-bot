"""Меню, правила и текстовые диалоги."""

import command_context
import comparison
import core
import dialog_state
import editor_rules
import ops
import post_footer
import spaces


def input_snapshot(adapter, uid):
    key = (adapter.name, str(uid))
    state = {}
    for name in ("WAITING", "WAITING_EDIT", "WAITING_REJECT"):
        value = getattr(core, name).get(key)
        if value is not None:
            state[name] = value
    if key in core.WAITING_RULE:
        state["rule"] = True
    if key in post_footer.WAITING:
        state["footer"] = post_footer.WAITING[key]
    if (spaces.active_id(), str(uid)) in spaces.WAITING_MEMBER:
        state["member"] = True
    if spaces.INSTALLED and not spaces.active_id():
        if str(uid) in spaces.WAITING_CREATE:
            state["create"] = True
        settings = spaces.settings_state().get(key)
        if settings and settings.get("waiting"):
            state["settings"] = settings
    return state


def input_rows(adapter, uid, renew=True):
    import json
    import secrets
    snapshot = input_snapshot(adapter, uid)
    if not snapshot:
        return None
    pending = dialog_state.PendingMap(core.db, "input_cancel")
    key = (adapter.name, str(uid))
    serialized = json.dumps(snapshot, sort_keys=True)
    saved = pending.get(key)
    if not renew and saved and saved["snapshot"] == serialized:
        nonce = saved["nonce"]
    else:
        nonce = secrets.token_urlsafe(12)
        pending[key] = {"nonce": nonce, "snapshot": serialized}
    return [[("↩️ Отмена", f"m:cancel:{nonce}")]]


def cancel_input(adapter, uid, payload):
    import json
    key = (adapter.name, str(uid))
    saved = dialog_state.PendingMap(core.db, "input_cancel").get(key)
    state = input_snapshot(adapter, uid)
    if (not saved or payload != "m:cancel:" + saved["nonce"] or not state
            or saved["snapshot"] != json.dumps(state, sort_keys=True)):
        return "Эта кнопка отмены устарела. Используйте кнопку под текущим запросом.", input_rows(adapter, uid, renew=False)
    dialog_state.PendingMap(core.db, "input_cancel").pop(key, None)
    for name in ("WAITING", "WAITING_EDIT", "WAITING_REJECT"):
        getattr(core, name).pop(key, None)
    core.WAITING_RULE.discard(key)
    post_footer.WAITING.pop(key, None)
    spaces.WAITING_MEMBER.discard((spaces.active_id(), str(uid)))
    if spaces.INSTALLED and not spaces.active_id():
        spaces.WAITING_CREATE.discard(str(uid))
        spaces.settings_state().pop(key, None)
    for name in ("WAITING_EDIT", "WAITING_REJECT", "WAITING"):
        pending = state.get(name)
        if pending is not None:
            did, attempt = pending if isinstance(pending, (tuple, list)) else (pending, 1)
            return "Ввод отменён. Пост остался на проверке.", [[("📨 Открыть пост", f"d:show:{did}:{attempt}")]]
    if "footer" in state:
        return post_footer.view()
    if "rule" in state:
        return core.rules_view()
    if "member" in state:
        return core.people_view(adapter)
    if "settings" in state:
        return spaces.settings_card(state["settings"]["sid"])
    if "create" in state:
        return spaces.view()
    return "Ввод отменён.", core.menu_for(adapter, uid)


def rules_command(adapter: core.Adapter, user_id, text: str) -> str | None:
    """/rules, /rule_add, /rule_del — только для согласующих. None = это не наша команда."""
    cmd, _, arg = text.strip().partition(" ")
    cmd, arg = cmd.split("@")[0].lower(), arg.strip()
    if cmd not in ("/rules", "/rule_add", "/rule_del") or not core.is_admin(adapter, user_id):
        return None
    if cmd == "/rules":
        return core.rules_view()[0]
    if cmd == "/rule_add":
        if not arg:
            return "Нажмите «Добавить правило» и отправьте текст."
        return editor_rules.add(arg)
    if not arg.isdigit():
        return "Удалите правило кнопкой 🗑 с его номером в списке."
    if len(arg) > 9 or int(arg) < 1:
        return "Правила с таким номером нет."
    def remove():
        row = core.db.execute("SELECT id FROM rules ORDER BY id LIMIT 1 OFFSET ?", (int(arg) - 1,)).fetchone()
        if not row:
            return "Правила с таким номером нет."
        core.db.execute("DELETE FROM rules WHERE id=?", (row["id"],))
        return "🗑 Правило удалено."
    return command_context.mutate("rule_delete", remove)



def rules_view(page=0) -> tuple[str, list]:
    total = core.db.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
    page = min(max(0, page), max(0, (total - 1) // 40))
    offset = page * 40
    rows = list(reversed(core.db.execute("SELECT id,text FROM rules ORDER BY id DESC LIMIT 40 OFFSET ?", (offset,)).fetchall()))
    numbered = list(enumerate(rows, total - offset - len(rows) + 1))
    body = "\n".join(f"{n}. {r['text']}" for n, r in numbered) or "Пока пусто."
    if len(body.encode("utf-16-le")) // 2 > 3500:
        body = body.encode("utf-16-le")[:3500 * 2].decode("utf-16-le", errors="ignore") + "\n…"
    text = "📝 Правила стиля:\n" + body
    text += "\n\nВ генерации активны последние 30 правил в пределах 12 000 знаков (до 1000 на правило)."
    if total > len(rows):
        text += f"\n\nСтраница {page + 1}. Показаны {len(rows)} из {total}."
    if rows:
        text += "\n\nНажмите 🗑 с номером, чтобы удалить правило."
    dels = [(f"🗑 {n}", f"m:del:{r['id']}" + (f":{page}" if page else "")) for n, r in numbered]
    buttons = [dels[i:i + 4] for i in range(0, len(dels), 4)]
    nav = []
    if page:
        nav.append(("Новые →", f"m:rules:{page - 1}"))
    if offset + 40 < total:
        nav.append(("← Более ранние", f"m:rules:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([("➕ Добавить правило", "m:add")])
    return text, buttons


async def on_menu(adapter: core.Adapter, user_id, payload: str) -> tuple[str, list | None]:
    """Нажатие кнопки меню (payload вида m:rules / m:add / m:del:5). Возвращает (текст, кнопки)."""
    uid = str(user_id)
    if not core.is_member(adapter, uid):
        return "⛔ У вас нет прав.", None
    if payload == "m:admin" and not core.is_admin(adapter, uid):
        return "⛔ Меню доступно только администратору.", None
    if payload == "m:cancel" or payload.startswith("m:cancel:"):
        return cancel_input(adapter, uid, payload)
    if not payload.startswith(("m:compare", "m:models_")):
        comparison.picker_state().pop((adapter.name, uid), None)
    dialog_state.PendingMap(core.db, "input_cancel").pop((adapter.name, uid), None)
    for waiting in (core.WAITING, core.WAITING_EDIT, core.WAITING_REJECT):  # любой переход по меню отменяет ожидание текста
        waiting.pop((adapter.name, uid), None)
    core.WAITING_RULE.discard((adapter.name, uid))
    post_footer.WAITING.pop((adapter.name, uid), None)
    if spaces.INSTALLED and not payload.startswith("m:space_save:"):
        spaces.settings_state().pop((adapter.name, uid), None)
    spaces.WAITING_CREATE.discard(uid)
    spaces.WAITING_MEMBER.discard((spaces.active_id(), uid))
    confirmation = dialog_state.PendingMap(core.db, "people_confirmation")
    key = (adapter.name, uid)
    parts = payload.split(":")
    if len(parts) > 1 and parts[1] in ("proleok", "pdelok"):
        if not core.is_admin(adapter, uid):
            return "⛔ Людей добавляет только администратор.", None
        saved = confirmation.pop(key, None)
        expected = saved.get("payload") if isinstance(saved, dict) else None
        target = parts[3] if parts[1] == "proleok" and len(parts) == 5 else parts[2] if parts[1] == "pdelok" and len(parts) == 4 else ""
        person = core._person(adapter, target)
        revision = [person["role"], person["created"]] if person else None
        if payload != expected or revision != (saved or {}).get("revision"):
            return "Это подтверждение больше не действует. Откройте человека и подтвердите действие заново.", None
        parts = parts[:-1]
    else:
        confirmation.pop(key, None)
    act = parts[1] if len(parts) > 1 else "menu"
    if act in ("rules", "del", "add") and not core.is_admin(adapter, uid):
        return "⛔ Настройки меняет только администратор.", None
    if act in ("member", "space_member", "inv", "invn"):  # старые кнопки ведут в действующее добавление
        if not core.is_admin(adapter, uid):
            return "⛔ Людей добавляет только администратор пространства.", None
        spaces.WAITING_MEMBER.add((spaces.active_id(), uid))
        return "Отправьте ID человека в MAX. Он будет добавлен как участник.", input_rows(adapter, uid)
    if act in ("space", "space_edit", "space_save"):
        return await spaces.settings_menu(adapter, uid, payload, parts)
    if act in ("spaces", "space_add"):
        if spaces.active_id() != 0 or not spaces.global_admin(uid):
            return "⛔ Пространства создаёт главный администратор.", None
        if act == "space_add":
            spaces.WAITING_CREATE.add(uid)
            return spaces.INPUT_HELP, input_rows(adapter, uid)
        page = int(parts[2]) if len(parts) == 3 and parts[2].isdigit() and len(parts[2]) < 8 else 0
        return spaces.view(page)
    if spaces.active_id() and act in ("upd", "updgo"):
        return "Эта операция доступна главному администратору в основном пространстве.", None
    if act in ("upd", "updgo") and not core.is_global_admin(adapter, uid):
        return "⛔ Обновление запускает только администратор из MAX_ADMINS (главный).", None
    if act in ("compare", "compare_toggle", "compare_set", "compare_refresh", "compare_pick") or act.startswith("models_"):
        if not core.is_admin(adapter, uid):
            return "⛔ Настройка доступна только администратору.", None
        if act == "compare_refresh":
            return await comparison.menu(refresh=True, adapter=adapter, uid=uid)
        if act == "compare_toggle":
            text, rows = await comparison.menu(adapter=adapter, uid=uid)
            return "Эта кнопка устарела. Используйте новую кнопку ниже.\n\n" + text, rows
        if act == "compare_set":
            if len(parts) != 3 or parts[2] not in ("0", "1"):
                return "Неизвестное состояние сравнения.", None
            return await comparison.menu(enabled=parts[2] == "1", adapter=adapter, uid=uid)
        return await comparison.menu(adapter=adapter, uid=uid, action=payload)
    if act.startswith("footer"):
        if not core.is_admin(adapter, uid):
            return "⛔ Настройка доступна только администратору.", None
        field = act.removeprefix("footer_")
        if act == "footer_toggle":
            text, rows = post_footer.view()
            return "Эта кнопка устарела. Используйте новую кнопку ниже.\n\n" + text, rows
        if act == "footer_set":
            if len(parts) != 3 or parts[2] not in ("0", "1"):
                return "Неизвестное состояние ссылки.", None
            c = post_footer.config()
            enabled = parts[2] == "1"
            if enabled and not (c["max"] or c["tg"]):
                return "Сначала задайте хотя бы один адрес.", post_footer.view()[1]
            c["enabled"] = enabled
            post_footer.save(c)
        elif field in ("text", "max", "tg", "marker"):
            post_footer.WAITING[(adapter.name, uid)] = field
            if field == "marker":
                return "Пришлите маркер без пробелов (до 32 знаков), например {bot}.", input_rows(adapter, uid)
            return ("Пришлите текст ссылки (до 80 знаков). " if field == "text" else
                    "Пришлите HTTPS-адрес бота или «-», чтобы убрать ссылку для этого канала."), input_rows(adapter, uid)
        return post_footer.view()
    if act == "admin":
        return "Меню администратора:", core.menu_for(adapter, uid)
    if act == "pending":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() and len(parts[2]) < 8 else 0
        return core.pending_view(adapter, page, uid)
    if act in ("inbox", "inretry"):
        if not core.is_admin(adapter, uid):
            return "⛔ Очередь доступна только администратору.", None
        import max_queue
        if act == "inretry" and len(parts) == 3:
            queue = spaces.queue_db()
            if spaces.active_id() or not core.is_global_admin(adapter, uid):
                queue.execute("UPDATE max_inbox SET state='pending',attempts=0,next_try=0 WHERE event_key=? AND state='failed' AND space_id=?", (parts[2], spaces.active_id()))
            else:
                queue.execute("UPDATE max_inbox SET state='pending',attempts=0,next_try=0 WHERE event_key=? AND state='failed'", (parts[2],))
            queue.commit()
        return max_queue.view(all_spaces=core.is_global_admin(adapter, uid))
    if act == "rules":
        page = int(parts[2]) if len(parts) == 3 and parts[2].isdigit() and len(parts[2]) < 8 else 0
        return core.rules_view(page)
    if act == "del" and len(parts) > 2 and parts[2].isdigit():
        def remove():
            core.db.execute("DELETE FROM rules WHERE id=?", (int(parts[2]),))
            return "removed"
        command_context.mutate("rule_delete_button", remove)
        page = int(parts[3]) if len(parts) == 4 and parts[3].isdigit() and len(parts[3]) < 8 else 0
        return core.rules_view(page)
    if act == "notif":
        return ops.notif_view(adapter, uid)
    if act == "nset":
        mode = parts[2] if len(parts) > 2 else ""
        if mode in ops.MODES:
            ops.set_mode(adapter, uid, mode)
            await ops.flush_deferred(adapter=adapter, uid=uid)  # сменил режим — придержанное придёт сразу
        return ops.notif_view(adapter, uid, saved=mode in ops.MODES)
    if act == "status":
        if not core.can_status(adapter, uid):
            return "⛔ Статус показывается администратору.", None
        return await core.status_text(adapter, include_global=core.is_global_admin(adapter, uid)), None
    if act in core.PEOPLE_ACTS:
        if not core.is_admin(adapter, uid):
            return "⛔ Людей добавляет только администратор.", None
        return await core.people_menu(adapter, act, parts, uid)
    if act == "add":
        core.WAITING.pop((adapter.name, uid), None)
        core.WAITING_EDIT.pop((adapter.name, uid), None)
        core.WAITING_RULE.add((adapter.name, uid))
        return "✏️ Напишите текст нового правила одним сообщением.", input_rows(adapter, uid)
    return "Выберите действие:", core.menu_for(adapter, uid)


def pending_view(adapter, page=0, uid=None):
    from datetime import datetime
    drafts = [dict(r) for r in core.db.execute("SELECT * FROM drafts WHERE status='pending' ORDER BY created,id")
              if core._approval_adapter(dict(r), adapter) is adapter
              and uid is not None and core.can_manage_draft(adapter, uid, dict(r))]
    total = len(drafts)
    page = min(max(0, page), max(0, (total - 1) // 8))
    lines = [f"📋 На проверке: {total}. Страница {page + 1}."]
    rows = []
    for d in drafts[page * 8:(page + 1) * 8]:
        date = datetime.fromtimestamp(d["created"], ops._tz()).strftime("%d.%m %H:%M")
        name = (d["author_name"] or "Сотрудник")[:40]
        snippet = " ".join((d["post"] or d["source_text"]).split())[:120]
        partial = " · публикация не завершена" if d["published_to"] else ""
        lines.append(f"\n№{d['id']} · {name} · {date}{partial}\n{snippet}")
        rows.append([(f"📨 Открыть №{d['id']}", f"d:show:{d['id']}:{d['attempts']}")])
        if d["published_to"] or d.get("publish_errors") != "{}":
            rows.append([(f"📊 Публикация №{d['id']}", f"d:pubstatus:{d['id']}:{d['attempts']}")])
    nav = []
    if page:
        nav.append(("← Назад", f"m:pending:{page - 1}"))
    if (page + 1) * 8 < total:
        nav.append(("Далее →", f"m:pending:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([("🔄 Обновить список", f"m:pending:{page}")])
    return "\n".join(lines), rows


async def on_text(adapter: core.Adapter, user_id, text: str) -> bool:
    """Текст от согласующего: кнопки нижнего меню, текст нового правила, комментарий к посту, старые команды.
    True = сообщение обработано."""
    uid, t = str(user_id), text.strip()
    if spaces.INSTALLED and await spaces.settings_input(adapter, uid, t):
        return True
    if spaces.INSTALLED and await spaces.create_input(adapter, uid, t):
        return True
    if await spaces.member_input(adapter, uid, t):
        return True
    if not core.is_member(adapter, uid):
        return False
    if t in ("/menu", "/admin", "/myspace"):
        action = "m:admin" if core.is_admin(adapter, uid) else "m:menu"
        message, rows = await core.on_menu(adapter, uid, action)
        await adapter.send_menu(uid, message, rows)
        return True
    key = (adapter.name, uid)
    if t == "/cancel":
        dialog_state.PendingMap(core.db, "people_confirmation").pop(key, None)
    if key in post_footer.WAITING:
        if not core.is_admin(adapter, uid):
            post_footer.WAITING.pop(key, None)
            await adapter.notify(uid, "⛔ Настройка доступна только администратору.")
            return True
        if t == "/cancel":
            post_footer.WAITING.pop(key, None)
            await adapter.send_menu(uid, *post_footer.view())
            return True
        field = post_footer.WAITING[key]
        try:
            value = post_footer.validate(field, t)
        except ValueError as e:
            await adapter.notify(uid, core.err(e))
            return True
        c = post_footer.config()
        c[field] = value
        post_footer.save(c)
        post_footer.WAITING.pop(key, None)
        await adapter.send_menu(uid, *post_footer.view())
        return True
    if t in core.LABELS:
        msg, rows = await core.on_menu(adapter, uid, core.LABELS[t])
        await adapter.send_menu(uid, msg, rows)
        return True
    cmd = t.split(maxsplit=1)[0].split("@")[0].lower() if t.startswith("/") else ""
    if key in core.WAITING_REJECT and cmd not in ("/rules", "/rule_add", "/rule_del", "/status"):
        if cmd == "/cancel":
            core.WAITING_REJECT.pop(key, None)
            await adapter.notify(uid, "Отклонение отменено. Пост остался на проверке.")
            return True
        if cmd or not t or len(t.encode("utf-16-le")) // 2 > 1000:
            await adapter.notify(uid, "Пришлите причину текстом до 1000 знаков или нажмите «Отмена».")
            return True
        did, attempt = core.WAITING_REJECT.pop(key)
        _, message = await core.reject_post(adapter, uid, did, attempt, t)
        await adapter.notify(uid, message)
        return True
    if cmd == "/status":  # ни при каких условиях не должна уходить дальше как комментарий к посту
        await adapter.notify(uid, await core.status_text(adapter, include_global=core.is_global_admin(adapter, uid)) if core.can_status(adapter, uid)
                             else "⛔ Статус показывается администратору.")
        return True
    if cmd and cmd not in core.SAFE_CMDS and (key in core.WAITING_EDIT or key in core.WAITING_RULE or key in core.WAITING):
        # неизвестная команда не должна стать ни текстом поста, ни правилом, ни комментарием для нейросети
        await adapter.notify(uid, "Неизвестная команда. Пришлите текст или нажмите «Отмена».")
        return True
    if key in core.WAITING_EDIT and cmd not in ("/rules", "/rule_add", "/rule_del", "/status"):
        did, attempt = core.WAITING_EDIT[key]
        if cmd == "/cancel":
            core.WAITING_EDIT.pop(key, None)
            await adapter.notify(uid, "Отменено. Кнопки под постом работают как раньше.")
            return True
        keep, msg = await core._apply_edit(adapter, uid, did, attempt, t)
        if not keep:
            core.WAITING_EDIT.pop(key, None)
        await adapter.notify(uid, msg)
        return True
    if key in core.WAITING_RULE and not core.is_admin(adapter, uid):
        core.WAITING_RULE.discard(key)
        await adapter.notify(uid, "⛔ Настройки меняет только администратор.")
        return True
    if key in core.WAITING_RULE and cmd not in ("/rules", "/rule_add", "/rule_del", "/status"):
        core.WAITING_RULE.discard(key)
        if t.lower().startswith("/cancel"):
            await adapter.notify(uid, "Отменено.")
            return True
        result = editor_rules.add(t)
        if result.startswith("Пришлите"):
            core.WAITING_RULE.add(key)
        msg, rows = core.rules_view()
        await adapter.send_menu(uid, result + "\n\n" + msg, rows)
        return True
    if t.lower().startswith("/cancel") and await core.on_comment(adapter, uid, t):
        return True
    reply = core.rules_command(adapter, uid, t)  # команды важнее ожидания комментария: не улетают в LLM
    if reply:
        await adapter.send_menu(uid, reply, core.rules_view()[1])
        if key in core.WAITING:
            await adapter.notify(uid, "✏️ Жду комментарий к посту.")
        return True
    if await core.on_comment(adapter, uid, t):
        return True
    return False
