"""Удаление медиа только после полной публикации и срок хранения черновиков."""
from pathlib import Path
import time

import core
from conftest import FakeAdapter, run


def test_single_photo_removed_after_publication_but_text_remains(tg, llm, png):
    did = run(core.submit(tg, "9", "Анна", "Описание работы", png))
    paths = core.draft_files(core.get(did))
    assert all(Path(p).exists() for p in paths)
    assert run(core.on_action(tg, "1", "ok", did, 1))[0]
    assert all(not Path(p).exists() for p in paths)
    assert core.get(did)["status"] == "published" and core.get(did)["post"] == llm.reply


def test_partial_album_retained_until_retry_succeeds(llm, png):
    mx = FakeAdapter("max", ["5"])
    tg = FakeAdapter("tg", [], fail=[RuntimeError("timeout")])
    tg.publish_only = True
    core.ADAPTERS.update(max=mx, tg=tg)
    did = run(core.submit_album(mx, "9", "Анна", "Описание работы", [{"type": "image", "data": png}] * 2))
    paths = core.draft_files(core.get(did))
    assert not run(core.on_action(mx, "5", "ok", did, 1))[0]
    core.housekeeping()
    assert all(Path(p).exists() for p in paths)
    assert run(core.on_action(mx, "5", "okforce", did, 1))[0]
    assert all(not Path(p).exists() for p in paths)
    assert len(mx.sent("publish_media")) == 1


def test_default_seven_days_from_last_delivery(tg, llm, png):
    old = run(core.submit(tg, "9", "Анна", "Старый материал", png))
    fresh = run(core.submit(tg, "9", "Анна", "Новый материал", png))
    now = time.time()
    core._set(old, created=now - 10 * 86400, last_sent=now - 8 * 86400)
    core._set(fresh, created=now - 10 * 86400, last_sent=now - 6 * 86400)
    assert core.housekeeping() == 1
    assert core.get(old)["status"] == "expired"
    assert all(not Path(p).exists() for p in core.draft_files(core.get(old)))
    assert core.get(fresh)["status"] == "pending"
    assert all(Path(p).exists() for p in core.draft_files(core.get(fresh)))


def test_startup_removes_recently_published_files_from_previous_version(tg, llm, png):
    did = run(core.submit(tg, "9", "Анна", "Описание работы", png))
    core._set(did, status="published")
    paths = core.draft_files(core.get(did))
    core.startup()
    assert all(not Path(p).exists() for p in paths)
    assert core.get(did)["post"] == llm.reply
