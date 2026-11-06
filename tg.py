"""Telegram: только публикация согласованных в MAX фото и видео."""
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo

import core
import post_footer

CAPTION_LIMIT = 1000


def _tg_len(s: str) -> int:
    if isinstance(s, post_footer.LinkedPost):
        s = s.plain
    return len(s.encode("utf-16-le")) // 2


class Telegram(core.Adapter):
    name = "tg"
    publish_only = True

    def __init__(self, token, channel):
        self.bot = Bot(token)
        self.channel = channel
        self.admins, self.approvers, self.allowed = [], [], set()

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

    async def publish(self, photo, post, video=None, media=None):
        ch: str | int = self.channel
        if self.channel.startswith("user:"):       # пост в личку конкретному человеку
            ch = int(self.channel.split(":", 1)[1])
        elif self.channel.lstrip("-").isdigit():   # id пользователя, группы или канала
            ch = int(ch)
        if media:
            formatting: dict[str, Any] = {"parse_mode": "HTML"} if isinstance(post, post_footer.LinkedPost) else {}
            group: list[Any] = []
            short = _tg_len(post) <= CAPTION_LIMIT
            for i, item in enumerate(media):
                cls = InputMediaVideo if item["type"] == "video" else InputMediaPhoto
                kw: dict[str, Any] = {"supports_streaming": True} if item["type"] == "video" else {}
                group.append(cls(media=FSInputFile(item["path"]), caption=post if i == 0 and short else None, **kw, **formatting))
            await self.bot.send_media_group(chat_id=ch, media=group)
            if not short:
                try:
                    await self.bot.send_message(ch, post, **formatting)
                except Exception as e:
                    setattr(e, "partial", True)
                    raise
        else:
            await self._send(ch, photo, post, track=True, video=video)
