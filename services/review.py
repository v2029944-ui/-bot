"""Правка, повторная генерация и отклонение."""
import json

import comparison
import core
import post_footer
from services import publication


def approval_rows(did: int, attempt: int):
    return [[("❌ Отклонить", f"d:del:{did}:{attempt}"),
             ("✅ Опубликовать", f"d:ok:{did}:{attempt}")],
            [("✏️ Изменить", f"d:change:{did}:{attempt}")]]


async def _apply_edit(adapter: core.Adapter, user_id, did: int, attempt: int, text: str) -> tuple[bool, str]:
    """Автор или администратор прислал свой текст: подставляем его как новый вариант
    и доставляем автору и действующему администратору с кнопками. Возвращает (ждать ли текст дальше, сообщение)."""
    text = text.strip()
    if not text:
        return True, "Пустой текст. Пришлите исправленный пост или нажмите «Отмена»."
    d = core.get(did)
    if d and not core.can_manage_draft(adapter, user_id, d):
        return False, "⛔ У вас нет прав на этот пост."
    if not d or d["status"] != "pending" or d["attempts"] != attempt:
        return False, "Этот вариант уже изменён или обработан, правка не применена."
    if core._parse_marks(d.get("published_to")):
        return False, core.PARTIAL_EDIT_BLOCKED
    footer = json.loads(d["footer"]) if d.get("footer") is not None else post_footer.config()
    try:
        text = post_footer.restore_copied_marker(text, d["post"], footer)
    except ValueError as e:
        return True, core.err(e)
    length = len(text.encode("utf-16-le")) // 2
    limit = min(core.MAX_POST_CHARS, 3900)
    if length > limit:
        return True, f"Слишком длинно ({length} ед. UTF-16, максимум {limit}). Сократите и пришлите снова."
    if not core._claim(did, "pending", "regenerating"):
        return False, core.BUSY
    core._set(did, post=text, footer=json.dumps(footer, ensure_ascii=False), attempts=d["attempts"] + 1, status="regenerating")
    sent, last = await core._send_draft(d, did, text, adapter, actor=str(user_id))
    if not sent:  # откат, как при пересоздании: сообщения с новыми кнопками нет
        core._set(did, post=d["post"], footer=d.get("footer"), attempts=d["attempts"], status="pending")
        return False, f"⚠️ Текст сохранить не удалось: {core.err(last)}"
    core._set(did, status="pending")
    return False, "✍️ Ваш текст заменил черновик. Проверьте пост выше и нажмите ✅ Опубликовать."


async def on_action(adapter: core.Adapter, user_id, action: str, did: int, attempt: int) -> tuple[bool, str]:
    """Нажатие кнопки автором или администратором. Возвращает (ок, текст ответа)."""
    if core.SHUTTING_DOWN and action != "show":  # идёт остановка: новую работу не начинаем, чтобы не оборвать на середине
        return False, "🔄 Бот перезапускается. Повторите нажатие через минуту: черновик сохранён."
    d = core.get(did)
    if d and not core.can_manage_draft(adapter, user_id, d):
        return False, "⛔ У вас нет прав на этот пост."
    if not d:
        return True, "Черновик не найден."
    if d["status"] in ("publishing", "regenerating"):
        return False, core.BUSY
    if d["status"] != "pending":
        return True, "Этот пост уже обработан."
    if action == "show":  # из напоминания: показываем актуальный вариант, даже если пост уже пересоздали
        await adapter.send_for_approval(str(user_id), did, d["photo"], d["post"], **core.media_kw(d))
        return False, ""
    if d["attempts"] != attempt:
        return True, "Это устаревший вариант. Используйте последнее сообщение с постом."
    options = json.loads(d.get("candidates") or "[]")
    if action.startswith("choose_"):
        index = action.removeprefix("choose_")
        if not index.isdigit() or int(index) >= len(options) or not options[int(index)].get("post"):
            return False, "Этот вариант недоступен."
        if not core._claim(did, "pending", "regenerating"):
            return False, core.BUSY
        chosen = options[int(index)]
        core._set(did, post=chosen["post"], selected_model=chosen["model"], candidates="[]", attempts=attempt + 1)
        sent, last = await core._send_draft(core.get(did), did, chosen["post"], adapter, actor=str(user_id))
        if not sent:
            core._set(did, post=d["post"], selected_model=d.get("selected_model", ""), candidates=d["candidates"], attempts=attempt, status="pending")
            return False, "Не удалось отправить выбранный вариант. Повторите выбор."
        core._set(did, status="pending")
        return True, ""
    comparison_actions = {"redo", "regen", "redoc", "change", "length", "back", "del", "delyes", "delno", "rejectphoto", "rejecttext", "rejectcustom"}
    if options and action not in comparison_actions and not action.startswith("len_"):
        return False, "Сначала выберите один из вариантов моделей."

    if action == "pubstatus":
        text, rows = core.publication_view(d, adapter)
        await adapter.send_menu(str(user_id), text, rows)
        return False, ""

    if action.startswith("seen_"):
        target = action.removeprefix("seen_")
        marks = core._parse_marks(d.get("published_to"))
        if marks.get(target) != "unknown":
            return False, "Для этого канала подтверждение не требуется."
        progress = json.loads(d.get("publish_progress") or "{}").get(target) or {}
        if progress.get("media") and not progress.get("text") and not progress.get("text_started"):
            return False, "В этом канале опубликовано только медиа, а текст точно не отправлен. Завершите отправку текста."
        marks[target] = "done"
        core._set(did, published_to=core._dump_marks(marks))
        if all(marks.get(t.name) == "done" for t in core.publication_targets(d, adapter)):
            await core.finish_publication(d, adapter, user_id)
            return False, ""
        text, rows = core.publication_view(core.get(did), adapter)
        await adapter.send_menu(str(user_id), text, rows)
        return False, ""

    if (action in ("redo", "regen", "redoc", "edit", "change", "length") or action.startswith("len_")) and core._parse_marks(d.get("published_to")):
        return False, core.PARTIAL_EDIT_BLOCKED

    if action in ("redo", "change", "length", "back"):
        key = (adapter.name, str(user_id))
        core.WAITING.pop(key, None)
        core.WAITING_EDIT.pop(key, None)
        core.WAITING_REJECT.pop(key, None)
        core.WAITING_RULE.discard(key)
        def button(label, act):
            return (label, f"d:{act}:{did}:{attempt}")
        if action == "redo":
            rows = [[button("🔁 Без изменений", "regen")],
                    [button("📏 Другой размер", "length"), button("✏️ С комментарием", "redoc")],
                    [button("↩️ Назад", "back")]]
            c = comparison.config()
            scope = "все выбранные модели" if c["enabled"] and len(c["models"]) > 1 else "одна модель"
            text = f"Перегенерировать пост №{did}: {scope}. Выберите способ."
        elif action == "change":
            rows = [[button("🔁 Заново", "redo"), button("✏️ С комментарием", "redoc")],
                    [button("✍️ Править самому", "edit"), button("📏 Длина поста", "length")],
                    [button("↩️ Назад", "back")]]
            text = f"Изменить пост №{did}:"
        elif action == "length":
            rows = [[button(f"{label} · {low}–{high} знаков", f"len_{name}")]
                    for name, (label, low, high) in core.POST_LENGTHS.items()]
            rows.append([button("↩️ Назад", "change")])
            current = core.POST_LENGTHS.get(d.get("post_length"), ("По правилам редакции",))[0]
            scope = "варианты всеми моделями" if options and comparison.config()["enabled"] else "один новый вариант"
            text = (f"Длина поста №{did}: {current}.\nВыберите длину — бот подготовит {scope}. "
                    "Размер примерный: если исходных фактов мало, текст будет короче.")
        elif options:
            await adapter.send_for_approval(str(user_id), did, d["photo"], d["post"], **core.media_kw(d))
            return False, ""
        else:
            rows = core.approval_rows(did, attempt)
            text = f"Пост №{did}:\n\n{d['post']}"
        await adapter.send_menu(str(user_id), text, rows)
        return False, ""

    if action.startswith("len_"):
        length = action.removeprefix("len_")
        if length not in core.POST_LENGTHS:
            return False, "Неизвестная длина поста."
        return await core._regenerate(adapter, user_id, did, post_length=length, attempt=attempt)

    if action == "ok":
        if not core._parse_marks(d.get("published_to")):
            core._set(did, publication_channels=json.dumps(
                core.publication_channels(core.publication_targets(d, adapter)), ensure_ascii=False))
        names = {"max": "MAX", "tg": "Telegram"}
        marks = core._parse_marks(d.get("published_to"))
        targets = [names.get(t.name, t.name) for t in core.publication_targets(d, adapter)
                   if marks.get(t.name) != "done"]
        preview = d["post"][:200] + ("…" if len(d["post"]) > 200 else "")
        channels = core.publication_channels(core.publication_targets(d, adapter))
        destination = ", ".join(f"{name}: {channel}" for name, channel in channels.items())
        token = publication.PROMPT_CHANNELS.set(channels)
        try:
            await adapter.send_menu(str(user_id),
                                    f"Опубликовать пост №{did} в {', '.join(targets)}?\n{destination}\n\n«{preview}»",
                                    [[("✅ Да, опубликовать", f"d:okyes:{did}:{attempt}")],
                                     [("↩️ Отмена", f"d:okno:{did}:{attempt}")]])
        finally:
            publication.PROMPT_CHANNELS.reset(token)
        return False, ""

    if action == "okno":
        return False, "Публикация отменена. Пост остался на согласовании."

    if action == "okyes":
        return await core._publish(adapter, user_id, did, attempt)

    if action == "okforce":  # «да, опубликовать повторно, хотя прошлая попытка оборвалась»
        return await core._publish(adapter, user_id, did, attempt, force=True)

    if action == "del":  # сначала спрашиваем подтверждение, ничего не удаляя
        preview = d["post"][:100] + ("…" if len(d["post"]) > 100 else "")
        rows = [[("📷 Нужны другие фото", f"d:rejectphoto:{did}:{attempt}")],
                [("📝 Нужно описание", f"d:rejecttext:{did}:{attempt}")],
                [("✍️ Своя причина", f"d:rejectcustom:{did}:{attempt}")],
                [("❌ Без причины", f"d:delyes:{did}:{attempt}"), ("↩️ Отмена", f"d:delno:{did}:{attempt}")]]
        await adapter.send_menu(str(user_id), f"Отклонить этот пост? Черновик и его файлы будут удалены без возможности восстановления.\n\n«{preview}»", rows)
        return False, ""

    if action == "delno":
        core.WAITING_REJECT.pop((adapter.name, str(user_id)), None)
        return True, "↩️ Действие отменено. Пост остался на согласовании."

    if action == "rejectcustom":
        key = (adapter.name, str(user_id))
        core.WAITING.pop(key, None)
        core.WAITING_EDIT.pop(key, None)
        core.WAITING_RULE.discard(key)
        core.WAITING_REJECT[key] = (did, attempt)
        return False, "Напишите причину отклонения (до 1000 знаков). Она будет отправлена автору."

    if action in ("delyes", "rejectphoto", "rejecttext"):
        reason = {"rejectphoto": "Нужны другие фотографии. Пожалуйста, отправьте материал заново.",
                  "rejecttext": "Добавьте описание выполненной работы и отправьте материал заново."}.get(action, "")
        return await core.reject_post(adapter, user_id, did, attempt, reason)

    if action == "regen":
        return await core._regenerate(adapter, user_id, did, attempt=attempt, compare=True)

    if action == "redoc":
        core.WAITING_REJECT.pop((adapter.name, str(user_id)), None)
        core.WAITING[(adapter.name, str(user_id))] = (did, attempt)
        core.WAITING_RULE.discard((adapter.name, str(user_id)))
        core.WAITING_EDIT.pop((adapter.name, str(user_id)), None)
        scope = " Будут пересозданы все варианты сравнения." if options and comparison.config()["enabled"] else ""
        return False, "✏️ Напишите одним сообщением, что изменить в посте." + scope

    if action == "edit":  # «Править самому»: следующее сообщение автора или администратора станет текстом поста
        key = (adapter.name, str(user_id))
        core.WAITING_REJECT.pop(key, None)
        core.WAITING.pop(key, None)
        core.WAITING_RULE.discard(key)
        core.WAITING_EDIT[key] = (did, attempt)
        return False, ("✍️ Пришлите исправленный текст поста одним сообщением. Я заменю им черновик, "
                       "а вы проверите и опубликуете.")


    return False, "Неизвестное действие."


async def reject_post(adapter, user_id, did, attempt, reason):
    d = core.get(did)
    if d and not core.can_manage_draft(adapter, user_id, d):
        return False, "⛔ У вас нет прав на этот пост."
    if not d or d["status"] != "pending" or d["attempts"] != attempt:
        return False, "Этот вариант уже изменён или обработан."
    if core._parse_marks(d.get("published_to")):
        return False, "Публикация уже началась. Завершите её или подтвердите результат в каналах; отклонение недоступно."
    if not core._claim(did, "pending", "deleted"):
        return False, core.BUSY
    core._set(did, rejection_reason=reason)
    core.WAITING_REJECT.pop((adapter.name, str(user_id)), None)
    for path in core.draft_files(d):
        core._unlink(path)
    core.clear_source_cache(d)
    author = core.ADAPTERS.get(d["platform"])
    text = f"❌ Материал «{d['source_text'][:100]}» отклонён." + (f"\nПричина: {reason}" if reason else "")
    # Причина — необходимая обратная связь, даже при тихом режиме сотрудников.
    try:
        if author and not author.publish_only:
            await author.notify(d["author_id"], text)
    except Exception:
        core.log.warning("причина отклонения не доставлена", exc_info=True)
        return True, "❌ Пост отклонён. ⚠️ Причину не удалось доставить автору; сообщите ему отдельно."
    return True, "❌ Пост отклонён."


async def _regenerate(adapter: core.Adapter, user_id, did: int, comment: str | None = None,
                      post_length: str | None = None, attempt: int | None = None, compare=False,
                      keep_input=False) -> tuple[bool, str]:
    return await core._guard(core._regenerate_inner(adapter, user_id, did, comment, post_length, attempt, compare, keep_input))


async def _regenerate_inner(adapter: core.Adapter, user_id, did: int, comment: str | None = None,
                            post_length: str | None = None, attempt: int | None = None, compare=False,
                            keep_input=False) -> tuple[bool, str]:
    d = core.get(did)
    if d and not core.can_manage_draft(adapter, user_id, d):
        return False, "⛔ У вас нет прав на этот пост."
    if not d:
        return True, "Черновик не найден."
    if d["status"] in ("published", "deleted", "expired"):
        return True, "Этот пост уже обработан."
    if attempt is not None and d["attempts"] != attempt:
        return False, "Это устаревший вариант. Используйте последнее сообщение с постом."
    if core._parse_marks(d.get("published_to")):
        return False, core.PARTIAL_EDIT_BLOCKED
    if not core._claim(did, "pending", "regenerating"):
        cur = core.get(did)
        if cur and cur["status"] in ("published", "deleted", "expired"):
            return True, "Этот пост уже обработан."
        return False, core.BUSY
    key = (adapter.name, str(user_id))
    if not keep_input:
        core.WAITING.pop(key, None)
    core.WAITING_EDIT.pop(key, None)
    core.WAITING_REJECT.pop(key, None)
    core.WAITING_RULE.discard(key)
    try:
        length = (d.get("post_length") or "") if post_length is None else post_length
        selected_model = comparison.request_model(d["selected_model"]) if d.get("selected_model") else ""
        model_token = comparison.MODEL.set(selected_model or None)
        try:
            candidates = json.loads(d.get("candidates") or "[]")
            generate = core.generate_options if compare or candidates else core.generate_post
            previous = {o["model"]: o["post"] for o in candidates if o.get("post")}
            extra = {"previous": previous} if generate is core.generate_options else {}
            post = await generate(d["photo"], d["source_text"], prev=d["post"], comment=comment,
                                       video=bool(d.get("video")), media=core.draft_media(d), post_length=length, **extra)
        finally:
            comparison.MODEL.reset(model_token)
    except Exception as e:
        core.log.exception("regenerate failed")
        core._set(did, status="pending")
        return False, f"⚠️ Не удалось пересоздать: {core.err(e)}"

    selection = comparison.draft_selection(post) if isinstance(post, comparison.Options) else {
        "candidates": "[]", "selected_model": selected_model}
    core._set(did, post=post, **selection,
         footer=None, post_length=length, attempts=d["attempts"] + 1)
    sent, last = await core._send_draft(d, did, post, adapter, actor=str(user_id))
    if not sent:  # откат: иначе в базе attempts=N+1, а сообщения с такими кнопками нет
        core._set(did, post=d["post"], candidates=d.get("candidates") or "[]", selected_model=d.get("selected_model", ""),
             footer=d.get("footer"), post_length=d.get("post_length") or "", attempts=d["attempts"], status="pending")
        return False, f"⚠️ Новый вариант готов, но не отправился: {core.err(last)}"
    core._set(did, status="pending")
    # новый вариант получает автор и администратор, начавший правку
    if adapter.name == "max":
        return True, ""  # новый пост уже доставлен с кнопками; служебный ответ после него не нужен
    return True, "🔁 Новый вариант ниже."


async def on_comment(adapter: core.Adapter, user_id, text: str) -> bool:
    """Текстовое сообщение от автора или администратора. True, если это был комментарий к черновику (сообщение обработано)."""
    key = (adapter.name, str(user_id))
    pending = core.WAITING.get(key)
    if pending is None:
        return False
    did, attempt = pending if isinstance(pending, tuple) else (pending, None)
    if text.strip().lower().startswith("/cancel"):
        core.WAITING.pop(key, None)
        await adapter.notify(str(user_id), "Отменено. Кнопки под постом работают как раньше.")
        return True
    await adapter.notify(str(user_id), "Переписываю с учётом комментария…")
    before = core.get(did)
    _, msg = await core._regenerate(adapter, user_id, did, comment=text, attempt=attempt, keep_input=True)
    core.WAITING.pop(key, None)
    if msg:
        await adapter.notify(str(user_id), msg)
    after = core.get(did)
    if core.is_admin(adapter, user_id) and before and after and after["attempts"] > before["attempts"]:
        rule = await core._learn(text, before["post"]) if core.env("LEARN_RULES", "1") != "0" else None
        if rule:
            await adapter.send_menu(str(user_id), f"🧠 Запомнил на будущее: «{rule}»", [[("📝 Правила стиля", "m:rules")]])
    return True
