"""MAX-адаптер на прямых запросах к официальному Bot API (https://dev.max.ru/docs-api)."""
import asyncio
import contextvars
import hashlib
import html
import json
import logging
import os
import re
import secrets
import time
import weakref
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

import comparison
import core
import dialog_state
import max_actions
import max_queue
import max_tls
import post_footer
import schema
import spaces
from services import publication

log = logging.getLogger("max")
API = "https://platform-api2.max.ru"


class _RateGate:
    """Simple sliding-window limiter for documented MAX API request caps."""

    def __init__(self, limit: int, period: float = 1.0):
        self.limit, self.period = limit, period
        self._times: list[float] = []
        self._lock = asyncio.Lock()

    async def wait(self):
        async with self._lock:
            while True:
                now = time.monotonic()
                self._times = [t for t in self._times if now - t < self.period]
                if len(self._times) < self.limit:
                    self._times.append(now)
                    return
                await asyncio.sleep(max(0.001, self.period - (now - self._times[0])))


class _RateRegistry:
    def __init__(self):
        self.api = _RateGate(30)
        self.answers: dict[str, _RateGate] = {}
        self.message_mutation = _RateGate(2)
        self.messages: dict[str, _RateGate] = {}


_RATE_LOOPS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, _RateRegistry]] = weakref.WeakKeyDictionary()


core.db.execute("CREATE TABLE IF NOT EXISTS max_ui (uid TEXT PRIMARY KEY, menu_mid TEXT, btn TEXT)")
core.db.execute("CREATE TABLE IF NOT EXISTS max_draft_ui (mid TEXT PRIMARY KEY, did INTEGER, attempt INTEGER, uid TEXT DEFAULT '')")
schema.add_columns(core.db, "max_draft_ui", ("uid TEXT DEFAULT ''",))
core.db.execute("""CREATE TABLE IF NOT EXISTS max_ui_cleanup (
    op TEXT, mid TEXT, uid TEXT DEFAULT '', attempts INTEGER DEFAULT 0,
    next_try REAL DEFAULT 0, created REAL, last_error TEXT DEFAULT '',
    PRIMARY KEY(op,mid))""")
core.db.execute("CREATE INDEX IF NOT EXISTS max_ui_cleanup_due ON max_ui_cleanup(next_try,created)")
core.db.execute("CREATE TABLE IF NOT EXISTS max_notices (mid TEXT PRIMARY KEY, uid TEXT, remaining INTEGER)")
core.db.execute("CREATE TABLE IF NOT EXISTS max_button_cache (mid TEXT PRIMARY KEY, attachments TEXT)")
core.db.execute("""DELETE FROM max_button_cache WHERE mid NOT IN (
    SELECT u.mid FROM max_draft_ui u JOIN drafts d ON d.id=u.did
    WHERE d.status='pending' AND d.attempts=u.attempt)""")
for _row in core.db.execute("SELECT uid,btn FROM max_ui WHERE btn IS NOT NULL").fetchall():
    try:
        _old = json.loads(_row["btn"])
        core.db.execute("UPDATE max_ui SET btn=? WHERE uid=?",
                        (json.dumps({"mid": _old.get("mid")}), _row["uid"]))
    except (ValueError, AttributeError):
        core.db.execute("UPDATE max_ui SET btn=NULL WHERE uid=?", (_row["uid"],))
core.db.commit()

# Кто сейчас нажал кнопку или написал команду: сообщения-меню для этого человека считаются «шагами меню».
_NAV: contextvars.ContextVar = contextvars.ContextVar("max_nav", default=None)


def _mid(resp):
    """id отправленного сообщения из ответа MAX ({"message": {"body": {"mid": ...}}})."""
    try:
        return (resp or {})["message"]["body"]["mid"] or None
    except (KeyError, TypeError):
        return None


def _id(x):
    """MAX ждёт числовые id, а из настроек и колбэков приходят строки."""
    s = str(x)
    return int(s) if re.fullmatch(r"-?[0-9]+", s) else s


class Max(core.Adapter):
    name = "max"

    def __init__(self, token, channel, approvers, employees, admins=()):
        self.http = httpx.AsyncClient(base_url=API, headers={"Authorization": token}, timeout=60,
                                     verify=max_tls.create_ssl_context())
        self.channel = channel
        self.base_approvers, self.base_employees = list(approvers), list(employees)  # из настроек
        self.admins, self.approvers, self.allowed = [str(a) for a in admins], list(approvers), None
        core.refresh_people(self)  # + люди, добавленные кнопками (хранятся в базе)
        self._tasks: set[asyncio.Task] = set()
        self._processing = asyncio.Semaphore(2)
        self._send_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._upload_cache = {}
        self._upload_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._rate_token = hashlib.sha256(token.encode()).hexdigest()

    CONFIRM_ACTIONS = {"okyes", "okforce", "delyes", "rejectphoto", "rejecttext", "rejectcustom"}

    def _confirmations(self):
        return dialog_state.PendingMap(core.db, "max_confirmation")

    def _clear_confirmation(self, uid):
        self._confirmations().pop((self.name, str(uid)), None)
        dialog_state.PendingMap(core.db, "confirmation_channels").pop((self.name, str(uid)), None)

    def _rates(self) -> _RateRegistry:
        loop = asyncio.get_running_loop()
        registries = _RATE_LOOPS.setdefault(loop, {})
        return registries.setdefault(self._rate_token, _RateRegistry())

    def not_sent(self, e):
        """MAX ответил отказом 4xx (кроме таймаута 408): сообщение не создано."""
        return (bool(getattr(e, "max_not_sent", False))
                or (isinstance(e, httpx.HTTPStatusError)
                    and 400 <= e.response.status_code < 500 and e.response.status_code != 408))

    async def call(self, method, path, **kw):
        rate_key = str(kw.pop("_rate_key", "global"))
        rates = self._rates()
        await rates.api.wait()
        if path == "/answers":
            await rates.answers.setdefault(rate_key, _RateGate(2)).wait()
        elif path == "/messages":
            if method == "POST":
                params = kw.get("params") or {}
                target = next((f"{k}:{params[k]}" for k in ("user_id", "chat_id") if k in params), "unknown")
                await rates.messages.setdefault(target, _RateGate(2)).wait()
            elif method in ("PUT", "DELETE"):
                # The edit/delete API has the same documented 2 rps cap. message_id
                # does not identify its chat locally, so use a conservative shared gate.
                await rates.message_mutation.wait()
        started = time.monotonic()
        try:
            r = await self.http.request(method, path, **kw)
        finally:
            if path != "/updates":
                log.info("timing stage=max_api method=%s endpoint=%s seconds=%.3f", method, path.split("/")[1], time.monotonic() - started)
        r.raise_for_status()
        result = r.json() if r.content else {}
        if isinstance(result, dict) and result.get("success") is False:
            # SimpleQueryResult допускает отказ операции даже при HTTP 200.
            raise RuntimeError("MAX не выполнил запрос: " + str(result.get("message") or "success=false"))
        return result

    # --- отправка ---
    def forget_uploads(self, paths):
        for path in paths:
            self._upload_cache.pop(str(Path(path).resolve()), None)

    async def _cached_upload(self, path, kind, force=False):
        key = str(Path(path).resolve())
        lock = self._upload_locks.setdefault(key, asyncio.Lock())
        async with lock:
            stat = Path(path).stat()
            signature = (stat.st_size, stat.st_mtime_ns, kind)
            entry = self._upload_cache.get(key)
            if not force and entry and entry[0] == signature and time.monotonic() < entry[2]:
                log.info("timing stage=max_upload cache=hit kind=%s", kind)
                return entry[1]
            upload = self._upload_video if kind == "video" else self._upload_image
            started = time.monotonic()
            try:
                token = await upload(path)
            finally:
                log.info("timing stage=max_upload kind=%s seconds=%.3f", kind, time.monotonic() - started)
            self._upload_cache[key] = (signature, token, time.monotonic() + 600)
            return token

    async def _upload_image(self, path) -> str:
        info = await self.call("POST", "/uploads", params={"type": "image"})
        with open(path, "rb") as f:  # обычный файл: в «async with» его нельзя, поэтому отдельный «with»
            async with httpx.AsyncClient(timeout=120) as c:
                r = await c.post(info["url"], files={"data": ("photo.jpg", f, "image/jpeg")})
        r.raise_for_status()
        return next(iter(r.json()["photos"].values()))["token"]

    async def _upload_video(self, path) -> str:
        """Для видео MAX отдаёт токен уже в ответе на POST /uploads (по документации; с реальным токеном не проверено)."""
        info = await self.call("POST", "/uploads", params={"type": "video"})
        with open(path, "rb") as f:
            async with httpx.AsyncClient(timeout=600) as c:
                r = await c.post(info["url"], files={"data": (os.path.basename(path), f, "video/mp4")})
        r.raise_for_status()
        token = info.get("token")
        if not token:  # на случай, если токен придёт в ответе на загрузку файла
            try:
                token = (r.json() or {}).get("token")
            except ValueError:
                token = None
        if not token:
            raise RuntimeError("MAX не вернул токен загруженного видео")
        return token

    # --- состояние интерфейса: одно меню и одна кнопка «Меню админа» на человека ---
    def _ui(self, uid) -> dict:
        r = core.db.execute("SELECT menu_mid, btn FROM max_ui WHERE uid=?", (str(uid),)).fetchone()
        try:
            btn = json.loads(r["btn"]) if r and r["btn"] else None
        except ValueError:
            btn = None
        return {"menu": r["menu_mid"] if r else None, "btn": btn}

    def _ui_set(self, uid, menu=..., btn=...):
        cur = self._ui(uid)
        if menu is not ...:
            cur["menu"] = menu
        if btn is not ...:
            cur["btn"] = btn
        core.db.execute("INSERT OR REPLACE INTO max_ui(uid, menu_mid, btn) VALUES(?,?,?)",
                        (str(uid), cur["menu"], json.dumps(cur["btn"], ensure_ascii=False) if cur["btn"] else None))
        core.db.commit()

    def _queue_ui_cleanup(self, op, mid, uid=None, delay=0):
        if not mid:
            return
        now = time.time()
        core.db.execute("""INSERT INTO max_ui_cleanup(op,mid,uid,next_try,created)
                           VALUES(?,?,?,?,?) ON CONFLICT(op,mid) DO UPDATE SET
                           uid=CASE WHEN excluded.uid!='' THEN excluded.uid ELSE max_ui_cleanup.uid END,
                           next_try=MIN(max_ui_cleanup.next_try,excluded.next_try)""",
                        (op, str(mid), str(uid or ""), now + max(0, delay), now))
        core.db.commit()

    async def _delete(self, mid, uid=None, queue_on_fail=True):
        """Удаляет своё сообщение. 404 означает, что желаемое состояние уже достигнуто."""
        try:
            await self.call("DELETE", "/messages", params={"message_id": mid})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                if queue_on_fail:
                    self._queue_ui_cleanup("delete", mid, uid)
                log.warning("не удалось удалить сообщение %s", mid, exc_info=True)
                return False
        except Exception:
            if queue_on_fail:
                self._queue_ui_cleanup("delete", mid, uid)
            log.warning("не удалось удалить сообщение %s", mid, exc_info=True)
            return False
        if uid is not None:  # удалённое сообщение больше не держит кнопку «Меню админа»
            btn = self._ui(uid)["btn"]
            if btn and btn.get("mid") == mid:
                self._ui_set(uid, btn=None)
        core.db.execute("DELETE FROM max_button_cache WHERE mid=?", (mid,))
        core.db.execute("DELETE FROM max_draft_ui WHERE mid=?", (mid,))
        core.db.execute("DELETE FROM max_notices WHERE mid=?", (mid,))
        core.db.execute("DELETE FROM max_ui_cleanup WHERE mid=?", (mid,))
        core.db.commit()
        return True

    async def _strip_admin_button(self, prev: dict, draft_buttons=False, replacement=None, queue_on_fail=True):
        lock = self._send_locks.setdefault("message:" + str(prev["mid"]), asyncio.Lock())
        async with lock:
            ok = await self._edit_buttons(prev, draft_buttons, replacement)
        if ok is not True and replacement is None and queue_on_fail:
            self._queue_ui_cleanup("strip_draft" if draft_buttons else "strip_admin", prev["mid"])
        return ok

    async def _edit_buttons(self, prev, draft_buttons=False, replacement=None, use_cache=True):
        """Убирает «Меню админа» из старого сообщения: правим клавиатуру, текст и вложения оставляем как были.
        По документации MAX вложения при правке заменяются целиком, поэтому передаём их заново (те же токены)."""
        cached = core.db.execute("SELECT attachments FROM max_button_cache WHERE mid=?", (prev["mid"],)).fetchone() if use_cache else None
        try:
            if cached:
                body = {"attachments": json.loads(cached[0]), "markup": True}
            else:
                message = await self.call("GET", "/messages/" + quote(str(prev["mid"]), safe=""))
                body = message["body"]
            atts = []
            admin_rows = []
            for att in body.get("attachments") or []:
                kind, payload = att["type"], att["payload"]
                if kind == "inline_keyboard":
                    if replacement is not None:
                        admin_rows += [[b for b in row if spaces.payload(b) == "m:admin"] for row in payload["buttons"]]
                        continue
                    rows = [[b for b in row if (not spaces.payload(b).startswith("d:") if draft_buttons
                                               else spaces.payload(b) != "m:admin")] for row in payload["buttons"]]
                    rows = [row for row in rows if row]
                    if rows:
                        atts.append({"type": kind, "payload": {"buttons": rows}})
                elif kind in ("image", "video"):
                    atts.append({"type": kind, "payload": {"token": payload["token"]}})
                else:
                    return  # неизвестное вложение не удаляем при замене клавиатуры
            if replacement is not None:
                rows = spaces.namespace(replacement, getattr(self, "workspace_id", None)) + [row for row in admin_rows if row]
                if rows:
                    atts.append({"type": "inline_keyboard", "payload": {"buttons": rows}})
            edit: dict[str, Any] = {"attachments": atts}
            if not body.get("markup"):
                edit["text"] = body.get("text") or ""
                if body.get("format") in ("html", "markdown"):
                    edit["format"] = body["format"]
            # У форматированного сообщения меняем только вложения: текст и ссылки
            # остаются в MAX без повторного преобразования разметки.
            await self.call("PUT", "/messages", params={"message_id": prev["mid"]}, json=edit)
            # Не кешируем текст или разметку, только запросные токены и клавиатуру.
            if not draft_buttons:
                core.db.execute("UPDATE max_button_cache SET attachments=? WHERE mid=?",
                                (json.dumps(atts, ensure_ascii=False), prev["mid"]))
                core.db.commit()
            return True
        except Exception as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
                return True
            if cached:
                core.db.execute("DELETE FROM max_button_cache WHERE mid=?", (prev["mid"],))
                core.db.commit()
                return await self._edit_buttons(prev, draft_buttons, replacement, use_cache=False)
            log.warning("не удалось убрать кнопку меню из сообщения %s", prev.get("mid"), exc_info=True)

    async def _process_ui_cleanup(self, limit=20):
        now = time.time()
        rows = core.db.execute("""SELECT * FROM max_ui_cleanup WHERE next_try<=?
                                  ORDER BY created LIMIT ?""", (now, limit)).fetchall()
        for row in rows:
            op, mid, uid = row["op"], row["mid"], row["uid"] or None
            if op == "delete":
                ok = await self._delete(mid, uid, queue_on_fail=False)
            elif op in ("strip_admin", "strip_draft"):
                ok = await self._strip_admin_button({"mid": mid}, draft_buttons=op == "strip_draft",
                                                    queue_on_fail=False)
            else:
                ok = True
            if ok is True:
                core.db.execute("DELETE FROM max_ui_cleanup WHERE op=? AND mid=?", (op, mid))
                core.db.commit()
                continue
            attempts = row["attempts"] + 1
            if attempts >= 8:
                log.warning("UI cleanup abandoned op=%s mid=%s after %s attempts", op, mid, attempts)
                core.db.execute("DELETE FROM max_ui_cleanup WHERE op=? AND mid=?", (op, mid))
            else:
                core.db.execute("""UPDATE max_ui_cleanup SET attempts=?,next_try=?,last_error=?
                                   WHERE op=? AND mid=?""",
                                (attempts, now + min(30 * 2 ** (attempts - 1), 3600),
                                 "MAX UI operation failed", op, mid))
            core.db.commit()

    async def cleanup_stale_buttons(self):
        await self._process_ui_cleanup()
        for path, entry in list(self._upload_cache.items()):
            if time.monotonic() >= entry[2] or not Path(path).exists():
                self._upload_cache.pop(path, None)
        stale = ("SELECT u.* FROM max_draft_ui u LEFT JOIN drafts d ON d.id=u.did "
                 "WHERE (d.id IS NULL OR d.status IN ('published','deleted','expired') OR d.attempts!=u.attempt) ")
        cursor = getattr(self, "_stale_cursor", "")
        rows = core.db.execute(stale + "AND u.mid>? ORDER BY u.mid LIMIT 20", (cursor,)).fetchall()
        if not rows and cursor:
            rows = core.db.execute(stale + "ORDER BY u.mid LIMIT 20").fetchall()
        for row in rows:
            # A permanently uneditable message must not starve all later cleanups.
            self._stale_cursor = row["mid"]
            if await self._strip_admin_button({"mid": row["mid"]}, draft_buttons=True):
                core.db.execute("DELETE FROM max_button_cache WHERE mid=?", (row["mid"],))
                core.db.execute("DELETE FROM max_draft_ui WHERE mid=?", (row["mid"],))
                core.db.commit()

    async def _send(self, target: dict, post: str, photo: str | None = None, buttons=None, silent=False, video=None, media=None,
                    admin_menu=True):
        if admin_menu and "user_id" in target:
            lock = self._send_locks.setdefault(str(target["user_id"]), asyncio.Lock())
            async with lock:
                return await self._send_inner(target, post, photo, buttons, silent, video, media, admin_menu)
        return await self._send_inner(target, post, photo, buttons, silent, video, media, admin_menu)

    async def _send_inner(self, target, post, photo=None, buttons=None, silent=False, video=None, media=None, admin_menu=True):
        target = {k: _id(v) for k, v in target.items()}  # user_id/chat_id — числами
        with_admin = bool(admin_menu and "user_id" in target and core.is_admin(self, target["user_id"]))
        base_buttons = [[dict(button) for button in row] for row in (buttons or [])]  # без кнопки админа: нужны, чтобы потом её убрать
        if "user_id" in target:
            issued = []
            for row in base_buttons:
                for button in row:
                    parts = str(button.get("payload", "")).split(":")
                    if len(parts) == 4 and parts[0] == "d" and parts[1] in self.CONFIRM_ACTIONS:
                        nonce = secrets.token_urlsafe(12)
                        button["payload"] += ":" + nonce
                        issued.append(button["payload"])
            if issued:
                key = (self.name, str(target["user_id"]))
                self._confirmations()[key] = issued
                channels = publication.PROMPT_CHANNELS.get()
                if channels is None:
                    draft = core.get(int(issued[0].split(":")[2]))
                    channels = json.loads((draft or {}).get("publication_channels") or "{}")
                dialog_state.PendingMap(core.db, "confirmation_channels")[key] = {p: channels for p in issued}
        buttons = [list(row) for row in base_buttons]
        if with_admin and not any(b.get("payload") == "m:admin" for row in buttons for b in row):
            # Кнопка стоит только под последним сообщением: при отправке нового она убирается из прежнего.
            # Не меняем переданную клавиатуру: она может использоваться для других получателей.
            buttons.append([{"type": "callback", "text": "⚙️ Меню админа", "payload": "m:admin"}])
        attachments = []
        upload_items = media or ([{"type": "video", "path": video}] if video else [{"type": "image", "path": photo}] if photo else [])
        try:
            if media:
                for item in media:
                    attachments.append({"type": item["type"], "payload": {"token": await self._cached_upload(item["path"], item["type"])}})
            elif video:  # само видео; картинка-превью из кадров здесь не нужна
                attachments.append({"type": "video", "payload": {"token": await self._cached_upload(video, "video")}})
            elif photo:
                attachments.append({"type": "image", "payload": {"token": await self._cached_upload(photo, "image")}})
        except Exception as exc:
            # No POST /messages has happened yet: publication retry is definitely safe.
            setattr(exc, "max_not_sent", True)
            raise
        if buttons:
            attachments.append({"type": "inline_keyboard", "payload": {"buttons": spaces.namespace(buttons, getattr(self, "workspace_id", None))}})
        body = {"text": post, "attachments": attachments}
        if isinstance(post, post_footer.LinkedPost):
            body["format"] = "html"
        if silent:
            body["notify"] = False  # без звука (по документации MAX; с реальным токеном не проверено)
        tries, pause = (30, 2.0) if video or any(i["type"] == "video" for i in (media or [])) else (6, 1.5)  # видео MAX обрабатывает дольше картинки
        refreshed = False
        for i in range(tries):  # MAX отвечает "attachment.not.ready", пока вложение обрабатывается
            try:
                resp = await self.call("POST", "/messages", params=target, json=body)
                break
            except httpx.HTTPStatusError as e:
                if "not.ready" in e.response.text and i < tries - 1:
                    await asyncio.sleep(min(pause * (1.6 ** i), 15.0))
                    continue
                if not refreshed and i < tries - 1 and e.response.status_code in (400, 404, 422) and "attachment" in e.response.text.lower() and "not.ready" not in e.response.text and upload_items:
                    refreshed = True
                    try:
                        for index, item in enumerate(upload_items):
                            attachments[index]["payload"]["token"] = await self._cached_upload(item["path"], item["type"], force=True)
                    except Exception as exc:
                        # The preceding POST /messages was explicitly rejected.
                        setattr(exc, "max_not_sent", True)
                        raise
                    continue
                raise
        mid = _mid(resp)
        for row in base_buttons:
            for button in row:
                parts = str(button.get("payload", "")).split(":")
                if mid and len(parts) == 4 and parts[0] == "d" and parts[2].isdigit() and parts[3].isdigit():
                    did, attempt = int(parts[2]), int(parts[3])
                    uid = str(target.get("user_id") or "")
                    core.db.execute("""INSERT OR REPLACE INTO max_draft_ui(mid,did,attempt,uid)
                                       VALUES(?,?,?,?)""", (mid, did, attempt, uid))
                    if uid:
                        for old in core.db.execute("""SELECT mid FROM max_draft_ui
                                                     WHERE did=? AND uid=? AND attempt<? AND mid!=?""",
                                                   (did, uid, attempt, mid)).fetchall():
                            self._queue_ui_cleanup("delete", old["mid"], uid)
                    current = core.get(did)
                    if current and current["status"] == "pending" and current["attempts"] == int(parts[3]):
                        core.db.execute("INSERT OR REPLACE INTO max_button_cache VALUES(?,?)", (mid, json.dumps(attachments, ensure_ascii=False)))
                    core.db.commit()
        if with_admin:
            uid = target["user_id"]
            prev, mid = self._ui(uid)["btn"], _mid(resp)
            self._ui_set(uid, btn={"mid": mid} if mid else prev)
            if prev and prev.get("mid") and prev["mid"] != mid:
                await self._strip_admin_button(prev)
        if admin_menu and mid and "user_id" in target:
            uid = str(target["user_id"])
            core.db.execute("UPDATE max_notices SET remaining=remaining-1 WHERE uid=?", (uid,))
            core.db.commit()
            for notice in core.db.execute("SELECT mid FROM max_notices WHERE uid=? AND remaining<=0", (uid,)).fetchall():
                if await self._delete(notice["mid"], uid):
                    core.db.execute("DELETE FROM max_notices WHERE mid=?", (notice["mid"],))
                    core.db.commit()
            if post.startswith("Это устаревший вариант."):
                core.db.execute("INSERT OR REPLACE INTO max_notices VALUES(?,?,3)", (mid, uid))
                core.db.commit()
        return resp

    async def send_for_approval(self, approver, did, photo, post, silent=False, video=None, media=None):
        d = core.get(did)
        if not d:  # черновик успели удалить — не роняем рассылку остальным
            log.warning("черновик %s не найден, доставка получателю %s пропущена", did, approver)
            return
        n = d["attempts"]
        settings = post_footer.for_draft(did)
        preview = dict(settings)
        if not preview.get("max"):
            preview["max"] = preview.get("tg", "")
        options = json.loads(d.get("candidates") or "[]")
        if options:
            delivered = 0
            for index, option in enumerate(options):
                if not option.get("post"):
                    await self.notify(approver, f"Модель {option['model']}: не удалось подготовить вариант. {option.get('error', '')}")
                    continue
                content = post_footer.render(option["post"], "max", preview)
                header = f"Модель: {option['model']}\n\n"
                if isinstance(content, post_footer.LinkedPost):
                    content = post_footer.LinkedPost(html.escape(header) + content, header + content.plain)
                else:
                    content = header + content
                if len(content.encode("utf-16-le")) // 2 > 4000:
                    await self.notify(approver, f"Вариант {option['model']} слишком длинный для показа со ссылкой.")
                    continue
                rows = [[{"type": "callback", "text": "✅ Выбрать этот вариант", "payload": f"d:choose_{index}:{did}:{n}"}],
                        [{"type": "callback", "text": "🔁 Заново", "payload": f"d:redo:{did}:{n}"}],
                        [{"type": "callback", "text": "❌ Отклонить материал", "payload": f"d:del:{did}:{n}"}]]
                await self._send({"user_id": approver}, content, photo, rows, silent=silent, video=video, media=media)
                delivered += 1
            if not delivered:
                raise RuntimeError("Нет доступных вариантов для согласования")
            return
        post = post_footer.render(post, "max", preview)
        buttons = [[{"type": "callback", "text": label, "payload": payload}
                    for label, payload in row] for row in core.approval_rows(did, n)]
        await self._send({"user_id": approver}, post, photo, buttons, silent=silent, video=video, media=media)

    async def publish(self, photo, post, video=None, media=None):
        if str(self.channel).startswith("user:"):
            target = {"user_id": self.channel.split(":", 1)[1]}  # в личку
        else:
            target = {"chat_id": self.channel}  # группа или канал
        await self._send(target, post, photo, video=video, media=media, admin_menu=False)

    async def notify(self, user_id, text):
        if rows := core.input_rows(self, user_id):
            return await self.send_menu(user_id, text, rows)
        await self._send({"user_id": user_id}, text)

    async def send_notice_menu(self, user_id, text, rows):
        # Notices created before on_callback still participate in single-menu cleanup.
        token = _NAV.set({"uid": str(user_id), "sent_menu": False})
        try:
            return await self.send_menu(user_id, text, rows)
        finally:
            _NAV.reset(token)

    async def send_menu(self, user_id, text, rows, silent=False):
        if spaces.INSTALLED:
            name = spaces.CURRENT.get().name if spaces.active_id() else spaces.root().name
            titled = f"Пространство: {name}\n\n{text}"
            if len(titled.encode("utf-16-le")) // 2 <= 4000:
                text = titled
        buttons = [[{"type": "callback", "text": t, "payload": p} for t, p in row] for row in rows] if rows else None
        nav = _NAV.get()
        if nav is None or str(nav["uid"]) != str(user_id):  # рассылка (напоминания и т. п.): обычное сообщение
            return await self._send({"user_id": user_id}, text, None, buttons, silent=silent)
        if nav.get("inline_mid") and buttons:
            if await self._strip_admin_button({"mid": nav["inline_mid"]}, replacement=buttons):
                nav["sent_menu"] = True
                return {"message": {"body": {"mid": nav["inline_mid"]}}}
            # Если сервис не разрешил правку, оставляем доступное отдельное меню.
        # Шаг меню: прошлое меню этого человека удаляется — остаётся одно. Кнопка «Меню админа» есть на всех
        # экранах, кроме самой главной страницы меню админа (там она не нужна, а на остальных — путь назад).
        main = bool(rows) and rows == core.menu_for(self, user_id)
        resp = await self._send({"user_id": user_id}, text, None, buttons, silent=silent, admin_menu=not main)
        old, new = self._ui(user_id)["menu"], _mid(resp)
        nav["sent_menu"] = True
        self._ui_set(user_id, menu=new)
        if old and old != new:
            nav["deleted"] = old
            await self._delete(old, user_id)
        return resp

    # --- входящие ---
    async def run(self):
        marker = None
        types = "message_created,message_callback,bot_started"
        while True:
            try:
                started = time.monotonic()
                params = {"timeout": 30, "limit": 100, "types": types}
                if marker is not None:
                    params["marker"] = marker
                data = await self.call("GET", "/updates", params=params)
                # MAX считает события подтверждёнными при следующем запросе с marker.
                # Медиа сначала фиксируются в постоянной очереди; остальные события
                # обрабатываются здесь. Ошибка записи в БД не подтверждает пакет.
                batch = [asyncio.create_task(core._guard(self._handle_limited(u)))
                         for u in data.get("updates", [])]
                self._tasks.update(batch)
                try:
                    await asyncio.gather(*batch)
                finally:
                    self._tasks.difference_update(batch)
                marker = data.get("marker", marker)
                if core.SHUTTING_DOWN:
                    return
                # MAX specifies at least 300 ms between polling requests. A fast
                # empty response must not turn long polling into a busy loop.
                delay = 0.3 - (time.monotonic() - started)
                if delay > 0:
                    await asyncio.sleep(delay)
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                if code in (401, 403):  # неверный/просроченный токен: не долбим API каждые 3 секунды
                    log.error("MAX отклонил токен (%s): проверьте MAX_TOKEN. Повтор через 5 минут.", code)
                    await asyncio.sleep(300)
                elif 400 <= code < 500:
                    log.error("MAX вернул %s: %s. Повтор через 30 с.", code, e.response.text[:200])
                    await asyncio.sleep(30)
                else:
                    log.exception("polling error")
                    await asyncio.sleep(3)
            except Exception:
                log.exception("polling error")
                await asyncio.sleep(3)

    async def _handle_limited(self, update):
        async with self._processing:
            if os.getenv("MAX_LOG_UPDATES") == "1":
                message = update.get("message") or {}
                body = message.get("body") or {}
                callback = update.get("callback") or {}
                sender = message.get("sender") or callback.get("user") or update.get("user") or {}
                log.info("MAX update type=%s has_user=%s has_text=%s attachments=%s",
                         update.get("update_type"), bool(sender.get("user_id")), bool(body.get("text")),
                         [a.get("type") for a in (body.get("attachments") or [])])
            if max_queue.is_material(update):
                update = spaces.stamp(update)
                max_queue.enqueue(update)
            elif update.get("update_type") == "message_callback" or ((update.get("message") or {}).get("body") or {}).get("text"):
                # Текст маршрутизируем после предыдущих команд этого человека:
                # нажатие «Править» могло ещё не успеть открыть ожидание ввода.
                max_actions.enqueue({**update, "_route_on_process": True})
            else:
                await self.handle(update)

    async def handle(self, u: dict):
        try:
            if await spaces.route_update(self, u):
                return
            kind = u.get("update_type")
            if kind == "bot_started":
                uid = u["user"]["user_id"]
                if await self._stranger(uid, u["user"].get("name") or u["user"].get("first_name"),
                                        u["user"].get("username")):
                    return
                if not core.quiet(self, uid):
                    await self.notify(uid, self.HELLO)
                if core.is_member(self, uid):
                    await self.send_menu(uid, "Меню участника:", core.menu_for(self, uid))
            elif kind == "message_created":
                await self.on_message(u["message"])
            elif kind == "message_callback":
                await self.on_callback({**u["callback"], "message": u.get("message")})
        except Exception:
            log.exception("handle failed")
            raise  # постоянная очередь должна увидеть ошибку, а не пометить команду выполненной

    HELLO = ("Привет! Пришлите фото или короткое видео вашей работы с кратким описанием в подписи — "
             "я подготовлю пост, который вы сможете отредактировать и опубликовать.")

    async def _stranger(self, uid, name, username=None) -> bool:
        """Человека нет в списках: тихо передаём заявку администраторам. True = обработано."""
        if self.allowed is None or str(uid) in self.allowed:
            return False
        asked = await core.unknown_user(self, uid, name or str(uid), username)
        if not core.quiet(self, uid):
            await self.notify(uid, "Заявка на доступ отправлена администратору 🙏" if asked
                              else "У вас нет доступа к этому боту.")
        return True

    async def on_message(self, m: dict, source_key=None):
        if await spaces.route_message(self, m, source_key):
            return
        if (m.get("recipient") or {}).get("chat_type", "dialog") != "dialog":
            return  # отвечаем только в личке
        sender = m.get("sender") or {}
        if not sender.get("user_id"):
            return
        uid = sender["user_id"]
        quiet = core.quiet(self, uid)
        if await self._stranger(uid, sender.get("name") or sender.get("first_name"), sender.get("username")):
            return
        if source_key and await core.resume_submission(self, source_key) is not None:
            return  # готовый черновик уже есть: повтор события не создаёт ещё один
        body = m.get("body") or {}
        atts = body.get("attachments") or []
        videos = [a for a in atts if a.get("type") == "video"]
        images = [a for a in atts if a.get("type") == "image"]
        if not images and not videos:
            if body.get("text"):
                self._clear_confirmation(uid)
                token = _NAV.set({"uid": str(uid), "sent_menu": False})
                try:
                    if await core.on_text(self, uid, body["text"]):
                        return
                    if core.is_member(self, uid):
                        return await self.send_menu(uid, "Меню участника:", core.menu_for(self, uid))
                finally:
                    _NAV.reset(token)
            return None if quiet else await self.notify(uid, "Пришлите, пожалуйста, фото или видео с описанием в подписи 📷")
        media_atts = [a for a in atts if a.get("type") in ("image", "video")]
        if len(media_atts) > core.MAX_ALBUM_ITEMS:
            return await self.notify(uid, f"В одном посте можно отправить до {core.MAX_ALBUM_ITEMS} фото и видео. Разделите материалы.")
        name = sender.get("name") or sender.get("first_name") or str(uid)
        if core.SHUTTING_DOWN:
            if source_key:
                raise RuntimeError("Бот перезапускается; материал сохранён в очереди")
            return await self.notify(uid, "🔄 Бот перезапускается. Пришлите материал через минуту.")
        if not core.caption_ok(body.get("text")):  # единственное, что отвечаем сотруднику в тихом режиме
            return await self.notify(uid, core.CAPTION_ASK)
        progress_mid = None
        if not quiet and core.confirm_mode() != "off":
            show_progress = True
            if source_key:
                row = spaces.queue_db().execute("SELECT attempts FROM max_inbox WHERE event_key=?", (source_key,)).fetchone()
                show_progress = not row or row["attempts"] <= 1
            if show_progress:
                progress_mid = _mid(await self.notify(uid, "Принято, готовлю пост…"))
        try:
            source_opts = {"source_key": source_key} if source_key else {}
            if len(media_atts) > 1:
                items, total = [], 0
                for index, att in enumerate(media_atts):
                    limit = core.MAX_VIDEO_BYTES if att["type"] == "video" else core.MAX_PHOTO_BYTES
                    data = await max_queue.download(self, att, min(limit, core.MAX_ALBUM_BYTES - total), att["type"], source_key, index)
                    total += len(data)
                    items.append({"type": att["type"], "data": data})
                await core.submit_album(self, str(uid), name, body.get("text") or "", items, **source_opts)
            elif videos:
                data = await max_queue.download(self, videos[0], core.MAX_VIDEO_BYTES, "video", source_key, 0)
                await core.submit(self, str(uid), name, body.get("text") or "", data, kind="video", filename="video.mp4", **source_opts)
            else:
                data = await max_queue.download(self, images[0], core.MAX_PHOTO_BYTES, "image", source_key, 0)
                await core.submit(self, str(uid), name, body.get("text") or "", data, **source_opts)
            # Сам черновик уже доставлен выше, поэтому отдельное «Пост готов» только захламляет чат.
            if progress_mid:
                await self._delete(progress_mid, uid)
        except core.RateLimited:
            if progress_mid:
                await self._delete(progress_mid, uid)
            if source_key:
                raise
            log.warning("лимит материалов: %s", uid)
            if not quiet:
                await self.notify(uid, "Слишком много материалов за час. Попробуйте позже.")
        except core.BadImage as e:  # файл не картинка: администраторов не тревожим
            if progress_mid:
                await self._delete(progress_mid, uid)
            log.info("неподходящий материал от %s: %s", uid, e)
            await self.notify(uid, f"⚠️ {core.err(e)}. Материал не принят. Исправьте файл и отправьте весь набор заново.")
        except Exception as e:
            log.exception("submit failed")
            if progress_mid:
                await self._delete(progress_mid, uid)
            if source_key:
                raise  # очередь повторит временный сбой без повторного запроса сотруднику
            if quiet:  # сотруднику ничего не пишем, ошибка уходит администраторам
                await core.alert(self, f"⚠️ Не удалось обработать материал от {name}: {core.err(e)}")
            else:
                await self.notify(uid, "⚠️ Не получилось обработать материал. Попробуйте ещё раз позже.")

    async def _download(self, att: dict, limit: int, what: str) -> bytes:
        started = time.monotonic()
        try:
            return await self._download_inner(att, limit, what)
        finally:
            log.info("timing stage=download kind=%s seconds=%.3f", what, time.monotonic() - started)

    async def _download_inner(self, att: dict, limit: int, what: str) -> bytes:
        """Скачивает вложение по ссылке из сообщения, не читая больше limit байт (защита памяти от огромных файлов)."""
        payload = att.get("payload") or {}
        url = payload.get("url")
        if what == "video":
            # payload.url у видео не является прямой ссылкой на файл.
            # Официальный getVideoAttachmentDetails: GET /videos/{videoToken}.
            token = payload.get("token")
            if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
                raise RuntimeError("MAX не передал корректный токен видео")
            url = None
            for attempt in range(6):
                details = await self.call("GET", f"/videos/{token}")
                urls = details.get("urls") or {}
                url = next((urls[k] for k in ("mp4_1080", "mp4_720", "mp4_480", "mp4_360", "mp4_240", "mp4_144")
                            if urls.get(k)), None)
                if url:
                    break
                if attempt < 5:
                    await asyncio.sleep(1.5)
        if not url:
            raise RuntimeError(f"MAX не передал ссылку для скачивания {what}")
        async with httpx.AsyncClient(timeout=300) as c, c.stream("GET", url) as r:
            r.raise_for_status()
            if int(r.headers.get("content-length") or 0) > limit:
                raise core.BadImage(f"Файл больше {limit // 1024 // 1024} МБ")
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > limit:
                    raise core.BadImage(f"Файл больше {limit // 1024 // 1024} МБ")
        return bytes(buf)

    async def on_callback(self, cb: dict):
        uid = cb["user"]["user_id"]
        try:
            await self.call("POST", "/answers", params={"callback_id": cb["callback_id"]},
                            json={"notification": "Работаю…"}, _rate_key=f"user:{uid}")
        except Exception:  # ответ на нажатие не критичен (например, он устарел)
            log.warning("callback answer failed", exc_info=True)
        payload = cb.get("payload")
        if not isinstance(payload, str):
            return  # payload не обязателен в официальной схеме Callback
        if not payload.startswith(("m:compare", "m:models_")):
            comparison.picker_state().pop((self.name, str(uid)), None)
        expected_channels = None
        if not payload.startswith(("m:proleok:", "m:pdelok:")):
            dialog_state.PendingMap(core.db, "people_confirmation").pop((self.name, str(uid)), None)
        parts = payload.split(":")
        if len(parts) >= 2 and parts[0] == "d" and parts[1] in self.CONFIRM_ACTIONS:
            pending = self._confirmations().get((self.name, str(uid)), ())
            if len(parts) != 5 or payload not in pending:
                await self.notify(uid, "Это подтверждение больше не действует. Откройте пост и подтвердите действие заново.")
                return
            if parts[1] in ("okyes", "okforce"):
                expected_channels = dialog_state.PendingMap(core.db, "confirmation_channels").get((self.name, str(uid)), {}).get(payload)
                if expected_channels is None:
                    self._clear_confirmation(uid)
                    await self.notify(uid, "Откройте новое подтверждение публикации: прежнее не содержит получателей.")
                    return
            self._clear_confirmation(uid)  # consume before the first await/mutation
            payload = ":".join(parts[:4])
        else:
            self._clear_confirmation(uid)  # cancellation or another menu supersedes the prompt
        pressed = ((cb.get("message") or {}).get("body") or {}).get("mid")  # сообщение, под которым нажали
        ctx = {"uid": str(uid), "sent_menu": False}
        parts = payload.split(":")
        if pressed and len(parts) == 4 and parts[0] == "d" and parts[1] in ("redo", "change", "length", "back"):
            ctx["inline_mid"] = pressed
        token = _NAV.set(ctx)
        channel_token = publication.EXPECTED_CHANNELS.set(expected_channels)
        if pressed and pressed == self._ui(uid)["menu"] and payload.startswith("d:"):
            await self._delete(pressed, uid)
            self._ui_set(uid, menu=None)
            ctx["deleted"] = pressed
        ok = True
        try:
            if payload.startswith("m:"):  # кнопки меню
                text, rows = await core.on_menu(self, uid, payload)
                await self.send_menu(uid, text, rows)
                text = None
            else:
                _, action, did, attempt = payload.split(":")
                _, text = await core.on_action(self, uid, action, int(did), int(attempt))
                if text and (rows := core.input_rows(self, uid)):
                    await self.send_menu(uid, text, rows)
                    text = None
        except Exception as e:
            ok = False
            log.exception("callback failed")
            text = f"⚠️ Ошибка: {core.err(e)}"
        finally:
            _NAV.reset(token)
            publication.EXPECTED_CHANNELS.reset(channel_token)
        if text:
            await self.notify(uid, text)
        # Прошлый шаг не должен висеть. Пост на согласование (кнопки d:) не трогаем: удаляем только сообщения-меню.
        if ok and pressed and pressed != ctx.get("deleted"):
            tracked = self._ui(uid)["menu"]
            if payload.startswith("m:"):
                if payload != "m:admin" and pressed != tracked:
                    await self._delete(pressed, uid)  # старое меню вне учёта (например, от прошлой версии бота)
            elif pressed == tracked and not ctx["sent_menu"]:
                await self._delete(pressed, uid)  # подтверждение или подменю закрыто нажатием без нового меню
                self._ui_set(uid, menu=None)
