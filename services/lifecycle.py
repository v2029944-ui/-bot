"""Защита текущих операций и остановка."""
import core
import asyncio
import time


async def _guard(coro):
    """Запускает работу отдельной задачей под shield и регистрирует её в INFLIGHT: остановка бота (docker stop)
    дожидается её, а не обрывает на середине (фото принято без черновика, пост опубликован без пометки)."""
    task = asyncio.ensure_future(coro)
    core.INFLIGHT.add(task)
    task.add_done_callback(core.INFLIGHT.discard)
    task.add_done_callback(lambda t: t.cancelled() or t.exception())  # результат заберёт await ниже
    return await asyncio.shield(task)


async def drain_inflight(timeout: float) -> int:
    """Ждём, пока дорабатывают начатые работы (обработка фото, публикация, пересоздание).
    Возвращает, сколько не успело."""
    deadline = time.monotonic() + timeout
    while True:
        pending = {t for t in core.INFLIGHT if not t.done()}
        if not pending or time.monotonic() >= deadline:
            return len(pending)
        core.log.info("жду завершения начатых работ: %s", len(pending))
        await asyncio.wait(pending, timeout=max(0.0, deadline - time.monotonic()))


async def _safe_notify(adapter, user_id, text):
    try:
        if adapter and not core.quiet(adapter, user_id):
            await adapter.notify(user_id, text)
    except Exception:
        core.log.warning("notify failed", exc_info=True)
