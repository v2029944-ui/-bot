"""Удаление медиа только после полной публикации и срок хранения черновиков."""
import time
from pathlib import Path

from conftest import FakeAdapter, run

import core


def test_single_photo_and_text_removed_after_publication(tg, llm, png):
    did = run(core.submit(tg, "9", "Анна", "Описание работы", png))
    paths = core.draft_files(core.get(did))
    assert all(Path(p).exists() for p in paths)
    assert run(core.on_action(tg, "1", "okyes", did, 1))[0]
    assert all(not Path(p).exists() for p in paths)
    assert core.get(did)["status"] == "published" and core.get(did)["post"] == ""
    assert core.get(did)["source_text"] == core.get(did)["published_to"] == ""


def test_partial_album_retained_until_retry_succeeds(llm, png):
    mx = FakeAdapter("max", ["5"], admins=["5"])
    tg = FakeAdapter("tg", [], fail=[RuntimeError("timeout")], admins=[])
    tg.publish_only = True
    core.ADAPTERS.update(max=mx, tg=tg)
    did = run(core.submit_album(mx, "9", "Анна", "Описание работы", [{"type": "image", "data": png}] * 2))
    paths = core.draft_files(core.get(did))
    assert not run(core.on_action(mx, "5", "okyes", did, 1))[0]
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
    assert core.get(did)["post"] == core.get(did)["source_text"] == ""


def test_partial_publication_preserves_text_until_retry_succeeds(mx, llm, png):
    tg = FakeAdapter("tg", [], fail=[ValueError("Отказ")], sure=(ValueError,), admins=[])
    tg.publish_only = True
    core.ADAPTERS["tg"] = tg
    did = run(core.submit(mx, "9", "Анна", "Важная подпись", png, source_key="source-1"))
    run(core.on_action(mx, "5", "okyes", did, 1))
    core.housekeeping()
    pending = core.get(did)
    assert pending["post"] == llm.reply and pending["source_text"] == "Важная подпись"
    assert core._parse_marks(pending["published_to"]) == {"max": "done"}
    assert Path(pending["photo"]).exists()
    run(core.on_action(mx, "5", "okyes", did, 1))
    finished = core.get(did)
    assert finished["status"] == "published" and finished["source_key"] == "source-1"
    for field in ("post", "source_text", "published_to", "author_id", "author_name", "photo", "video"):
        assert finished[field] == ""
    assert finished["media"] == "[]" and finished["publish_errors"] == "{}"
    assert "MAX — ✅ опубликовано" in mx.sent("menu")[-1][2]
    assert "Telegram — ✅ опубликовано" in mx.sent("menu")[-1][2]
    assert "уже обработан" in run(core.on_action(mx, "5", "okyes", did, 1))[1]
    assert run(core.submit(mx, "9", "Анна", "Повтор события", png, source_key="source-1")) == did
    assert len(llm.calls) == 1 and len(mx.sent("publish")) == len(tg.sent("publish")) == 1


def test_startup_scrubs_old_published_rows_without_touching_pending(mx, llm, png):
    published = run(core.submit(mx, "9", "Анна", "Старая подпись", png))
    pending = run(core.submit(mx, "9", "Анна", "Новая подпись", png))
    core._set(published, status="published", published_to="max,tg", publish_errors='{"tg":"old error"}')
    core.startup()
    assert core.get(published)["post"] == core.get(published)["published_to"] == ""
    assert core.get(published)["author_name"] == ""
    assert core.get(pending)["post"] == llm.reply and core.get(pending)["source_text"] == "Новая подпись"


def test_queue_failure_after_publication_cannot_restore_erased_payload(mx, llm, png, monkeypatch):
    import max_queue
    key = max_queue.enqueue({"message": {"sender": {"user_id": 9},
                                        "body": {"mid": "mid-1", "text": "Исходная подпись"}}})

    async def process(message, source_key=None):
        did = await core.submit(mx, "9", "Анна", message["body"]["text"], png, source_key=source_key)
        await core.on_action(mx, "5", "okyes", did, 1)
        raise RuntimeError("Уведомление сотруднику не дошло")

    monkeypatch.setattr(mx, "on_message", process, raising=False)
    worker = max_queue.Worker(mx)
    assert run(worker.process_once())
    row = core.db.execute("SELECT * FROM max_inbox WHERE event_key=?", (key,)).fetchone()
    assert row["state"] == "done" and row["payload"] == row["error"] == ""
    assert not run(worker.process_once(time.time() + 1000))
    d = dict(core.db.execute("SELECT * FROM drafts WHERE source_key=?", (key,)).fetchone())
    assert d["status"] == "published" and d["post"] == d["source_text"] == ""
