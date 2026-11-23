"""Статус, уведомления и напоминания."""
import asyncio
import os
import shutil
import time
from typing import Any

import httpx

import comparison
import core
import ops
import spaces


def quiet(adapter: core.Adapter, user_id) -> bool:
    """Участники получают ответы; незнакомцам оставляем прежний тихий режим."""
    return core.env("EMPLOYEE_NOTIFY", "0") != "1" and not core.is_member(adapter, user_id)


def confirm_mode() -> str:
    """Подтверждение в MAX: текст или off. Старое reaction принимается как text."""
    return "off" if core.env("EMPLOYEE_CONFIRM", "text").lower() == "off" else "text"


async def alert(adapter: core.Adapter, text: str):
    """Сообщение об ошибке — администраторам. С троттлингом: сломанный ключ LLM
    не превращается в поток сообщений на каждое фото."""
    text = core.redact(text)
    key = text[:60]
    now = time.time()
    if now - core._ALERT_LAST.get(key, 0) < int(core.env("ALERT_THROTTLE_SEC", "300")):
        core.log.info("alert suppressed: %s", text[:120])
        return
    core._ALERT_LAST[key] = now
    approval = core.ADAPTERS.get(os.getenv("APPROVAL_PLATFORM") or adapter.name, adapter)
    for a in getattr(approval, "admins", []):
        try:
            await approval.notify(a, text[:500])
        except Exception:
            core.log.warning("alert failed", exc_info=True)


def staff(adapter: core.Adapter) -> list[str]:
    """О состоянии бота сообщаем администраторам; адаптер публикации не получает уведомлений."""
    if getattr(adapter, "publish_only", False):
        return []
    return list(getattr(adapter, "admins", None) or [])


def can_status(adapter: core.Adapter, uid) -> bool:
    return str(uid) in core.staff(adapter)


async def alert_staff(adapter, text: str):
    """Сообщение о сбое бота. Пробуем все подключённые мессенджеры: если упал один, второй сообщит.
    Одинаковые сообщения не чаще раза в ALERT_THROTTLE_SEC."""
    text = core.redact(text)
    key, now = "staff:" + text[:60], time.time()
    if now - core._ALERT_LAST.get(key, 0) < int(core.env("ALERT_THROTTLE_SEC", "300")):
        return
    core._ALERT_LAST[key] = now
    seen: list[core.Adapter] = []
    for a in [adapter, *core.ADAPTERS.values()]:
        if a is None or any(a is x for x in seen):
            continue
        seen.append(a)
        for uid in core.staff(a):
            try:
                await a.notify(uid, text[:500])
            except Exception:
                core.log.warning("не удалось отправить сообщение о состоянии %s", uid, exc_info=True)


async def announce_start():
    """Сообщение «бот запущен» (STARTUP_NOTIFY=0 — не слать)."""
    if core.env("STARTUP_NOTIFY", "1") == "0":
        return
    targets = [(a, uid) for a in core.ADAPTERS.values() for uid in core.staff(a)]
    seen = set()
    for a, uid in targets:
        key = (a.name, str(uid))
        if key in seen:
            continue
        seen.add(key)
        try:
            with spaces.notification_target(a, uid) as target:
                if core.is_admin(target, uid):
                    n = core.db.execute("SELECT COUNT(*) FROM drafts WHERE status='pending'").fetchone()[0]
                else:
                    n = core.db.execute("SELECT COUNT(*) FROM drafts WHERE status='pending' AND platform=? AND author_id=?",
                                        (target.name, str(uid))).fetchone()[0]
                text = "🟢 Бот запущен." + (f" Ждут согласования: {n}." if n else "")
                if not spaces.active_id() and core.LAST_EXPIRED:
                    text += (f"\nЗакрыто неразобранных черновиков старше {core.env('DRAFT_TTL_DAYS', '7')} дн.: "
                             f"{core.LAST_EXPIRED}.")
                await target.notify(uid, text)
        except Exception:
            core.log.warning("не удалось отправить сообщение о запуске %s", uid, exc_info=True)


def _ago(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 3600:
        return f"{max(1, sec // 60)} мин"
    if sec < 86400:
        return f"{sec // 3600} ч"
    return f"{sec // 86400} дн"


async def aitunnel_balance() -> str | None:
    """Баланс по документированному GET /v1/aitunnel/balance; ключ не выводится."""
    from decimal import Decimal, InvalidOperation
    from urllib.parse import urlsplit
    try:
        base = urlsplit(core.env("LLM_BASE_URL"))
        if core.PROVIDER != "openai" or base.scheme != "https" or base.hostname != "api.aitunnel.ru":
            return None
    except ValueError:
        return None
    token = core.env("LLM_API_KEY")
    if not token:
        return "💰 Баланс AITunnel: не задан LLM_API_KEY."

    def money(value):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("invalid amount")
        amount = Decimal(str(value))
        if not amount.is_finite() or abs(amount) > Decimal("1e15"):
            raise ValueError("invalid amount")
        return f"{amount:,.2f}".replace(",", " ").replace(".", ",") + " ₽"

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get("https://api.aitunnel.ru/v1/aitunnel/balance",
                                        headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
            data = response.json()
        text = "💰 Баланс AITunnel: " + money(data["balance"])
        if data.get("budget") is not None:
            text += "\nБюджет ключа: " + money(data["budget"])
        return text
    except httpx.HTTPStatusError as exc:
        return f"💰 Баланс AITunnel: недоступен (HTTP {exc.response.status_code})."
    except httpx.RequestError:
        return "💰 Баланс AITunnel: не удалось связаться с сервисом."
    except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation):
        return "💰 Баланс AITunnel: сервис вернул неожиданный ответ."


async def status_text(adapter: core.Adapter, include_global=True) -> str:
    """Сводка для /status: жив ли бот, что в очереди, отвечает ли нейросеть (проверка стоит доли копейки)."""
    now = time.time()
    def one(sql, *a):
        return core.db.execute(sql, a).fetchone()[0]
    pend = one("SELECT COUNT(*) FROM drafts WHERE status='pending'")
    oldest = one("SELECT MIN(COALESCE(NULLIF(last_sent,0), created)) FROM drafts WHERE status='pending'")
    stuck = one("SELECT COUNT(*) FROM drafts WHERE status IN ('publishing','regenerating')")
    day = one("SELECT COUNT(*) FROM drafts WHERE created > ?", now - 86400)
    pub7 = one("SELECT COUNT(*) FROM drafts WHERE status='published' AND published_at > ?", now - 7 * 86400)
    undated = one("SELECT COUNT(*) FROM drafts WHERE status='published' AND COALESCE(published_at,0)=0")
    root_db = spaces.root().db if spaces.INSTALLED else core.db
    sid = spaces.active_id()
    handoff_where, handoff_args = ("", ()) if include_global else (" WHERE space_id=?", (sid,))
    handoff_failed = root_db.execute(
        "SELECT COUNT(*) FROM max_handoff_failures" + handoff_where, handoff_args).fetchone()[0]
    notice_where = "" if include_global else " AND space_id=?"
    notice_args = () if include_global else (sid,)
    notice_issues = root_db.execute(
        "SELECT COUNT(*) FROM max_handoff_notices WHERE state IN ('pending','unknown','failed')" + notice_where,
        notice_args).fetchone()[0]
    async def check_llm():
        try:
            await asyncio.wait_for(core._complete("Отвечай одним словом.", "Ответь: ОК"), 30)
            return "✅ отвечает"
        except Exception as e:
            return f"❌ {type(e).__name__}: {core.err(e)[:150]}"
    async def balance_for_space():
        return await core.aitunnel_balance() if include_global and not spaces.active_id() else None
    llm, balance = await asyncio.gather(check_llm(), balance_for_space())
    last = ("нет данных" if not (core.LLM_STATE["ok"] or core.LLM_STATE["err"]) else
            f"последний пост написан {core._ago(now - core.LLM_STATE['ok'])} назад" if core.LLM_STATE["ok"] >= core.LLM_STATE["err"] else
            f"последняя генерация закончилась ошибкой {core._ago(now - core.LLM_STATE['err'])} назад: {core.LLM_STATE['msg'][:100]}")
    try:
        free = f"{shutil.disk_usage(core.DATA).free / 1e9:.1f} ГБ"
    except OSError:
        free = "неизвестно"
    lines = [f"🟢 Бот работает {core._ago(now - core.STARTED)}.",
             f"Версия: {await ops.version()}",
             f"Мессенджеры: {', '.join(core.ADAPTERS) or 'нет'}",
             f"Нейросеть ({comparison.default_model()}): {llm}", f"Генерация: {last}",
             f"На согласовании: {pend}" + (f" (старейший ждёт {core._ago(now - oldest)})" if pend and oldest else ""),
             f"За сутки получено фото: {day}, опубликовано за неделю: {pub7}"]
    if stuck:
        lines.append(f"⚠️ Зависших черновиков: {stuck} (после перезапуска вернутся в работу)")
    if undated:
        lines.append(f"У {undated} старых публикаций дата отправки неизвестна; они не включены в недельную статистику.")
    if handoff_failed:
        lines.append(f"⚠️ Материалов с terminal failure/истечением: {handoff_failed}.")
        latest = root_db.execute(
            "SELECT uid,source_mid,space_id,attempts,reason FROM max_handoff_failures"
            + handoff_where + " ORDER BY failed_at DESC LIMIT 3", handoff_args).fetchall()
        for row in latest:
            space = f" · пространство {row['space_id']}" if include_global else ""
            lines.append(f"• пользователь {row['uid']}{space} · mid {row['source_mid'] or '—'} · "
                         f"{row['attempts']} попыток · {row['reason'][:100]}")
    if notice_issues:
        lines.append(f"⚠️ Не завершены предупреждения о потерянных материалах: {notice_issues}.")
    if balance:
        lines.append(balance)
    lines.append(f"Свободно на диске: {free}")
    return "\n".join(lines)


async def _send_review_notice(adapter, uid, text, rows, drafts, silent=False):
    def require_access():
        if any(not core.can_manage_draft(adapter, uid, core.get(d["id"])) for d in drafts):
            raise PermissionError("Права на черновик отозваны до отправки уведомления")

    require_access()
    kw = {"preflight": require_access} if getattr(adapter, "supports_review_preflight", False) else {}
    await adapter.send_menu(uid, text, rows, silent=silent, **kw)


async def remind_once(now: float | None = None) -> int:
    """Если черновик ждёт дольше REMIND_AFTER_MIN минут (0 — выключено), напоминаем администраторам пространства.
    Возвращает число черновиков, о которых напомнили."""
    now = now or time.time()
    after = float(core.env("REMIND_AFTER_MIN", "180")) * 60
    # напоминаем только в рабочее время (NOTIFY_HOURS/NOTIFY_DAYS/NOTIFY_TZ)
    if after <= 0 or not ops.in_window(now):
        return 0
    rows = core.db.execute("SELECT * FROM drafts WHERE status='pending' AND reminded=0 "
                      "AND COALESCE(NULLIF(last_sent,0), created) < ? ORDER BY id", (now - after,)).fetchall()
    groups: dict[tuple[int, str], Any] = {}
    for r in rows:
        d = dict(r)
        ad = core._approval_adapter(d, None)
        if ad is not None:
            for uid in core.draft_recipients(d, ad):
                groups.setdefault((id(ad), uid), (ad, []))[1].append(d)
    delivered: set[int] = set()
    for (_, uid), (ad, ds) in groups.items():
        ds = [d for d in ds if core.can_manage_draft(ad, uid, core.get(d["id"]))]
        if not ds:
            continue
        def age(d):
            return now - (d["last_sent"] or d["created"])
        waiting, oldest = len(ds), max(age(d) for d in ds)
        ds = ds[:5]
        text = (f"⏰ Ждут решения: {waiting} (самый давний — {core._ago(oldest)}).\n"
                "Нажмите, чтобы показать пост ещё раз:")
        rows_ = [[(f"📨 {(d['author_name'] or 'Пост')[:14]}, {core._ago(age(d))}", f"d:show:{d['id']}:{d['attempts']}")] for d in ds]
        try:
            await _send_review_notice(ad, uid, text, rows_, ds, silent=ops.delivery_for(ad, uid, now) == "silent")
            delivered.update(d["id"] for d in ds)
        except Exception:
            core.log.warning("не удалось напомнить участнику %s", uid, exc_info=True)
    core.db.executemany("UPDATE drafts SET reminded=1 WHERE id=?", [(did,) for did in delivered])
    core.db.commit()
    total = len(delivered)
    return total


async def reminder_loop():
    last_clean = time.time()
    while True:
        try:
            await ops.announce_update_result()
            await ops.flush_deferred()  # придержанные черновики уходят, когда началось рабочее время
            await core.remind_once()
            await core.warn_expiring_once()
            if time.time() - last_clean > int(core.env("HOUSEKEEPING_SEC", "21600")) or core.cleanup_pending():  # раз в 6 часов
                last_clean = time.time()
                core.housekeeping()  # в потоке нельзя: соединение SQLite принадлежит потоку с event loop
        except Exception:
            core.log.exception("reminder failed")
        await asyncio.sleep(int(core.env("REMIND_CHECK_SEC", "300")))


async def warn_expiring_once(now=None):
    now = time.time() if now is None else now
    ttl = int(core.env("DRAFT_TTL_DAYS", "7")) * 86400
    if ttl <= 0:
        return 0
    count = 0
    for row in core.db.execute("SELECT * FROM drafts WHERE status='pending'").fetchall():
        d = dict(row)
        if core._parse_marks(d.get("published_to")):
            continue  # незавершённая публикация сохраняется до решения человека
        deadline = (d["last_sent"] or d["created"]) + ttl
        if not now < deadline <= now + 86400 or d["expiry_warned"] == deadline:
            continue
        ad = core._approval_adapter(d, None)
        recipients = core.draft_recipients(d, ad) if ad else []
        if not recipients:
            continue
        sent = 0
        for uid in recipients:
            if not core.can_manage_draft(ad, uid, core.get(d["id"])):
                continue
            try:
                await _send_review_notice(ad, uid, f"⏳ Пост №{d['id']} от {d['author_name'] or 'сотрудника'} "
                                   f"будет удалён примерно через {core._ago(deadline - now)}.\n"
                                   "Проверьте материал до истечения срока хранения.",
                                   [[("📨 Открыть пост", f"d:show:{d['id']}:{d['attempts']}")]], [d],
                                   silent=ops.delivery_for(ad, uid, now) != "normal")
                sent += 1
            except Exception:
                core.log.warning("не удалось предупредить об удалении %s", d["id"], exc_info=True)
        if sent == len(recipients):
            core._set(d["id"], expiry_warned=deadline)
            count += 1
    return count
