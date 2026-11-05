"""Остановка защищённых операций и освобождение процессов проверки обновлений."""
import asyncio
import os

import pytest

import core
import main
import ops
from conftest import run


def test_shutdown_finishes_cancelling_guarded_work_before_return(monkeypatch):
    async def scenario():
        started, stopped = asyncio.Event(), asyncio.Event()

        async def operation():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        class Worker:
            name = "guarded-worker"

            async def run(self):
                await core._guard(operation())

        async def stop():
            await started.wait()
            ops.request_restart(42, delay=0)

        monkeypatch.setenv("SHUTDOWN_DRAIN_SEC", "0")
        trigger = asyncio.create_task(stop())
        with pytest.raises(SystemExit) as exc:
            await main.serve([Worker()])
        assert exc.value.code == 42
        assert stopped.is_set() and all(t.done() for t in core.INFLIGHT)
        await trigger
    run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_git_timeout_and_cancellation_reap_process(monkeypatch, cancel):
    async def scenario():
        started = asyncio.Event()

        class Process:
            returncode = None
            pid = 987654
            killed = False
            collected = False

            def kill(self):
                self.killed = True
                self.returncode = -9

            async def communicate(self):
                if self.killed:
                    self.collected = True
                    return b"", None
                started.set()
                await asyncio.Event().wait()

        process = Process()

        async def spawn(*args, **kwargs):
            assert kwargs["start_new_session"] == (os.name == "posix")
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        if os.name == "posix":
            monkeypatch.setattr(os, "killpg", lambda pid, sig: process.kill())
        task = asyncio.create_task(ops._git("fetch", timeout=60 if cancel else 0.01))
        await started.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            rc, message = await task
            assert rc == 1 and "вовремя" in message
        assert process.killed and process.collected
    run(scenario())
