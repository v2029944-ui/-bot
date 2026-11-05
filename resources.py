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


async def media(function, *args, discard=None):
    # Cancelling to_thread does not stop the thread. Keep its permit until the
    # actual work finishes, even if the coroutine waiting for it was cancelled.
    gates = _loops.setdefault(asyncio.get_running_loop(), weakref.WeakValueDictionary())
    gate = gates.setdefault("media", asyncio.Semaphore(1))
    await gate.acquire()
    try:
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
    except BaseException:
        gate.release()
        raise

    def finished(task):
        gate.release()
        if not task.cancelled():
            task.exception()  # collect errors even if the original caller is gone

    worker.add_done_callback(finished)
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        if discard is not None:
            def clean(task):
                if not task.cancelled() and task.exception() is None:
                    discard(task.result())
            worker.add_done_callback(clean)
        raise
