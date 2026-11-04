"""MAX-адаптер на прямых запросах к официальному Bot API (https://dev.max.ru/docs-api)."""
import asyncio
import json
import logging
import os
import re

import httpx

import core
import ops

log = logging.getLogger("max")
API = "https://platform-api.max.ru"


def _id(x):
    """MAX ждёт числовые id, а из настроек и колбэков приходят строки."""
    s = str(x)
    return int(s) if s.lstrip("-").isdigit() else s


class Max(core.Adapter):
    name = "max"

    def __init__(self, token, channel, approvers, employees, admins=()):
        self.http = httpx.AsyncClient(base_url=API, headers={"Authorization": token}, timeout=60)
        self.channel = channel
        self.base_approvers, self.base_employees = list(approvers), list(employees)  # из настроек
        self.admins, self.approvers, self.allowed = [str(a) for a in admins], list(approvers), None
        core.refresh_people(self)  # + люди, добавленные кнопками (хранятся в базе)
        self._tasks: set[asyncio.Task] = set()
        self._processing = asyncio.Semaphore(2)

    def not_sent(self, e):
        """MAX ответил отказом 4xx (кроме таймаута 408): сообщение не создано."""
        return (isinstance(e, httpx.HTTPStatusError)
                and 400 <= e.response.status_code < 500 and e.response.status_code != 408)

    async def call(self, method, path, **kw):
        r = await self.http.request(method, path, **kw)
        r.raise_for_status()
        return r.json() if r.content else {}

    # --- отправка ---
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

    async def _send(self, target: dict, post: str, photo: str | None = None, buttons=None, silent=False, video=None, media=None,
                    admin_menu=True):
        target = {k: _id(v) for k, v in target.items()}  # user_id/chat_id — числами
        if admin_menu and "user_id" in target and core.is_admin(self, target["user_id"]):
            # Каждое новое сообщение в личке оставляет доступ к меню под рукой.
            # Не меняем переданную клавиатуру: она может использоваться для других получателей.
            buttons = [list(row) for row in (buttons or [])]
            if not any(b.get("payload") == "m:admin" for row in buttons for b in row):
                buttons.append([{"type": "callback", "text": "⚙️ Меню админа", "payload": "m:admin"}])
        attachments = []
        if media:
            for item in media:
                upload = self._upload_video if item["type"] == "video" else self._upload_image
                attachments.append({"type": item["type"], "payload": {"token": await upload(item["path"])}})
        elif video:  # само видео; картинка-превью из кадров здесь не нужна
            attachments.append({"type": "video", "payload": {"token": await self._upload_video(video)}})
        elif photo:
            attachments.append({"type": "image", "payload": {"token": await self._upload_image(photo)}})
        if buttons:
            attachments.append({"type": "inline_keyboard", "payload": {"buttons": buttons}})
        body = {"text": post, "attachments": attachments}
        if silent:
            body["notify"] = False  # без звука (по документации MAX; с реальным токеном не проверено)
        tries, pause = (30, 2.0) if video or any(i["type"] == "video" for i in (media or [])) else (6, 1.5)  # видео MAX обрабатывает дольше картинки
        for i in range(tries):  # MAX отвечает "attachment.not.ready", пока вложение обрабатывается
            try:
                return await self.call("POST", "/messages", params=target, json=body)
            except httpx.HTTPStatusError as e:
                if "not.ready" in e.response.text and i < tries - 1:
                    await asyncio.sleep(pause)
                    continue
                raise

    async def send_for_approval(self, approver, did, photo, post, silent=False, video=None, media=None):
        d = core.get(did)
        if not d:  # черновик успели удалить — не роняем рассылку остальным
            log.warning("черновик %s не найден, отправка согласующему %s пропущена", did, approver)
            return
        n = d["attempts"]
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
        await self._send({"user_id": user_id}, text)

    async def send_menu(self, user_id, text, rows, silent=False):
        buttons = [[{"type": "callback", "text": t, "payload": p} for t, p in row] for row in rows] if rows else None
        await self._send({"user_id": user_id}, text, None, buttons, silent=silent)

    # --- входящие ---
    async def run(self):
        marker = None
        types = "message_created,message_callback,bot_started"
        while True:
            try:
                params = {"timeout": 30, "limit": 100, "types": types}
                if marker is not None:
                    params["marker"] = marker
                data = await self.call("GET", "/updates", params=params)
                # MAX считает события подтверждёнными при следующем запросе с marker.
                # Не передаём его, пока весь пакет не обработан (включая скачивание).
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
                log.info("MAX update: %s", core.redact(json.dumps(update, ensure_ascii=False))[:1000])
            await self.handle(update)

    async def handle(self, u: dict):
        try:
            kind = u.get("update_type")
            if kind == "bot_started":
                uid = u["user"]["user_id"]
                if await self._stranger(uid, u["user"].get("name") or u["user"].get("first_name"),
                                        u["user"].get("username")):
                    return
                if not core.quiet(self, uid):
                    await self.notify(uid, self.HELLO)
                if str(uid) in self.approvers:
                    await self.send_menu(uid, "Меню согласующего:", core.menu_for(self, uid))
            elif kind == "message_created":
                await self.on_message(u["message"])
            elif kind == "message_callback":
                await self.on_callback(u["callback"])
        except Exception:
            log.exception("handle failed")

    HELLO = ("Привет! Пришлите фото или короткое видео вашей работы с кратким описанием в подписи — "
             "я подготовлю пост и отправлю его на согласование.")

    async def _stranger(self, uid, name, username=None) -> bool:
        """Человека нет в списках: тихо передаём заявку администраторам. True = обработано."""
        if self.allowed is None or str(uid) in self.allowed:
            return False
        asked = await core.unknown_user(self, uid, name or str(uid), username)
        if not core.quiet(self, uid):
            await self.notify(uid, "Заявка на доступ отправлена администратору 🙏" if asked
                              else "У вас нет доступа к этому боту.")
        return True

    async def on_message(self, m: dict):
        if m.get("recipient", {}).get("chat_type", "dialog") != "dialog":
            return  # отвечаем только в личке
        sender = m["sender"]
        uid = sender["user_id"]
        quiet = core.quiet(self, uid)
        if await self._stranger(uid, sender.get("name") or sender.get("first_name"), sender.get("username")):
            return
        body = m.get("body", {})
        atts = body.get("attachments") or []
        videos = [a for a in atts if a.get("type") == "video"]
        images = [a for a in atts if a.get("type") == "image"]
        if not images and not videos:
            if body.get("text"):
                if await core.on_text(self, uid, body["text"]):
                    return
                if str(uid) in self.approvers:
                    return await self.send_menu(uid, "Меню согласующего:", core.menu_for(self, uid))
            return None if quiet else await self.notify(uid, "Пришлите, пожалуйста, фото или видео с описанием в подписи 📷")
        media_atts = [a for a in atts if a.get("type") in ("image", "video")]
        if len(media_atts) > core.MAX_ALBUM_ITEMS:
            return await self.notify(uid, f"В одном посте можно отправить до {core.MAX_ALBUM_ITEMS} фото и видео. Разделите материалы.")
        name = sender.get("name") or sender.get("first_name") or str(uid)
        if core.SHUTTING_DOWN:
            return await self.notify(uid, "🔄 Бот перезапускается. Пришлите материал через минуту.")
        if ops.is_paused():
            return await self.notify(uid, ops.PAUSED_MSG)
        if not core.caption_ok(body.get("text")):  # единственное, что отвечаем сотруднику в тихом режиме
            return await self.notify(uid, core.CAPTION_ASK)
        if not quiet:
            await self.notify(uid, "Принято, готовлю пост…")
        try:
            if len(media_atts) > 1:
                items, total = [], 0
                for att in media_atts:
                    limit = core.MAX_VIDEO_BYTES if att["type"] == "video" else core.MAX_PHOTO_BYTES
                    data = await self._download(att, min(limit, core.MAX_ALBUM_BYTES - total), att["type"])
                    total += len(data)
                    items.append({"type": att["type"], "data": data})
                await core.submit_album(self, str(uid), name, body.get("text") or "", items)
            elif videos:
                data = await self._download(videos[0], core.MAX_VIDEO_BYTES, "video")
                await core.submit(self, str(uid), name, body.get("text") or "", data, kind="video", filename="video.mp4")
            else:
                data = await self._download(images[0], core.MAX_PHOTO_BYTES, "image")
                await core.submit(self, str(uid), name, body.get("text") or "", data)
            if not quiet:
                await self.notify(uid, "Пост отправлен на согласование ✅")
            elif core.confirm_mode() != "off":  # в MAX вместо реакции короткое сообщение
                await self.notify(uid, "✅ Принято")
        except core.RateLimited:
            log.warning("лимит материалов: %s", uid)
            if not quiet:
                await self.notify(uid, "Слишком много материалов за час. Попробуйте позже.")
        except core.BadImage as e:  # файл не картинка: согласующих не тревожим
            log.info("неподходящий материал от %s: %s", uid, e)
            await self.notify(uid, f"⚠️ {core.err(e)}. Материал не принят. Исправьте файл и отправьте весь набор заново.")
        except Exception as e:
            log.exception("submit failed")
            if quiet:  # сотруднику ничего не пишем, ошибка уходит согласующим
                await core.alert(self, f"⚠️ Не удалось обработать материал от {name}: {core.err(e)}")
            else:
                await self.notify(uid, "⚠️ Не получилось обработать материал. Попробуйте ещё раз позже.")

    async def _download(self, att: dict, limit: int, what: str) -> bytes:
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
                            json={"notification": "Работаю…"})
        except Exception:  # ответ на нажатие не критичен (например, он устарел)
            log.warning("callback answer failed", exc_info=True)
        payload = cb.get("payload")
        if not isinstance(payload, str):
            return  # payload не обязателен в официальной схеме Callback
        try:
            if payload.startswith("m:"):  # кнопки меню правил
                text, rows = await core.on_menu(self, uid, payload)
                return await self.send_menu(uid, text, rows)
            _, action, did, attempt = payload.split(":")
            _, text = await core.on_action(self, uid, action, int(did), int(attempt))
        except Exception as e:
            log.exception("callback failed")
            text = f"⚠️ Ошибка: {core.err(e)}"
        if text:
            await self.notify(uid, text)
