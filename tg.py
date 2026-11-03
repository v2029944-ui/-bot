"""Telegram: только публикация согласованных в MAX фото и видео."""
from aiogram import Bot
from aiogram.exceptions import (TelegramBadRequest, TelegramEntityTooLarge, TelegramForbiddenError,
                                TelegramNotFound, TelegramRetryAfter, TelegramUnauthorizedError)
from aiogram.types import FSInputFile

import core

CAPTION_LIMIT = 1000


def _tg_len(s: str) -> int:
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
        send, media, extra = ((self.bot.send_video, FSInputFile(video), {"supports_streaming": True}) if video
                              else (self.bot.send_photo, FSInputFile(photo), {}))
        if _tg_len(post) <= CAPTION_LIMIT:
            await send(chat, media, caption=post, reply_markup=kb, disable_notification=silent, **extra)
            return
        await send(chat, media, disable_notification=silent, **extra)
        try:
            await self.bot.send_message(chat, post, reply_markup=kb, disable_notification=silent)
        except Exception as e:
            if track:
                e.partial = True  # Медиа уже отправлено; повтор может создать дубль.
            raise

    def not_sent(self, e):
        """Telegram ответил отказом (нет прав, чат не найден и т.п.) и пост не ушёл частями."""
        refused = (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound,
                   TelegramUnauthorizedError, TelegramRetryAfter, TelegramEntityTooLarge)
        return isinstance(e, refused) and not getattr(e, "partial", False)

    async def publish(self, photo, post, video=None):
        ch = self.channel
        if ch.startswith("user:"):       # пост в личку конкретному человеку
            ch = int(ch.split(":", 1)[1])
        elif ch.lstrip("-").isdigit():   # id пользователя, группы или канала
            ch = int(ch)
        await self._send(ch, photo, post, track=True, video=video)
