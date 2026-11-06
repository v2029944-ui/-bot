import asyncio
import threading

from conftest import run

import core
import max_queue
import resources


def event(mid, sid):
    return {"update_type": "message_created", "message": {"_space": sid,
        "sender": {"user_id": sid}, "body": {"mid": mid, "text": "Материал"}}}


def test_parallel_materials_use_different_spaces_and_keep_one_per_space():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        class Adapter:
            async def on_message(self, message, **kwargs):
                calls.append(message["body"]["mid"])
                started.set()
                await release.wait()

        keys = [max_queue.enqueue(event(mid, sid)) for mid, sid in [("a", 1), ("b", 1), ("c", 2)]]
        worker = max_queue.Worker(Adapter())
        first = asyncio.create_task(worker.process_once())
        await started.wait()
        started.clear()
        second = asyncio.create_task(worker.process_once())
        await started.wait()
        assert calls == ["a", "c"]
        assert not await worker.process_once()  # обе команды заняты; b ждёт завершения a
        release.set()
        await asyncio.gather(first, second)
        assert await worker.process_once()
        assert calls == ["a", "c", "b"]
        assert core.db.execute("SELECT COUNT(*) FROM max_inbox WHERE state='done'").fetchone()[0] == len(keys)
    run(scenario())


def test_shared_llm_limit_includes_parallel_generations(monkeypatch):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        active, peak = 0, 0

        async def provider(*args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 3:
                started.set()
            await release.wait()
            active -= 1
            return "Пост"

        monkeypatch.setattr(core, "PROVIDER", "openai")
        monkeypatch.setattr(core, "_openai", provider)
        tasks = [asyncio.create_task(core._complete("Правила", "Материал")) for _ in range(6)]
        await asyncio.wait_for(started.wait(), 2)
        assert active == 3
        release.set()
        assert await asyncio.gather(*tasks) == ["Пост"] * 6
        assert peak == 3
    run(scenario())


def test_heavy_media_processing_is_serial_across_parallel_jobs():
    async def scenario():
        started, release = threading.Event(), threading.Event()
        calls = []

        def convert(number):
            calls.append(number)
            if number == 1:
                started.set()
                assert release.wait(2)
            return number

        first = asyncio.create_task(resources.media(convert, 1))
        assert await asyncio.to_thread(started.wait, 2)
        second = asyncio.create_task(resources.media(convert, 2))
        tick = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_later(0.02, tick.set_result, None)
        await tick
        assert calls == [1]
        release.set()
        assert await asyncio.gather(first, second) == [1, 2]
        assert calls == [1, 2]
    run(scenario())
