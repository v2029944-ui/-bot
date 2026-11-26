"""Подтверждение и отправка в каналы."""
import json
import os
import time
from contextvars import ContextVar

import core
import post_footer
from tg import media_chat_id

EXPECTED_CHANNELS: ContextVar[dict | None] = ContextVar("confirmed_channels", default=None)
PROMPT_CHANNELS: ContextVar[dict | None] = ContextVar("prompt_channels", default=None)
CLOSURE_SNAPSHOT: ContextVar[dict | None] = ContextVar("closure_snapshot", default=None)


async def _publish(adapter: core.Adapter, user_id, did: int, attempt: int, force: bool = False,
                   *, text_only: bool = False) -> tuple[bool, str]:
    return await core._guard(core._publish_safe(adapter, user_id, did, attempt, force, text_only=text_only))


def publication_channels(targets):
    return {target.name: str(getattr(target, "channel", "")) for target in targets}


def publication_targets(d, adapter):
    if os.getenv("PUBLISH_TO_ALL") == "1":
        return list(core.ADAPTERS.values())
    source = core.ADAPTERS.get(d["platform"])
    targets = [source if source is not None and not source.publish_only else core._approval_adapter(d, adapter)]
    return [t for t in targets if t] + [a for a in core.ADAPTERS.values() if a.publish_only and a not in targets]


def _recover_telegram_mark(marks, progress):
    telegram = progress.get("tg") or {}
    if marks.get("tg") == "unknown" and telegram.get("media") and telegram.get("text"):
        marks["tg"] = "done"
        return True
    return False


def text_pending(mark, progress):
    return mark == "unknown" and progress.get("media") and not (progress.get("text") or progress.get("text_started"))


def text_unknown(mark, progress):
    return mark == "unknown" and progress.get("media") and progress.get("text_started") and not progress.get("text")


def _saved_channels(d):
    return json.loads(d.get("publication_channels") or "{}")


def _remaining_channels(channels, marks):
    return [kind for kind in dict.fromkeys((*channels, *marks)) if marks.get(kind) != "done"]


def publication_complete(d, adapter, marks):
    channels = _saved_channels(d) or core.publication_channels(core.publication_targets(d, adapter))
    return bool(channels or marks) and not _remaining_channels(channels, marks)


def closure_snapshot(d):
    return {key: d.get(key) for key in ("published_to", "publication_channels", "publish_progress")}


def publication_view(d, adapter, *, title=None):
    marks = core._parse_marks(d.get("published_to"))
    errors = json.loads(d.get("publish_errors") or "{}")
    progress = json.loads(d.get("publish_progress") or "{}")
    _recover_telegram_mark(marks, progress)
    names = {"max": "MAX", "tg": "Telegram"}
    lines = [title or f"Публикация поста №{d['id']}:"]
    rows = []
    targets = {target.name: target for target in core.publication_targets(d, adapter)}
    saved = _saved_channels(d)
    configured = core.publication_channels(targets.values())
    channels = saved or configured
    can_send_text = False
    telegram_resume_blocked = False
    for kind in dict.fromkeys((*channels, *marks, *errors)):
        name = names.get(kind, kind)
        mark = marks.get(kind)
        channel_progress = progress.get(kind) or {}
        partial = text_pending(mark, channel_progress)
        unknown_text = text_unknown(mark, channel_progress)
        destination_id = channels.get(kind)
        if kind == "tg" and channel_progress.get("media"):
            pinned_chat = media_chat_id(saved.get(kind, ""), channel_progress)
            destination_id = channel_progress.get("media_chat_id") or destination_id
            telegram_resume_blocked = mark != "done" and pinned_chat is None
            can_send_text = (partial and pinned_chat is not None and saved.get(kind) == configured.get(kind)
                             and getattr(targets.get(kind), "supports_publish_resume", False))
        status = ("✅ опубликовано" if mark == "done" else
                  "⚠️ медиа опубликовано, текст не отправлен" if partial else
                  "❓ медиа опубликовано, результат отправки текста неизвестен" if unknown_text else
                  "❓ результат неизвестен" if mark == "unknown" else
                  "❌ ошибка" if kind in errors else "⏳ не отправлено")
        destination = f" (канал {destination_id})" if destination_id else ""
        lines.append(f"{name} — {status}{destination}")
        if not title and mark != "done" and kind in errors:
            lines.append(errors[kind][:180])
        if mark == "unknown" and not partial:
            part = "текст" if unknown_text else "пост"
            action = "seen_tg_text" if kind == "tg" and unknown_text else f"seen_{kind}"
            rows.append([(f"Проверил: {part} есть в {name}", f"d:{action}:{d['id']}:{d['attempts']}")])
    if d["status"] != "pending" or title:
        return "\n".join(lines), []
    suffix = f"{d['id']}:{d['attempts']}"
    if can_send_text:
        rows.append([("Дослать текст", f"d:sendtext:{suffix}")])
    remaining = _remaining_channels(channels, marks)
    if remaining and marks:
        rows.append([("Закрыть незавершённую публикацию", f"d:pubclose:{suffix}")])
    if telegram_resume_blocked:
        lines.append("Числовой ID прежнего канала Telegram не сохранился. Автоматическая досылка недоступна.")
    if (not telegram_resume_blocked and (not saved or configured == saved)
            and set(remaining) <= configured.keys() and not (can_send_text and remaining == ["tg"])):
        rows.append([("🔁 Отправить в оставшиеся каналы", f"d:ok:{suffix}")])
    return "\n".join(lines), rows


async def finish_publication(d, adapter=None, user_id=None):
    snapshot = core.get(d["id"])
    core._set(d["id"], status="published")
    _scrub_publication(snapshot)
    await core._safe_notify(core.ADAPTERS.get(d["platform"]), d["author_id"], "✅ Ваш пост опубликован. Спасибо!")
    if adapter is not None:
        await _send_final_status(snapshot, adapter, user_id, f"✅ Пост №{d['id']} опубликован.")


def _scrub_publication(d):
    for path in core.draft_files(d):
        core._unlink(path)
    core.clear_source_cache(d)
    core.scrub_published(d)


async def _send_final_status(d, adapter, user_id, title):
    try:
        text, rows = core.publication_view(d, adapter, title=title)
        await adapter.send_menu(str(user_id), text, rows)
    except Exception:
        core.log.warning("не удалось отправить статус публикации", exc_info=True)


async def close_publication(d, adapter, user_id, *, confirm):
    marks = core._parse_marks(d.get("published_to"))
    _recover_telegram_mark(marks, json.loads(d.get("publish_progress") or "{}"))
    if not marks or publication_complete(d, adapter, marks):
        return False, "Закрыть можно только начатую и незавершённую публикацию."
    did, attempt = d["id"], d["attempts"]
    snapshot = closure_snapshot(d)
    if not confirm:
        text, _ = core.publication_view(d, adapter)
        token = CLOSURE_SNAPSHOT.set(snapshot)
        try:
            await adapter.send_menu(str(user_id), text + "\n\nЕсли прежний канал больше недоступен, можно закрыть публикацию. "
                                    "Бот прекратит отправку и удалит черновик. Уже отправленные сообщения останутся в каналах.",
                                    [[("Подтверждаю закрытие", f"d:pubcloseyes:{did}:{attempt}")],
                                     [("Отмена", f"d:pubstatus:{did}:{attempt}")]])
        finally:
            CLOSURE_SNAPSHOT.reset(token)
        return False, ""
    if CLOSURE_SNAPSHOT.get() != snapshot:
        return False, "Результат изменился или подтверждение устарело. Откройте статус публикации заново."
    if not core._claim(did, "pending", "expired"):
        return False, core.BUSY
    _scrub_publication(d)
    await _send_final_status(d, adapter, user_id, f"Публикация поста №{did} закрыта. Повторной отправки не будет.")
    return True, ""


async def _publish_safe(adapter: core.Adapter, user_id, did: int, attempt: int, force: bool = False,
                        *, text_only: bool = False) -> tuple[bool, str]:
    """Обёртка: непредвиденный сбой не должен оставить черновик навсегда в статусе «publishing»."""
    if not core.can_manage_draft(adapter, user_id, core.get(did)):
        return False, "⛔ У вас нет прав на этот пост."
    try:
        result = await core._publish_inner(adapter, user_id, did, attempt, force, text_only=text_only)
        d = core.get(did)
        if result[1] and d and (d.get("published_to") or d.get("publish_errors") != "{}"):
            try:
                text, rows = core.publication_view(d, adapter)
                await adapter.send_menu(str(user_id), text, rows)
            except Exception:
                core.log.warning("не удалось отправить статус публикации", exc_info=True)
        return result
    except Exception as e:
        core.log.exception("publish crashed")
        try:
            core.db.execute("UPDATE drafts SET status='pending' WHERE id=? AND status='publishing'", (did,))
            core.db.commit()
        except Exception:
            core.log.exception("не удалось вернуть черновик в работу")
        return False, (f"⚠️ Сбой при публикации: {core.err(e)}. Проверьте канал, прежде чем нажимать ✅ снова "
                       "(пометки о публикации сохранены, дубль не выйдет без подтверждения).")


async def _publish_inner(adapter: core.Adapter, user_id, did: int, attempt: int, force: bool = False,
                         *, text_only: bool = False) -> tuple[bool, str]:
    """Публикация с защитой от дублей: перед отправкой в канал ставим пометку «могло уйти»,
    после успеха — «опубликовано». Если ответ потерялся, повтор спросит подтверждение."""
    if not core.can_manage_draft(adapter, user_id, core.get(did)):
        return False, "⛔ У вас нет прав на этот пост."
    if not core._claim(did, "pending", "publishing"):
        return False, core.BUSY
    d = core.get(did)
    if not d:
        return True, "Черновик не найден."
    if d["attempts"] != attempt:
        core._set(did, status="pending")
        return False, "Это устаревший вариант. Используйте последнее сообщение с постом."

    targets = core.publication_targets(d, adapter)
    channels = core.publication_channels(targets)
    saved = _saved_channels(d)
    marks = core._parse_marks(d.get("published_to"))
    progress = json.loads(d.get("publish_progress") or "{}")
    telegram_progress = progress.get("tg") or {}
    if _recover_telegram_mark(marks, progress):
        core._set(did, published_to=core._dump_marks(marks))
    if text_only:
        tg = next((t for t in targets if t.name == "tg" and getattr(t, "supports_publish_resume", False)), None)
        if not text_pending(marks.get("tg"), telegram_progress):
            core._set(did, status="pending")
            return False, "Досылка текста недоступна. Проверьте текущий статус публикации."
        if tg is None or not saved.get("tg") or saved["tg"] != channels.get("tg"):
            core._set(did, status="pending")
            return False, "Прежний канал Telegram недоступен в настройках. Верните его или закройте публикацию."
        targets = [tg]
    elif publication_complete(d, adapter, marks):
        await core.finish_publication(d, adapter, user_id)
        return True, "✅ Опубликовано."
    expected = EXPECTED_CHANNELS.get()
    if expected is not None and channels != expected:
        core._set(did, status="pending")
        return False, "Каналы публикации изменились. Откройте новое подтверждение."
    if not text_only and saved and channels != saved:
        core._set(did, status="pending")
        return False, ("⛔ Каналы публикации изменились после подтверждения или начала отправки. "
                       "Верните прежние настройки каналов и проверьте результат в них. "
                       "Автоматическая отправка в новые каналы заблокирована.")
    if not text_only and set(_remaining_channels(saved or channels, marks)) - channels.keys():
        core._set(did, status="pending")
        return False, "Прежний канал недоступен в настройках. Верните его или закройте публикацию."
    if telegram_progress.get("media") and marks.get("tg") != "done":
        if media_chat_id(saved.get("tg", ""), telegram_progress) is None:
            core._set(did, status="pending")
            return False, ("Числовой ID прежнего канала Telegram не сохранился. "
                           "Проверьте результат вручную или закройте публикацию.")
        if not any(t.name == "tg" and getattr(t, "supports_publish_resume", False) for t in targets):
            core._set(did, status="pending")
            return False, "Досылка текста в прежний канал Telegram недоступна."
    if not saved:
        core._set(did, publication_channels=json.dumps(channels, ensure_ascii=False))
    footer = json.loads(d["footer"]) if d.get("footer") is not None else post_footer.config()
    posts = {t.name: post_footer.render(d["post"], t.name, footer) for t in targets}
    if d.get("footer") is None:
        core._set(did, footer=json.dumps(footer, ensure_ascii=False))

    todo = [t for t in targets if marks.get(t.name) != "done"]
    risky = [t.name for t in todo if marks.get(t.name) == "unknown"
             and not (t.name == "tg" and getattr(t, "supports_publish_resume", False) and
                      text_pending(marks.get("tg"), telegram_progress))]
    if risky and not force:
        core._set(did, status="pending")  # черновик снова рабочий: кнопки ниже
        summary, rows = core.publication_view(core.get(did), adapter)
        await adapter.send_menu(
            str(user_id),
            summary + f"\n\n⚠️ Прошлая публикация в {', '.join(risky)} оборвалась: пост мог выйти.\n"
            "Проверьте канал. Если пост или недостающий текст уже там — подтвердите это кнопкой. "
            "Если его нет — разрешите повторную отправку.",
            [[("✅ Опубликовать ещё раз", f"d:okforce:{did}:{attempt}")],
             *(row for row in rows if row[0][1] != f"d:ok:{did}:{attempt}"),
             [("↩️ Не публиковать (пост остаётся на согласовании)", f"d:delno:{did}:{attempt}")]])
        return False, ""

    for t in todo:
        prev = marks.get(t.name)  # None или "unknown" (если прошлая попытка оборвалась)
        marks[t.name] = "unknown"  # сначала фиксируем намерение: сбой после отправки не даст дубль
        core._set(did, published_to=core._dump_marks(marks))
        started = time.monotonic()
        try:
            publish_kw = core.media_kw(d)
            if getattr(t, "supports_publish_resume", False):
                progress = json.loads(core.get(did).get("publish_progress") or "{}")
                resume = dict(progress.get(t.name) or {})
                def record(stage, value=True):
                    current = json.loads(core.get(did).get("publish_progress") or "{}")
                    channel = dict(current.get(t.name) or {})
                    if stage == "media_chat_id":
                        channel["media"] = True
                    if value:
                        channel[stage] = value
                    else:
                        channel.pop(stage, None)
                    current[t.name] = channel
                    core._set(did, publish_progress=json.dumps(current, ensure_ascii=False))
                publish_kw.update(resume=resume, progress=record)
            await t.publish(d["photo"], posts[t.name], **publish_kw)
        except Exception as e:
            core.log.exception("publish failed")
            errors = json.loads(core.get(did).get("publish_errors") or "{}")
            errors[t.name] = core.err(e)[:180]
            core._set(did, publish_errors=json.dumps(errors, ensure_ascii=False))
            try:
                sure = t.not_sent(e)  # сервис явно отклонил запрос: поста в канале точно нет
            except Exception:
                sure = False
            if sure:  # возвращаем пометку как было до попытки, лишнего подтверждения не нужно
                if prev is None:
                    marks.pop(t.name, None)
                else:
                    marks[t.name] = prev
                core._set(did, published_to=core._dump_marks(marks))
            core._set(did, status="pending")
            done = [k for k, v in marks.items() if v == "done"]
            part = f"\nУже опубликовано: {', '.join(done)}." if done else ""
            tg_progress = json.loads(core.get(did).get("publish_progress") or "{}").get("tg") or {}
            tail = ("Медиа опубликовано. После устранения ошибки нажмите «Дослать текст»." if t.name == "tg" and text_pending(marks.get("tg"), tg_progress) else
                    "Медиа опубликовано. Проверьте, появился ли текст, прежде чем подтверждать повтор." if t.name == "tg" and tg_progress.get("media") else
                    "Пост не вышел: исправьте причину и нажмите ✅ ещё раз." if sure else
                    "Пост мог всё же выйти — проверьте канал, прежде чем нажимать ✅ снова.")
            return False, f"⚠️ Не удалось опубликовать в {t.name}: {core.err(e)}{part}\n{tail}"
        finally:
            core.log.info("timing stage=publish channel=%s seconds=%.3f", t.name, time.monotonic() - started)
        marks[t.name] = "done"
        errors = json.loads(core.get(did).get("publish_errors") or "{}")
        errors.pop(t.name, None)
        core._set(did, published_to=core._dump_marks(marks), publish_errors=json.dumps(errors, ensure_ascii=False))

    if publication_complete(core.get(did), adapter, marks):
        await core.finish_publication(d, adapter, user_id)
        return True, "✅ Опубликовано."
    core._set(did, status="pending")
    return True, "✅ Текст отправлен в Telegram."
