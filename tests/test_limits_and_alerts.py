"""Лимит фото, тихий режим, тротлинг предупреждений, чистка диска."""
import pytest
from conftest import run

import core


def test_rate_limit_per_employee_with_single_alert(tg, llm, png, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_HOUR", "2")
    run(core.submit(tg, "9", "Вася", "a", png))
    run(core.submit(tg, "9", "Вася", "b", png))
    for _ in range(3):
        with pytest.raises(core.RateLimited):
            run(core.submit(tg, "9", "Вася", "c", png))
    alerts = [o for o in tg.sent("notify") if "больше 2 фото" in o[2]]
    assert len(alerts) == 2  # по одному на каждого из двух согласующих, не по одному на каждое фото
    for _ in range(4):
        run(core.submit(tg, "1", "Админ", "x", png))  # у согласующих лимита нет


def test_alert_is_throttled(tg, monkeypatch):
    run(core.alert(tg, "⚠️ одинаковая ошибка"))
    run(core.alert(tg, "⚠️ одинаковая ошибка"))
    assert len(tg.sent("notify")) == 2  # 2 согласующих, но только одно срабатывание
    monkeypatch.setenv("ALERT_THROTTLE_SEC", "0")
    run(core.alert(tg, "⚠️ одинаковая ошибка"))
    assert len(tg.sent("notify")) == 4


def test_quiet_mode(tg, monkeypatch):
    assert not core.quiet(tg, "9") and not core.quiet(tg, "1")
    monkeypatch.setenv("EMPLOYEE_NOTIFY", "1")
    assert not core.quiet(tg, "9")


def test_llm_failure_leaves_no_orphans(tg, llm, png):
    before = set(core.PHOTOS.glob("*.jpg"))
    llm.reply = RuntimeError("нейросеть недоступна")
    with pytest.raises(RuntimeError):
        run(core.submit(tg, "9", "Вася", "x", png))
    assert core.db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0
    assert set(core.PHOTOS.glob("*.jpg")) == before  # фото этой попытки не осталось на диске


def test_no_deliverable_approver_removes_draft(llm, png):
    class Broken(core.Adapter):
        name, approvers = "tg", ["1"]

        async def send_for_approval(self, *a):
            raise RuntimeError("чат не найден")
    core.ADAPTERS["tg"] = Broken()
    with pytest.raises(RuntimeError, match="Не удалось доставить черновик получателям"):
        run(core.submit(core.ADAPTERS["tg"], "9", "Вася", "x", png))
    assert core.db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0


def test_other_reviewers_never_receive_authors_draft(llm, png):
    class Half(core.Adapter):
        name, approvers = "tg", ["1", "2"]

        def __init__(self):
            self.got = []

        async def send_for_approval(self, a, did, photo, post):
            if a == "1":
                raise RuntimeError("не нажал Start")
            self.got.append(a)
    a = Half()
    core.ADAPTERS["tg"] = a
    run(core.submit(a, "9", "Вася", "x", png))
    assert a.got == ["9"]
