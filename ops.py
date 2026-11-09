"""Управление ботом: уведомления по расписанию (у каждого свой режим), обновление с GitHub."""
import asyncio
import json
import os
import re
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import configuration
import core
import resources
import schema

log = core.log
REPO_DIR = Path(__file__).resolve().parent

core.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
core.db.execute("""CREATE TABLE IF NOT EXISTS prefs (
    platform TEXT, uid TEXT, key TEXT, value TEXT, PRIMARY KEY (platform, uid, key))""")
# черновики, которые ждут начала рабочего времени у тех, кто выбрал режим «отложить»
core.db.execute("""CREATE TABLE IF NOT EXISTS deferred (
    platform TEXT, uid TEXT, did INTEGER, created REAL, PRIMARY KEY (platform, uid, did))""")
schema.add_columns(core.db, "deferred", ("lease_until REAL DEFAULT 0",))
core.db.commit()


# ---------- Настройки в базе ----------
def get_setting(key: str, default: str = "") -> str:
    r = core.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key: str, value: str):
    core.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
    core.db.commit()


# ---------- Уведомления: окно рабочего времени и режим каждого человека ----------
MODES = {
    "normal": "🔔 Всегда со звуком",
    "silent": "🔕 Вне рабочих часов — без звука",
    "defer": "⏳ Вне рабочих часов — отложить",
    "muted": "🔇 Мут — всегда без звука",
}
DAY_NAMES = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _tz():
    name = core.env("NOTIFY_TZ", "Asia/Yekaterinburg")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=5))  # Екатеринбург


def _parse_hours(raw: str) -> tuple[int, int]:
    a, b = (int(x) for x in raw.split("-"))
    if not (0 <= a <= 24 and 0 <= b <= 24 and a != b) or (a, b) == (24, 0):
        raise ValueError(raw)
    return a, b


def _hours() -> tuple[int, int]:
    """Часы рабочего окна «начало-конец». Если начало больше конца (20-6), окно переходит через полночь.
    Неверное значение = окно весь день (а main при запуске сообщит об ошибке в настройке)."""
    try:
        return _parse_hours(core.env("NOTIFY_HOURS", "10-18"))
    except ValueError:
        return 0, 24


def _parse_days(raw: str) -> set[int]:
    """«1-5», «1,3,5», «6-2» (через воскресенье: сб, вс, пн, вт). Бросает ValueError на неверном значении."""
    out: set[int] = set()
    for part in raw.split(","):
        if "-" in part:
            a, b = (int(x) for x in part.split("-"))
            if not (1 <= a <= 7 and 1 <= b <= 7):
                raise ValueError(part)
            out |= set(range(a, b + 1)) if a <= b else set(range(a, 8)) | set(range(1, b + 1))
        else:
            out.add(int(part))
    if not out or any(not 1 <= d <= 7 for d in out):
        raise ValueError(raw)
    return out


def _days() -> set[int]:
    try:
        return _parse_days(core.env("NOTIFY_DAYS", "1-5"))  # 1 = понедельник … 7 = воскресенье
    except ValueError:
        return set(range(1, 8))


def config_problem() -> str | None:
    """Проверка настроек времени при запуске: ошибка видна сразу, а не как «черновики не приходят»."""
    if problem := configuration.validate():
        return problem
    if core.env("MATERIAL_WORKERS", "2") not in ("1", "2"):
        return "MATERIAL_WORKERS: укажите 1 или 2 обработчика материалов."
    raw_h = core.env("NOTIFY_HOURS", "10-18")
    try:
        _parse_hours(raw_h)
    except ValueError:
        return f"NOTIFY_HOURS={raw_h}: нужны часы вида 10-18 (числа от 0 до 24, начало не равно концу)."
    raw_d = core.env("NOTIFY_DAYS", "1-5")
    try:
        _parse_days(raw_d)
    except ValueError:
        return f"NOTIFY_DAYS={raw_d}: дни недели числами от 1 (понедельник) до 7, например 1-5 или 1,3,5."
    name = core.env("NOTIFY_TZ", "Asia/Yekaterinburg")
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
    except Exception:
        return f"NOTIFY_TZ={name}: неизвестный часовой пояс (пример: Asia/Yekaterinburg, Europe/Moscow)."
    if core.env("NOTIFY_DEFAULT", "silent") not in MODES:
        return f"NOTIFY_DEFAULT={os.getenv('NOTIFY_DEFAULT')}: допустимо {', '.join(MODES)}."
    return None


def in_window(now: float | None = None) -> bool:
    dt = datetime.fromtimestamp(time.time() if now is None else now, _tz())
    a, b = _hours()
    inside = (a <= dt.hour < b) if a < b else (dt.hour >= a or dt.hour < b)  # 20-6: вечер и ночь
    # After midnight an overnight shift still belongs to its starting weekday.
    day = dt - timedelta(days=1) if a > b and dt.hour < b else dt
    return day.isoweekday() in _days() and inside


def window_text() -> str:
    days = sorted(_days())
    a, b = _hours()
    if days == list(range(days[0], days[-1] + 1)) and len(days) > 1:
        d = f"{DAY_NAMES[days[0] - 1]}–{DAY_NAMES[days[-1] - 1]}"
    else:
        d = ", ".join(DAY_NAMES[x - 1] for x in days)
    return f"{d}, {a:02d}:00–{b:02d}:00 ({core.env('NOTIFY_TZ', 'Asia/Yekaterinburg')})"


def default_mode() -> str:
    v = core.env("NOTIFY_DEFAULT", "silent")
    return v if v in MODES else "silent"


def get_mode(adapter, uid) -> str:
    r = core.db.execute("SELECT value FROM prefs WHERE platform=? AND uid=? AND key='notify_mode'",
                        (adapter.name, str(uid))).fetchone()
    return r["value"] if r and r["value"] in MODES else default_mode()


def set_mode(adapter, uid, mode: str):
    core.db.execute("INSERT OR REPLACE INTO prefs(platform,uid,key,value) VALUES(?,?,'notify_mode',?)",
                    (adapter.name, str(uid), mode))
    core.db.commit()


def delivery_for(adapter, uid, now: float | None = None) -> str:
    """Как доставить человеку сообщение сейчас: normal (со звуком) / silent (без звука) / defer (придержать)."""
    mode = get_mode(adapter, uid)
    if mode == "normal":
        return "normal"
    if mode == "muted":
        return "silent"
    if in_window(now):
        return "normal"
    return "silent" if mode == "silent" else "defer"


def notif_view(adapter, uid, saved: bool = False) -> tuple[str, list]:
    mode = get_mode(adapter, uid)
    text = (("✅ Сохранено.\n\n" if saved else "") + f"🔔 Уведомления\n\nСейчас: {MODES[mode]}\n"
            f"Рабочие часы: {window_text()}\n\n"
            "• Без звука — сообщения приходят сразу, но тихо.\n"
            "• Отложить — черновики придут, когда начнётся рабочее время.\n"
            "• Мут — всегда без звука.\n"
            "Ошибки бота и ответы на ваши нажатия приходят как обычно.")
    rows = [[(("✓ " if m == mode else "") + label, f"m:nset:{m}")] for m, label in MODES.items()]
    return text, rows


def enqueue(adapter, uid, did: int):
    core.db.execute("INSERT OR IGNORE INTO deferred(platform,uid,did,created) VALUES(?,?,?,?)",
                    (adapter.name, str(uid), did, time.time()))
    core.db.commit()


async def flush_deferred(now: float | None = None, adapter=None, uid=None) -> int:
    """Доставляем придержанные черновики тем, у кого уже началось рабочее время (или сменился режим).
    Запись удаляется только когда черновик закончил жизнь; пока он публикуется или пересоздаётся — ждём."""
    now = now or time.time()
    args: tuple[Any, ...] = ()
    sql = "SELECT * FROM deferred"
    if adapter is not None and uid is not None:
        sql, args = sql + " WHERE platform=? AND uid=?", (adapter.name, str(uid))
    # Old deferred broadcasts may target former reviewers. Reassign those deliveries
    # before reading the batch so the author can receive them in the same pass.
    for r in core.db.execute(sql, args).fetchall():
        ad, d = core.ADAPTERS.get(r["platform"]), core.get(r["did"])
        if ad and d and d["status"] == "pending" and not core.can_manage_draft(ad, r["uid"], d):
            core.db.execute("DELETE FROM deferred WHERE platform=? AND uid=? AND did=?", (r["platform"], r["uid"], r["did"]))
            for recipient in core.draft_recipients(d, ad):
                enqueue(ad, recipient, d["id"])
            core.db.commit()
    done = 0
    for r in core.db.execute(sql, args).fetchall():
        ad, d = core.ADAPTERS.get(r["platform"]), core.get(r["did"])
        key = (r["platform"], r["uid"], r["did"])
        if ad is None or not d or d["status"] in ("published", "deleted", "expired") or not core.can_manage_draft(ad, r["uid"], d):
            core.db.execute("DELETE FROM deferred WHERE platform=? AND uid=? AND did=?", key)
            core.db.commit()
            continue
        if d["status"] != "pending":  # publishing/regenerating — состояние временное, вернёмся позже
            continue
        mode = delivery_for(ad, r["uid"], now)
        if mode == "defer":
            continue
        # A durable lease prevents concurrent delivery without deleting its retry record.
        lease = now + 600
        cur = core.db.execute("UPDATE deferred SET lease_until=? WHERE platform=? AND uid=? AND did=? AND COALESCE(lease_until,0)<=?", (lease, *key, now))
        core.db.commit()
        if cur.rowcount != 1:
            continue
        try:
            kw: dict[str, Any] = {"silent": True} if mode == "silent" else {}
            if getattr(ad, "supports_approval_resume", False):
                kw["resume"] = True
            await ad.send_for_approval(r["uid"], d["id"], d["photo"], d["post"], **kw, **core.media_kw(d))
        except BaseException as exc:
            core.db.execute("UPDATE deferred SET lease_until=0 WHERE platform=? AND uid=? AND did=? AND lease_until=?", (*key, lease))
            core.db.commit()
            if not isinstance(exc, Exception):
                raise
            log.warning("не удалось доставить отложенный черновик %s человеку %s", r["did"], r["uid"], exc_info=True)
            continue
        core.db.execute("DELETE FROM deferred WHERE platform=? AND uid=? AND did=? AND lease_until=?", (*key, lease))
        core.db.commit()
        core._set(d["id"], last_sent=now, reminded=0)  # напоминание считаем от фактической доставки
        done += 1
    return done


# ---------- Обновление с GitHub ----------
RESTART_CODE: int | None = None
RESTART_DELAY = 3  # даём ответу в чате уйти, прежде чем перезапускаться
_restart_ev: asyncio.Event | None = None


def request_restart(code: int, delay: float | None = None):
    """Просим main.serve остановить бота и выйти с этим кодом (42 — обновление, 0 — обычная остановка)."""
    global RESTART_CODE
    RESTART_CODE = code
    if _restart_ev is not None:
        try:
            asyncio.get_running_loop().call_later(RESTART_DELAY if delay is None else delay, _restart_ev.set)
        except RuntimeError:
            _restart_ev.set()


async def wait_restart() -> int:
    global _restart_ev
    _restart_ev = asyncio.Event()
    if RESTART_CODE is not None:
        _restart_ev.set()
    await _restart_ev.wait()
    return RESTART_CODE if RESTART_CODE is not None else 0


def scrub(text: str) -> str:
    """Токен GitHub в адресе репозитория не должен попасть ни в чат, ни в лог."""
    return re.sub(r"//[^/@\s]+@", "//***@", text or "")


def updates_enabled() -> bool:
    return bool(os.getenv("GITHUB_REPO")) and (REPO_DIR / ".git").exists()


async def _git(*args: str, timeout: int = 60) -> tuple[int, str]:
    p = None
    try:
        p = await asyncio.create_subprocess_exec(
            "git", *args, cwd=REPO_DIR, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, start_new_session=os.name == "posix")
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return p.returncode or 0, scrub(out.decode("utf-8", "replace")).strip()
    except (TimeoutError, asyncio.CancelledError) as exc:
        if p is not None:
            if os.name == "posix" or p.returncode is None:
                try:
                    if os.name == "posix":
                        os.killpg(p.pid, signal.SIGKILL)  # также git-remote-https/ssh с открытыми pipe
                    else:
                        p.kill()
                except ProcessLookupError:
                    pass
            await p.communicate()
        if isinstance(exc, asyncio.CancelledError):
            raise
        return 1, "git не ответил вовремя"
    except OSError as e:
        return 1, f"git недоступен: {scrub(str(e))}"


async def version() -> str:
    if not (REPO_DIR / ".git").exists():
        return "из образа (без GitHub)"
    rc, out = await _git("log", "-1", "--format=%h %s")
    return out[:100] if rc == 0 else "неизвестна"


async def check_update() -> tuple[str, list[str], str]:
    """Возвращает (состояние, новые коммиты, пояснение): state = disabled | error | current | available."""
    if not updates_enabled():
        return "disabled", [], ("Автообновление не настроено: в .env нет GITHUB_REPO, "
                                "либо бот запущен не из репозитория.")
    branch = core.env("GITHUB_BRANCH", "main")
    rc, out = await _git("fetch", "origin", branch)
    if rc:
        return "error", [], f"Не удалось связаться с GitHub: {out[-300:]}"
    _, cur = await _git("rev-parse", "--short", "HEAD")
    _, new = await _git("rev-parse", "--short", "FETCH_HEAD")
    if cur == new:
        return "current", [], cur
    _, lines = await _git("log", "--format=%h %s", "-n", "6", "HEAD..FETCH_HEAD")
    return "available", [x for x in lines.splitlines() if x], f"{cur} → {new}"


async def upd_view() -> tuple[str, list | None]:
    state, commits, info = await check_update()
    if state in ("disabled", "error"):
        return ("⚠️ " if state == "error" else "") + info, None
    if state == "current":
        return f"✅ Уже установлена последняя версия ({info}).", None
    text = (f"🔄 Доступно обновление ({info}):\n" + "\n".join(f"• {c}" for c in commits) +
            "\n\nБот проверит тесты новой версии и только потом переключится. "
            "Перезапуск займёт около минуты, сообщения за это время не потеряются.")
    return text, [[("Да, обновить", "m:updgo"), ("Отмена", "m:menu")]]


def _data_dir() -> Path:
    return Path(core.env("DATA_DIR", "data")).resolve()


def upd_go(adapter, uid) -> tuple[str, None]:
    if not core.is_global_admin(adapter, uid):
        return "⛔ Обновление запускает только администратор из MAX_ADMINS (главный).", None
    if not updates_enabled():
        return "Автообновление не настроено.", None
    busy = core.db.execute("SELECT COUNT(*) FROM drafts WHERE status IN ('publishing','regenerating')").fetchone()[0]
    if busy:
        return "⏳ Сейчас идёт публикация или пересоздание поста. Повторите через минуту.", None
    d = _data_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "update_request.json").write_text(json.dumps({"platform": adapter.name, "uid": str(uid), "ts": time.time()}))
    request_restart(42)  # код 42 понимает entrypoint.sh: сначала updater.py, потом запуск новой версии
    return "🔄 Перезапускаюсь для обновления. Через минуту-две напишу, чем всё закончилось.", None


async def announce_update_result():
    async with resources.slot("update_result_notice", 1):
        await _announce_update_result()


async def _announce_update_result():
    """После запуска сообщаем тому, кто нажал «Обновить», чем закончилось обновление или откат."""
    f = _data_dir() / "update_result.json"
    if not f.exists():
        return
    try:
        r = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("не удалось прочитать результат обновления", exc_info=True)
        return
    if not isinstance(r, dict):
        log.warning("неверный формат результата обновления")
        return
    try:
        if float(r.get("next_try", 0)) > time.time():
            return
    except (ValueError, TypeError):
        pass
    text = {
        "ok": f"✅ Бот обновлён: {r.get('commit', '')}",
        "nochange": f"✅ Уже последняя версия ({r.get('commit', '')}).",
        "tests_failed": ("❌ Обновление отменено: тесты новой версии не прошли, работает прежняя версия "
                         f"{r.get('commit', '')}.\n{r.get('detail', '')}"),
        "failed": f"❌ Обновление не удалось: {r.get('detail', '')}. Работает прежняя версия {r.get('commit', '')}.",
        "rolled_back": ("⚠️ Новая версия не запустилась, бот вернулся к предыдущей "
                        f"({r.get('commit', '')}). Подробности: docker compose logs --tail 80"),
    }.get(str(r.get("status") or ""), "")
    if not text:
        return
    remaining = r.get("notice_remaining")
    if remaining is None:
        ad = core.ADAPTERS.get(str(r.get("platform") or ""))
        remaining = ([[ad.name, str(r["uid"])]] if ad and r.get("uid") else
                     [[a.name, str(u)] for a in core.ADAPTERS.values() for u in core.staff(a)])
        if not remaining:
            return
    r["notice_remaining"] = remaining

    def save():
        temp = f.with_suffix(".tmp")
        temp.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
        temp.replace(f)

    save()  # preserve all destinations before the first network operation
    for platform, u in list(remaining):
        a = core.ADAPTERS.get(platform)
        if a is None:
            continue
        try:
            import spaces
            with spaces.notification_target(a, u) as target:
                await target.notify(u, scrub(text)[:1500])
        except Exception:
            log.warning("не удалось сообщить результат обновления %s", u, exc_info=True)
        else:
            remaining.remove([platform, u])
            save()
    if remaining:
        attempts = int(r.get("notice_attempts", 0)) + 1
        r.update(notice_attempts=attempts, next_try=time.time() + min(60 * 2 ** min(attempts - 1, 6), 3600))
        save()
    else:
        f.unlink(missing_ok=True)


async def probation_watch(seconds: int | None = None):
    """Если после обновления бот прожил PROBATION_SEC секунд, считаем версию удачной (откат больше не нужен)."""
    await asyncio.sleep(seconds if seconds is not None else int(core.env("PROBATION_SEC", "120")))
    f = _data_dir() / "update_state.json"
    try:
        s = json.loads(f.read_text(encoding="utf-8"))
        if s.get("status") == "probation":
            s["status"] = "ok"
            f.write_text(json.dumps(s))
    except (OSError, ValueError):
        pass
