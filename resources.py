"""Общие ограничения тяжёлых операций для всех пространств одного процесса."""
import asyncio
from contextlib import asynccontextmanager
import weakref

_loops = weakref.WeakKeyDictionary()


@asynccontextmanager
async def slot(kind, limit):
    # Отдельные циклы тестов не делят asyncio.Semaphore между собой.
    gates = _loops.setdefault(asyncio.get_running_loop(), weakref.WeakValueDictionary())
    gate = gates.setdefault(kind, asyncio.Semaphore(limit))
    async with gate:
        yield


async def media(function, *args):
    async with slot("media", 1):
        return await asyncio.to_thread(function, *args)
