"""Замечания четвёртого ревью: утечка токена, остановка по SIGTERM, потеря фото при перезапуске,
последний админ, настройки времени, лимит пикселей, _learn, отложенные черновики."""
import asyncio
import io
import logging
import os
import resource
import subprocess
import sys
import time
import types

import pytest
from conftest import FakeAdapter, draft_id, run
from PIL import Image

import core
import ops

S = types.SimpleNamespace
REAL_SLEEP = asyncio.sleep  # conftest подменяет asyncio.sleep пустышкой; для тестов со временем нужен настоящий
TOKEN = "123456:SECRET_TOKEN_abcdefghijklmnopqrstuvwxyz1"


# ---------- 1. токены не попадают в сообщения и логи ----------
def test_redact_hides_all_kinds_of_secrets(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "my-secret-llm-key-123")
    text = (f"404, url='http://x/file/bot{TOKEN}/photos/f.jpg' и {TOKEN} и sk-aitunnel-abcdef123456 и "
            "https://ghp_TOKEN123@github.com/o/r.git и my-secret-llm-key-123")
    out = core.redact(text)
    for secret in ("SECRET_TOKEN", "abcdef123456", "ghp_TOKEN123", "my-secret-llm-key-123"):
        assert secret not in out
    assert core.redact("обычный текст без секретов 12:30") == "обычный текст без секретов 12:30"


def test_alert_and_error_texts_never_contain_token(tg):
    run(core.alert(tg, f"⚠️ Не удалось обработать фото: 404, url='http://x/file/bot{TOKEN}/f.jpg'"))
    assert tg.sent("notify") and all("SECRET_TOKEN" not in o[2] for o in tg.sent("notify"))
    run(core.alert_staff(tg, f"⚠️ сбой {TOKEN}"))


def test_log_formatter_redacts_message_and_traceback():
    fmt = core.RedactingFormatter("%(message)s")
    try:
        raise RuntimeError(f"url=/bot{TOKEN}/getFile")
    except RuntimeError:
        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "ошибка %s", (TOKEN,), sys.exc_info())
    out = fmt.format(rec)
    assert "SECRET_TOKEN" not in out and "Traceback" in out  # трассировка осталась, секрета в ней нет


# ---------- 3. /status не становится комментарием ----------
def test_status_from_non_admin_never_becomes_a_comment(tg, llm, png):
    tg.admins = ["1"]
    run(core.submit(tg, "9", "Анна", "Заменили масло и фильтры", png))
    did = draft_id()
    run(core.on_action(tg, "2", "redoc", did, 1))  # «2» — согласующий, не админ; ждём комментарий
    calls = len(llm.calls)
    assert run(core.on_text(tg, "2", "/status")) is True
    assert len(llm.calls) == calls and core.get(did)["attempts"] == 1
    assert "администратору" in tg.sent("notify")[-1][2]


# ---------- 4. последний админ ----------
def test_last_admin_cannot_be_removed_but_one_of_two_can():
    a = FakeAdapter("tg", ["1"], admins=["1"])
    a.admins = []
    core.ADAPTERS["tg"] = a
    core.refresh_people(a)
    core._add_person(a, "7", "Админ", "adm")
    assert a.admins == ["7"]
    assert "последнего администратора" in run(core.on_menu(a, "7", "m:pdel:7"))[0]
    assert "подтверждение больше не действует" in run(core.on_menu(a, "7", "m:pdelok:7"))[0]
    assert a.admins == ["7"] and a.allowed is not None  # бот не открылся всем
    core._add_person(a, "8", "Второй", "adm")
    _, rows = run(core.on_menu(a, "7", "m:pdel:8"))
    run(core.on_menu(a, "7", rows[0][0][1]))
    assert a.admins == ["7"]


# ---------- 5. фото не теряется при остановке ----------
def test_cancelled_submit_keeps_working_and_drain_waits_for_it(tg, llm, png, monkeypatch):
    gate = asyncio.Event()

    async def slow(system, text, b64=None):
        await gate.wait()
        return "Готовый пост"
    monkeypatch.setattr(core, "_complete", slow)

    async def scenario():
        outer = asyncio.create_task(core.submit(tg, "9", "Анна", "Заменили масло и фильтры", png))
        for _ in range(200):
            if core.INFLIGHT:
                break
            await REAL_SLEEP(0.01)
        outer.cancel()  # как при остановке: внешняя задача сорвана
        with pytest.raises(asyncio.CancelledError):
            await outer
        assert core.INFLIGHT  # а сама обработка продолжается
        drain = asyncio.create_task(core.drain_inflight(5))
        await REAL_SLEEP(0.05)
        gate.set()
        return await drain
    assert run(scenario()) == 0
    assert core.db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1 and tg.sent("approval")


def test_hard_cancel_of_submit_leaves_no_orphan_file(tg, png, monkeypatch):
    async def hang(*a, **k):
        await asyncio.Event().wait()
    monkeypatch.setattr(core, "_complete", hang)
    before = set(core.PHOTOS.iterdir())

    async def scenario():
        outer = asyncio.create_task(core.submit(tg, "9", "Анна", "Заменили масло и фильтры", png))
        for _ in range(200):
            if core.INFLIGHT and set(core.PHOTOS.iterdir()) - before:
                break
            await REAL_SLEEP(0.01)
        for t in list(core.INFLIGHT):
            t.cancel()  # остановка по таймауту: задачу всё же отменили
        with pytest.raises(asyncio.CancelledError):
            await outer
    run(scenario())
    assert set(core.PHOTOS.iterdir()) == before


def test_housekeeping_removes_only_real_orphans(tg, llm, png):
    run(core.submit(tg, "9", "Анна", "Заменили масло и фильтры", png))
    kept = core.get(draft_id())["photo"]
    old_orphan, fresh_orphan = core.PHOTOS / "orphan_old.jpg", core.PHOTOS / "orphan_new.jpg"
    old_orphan.write_bytes(b"x")
    fresh_orphan.write_bytes(b"x")
    os.utime(old_orphan, (time.time() - 7200, time.time() - 7200))
    core.housekeeping()
    assert not old_orphan.exists() and fresh_orphan.exists() and os.path.exists(kept)
    fresh_orphan.unlink()


# ---------- 6. настройки времени ----------
def test_overnight_window_and_bad_values(monkeypatch):
    monkeypatch.setenv("NOTIFY_TZ", "UTC")
    monkeypatch.setenv("NOTIFY_DAYS", "1-7")
    monkeypatch.setenv("NOTIFY_HOURS", "20-6")
    def at(h):
        return __import__("datetime").datetime(2026, 11, 3, h, 0, tzinfo=__import__("datetime").timezone.utc).timestamp()
    assert ops.in_window(at(23)) and ops.in_window(at(3)) and not ops.in_window(at(12))
    assert "20:00–06:00" in ops.window_text()
    assert ops.config_problem() is None
    for hours in ("25-3", "10-10", "abc", "10"):
        monkeypatch.setenv("NOTIFY_HOURS", hours)
        assert "NOTIFY_HOURS" in ops.config_problem(), hours
    monkeypatch.setenv("NOTIFY_HOURS", "10-18")
    for days in ("1-8", "0-5", "x"):
        monkeypatch.setenv("NOTIFY_DAYS", days)
        assert "NOTIFY_DAYS" in ops.config_problem(), days
    monkeypatch.setenv("NOTIFY_DAYS", "1-5")
    monkeypatch.setenv("NOTIFY_TZ", "Mars/Olympus")
    assert "NOTIFY_TZ" in ops.config_problem()
    monkeypatch.setenv("NOTIFY_TZ", "UTC")
    monkeypatch.setenv("NOTIFY_DEFAULT", "всегда")
    assert "NOTIFY_DEFAULT" in ops.config_problem()


def test_bad_days_setting_does_not_break_notifications_menu(tg, monkeypatch):
    monkeypatch.setenv("NOTIFY_DAYS", "1-8")
    text, rows = ops.notif_view(tg, "1")  # раньше: IndexError
    assert "Рабочие часы" in text and len(rows) == 4


def test_bot_refuses_to_start_with_bad_notify_hours(monkeypatch):
    import main
    monkeypatch.setattr(core, "PROVIDER", "openai")
    for k, v in {"TG_TOKEN": "123456:ABCDEF", "TG_CHANNEL": "-1", "TG_APPROVERS": "1", "LLM_API_KEY": "k",
                 "LLM_BASE_URL": "http://x", "NOTIFY_HOURS": "20-20"}.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit, match="NOTIFY_HOURS"):
        run(main.main())


def test_defer_draft_is_not_lost_when_window_never_opens_with_valid_config(tg, llm, png, monkeypatch):
    monkeypatch.setenv("NOTIFY_HOURS", "20-6")
    monkeypatch.setenv("NOTIFY_DAYS", "1-7")
    monkeypatch.setenv("NOTIFY_TZ", "UTC")
    ops.set_mode(tg, "9", "defer")
    now = __import__("datetime").datetime(2026, 11, 3, 12, 0, tzinfo=__import__("datetime").timezone.utc).timestamp()
    monkeypatch.setattr(time, "time", lambda: now)
    run(core.submit(tg, "9", "Анна", "Заменили масло и фильтры", png))
    assert run(ops.flush_deferred()) == 0  # днём окно закрыто
    monkeypatch.setattr(time, "time", lambda: now + 11 * 3600)  # 23:00
    assert run(ops.flush_deferred()) == 1


# ---------- 7. лимит пикселей ----------
def _png(size, mode="1"):
    buf = io.BytesIO()
    Image.new(mode, size).save(buf, "PNG")
    return buf.getvalue()


def test_huge_png_is_rejected_by_header_without_memory_spike():
    data = _png((10000, 10000))  # 100 Мпикс, файл ~11 КБ
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    with pytest.raises(core.BadImage, match="разрешение"):
        core.save_photo(data)
    grown_mb = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before) // 1024
    assert grown_mb < 100  # раньше +382 МБ


def test_big_jpeg_is_decoded_downscaled(tmp_path):
    buf = io.BytesIO()
    Image.new("RGB", (6000, 6000), (10, 120, 200)).save(buf, "JPEG", quality=60)  # 36 Мпикс
    path = core.save_photo(buf.getvalue())
    with Image.open(path) as out:
        assert max(out.size) <= 1600
    os.unlink(path)


# ---------- 8. _learn ----------
@pytest.mark.parametrize("answer, saved", [
    ("НЕТ", False), ("Нет.", False), ("НЕТ, это разовая правка опечатки.", False), ("- нет, не общее", False),
    ("Пиши короче и без штампов", True), ("Нетривиальные факты выделяй первой строкой", True)])
def test_learn_ignores_negative_answers(llm, answer, saved):
    llm.reply = answer
    assert bool(run(core._learn("замени Иван на Пётр", "Пост про Ивана"))) is saved
    assert core.db.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == int(saved)


# ---------- 9. отложенные черновики ----------
def test_deferred_entry_survives_regeneration_and_is_not_sent_twice(tg, llm, png, monkeypatch):
    run(core.submit(tg, "9", "Анна", "Заменили масло и фильтры", png))
    did = draft_id()
    core.db.execute("DELETE FROM deferred")
    ops.enqueue(tg, "2", did)
    core._set(did, status="regenerating")
    assert run(ops.flush_deferred()) == 0
    assert core.db.execute("SELECT COUNT(*) FROM deferred").fetchone()[0] == 1  # запись на месте
    core._set(did, status="pending")
    before = len(tg.sent("approval"))

    async def twice():
        return await asyncio.gather(ops.flush_deferred(), ops.flush_deferred())
    assert sum(run(twice())) == 1  # цикл напоминаний и смена режима одновременно: дубля нет
    assert len(tg.sent("approval")) == before + 1


# ---------- 10. updater: библиотеки не ставятся в общее окружение до тестов ----------
def test_updater_installs_new_requirements_into_separate_folder(tmp_path):
    import json
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    def git(cwd, *a):
        return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True,
                                             env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                                  "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    (origin / "main.py").write_text("print(1)\n")
    (origin / "requirements.txt").write_text("# старые\n")
    git(origin, "add", "."), git(origin, "commit", "-qm", "v1")
    code = tmp_path / "code"
    git(tmp_path, "clone", "-q", str(origin), str(code))
    (origin / "requirements.txt").write_text("# новые библиотеки\n")
    git(origin, "add", "."), git(origin, "commit", "-qm", "v2")
    data = tmp_path / "data"
    data.mkdir()
    check = f"{sys.executable} -c \"import os,sys; sys.exit(0 if os.sep + 'libs' in os.environ['PYTHONPATH'] else 1)\""
    p = subprocess.run([sys.executable, os.path.join(root, "updater.py")], capture_output=True, text=True,
                       env={**os.environ, "CODE_DIR": str(code), "DATA_DIR": str(data), "UPDATE_TEST_CMD": check,
                            "GITHUB_BRANCH": "main"})
    assert p.returncode == 0
    assert json.loads((data / "update_result.json").read_text())["status"] == "ok"  # тесты видели папку libs


# ---------- мелочи ----------
def test_remind_marks_only_shown_drafts(tg, llm, png, monkeypatch):
    monkeypatch.setenv("NOTIFY_HOURS", "0-24")
    monkeypatch.setenv("NOTIFY_DAYS", "1-7")
    for i in range(7):
        run(core.submit(tg, "9", "Анна", f"Заменили масло, вариант {i}", png))
    t = time.time() - 4 * 3600
    core.db.execute("UPDATE drafts SET created=?, last_sent=?", (t, t))
    core.db.commit()
    assert run(core.remind_once()) == 5
    assert core.db.execute("SELECT COUNT(*) FROM drafts WHERE reminded=1").fetchone()[0] == 5
    assert run(core.remind_once()) == 2  # остальные двое — в следующий круг


def test_comment_on_published_post_says_already_handled(tg, llm, png):
    run(core.submit(tg, "9", "Анна", "Заменили масло и фильтры", png))
    did = draft_id()
    run(core.on_action(tg, "1", "okyes", did, 1))
    ok, msg = run(core._regenerate(tg, "1", did, comment="короче"))
    assert "уже обработан" in msg
