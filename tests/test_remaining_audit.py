"""Retention, overnight schedules and cancellation of real worker threads."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import threading
import time

import pytest

import core
import ops
import resources
from conftest import run


@pytest.mark.parametrize('mark', ['max', 'max?', 'max,tg?'])
def test_old_unfinished_publication_is_not_expired_or_warned(tg, llm, png, mark):
    did = run(core.submit(tg, '9', 'Author', 'Keep until channels are reconciled', png))
    now = time.time()
    core._set(did, created=now-10*86400, last_sent=now-8*86400, published_to=mark)
    paths = core.draft_files(core.get(did))
    assert core.housekeeping() == 0
    assert core.get(did)['status'] == 'pending'
    assert core.get(did)['published_to'] == mark
    assert all(Path(path).exists() for path in paths)
    core._set(did, last_sent=now-6.5*86400)
    assert run(core.warn_expiring_once(now)) == 0
    assert not tg.sent('menu')


@pytest.mark.parametrize('day,hour,expected', [
    (6, 23, True), (7, 2, True), (7, 6, False),  # Friday -> Saturday
    (8, 23, False), (9, 2, False), (9, 20, True), (10, 2, True),
])
def test_overnight_shift_belongs_to_starting_weekday(monkeypatch, day, hour, expected):
    monkeypatch.setenv('NOTIFY_HOURS', '20-6')
    monkeypatch.setenv('NOTIFY_DAYS', '1-5')
    monkeypatch.setenv('NOTIFY_TZ', 'UTC')
    ts = datetime(2026, 11, day, hour, tzinfo=timezone.utc).timestamp()
    assert ops.in_window(ts) is expected


def test_cancelled_media_keeps_limit_and_discards_created_files(tmp_path):
    async def scenario():
        started, release = threading.Event(), threading.Event()
        calls, discarded = [], []
        path = tmp_path / 'cancelled-output.jpg'
        loop = asyncio.get_running_loop()
        tick = loop.create_future()

        def convert(number):
            calls.append(number)
            if number == 1:
                started.set()
                assert release.wait(3)
                path.write_bytes(b'private media')
                return str(path)
            return number

        def discard(result):
            discarded.append(result)
            Path(result).unlink()

        first = asyncio.create_task(resources.media(convert, 1, discard=discard))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(resources.media(convert, 2))
            loop.call_later(.02, tick.set_result, None)
            await tick
            assert calls == [1]  # cancellation has not released the heavy-work permit
            release.set()
            assert await asyncio.wait_for(second, 2) == 2
            assert calls == [1, 2]
            assert discarded == [str(path)] and not path.exists()
        finally:
            release.set()
    run(scenario())
