"""Защита от дублей: что происходит при сбоях публикации."""
import core
from conftest import FakeAdapter, run


class Refused(Exception):
    """Сервис явно отклонил запрос: пост точно не вышел."""


class Timeout(Exception):
    """Ответ потерялся: пост мог выйти."""


def setup(monkeypatch, llm, png, mx_fail):
    monkeypatch.setenv("PUBLISH_TO_ALL", "1")
    tg = FakeAdapter("tg", ["1"])
    mx = FakeAdapter("max", ["5"], fail=mx_fail, sure=(Refused,))
    core.ADAPTERS.update(tg=tg, max=mx)
    did = run(core.submit(tg, "9", "Вася", "x", png))
    return tg, mx, did


def test_ambiguous_failure_asks_before_retry_and_never_duplicates(monkeypatch, llm, png):
    tg, mx, did = setup(monkeypatch, llm, png, [Timeout("обрыв")])
    ok, text = run(core.on_action(tg, "1", "okyes", did, 1))
    assert not ok and "мог всё же выйти" in text
    assert len(tg.sent("publish")) == 1  # в первый канал ушло, во второй — неизвестно
    run(core.on_action(tg, "1", "okyes", did, 1))  # повтор: вместо публикации — вопрос
    assert "оборвалась" in tg.sent("menu")[-1][2] and not mx.sent("publish")
    assert run(core.on_action(tg, "1", "okforce", did, 1)) == (True, "✅ Опубликовано.")
    assert len(tg.sent("publish")) == 1 and len(mx.sent("publish")) == 1  # дублей нет


def test_definite_failure_allows_plain_retry(monkeypatch, llm, png):
    tg, mx, did = setup(monkeypatch, llm, png, [Refused("нет прав в канале")])
    ok, text = run(core.on_action(tg, "1", "okyes", did, 1))
    assert not ok and "Пост не вышел" in text
    assert run(core.on_action(tg, "1", "okyes", did, 1)) == (True, "✅ Опубликовано.")  # без лишнего подтверждения
    assert len(tg.sent("publish")) == 1 and len(mx.sent("publish")) == 1


def test_definite_failure_keeps_earlier_unknown_mark(monkeypatch, llm, png):
    tg, mx, did = setup(monkeypatch, llm, png, [Timeout("обрыв"), Refused("нет прав")])
    run(core.on_action(tg, "1", "okyes", did, 1))      # 1-я попытка: неизвестно
    run(core.on_action(tg, "1", "okforce", did, 1))  # 2-я: сервис отказал
    assert core._parse_marks(core.get(did)["published_to"]).get("max") == "unknown"  # пометка не потеряна
    run(core.on_action(tg, "1", "okyes", did, 1))
    assert "оборвалась" in tg.sent("menu")[-1][2]


def test_marks_roundtrip():
    marks = {"tg": "done", "max": "unknown"}
    assert core._parse_marks(core._dump_marks(marks)) == marks
    assert core._parse_marks(None) == {} and core._parse_marks("") == {}


def test_startup_recovers_stuck_drafts_and_cleans_old_photos(tg, llm, png):
    from pathlib import Path
    did = run(core.submit(tg, "9", "Вася", "x", png))
    core.db.execute("UPDATE drafts SET status='publishing' WHERE id=?", (did,))
    old = run(core.submit(tg, "9", "Вася", "y", png))
    photo = core.get(old)["photo"]
    core.db.execute("UPDATE drafts SET status='published', created=? WHERE id=?", (0, old))
    core.db.commit()
    core.startup()
    assert core.get(did)["status"] == "pending"
    assert not Path(photo).exists()  # фото опубликованного поста старше 30 дней удалено
