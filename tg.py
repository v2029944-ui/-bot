"""Telegram-адаптер (aiogram 3)."""
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import (TelegramBadRequest, TelegramEntityTooLarge, TelegramForbiddenError,
                                TelegramNotFound, TelegramRetryAfter, TelegramUnauthorizedError)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message, ReactionTypeEmoji,
                           ReplyKeyboardMarkup)

import core
import ops

log = logging.getLogger("tg")
CAPTION_LIMIT = 1000  # лимит подписи к фото в Telegram — 1024; берём с запасом, считаем в единицах UTF-16
TG_FILE_LIMIT = 20 * 1024 * 1024  # больше 20 МБ Bot API боту файл не отдаёт


def _tg_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2  # эмодзи занимают 2 единицы
# постоянные кнопки внизу экрана (для согласующих)
REPLY_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📝 Правила стиля"), KeyboardButton(text="➕ Новое правило")],
              [KeyboardButton(text="🔔 Уведомления")]],
    resize_keyboard=True, is_persistent=True)
# у администратора вторым рядом кнопки управления людьми
ADMIN_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📝 Правила стиля"), KeyboardButton(text="➕ Новое правило"),
               KeyboardButton(text="🔔 Уведомления")],
              [KeyboardButton(text="👥 Люди"), KeyboardButton(text="🔗 Пригласить"),
               KeyboardButton(text="📊 Статус")],
              [KeyboardButton(text="⏯ Пауза"), KeyboardButton(text="🔄 Обновить")]],
    resize_keyboard=True, is_persistent=True)


# какие кнопки меню обновляют своё сообщение, а не шлют новое (ссылка-приглашение шлётся новым)
EDIT_IN_PLACE = ("rules", "del", "people", "pdel", "pdelok", "reqs", "acc", "inv", "pset", "prole", "proleok",
                 "notif", "nset", "pause", "pauseset", "upd")


class Telegram(core.Adapter):
    name = "tg"

    def __init__(self, token, channel, approvers, employees, admins=()):
        self.bot, self.dp = Bot(token), Dispatcher()
        self.channel = channel
        self.base_approvers, self.base_employees = list(approvers), list(employees)  # из настроек
        self.admins, self.approvers, self.allowed = [str(a) for a in admins], list(approvers), None
        core.refresh_people(self)  # + люди, добавленные кнопками (хранятся в базе)
        self._username = None
        private = F.chat.type == "private"  # бот-админ группы видит все её сообщения: их не обрабатываем
        self.dp.message.register(self.on_start, CommandStart(), private)
        self.dp.message.register(self.on_menu_cmd, Command("menu"), private)
        self.dp.message.register(self.on_photo, F.photo, private)
        self.dp.message.register(self.on_image_doc, F.document, private)  # фото «как файл»: mime проверяем внутри
        self.dp.message.register(self.on_other, private)
        self.dp.callback_query.register(self.on_cb, F.data.startswith("d:"))
        self.dp.callback_query.register(self.on_menu_cb, F.data.startswith("m:"))

    # --- входящие ---
    def _kb(self, uid):
        """Нижняя клавиатура: только согласующим; администратору — с кнопками «Люди»."""
        uid = str(uid)
        if uid not in self.approvers:
            return None
        return ADMIN_KB if core.is_admin(self, uid) else REPLY_KB

    async def _stranger(self, m: Message) -> bool:
        """Человека нет в списках: тихо передаём заявку администраторам. True = обработано."""
        uid = str(m.from_user.id)
        if self.allowed is None or uid in self.allowed:
            return False
        asked = await core.unknown_user(self, uid, m.from_user.full_name, getattr(m.from_user, "username", None))
        if not core.quiet(self, uid):
            await m.answer("Заявка на доступ отправлена администратору 🙏" if asked
                           else "У вас нет доступа к этому боту.")
        return True

    async def on_start(self, m: Message, command: CommandObject | None = None):
        uid = str(m.from_user.id)
        args = (command.args or "") if command else ""
        if args.startswith("inv_"):  # ссылка-приглашение: одно сообщение в ответ, даже в тихом режиме
            role = await core.redeem_invite(self, uid, m.from_user.full_name, args[4:])
            if role:
                return await m.answer(core.INVITE_WELCOME[role], reply_markup=self._kb(uid))
        if await self._stranger(m) or core.quiet(self, uid):
            return
        await m.answer("Привет! Пришлите фото вашей работы с кратким описанием в подписи — "
                       "я подготовлю пост и отправлю его на согласование.", reply_markup=self._kb(uid))

    async def on_menu_cmd(self, m: Message):
        if str(m.from_user.id) in self.approvers:
            await m.answer("Кнопки меню внизу экрана 👇", reply_markup=self._kb(m.from_user.id))
        elif not core.quiet(self, m.from_user.id):
            await m.answer("Пришлите, пожалуйста, фото с описанием в подписи 📷")

    async def on_other(self, m: Message):
        if m.text:
            if await core.on_text(self, m.from_user.id, m.text):
                return
            if str(m.from_user.id) in self.approvers:
                return await m.answer("Кнопки меню внизу экрана 👇", reply_markup=self._kb(m.from_user.id))
        if await self._stranger(m):
            return
        if not core.quiet(self, m.from_user.id):
            await m.answer("Пришлите, пожалуйста, фото с описанием в подписи 📷")

    async def on_photo(self, m: Message):
        await self._handle_image(m, m.photo[-1])

    async def on_image_doc(self, m: Message):
        mime = m.document.mime_type or ""  # у части клиентов mime_type приходит пустым
        if mime and not mime.startswith("image/"):  # при пустом mime пробуем открыть как картинку
            return await self.on_other(m)
        await self._handle_image(m, m.document)

    async def _handle_image(self, m: Message, file):
        uid, name = str(m.from_user.id), m.from_user.full_name
        quiet = core.quiet(self, uid)
        if await self._stranger(m):
            return None
        size = getattr(file, "file_size", None)
        if size and size > TG_FILE_LIMIT:  # Telegram не отдаст боту такой файл: скажем об этом, а не молча упадём
            if quiet:
                return await core.alert(self, f"⚠️ {name}: файл {size / 1e6:.0f} МБ не скачать (лимит Telegram 20 МБ)")
            return await m.answer("Файл больше 20 МБ — пришлите фото поменьше.")
        if core.SHUTTING_DOWN:  # перезапуск: без ответа фото молча пропало бы
            return await m.answer("🔄 Бот перезапускается. Пришлите фото через минуту.")
        if ops.is_paused():  # одно из исключений тихого режима: иначе фото молча пропало бы
            return await m.answer(ops.PAUSED_MSG)
        if not core.caption_ok(m.caption):  # единственное, что отвечаем сотруднику в тихом режиме
            if getattr(m, "media_group_id", None) and not (m.caption or "").strip():
                return None  # фото альбома без подписи: подпись придёт с другим фото альбома
            return await m.answer(core.CAPTION_ASK)
        if not quiet:
            await m.answer("Принято, готовлю пост…")
        try:
            data = (await self.bot.download(file)).read()
            await core.submit(self, uid, name, m.caption or "", data)
            if not quiet:
                await m.answer("Пост отправлен на согласование ✅")
            else:
                await self._confirm(m)  # доставлено согласующим
        except core.RateLimited:
            log.warning("лимит фото: %s", uid)
            if not quiet:
                await m.answer("Слишком много фото за час. Попробуйте позже.")
        except core.BadImage as e:  # файл не картинка: ошибка пользователя, согласующих не тревожим
            log.info("плохое фото от %s: %s", uid, e)
            if not quiet:
                await m.answer(f"⚠️ {e}. Пришлите обычное фото (JPEG/PNG).")
        except Exception as e:
            log.exception("submit failed")
            if quiet:  # сотруднику ничего не пишем, ошибка уходит согласующим
                await core.alert(self, f"⚠️ Не удалось обработать фото от {name}: {core.err(e)}")
            else:
                await m.answer("⚠️ Не получилось обработать фото. Попробуйте ещё раз позже.")

    async def _ack(self, cb: CallbackQuery):
        try:
            await cb.answer()
        except Exception:  # нажатие устарело: это не причина отказываться его обрабатывать
            log.info("callback answer failed", exc_info=True)

    async def on_cb(self, cb: CallbackQuery):
        await self._ack(cb)
        try:
            _, action, did, attempt = cb.data.split(":")
            ok, text = await core.on_action(self, cb.from_user.id, action, int(did), int(attempt))
            if ok and cb.message:  # убираем кнопки у обработанного черновика
                try:
                    await cb.message.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
        except Exception as e:
            log.exception("callback failed")
            text = f"⚠️ Ошибка: {core.err(e)}"
        if text:
            await self.notify(cb.from_user.id, text)

    async def _confirm(self, m: Message):
        """Подтверждение сотруднику без лишней переписки: реакция 👍 на его сообщение."""
        mode = core.confirm_mode()
        if mode == "off":
            return
        if mode == "reaction":
            try:
                await self.bot.set_message_reaction(chat_id=m.chat.id, message_id=m.message_id,
                                                    reaction=[ReactionTypeEmoji(emoji="👍")])
                return
            except Exception:
                log.warning("reaction failed, fallback to text", exc_info=True)
        await m.answer("✅ Принято")

    async def on_menu_cb(self, cb: CallbackQuery):
        await self._ack(cb)
        try:
            text, rows = await core.on_menu(self, cb.from_user.id, cb.data)
            if cb.message and cb.data.split(":")[1] in EDIT_IN_PLACE:
                try:  # обновляем сообщение на месте (список правил, людей, заявка)
                    return await cb.message.edit_text(text, reply_markup=self._inline(rows) if rows else None)
                except Exception:
                    pass
            await self.send_menu(str(cb.from_user.id), text, rows)
        except Exception as e:
            log.exception("menu callback failed")
            await self.notify(cb.from_user.id, f"⚠️ Ошибка: {core.err(e)}")

    # --- исходящие ---
    @staticmethod
    def _inline(rows):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=p) for t, p in row] for row in rows])

    async def send_menu(self, user_id, text, rows, silent=False):
        await self.bot.send_message(int(user_id), text, reply_markup=self._inline(rows) if rows else None,
                                    disable_notification=silent)

    async def _send(self, chat, photo, post, kb=None, track=False, silent=False):
        if _tg_len(post) <= CAPTION_LIMIT:
            await self.bot.send_photo(chat, FSInputFile(photo), caption=post, reply_markup=kb,
                                      disable_notification=silent)
            return
        await self.bot.send_photo(chat, FSInputFile(photo), disable_notification=silent)
        try:
            await self.bot.send_message(chat, post, reply_markup=kb, disable_notification=silent)
        except Exception as e:
            if track:
                e.partial = True  # фото уже в канале: сбой не значит «ничего не вышло». Признак живёт в самой
            raise                 # ошибке, а не в адаптере, поэтому одновременные публикации не мешают друг другу

    def not_sent(self, e):
        """Telegram ответил отказом (нет прав, чат не найден и т.п.) и пост не ушёл частями."""
        refused = (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound,
                   TelegramUnauthorizedError, TelegramRetryAfter, TelegramEntityTooLarge)
        return isinstance(e, refused) and not getattr(e, "partial", False)

    async def send_for_approval(self, approver, did, photo, post, silent=False):
        d = core.get(did)
        if not d:  # черновик успели удалить/подчистить — молча выходим, не роняя рассылку
            log.warning("черновик %s не найден, отправка согласующему %s пропущена", did, approver)
            return
        n = d["attempts"]
        btn = lambda t, a: InlineKeyboardButton(text=t, callback_data=f"d:{a}:{did}:{n}")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [btn("✅ Опубликовать", "ok")],
            [btn("🔁 Заново", "redo"), btn("✏️ С комментарием", "redoc")],
            [btn("✍️ Править самому", "edit"), btn("🗑 Удалить", "del")]])
        await self._send(int(approver), photo, post, kb, track=True, silent=silent)  # в логе видно: фото ушло, кнопок нет

    async def publish(self, photo, post):
        ch = self.channel
        if ch.startswith("user:"):       # пост в личку конкретному человеку
            ch = int(ch.split(":", 1)[1])
        elif ch.lstrip("-").isdigit():   # id пользователя, группы или канала
            ch = int(ch)
        await self._send(ch, photo, post, track=True)  # @username канала оставляем строкой

    async def notify(self, user_id, text):
        await self.bot.send_message(int(user_id), text)

    async def invite_link(self, token):
        if not self._username:
            self._username = (await self.bot.get_me()).username
        return f"https://t.me/{self._username}?start=inv_{token}"

    async def run(self):
        try:
            await self.bot.set_my_commands([BotCommand(command="menu", description="Меню правил стиля")])
        except Exception:
            log.warning("set_my_commands failed", exc_info=True)
        drop = os.getenv("TG_DROP_PENDING", "0") == "1"  # 1 — после простоя не разбирать старую очередь
        if drop:
            log.warning("TG_DROP_PENDING=1: накопленные за простой сообщения будут отброшены")
        # handle_signals=False: SIGTERM ловит main.serve. Иначе aiogram молча останавливает опрос, а процесс живёт
        await self.dp.start_polling(self.bot, drop_pending_updates=drop, handle_signals=False)