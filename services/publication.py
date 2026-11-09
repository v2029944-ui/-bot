"""Подтверждение и отправка в каналы."""
import json
import os
import time
from contextvars import ContextVar

import core
import post_footer

EXPECTED_CHANNELS: ContextVar[dict | None] = ContextVar("confirmed_channels", default=None)
PROMPT_CHANNELS: ContextVar[dict | None] = ContextVar("prompt_channels", default=None)


async def _publish(adapter: core.Adapter, user_id, did: int, attempt: int, force: bool = False) -> tuple[bool, str]:
    return await core._guard(core._publish_safe(adapter, user_id, did, attempt, force))


def publication_channels(targets):
    return {target.name: str(getattr(target, "channel", "")) for target in targets}


def publication_targets(d, adapter):
    if os.getenv("PUBLISH_TO_ALL") == "1":
        return list(core.ADAPTERS.values())
    source = core.ADAPTERS.get(d["platform"])
    targets = [source if source is not None and not source.publish_only else core._approval_adapter(d, adapter)]
    return [t for t in targets if t] + [a for a in core.ADAPTERS.values() if a.publish_only and a not in targets]


def publication_view(d, adapter):
    if d["status"] == "published" and not d.get("published_to"):
        return f"✅ Пост №{d['id']} опубликован. Содержимое удалено с сервера.", []
    marks = core._parse_marks(d.get("published_to"))
    errors = json.loads(d.get("publish_errors") or "{}")
    progress = json.loads(d.get("publish_progress") or "{}")
    names = {"max": "MAX", "tg": "Telegram"}
    lines = [f"Публикация поста №{d['id']}:"]
    rows = []
    channels = json.loads(d.get("publication_channels") or "{}")
    for target in core.publication_targets(d, adapter):
        name = names.get(target.name, target.name)
        mark = marks.get(target.name)
        channel_progress = progress.get(target.name) or {}
        partial = channel_progress.get("media") and not channel_progress.get("text") and not channel_progress.get("text_started")
        text_unknown = channel_progress.get("media") and channel_progress.get("text_started") and not channel_progress.get("text")
        status = ("✅ опубликовано" if mark == "done" else
                  "⚠️ медиа опубликовано, текст не отправлен" if mark == "unknown" and partial else
                  "❓ медиа опубликовано, результат отправки текста неизвестен" if mark == "unknown" and text_unknown else
                  "❓ результат неизвестен" if mark == "unknown" else
                  "❌ ошибка" if target.name in errors else "⏳ не отправлено")
        destination = f" (канал {channels[target.name]})" if channels.get(target.name) else ""
        lines.append(f"{name} — {status}{destination}")
        if mark != "done" and target.name in errors:
            lines.append(errors[target.name][:180])
        if mark == "unknown" and not partial:
            rows.append([(f"Проверил: пост есть в {name}", f"d:seen_{target.name}:{d['id']}:{d['attempts']}")])
    if d["status"] == "pending":
        rows.append([("🔁 Отправить в оставшиеся каналы", f"d:ok:{d['id']}:{d['attempts']}")])
    return "\n".join(lines), rows


async def finish_publication(d, adapter=None, user_id=None):
    core._set(d["id"], status="published")
    snapshot = core.get(d["id"])  # статус каналов нужен только для заключительного сообщения
    for path in core.draft_files(d):
        core._unlink(path)
    core.clear_source_cache(d)
    core.scrub_published(snapshot)
    await core._safe_notify(core.ADAPTERS.get(d["platform"]), d["author_id"], "✅ Ваш пост опубликован. Спасибо!")
    if adapter is not None:
        try:
            text, rows = core.publication_view(snapshot, adapter)
            await adapter.send_menu(str(user_id), text, rows)
        except Exception:
            core.log.warning("не удалось отправить статус публикации", exc_info=True)


async def _publish_safe(adapter: core.Adapter, user_id, did: int, attempt: int, force: bool = False) -> tuple[bool, str]:
    """Обёртка: непредвиденный сбой не должен оставить черновик навсегда в статусе «publishing»."""
    if not core.can_manage_draft(adapter, user_id, core.get(did)):
        return False, "⛔ У вас нет прав на этот пост."
    try:
        result = await core._publish_inner(adapter, user_id, did, attempt, force)
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


async def _publish_inner(adapter: core.Adapter, user_id, did: int, attempt: int, force: bool = False) -> tuple[bool, str]:
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
    expected = EXPECTED_CHANNELS.get()
    if expected is not None and channels != expected:
        core._set(did, status="pending")
        return False, "Каналы публикации изменились. Откройте новое подтверждение."
    saved = json.loads(d.get("publication_channels") or "{}")
    if saved and channels != saved:
        core._set(did, status="pending")
        return False, ("⛔ Каналы публикации изменились после подтверждения или начала отправки. "
                       "Верните прежние настройки каналов и проверьте результат в них. "
                       "Автоматическая отправка в новые каналы заблокирована.")
    if not saved:
        core._set(did, publication_channels=json.dumps(channels, ensure_ascii=False))
    footer = json.loads(d["footer"]) if d.get("footer") is not None else post_footer.config()
    posts = {t.name: post_footer.render(d["post"], t.name, footer) for t in targets}
    if d.get("footer") is None:
        core._set(did, footer=json.dumps(footer, ensure_ascii=False))

    marks = core._parse_marks(d.get("published_to"))
    todo = [t for t in targets if marks.get(t.name) != "done"]
    risky = [t.name for t in todo if marks.get(t.name) == "unknown"]
    if risky and not force:
        core._set(did, status="pending")  # черновик снова рабочий: кнопки ниже
        summary, rows = core.publication_view(core.get(did), adapter)
        await adapter.send_menu(
            str(user_id),
            summary + f"\n\n⚠️ Прошлая публикация в {', '.join(risky)} оборвалась: пост мог выйти.\n"
            "Проверьте канал. Если пост уже там — подтвердите это кнопкой. Если его нет — разрешите повторную отправку.",
            [[("✅ Опубликовать ещё раз", f"d:okforce:{did}:{attempt}")],
             *rows[:-1], [("↩️ Не публиковать (пост остаётся на согласовании)", f"d:delno:{did}:{attempt}")]])
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
                    if value:
                        channel[stage] = True
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
            tail = ("Пост не вышел: исправьте причину и нажмите ✅ ещё раз." if sure else
                    "Пост мог всё же выйти — проверьте канал, прежде чем нажимать ✅ снова.")
            return False, f"⚠️ Не удалось опубликовать в {t.name}: {core.err(e)}{part}\n{tail}"
        finally:
            core.log.info("timing stage=publish channel=%s seconds=%.3f", t.name, time.monotonic() - started)
        marks[t.name] = "done"
        errors = json.loads(core.get(did).get("publish_errors") or "{}")
        errors.pop(t.name, None)
        core._set(did, published_to=core._dump_marks(marks), publish_errors=json.dumps(errors, ensure_ascii=False))

    await core.finish_publication(d, adapter, user_id)
    return True, "✅ Опубликовано."
