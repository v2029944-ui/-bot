"""Пятое ревью: счётчик напоминаний, дни через воскресенье, остановка во время публикации, пустые настройки,
лимит HEIC, закреплённые версии, защита от утечек {e}."""
import asyncio
import datetime
import re
import time
from pathlib import Path

import pytest
from conftest import FakeAdapter, draft_id, run

import core
import ops

ROOT = Path(__file__).resolve().parents[1]
REAL_SLEEP = asyncio.sleep


# ---------- A. заголовок напоминания ----------
def test_reminder_header_counts_all_waiting_drafts(tg, llm, png, monkeypatch):
    monkeypatch.setenv("NOTIFY_HOURS", "0-24")
    monkeypatch.setenv("NOTIFY_DAYS", "1-7")
    for i in range(12):
        run(core.submit(tg, "9", "Анна", f"Заменили масло, вариант {i}", png))
    t = time.time() - 4 * 3600
    core.db.execute("UPDATE drafts SET created=?, last_sent=?", (t, t))
    core.db.commit()
    headers = []
    for expected_marked in (5, 5, 2):  # три круга: 12 → 7 → 2 ждущих
        before = len(tg.sent("menu"))
        assert run(core.remind_once()) == expected_marked
        headers.append(tg.sent("menu")[before][2].split("\n")[0])
    assert [re.search(r"ждут решения: (\d+)", h, re.I).group(1) for h in headers] == ["12", "7", "2"]


# ---------- B. дни недели через воскресенье ----------
def test_days_can_wrap_through_sunday(monkeypatch):
    monkeypatch.setenv("NOTIFY_HOURS", "0-24")
    monkeypatch.setenv("NOTIFY_TZ", "UTC")
    for raw, expected in (("6-2", {6, 7, 1, 2}), ("7-1", {7, 1}), ("1-5", {1, 2, 3, 4, 5}), ("1,3,5", {1, 3, 5})):
        monkeypatch.setenv("NOTIFY_DAYS", raw)
        assert ops._days() == expected and ops.config_problem() is None, raw
    monkeypatch.setenv("NOTIFY_DAYS", "6-2")
    def day(d):
        return datetime.datetime(2026, 11, d, 12, 0, tzinfo=datetime.UTC).timestamp()
    # 7 ноября 2026 — суббота
    assert [ops.in_window(day(d)) for d in (7, 8, 9, 10, 11)] == [True, True, True, True, False]
    assert "сб" in ops.window_text()
    for bad in ("1-8", "0-5", "x", "8-1"):
        monkeypatch.setenv("NOTIFY_DAYS", bad)
        assert "NOTIFY_DAYS" in ops.config_problem(), bad


# ---------- C. остановка не обрывает публикацию, а новые кнопки в этот момент не принимаются ----------
class SlowPublish(FakeAdapter):
    gate = None

    async def publish(self, photo, post):
        await self.gate.wait()
        await super().publish(photo, post)


def test_shutdown_waits_for_publish_in_progress(llm, png, monkeypatch):
    ad = SlowPublish("tg", ["1", "2"], admins=["1", "2"])
    core.ADAPTERS["tg"] = ad
    run(core.submit(ad, "9", "Анна", "Заменили масло и фильтры", png))
    did = draft_id()

    async def scenario():
        ad.gate = asyncio.Event()
        press = asyncio.create_task(core.on_action(ad, "1", "okyes", did, 1))
        for _ in range(200):
            if core.INFLIGHT:
                break
            await REAL_SLEEP(0.01)
        assert core.get(did)["status"] == "publishing"
        monkeypatch.setattr(core, "SHUTTING_DOWN", True)  # пошла остановка
        ok, msg = await core.on_action(ad, "2", "redo", did, 1)  # новую работу не начинаем
        assert not ok and "перезапускается" in msg
        press.cancel()  # внешняя задача сорвана, как при остановке
        with pytest.raises(asyncio.CancelledError):
            await press
        drain = asyncio.create_task(core.drain_inflight(5))
        await REAL_SLEEP(0.05)
        assert not drain.done()  # ждём публикацию, а не выходим сразу
        ad.gate.set()
        return await drain
    assert run(scenario()) == 0
    assert core.get(did)["status"] == "published" and ad.sent("publish")  # пометка и статус сохранены, вопроса «могло выйти» нет
    assert core.get(did)["published_to"] == ""  # конечного статуса достаточно для защиты от дублей


def test_drain_returns_how_many_did_not_finish():
    async def scenario():
        t = asyncio.ensure_future(asyncio.Event().wait())
        core.INFLIGHT.add(t)
        left = await core.drain_inflight(0.05)
        t.cancel()
        core.INFLIGHT.discard(t)
        return left
    assert run(scenario()) == 1


# ---------- D. пустое значение настройки = «не задано» ----------
def test_empty_settings_fall_back_to_defaults(tg, monkeypatch):
    for name in ("NOTIFY_HOURS", "NOTIFY_DAYS", "NOTIFY_TZ", "NOTIFY_DEFAULT", "MIN_CAPTION_CHARS", "REMIND_AFTER_MIN",
                 "RATE_LIMIT_PER_HOUR", "ALERT_THROTTLE_SEC", "DRAFT_TTL_DAYS", "HOUSEKEEPING_SEC", "STYLE", "COMPANY"):
        monkeypatch.setenv(name, "")
    assert ops.config_problem() is None
    assert ops._hours() == (10, 18) and ops._days() == {1, 2, 3, 4, 5}
    assert ops.default_mode() == "silent"
    assert core.caption_ok("Заменили масло") and not core.caption_ok("ок")
    assert core.env("НЕТ_ТАКОЙ", "по умолчанию") == "по умолчанию"
    assert core.env("X_SPACES", "d") == "d"  # не задана
    monkeypatch.setenv("X_SPACES", "   ")
    assert core.env("X_SPACES", "d") == "d"
    monkeypatch.setenv("X_SPACES", " 5 ")
    assert core.env("X_SPACES", "d") == "5"
    core.startup()  # TTL и прочие числа читаются без падения


# ---------- лимит HEIC ----------
def test_iphone_48mp_heif_is_allowed_but_huge_png_is_not():
    iphone = 8064 * 6048  # 48,8 Мпикс
    assert iphone <= core._pixel_limit("HEIF")
    assert iphone > core._pixel_limit("PNG")  # у PNG/WebP остаётся 40 Мпикс: они разворачиваются целиком
    assert core._pixel_limit("MPO") == core._pixel_limit("JPEG") > 100_000_000
    assert core._pixel_limit(None) == core._pixel_limit("что-то-новое")


# ---------- закреплённые версии ----------
def test_direct_dependencies_are_pinned_so_rollback_can_downgrade():
    lines = [line.split("#")[0].strip() for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()]
    reqs = [line for line in lines if line]
    unpinned = [r for r in reqs if "==" not in r and not r.startswith("tzdata")]
    assert reqs and not unpinned, f"версии не закреплены (откат библиотек не сработает): {unpinned}"


# ---------- сторож: сообщение об ошибке не должно уйти человеку или в лог без очистки ----------
def test_error_text_is_never_interpolated_without_redaction():
    """Чистка стоит на выходе, а само исключение содержит токен. Любая новая строка вида f"…{e}" без core.err()
    снова создала бы утечку, поэтому тест падает на ней. Исключения перечислены явно."""
    allowed = ("Пришлите обычное фото",  # BadImage: текст — собственный текст бота, а не сетевой ошибки
               "Генерация: {last}")  # status_text: тут last — готовая строка (текст ошибки уже прошёл err())
    offenders = []
    for path in [*ROOT.glob("*.py"), *ROOT.glob("services/*.py")]:
        name = str(path.relative_to(ROOT))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            leaky = re.search(r'\{(e|exc|ex|last)\}', line) or (
                re.search(r"str\((e|exc|ex|last)\)", line) and not re.search(r"(redact|scrub)\(str\(", line))
            if leaky and not any(a in line for a in allowed):
                offenders.append(f"{name}:{n}: {line.strip()[:100]}")
    assert not offenders, "ошибка вставлена в текст без core.err()/redact():\n" + "\n".join(offenders)
