"""Telegram: только публикация согласованных в MAX фото и видео."""
import asyncio
from typing import Any

from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.exceptions import (
    AiogramError,
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import (
    ChatMemberAdministrator,
    ChatMemberBanned,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberOwner,
    ChatMemberRestricted,
    FSInputFile,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)

import configuration
import core
import post_footer

CAPTION_LIMIT = 1000


def _tg_len(s: str) -> int:
    if isinstance(s, post_footer.LinkedPost):
        s = s.plain
    return len(s.encode("utf-16-le")) // 2


def _numeric_chat_id(channel: str) -> int | None:
    channel = channel.removeprefix("user:")
    digits = channel.removeprefix("-")
    return int(channel) if digits.isascii() and digits.isdecimal() else None


def media_chat_id(channel: str, progress: dict) -> int | None:
    recorded = progress.get("media_chat_id")
    return recorded if type(recorded) is int else _numeric_chat_id(channel)


def _sent_media_chat(messages) -> int | None:
    messages = messages if isinstance(messages, list) else [messages]
    ids = {getattr(getattr(message, "chat", None), "id", None) for message in messages}
    if len(ids) == 1:
        (chat_id,) = ids
        if type(chat_id) is int:
            return chat_id
    return None


class Telegram(core.Adapter):
    name = "tg"
    publish_only = True
    supports_publish_resume = True

    def __init__(self, token, channel):
        self.bot = Bot(token)
        self.channel = channel
        self.admins, self.approvers, self.allowed = [], [], set()

    async def check_channel(self, channel: str) -> str:
        target = channel.removeprefix("user:")
        chat_id = int(target) if configuration.chat_id(target) else target
        try:
            async with asyncio.timeout(5):
                chat = await self.bot.get_chat(chat_id)
        except (AiogramError, TimeoutError) as exc:
            if isinstance(exc, TelegramUnauthorizedError):
                detail = "Telegram отклонил авторизацию бота."
            elif isinstance(exc, TelegramForbiddenError):
                detail = "Telegram запретил боту доступ к чату."
            elif isinstance(exc, TelegramNotFound) or (
                isinstance(exc, TelegramBadRequest) and "chat not found" in exc.message.lower()
            ):
                detail = "чат не найден в Telegram."
            else:
                detail = "не удалось получить сведения о чате."
            return f"Telegram ({channel}): {detail}"
        title = " ".join((chat.title or chat.first_name or "").split())[:200] or "без названия"
        report = f"Telegram: «{title}» (ID {chat.id})"
        if chat.type == ChatType.PRIVATE:
            return f"{report}\nTelegram не сообщает права отправки в личный чат."
        try:
            async with asyncio.timeout(5):
                member = await self.bot.get_chat_member(chat.id, self.bot.id)
        except (AiogramError, TimeoutError):
            return f"{report}\nНе удалось проверить права бота."

        if isinstance(member, ChatMemberOwner):
            rights = "бот — владелец чата."
        elif isinstance(member, (ChatMemberLeft, ChatMemberBanned)) or (
            isinstance(member, ChatMemberRestricted) and not member.is_member
        ):
            rights = "бот не состоит в чате."
        elif chat.type == ChatType.CHANNEL:
            can_publish = member.can_post_messages if isinstance(member, ChatMemberAdministrator) else False
            rights = {True: "есть право публикации в канале.", False: "нет права публикации в канале.",
                      None: "Telegram не сообщил право публикации."}[can_publish]
        elif isinstance(member, ChatMemberAdministrator):
            rights = "бот — администратор группы."
        else:
            allowed = member if isinstance(member, ChatMemberRestricted) else chat.permissions
            if isinstance(member, (ChatMemberMember, ChatMemberRestricted)) and allowed is not None:
                flags = {True: "да", False: "нет", None: "нет данных"}
                rights = ", ".join(f"{label} — {flags[getattr(allowed, field)]}" for label, field in (
                    ("текст", "can_send_messages"), ("фото", "can_send_photos"), ("видео", "can_send_videos"),
                )) + "."
            else:
                rights = "Telegram не сообщил права отправки."
        return f"{report}\nПрава бота: {rights}"

    async def _send(self, chat, photo, post, kb=None, track=False, silent=False, video=None):
        # видео уходит самим файлом (до 50 МБ), а не картинкой-превью
        extra: dict[str, Any]
        send = self.bot.send_video if video else self.bot.send_photo
        media = FSInputFile(video or photo)
        extra = {"supports_streaming": True} if video else {}
        formatting: dict[str, Any] = {"parse_mode": "HTML"} if isinstance(post, post_footer.LinkedPost) else {}
        if _tg_len(post) <= CAPTION_LIMIT:
            await send(chat, media, caption=post, reply_markup=kb, disable_notification=silent, **extra, **formatting)
            return
        await send(chat, media, disable_notification=silent, **extra)
        try:
            await self.bot.send_message(chat, post, reply_markup=kb, disable_notification=silent, **formatting)
        except Exception as e:
            if track:
                setattr(e, "partial", True)  # Медиа уже отправлено; повтор может создать дубль.
            raise

    def not_sent(self, e):
        """Telegram ответил отказом (нет прав, чат не найден и т.п.) и пост не ушёл частями."""
        refused = (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound,
                   TelegramUnauthorizedError, TelegramRetryAfter, TelegramEntityTooLarge)
        return isinstance(e, refused) and not getattr(e, "partial", False)

    async def publish(self, photo, post, video=None, media=None, resume=None, progress=None):
        ch: str | int = _numeric_chat_id(self.channel) or self.channel
        resume = resume or {}
        if resume.get("media") and resume.get("text"):
            return
        progress = progress or (lambda stage, value=True: None)
        formatting: dict[str, Any] = {"parse_mode": "HTML"} if isinstance(post, post_footer.LinkedPost) else {}
        short = _tg_len(post) <= CAPTION_LIMIT

        if resume.get("media"):
            stored_chat = media_chat_id(self.channel, resume)
            if stored_chat is None:
                raise RuntimeError("Числовой ID канала Telegram для отправленного медиа не сохранился")
            ch = stored_chat
        else:
            sent: Message | list[Message]
            if media:
                group: list[Any] = []
                for i, item in enumerate(media):
                    cls = InputMediaVideo if item["type"] == "video" else InputMediaPhoto
                    kw: dict[str, Any] = {"supports_streaming": True} if item["type"] == "video" else {}
                    group.append(cls(media=FSInputFile(item["path"]), caption=post if i == 0 and short else None, **kw, **formatting))
                sent = await self.bot.send_media_group(chat_id=ch, media=group)
                if short:
                    return
            elif short:
                await self._send(ch, photo, post, track=True, video=video)
                return
            else:
                send = self.bot.send_video if video else self.bot.send_photo
                extra: dict[str, Any] = {"supports_streaming": True} if video else {}
                sent = await send(ch, FSInputFile(video or photo), **extra)
            if isinstance(ch, str):
                stored_chat = _sent_media_chat(sent)
                progress("media_chat_id", stored_chat)
                if stored_chat is None:
                    raise RuntimeError("Telegram подтвердил отправку медиа, но не вернул числовой ID канала")
                ch = stored_chat
            else:
                progress("media")
        progress("text_started")
        try:
            await self.bot.send_message(ch, post, **formatting)
        except Exception as e:
            if not resume.get("text_started") and self.not_sent(e):
                progress("text_started", False)
            setattr(e, "partial", True)
            raise
        progress("text")
