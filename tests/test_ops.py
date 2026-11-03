"""Пауза, уведомления по расписанию, роли, обновление с GitHub, скрипты updater.py и entrypoint.sh."""
import asyncio
import json
import os
import subprocess
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

import core
import ops
from conftest import FakeAdapter, draft_id, run

S = types.SimpleNamespace
ROOT = Path(__file__).resolve().parents[1]


def admin_adapter(name="tg", approvers=("1", "2"), admins=("1",)):
    a = FakeAdapter(name, list(approvers))
    a.admins = list(admins)
    core.ADAPTERS[name] = a
    core.refresh_people(a)
    return a


def ts(y, m, d, h, tz_hours=5):
    """Метка времени для локального часа в часовом поясе UTC+tz_hours (Екатеринбург = +5)."""
    return datetime(y, m, d, h - tz_hours, 0, tzinfo=timezone.utc).timestamp()


# ---------- рабочее окно ----------
def test_window_weekdays_10_to_18_yekaterinburg(monkeypatch):
    for k in ("NOTIFY_HOURS", "NOTIFY_DAYS", "NOTIFY_TZ"):
        monkeypatch.delenv(k, raising=False)
    mon = (2026, 10, 26)  # понедельник
    assert ops.in_window(ts(*mon, 10)) and ops.in_window(ts(*mon, 17))
    assert not ops.in_window(ts(*mon, 9)) and not ops.in_window(ts(*mon, 18))
    assert not ops.in_window(ts(2026, 10, 24, 12))  # суббота
    assert not ops.in_window(ts(2026, 10, 25, 12))  # воскресенье
    assert "пн–пт, 10:00–18:00" in ops.window_text()


def test_window_settings_and_bad_values(monkeypatch):
    monkeypatch.setenv("NOTIFY_DAYS", "1,3,5")
    assert ops._days() == {1, 3, 5}
    monkeypatch.setenv("NOTIFY_HOURS", "мусор")
    assert ops._hours() == (0, 24)


# ---------- режимы уведомлений ----------
def test_delivery_modes_inside_and_outside_window(monkeypatch):
    a = admin_adapter()
    night = ts(2026, 10, 26, 22)
    noon = ts(2026, 10, 26, 12)
    ops.set_mode(a, "2", "normal")
    assert ops.delivery_for(a, "2", night) == "normal"
    ops.set_mode(a, "2", "silent")
    assert ops.delivery_for(a, "2", night) == "silent" and ops.delivery_for(a, "2", noon) == "normal"
    ops.set_mode(a, "2", "defer")
    assert ops.delivery_for(a, "2", night) == "defer" and ops.delivery_for(a, "2", noon) == "normal"
    ops.set_mode(a, "2", "muted")
    assert ops.delivery_for(a, "2", noon) == "silent" and ops.delivery_for(a, "2", night) == "silent"


def test_default_mode_comes_from_settings(monkeypatch):
    a = admin_adapter()
    monkeypatch.setenv("NOTIFY_DEFAULT", "defer")
    assert ops.get_mode(a, "2") == "defer"
    monkeypatch.setenv("NOTIFY_DEFAULT", "что-то")
    assert ops.get_mode(a, "2") == "silent"


def test_menu_lets_every_approver_choose_mode():
    a = admin_adapter()
    text, rows = run(core.on_menu(a, "2", "m:notif"))  # обычный согласующий
    assert "Уведомления" in text and len(rows) == 4
    text, rows = run(core.on_menu(a, "2", "m:nset:defer"))
    assert "Сохранено" in text and ops.get_mode(a, "2") == "defer"
    assert any(b[0].startswith("✓ ⏳") for r in rows for b in [r[0]])
    run(core.on_menu(a, "2", "m:nset:мусор"))
    assert ops.get_mode(a, "2") == "defer"  # неизвестный режим не принимается


def test_draft_goes_silent_outside_hours_and_waits_in_defer_mode(tg, llm, png, monkeypatch):
    a = tg
    a.admins = ["1"]
    core.refresh_people(a)
    monkeypatch.setattr(time, "time", lambda: ts(2026, 10, 26, 22))  # понедельник, 22:00 по Екатеринбургу
    ops.set_mode(a, "1", "silent")
    ops.set_mode(a, "2", "defer")
    run(core.submit(a, "9", "Анна", "Заменили масло и фильтры", png))
    did = draft_id()
    assert [o[1] for o in a.sent("approval")] == ["1"] and a.sent("silent")  # "1" получил, но тихо
    assert core.db.execute("SELECT uid FROM deferred WHERE did=?", (did,)).fetchone()["uid"] == "2"
    assert core.get(did)["status"] == "pending"  # черновик не пропал, хотя «живых» доставок нет у второго

    # утро: началось рабочее время — придержанное приходит со звуком, напоминание считается от доставки
    monkeypatch.setattr(time, "time", lambda: ts(2026, 10, 27, 10))
    assert run(ops.flush_deferred()) == 1
    assert [o[1] for o in a.sent("approval")] == ["1", "2"] and len(a.sent("silent")) == 1
    assert core.db.execute("SELECT COUNT(*) FROM deferred").fetchone()[0] == 0
    assert core.get(did)["last_sent"] == ts(2026, 10, 27, 10)


def test_deferred_draft_is_dropped_if_resolved_and_sent_at_once_when_mode_changes(tg, llm, png, monkeypatch):
    a = tg
    monkeypatch.setattr(time, "time", lambda: ts(2026, 10, 26, 22))
    ops.set_mode(a, "2", "defer")
    run(core.submit(a, "9", "Анна", "Заменили масло и фильтры", png))
    did = draft_id()
    run(core.on_menu(a, "2", "m:nset:normal"))  # передумал: всё придержанное приходит сразу
    assert [o[1] for o in a.sent("approval")] == ["1", "2"]

    run(core.submit(a, "9", "Анна", "Ещё одно описание", png))
    did2 = draft_id()
    ops.set_mode(a, "2", "defer")
    core.db.execute("DELETE FROM deferred")
    ops.enqueue(a, "2", did2)
    core._set(did2, status="deleted")
    monkeypatch.setattr(time, "time", lambda: ts(2026, 10, 27, 11))
    assert run(ops.flush_deferred()) == 0
    assert core.db.execute("SELECT COUNT(*) FROM deferred").fetchone()[0] == 0  # удалённый черновик не шлём


def test_actor_gets_new_variant_immediately_even_in_defer_mode(tg, llm, png, monkeypatch):
    run(core.submit(tg, "9", "Анна", "Заменили масло и фильтры", png))
    did = draft_id()
    monkeypatch.setattr(time, "time", lambda: ts(2026, 10, 26, 22))
    ops.set_mode(tg, "1", "defer")
    before = len(tg.sent("approval"))
    run(core.on_action(tg, "1", "redo", did, 1))
    assert [o[1] for o in tg.sent("approval")[before:]] == ["1", "2"]  # он сам нажал — ждать нечего


def test_reminders_respect_mute_and_pause(tg, llm, png, monkeypatch):
    monkeypatch.setenv("NOTIFY_HOURS", "0-24")
    monkeypatch.setenv("NOTIFY_DAYS", "1-7")
    run(core.submit(tg, "9", "Анна", "Заменили масло и фильтры", png))
    did = draft_id()
    t = time.time() - 4 * 3600
    core.db.execute("UPDATE drafts SET created=?, last_sent=?", (t, t))
    core.db.commit()
    ops.set_paused(True)
    assert run(core.remind_once()) == 0  # на паузе не напоминаем
    ops.set_paused(False)
    ops.set_mode(tg, "2", "muted")
    assert run(core.remind_once()) == 1
    assert [o[1] for o in tg.sent("silent")] == ["2"]  # «мут» — тихо, второму — со звуком


# ---------- пауза ----------
def test_pause_is_admin_only_and_persistent():
    a = admin_adapter()
    assert "только администратор" in run(core.on_menu(a, "2", "m:pause"))[0]
    text, rows = run(core.on_menu(a, "1", "m:pause"))
    assert "работает" in text and rows[0][0][1] == "m:pauseset:1"
    text, rows = run(core.on_menu(a, "1", "m:pauseset:1"))
    assert ops.is_paused() and rows[0][0][1] == "m:pauseset:0"
    assert ops.get_setting("paused") == "1"  # лежит в базе: переживёт перезапуск
    run(core.on_menu(a, "1", "m:pauseset:0"))
    assert not ops.is_paused()


# ---------- роли ----------
def make_person(a, uid, name, role):
    core._add_person(a, uid, name, role)


def test_admin_changes_roles_with_confirmation():
    a = admin_adapter()
    make_person(a, "77", "Иван", "emp")
    text, rows = run(core.on_menu(a, "1", "m:pset:77"))
    assert "сейчас: сотрудник" in text and [b[1] for b in rows[0]] == ["m:prole:emp:77", "m:prole:apr:77", "m:prole:adm:77"]
    text, rows = run(core.on_menu(a, "1", "m:prole:adm:77"))
    assert "администратор" in text and rows[0][0][1] == "m:proleok:adm:77" and "добавляет и убирает людей" in text
    assert "77" not in a.admins  # без подтверждения ничего не изменилось
    run(core.on_menu(a, "1", "m:proleok:adm:77"))
    assert "77" in a.admins and "77" in a.approvers and core.role_of(a, "77") == "adm"
    assert any(o[1] == "77" and "роль изменена" in o[2] for o in a.sent("notify"))
    assert is_admin_menu_visible(a, "77")
    run(core.on_menu(a, "77", "m:proleok:apr:77"))  # сам себя понизил: другой админ остался
    assert "77" not in a.admins and "77" in a.approvers


def is_admin_menu_visible(a, uid):
    return ("👥 Люди", "m:people") in [b for r in core.menu_for(a, uid) for b in r]


def test_cannot_demote_last_admin_and_env_people_are_fixed():
    a = admin_adapter(admins=())  # админов в настройках нет
    make_person(a, "77", "Иван", "adm")
    assert a.admins == ["77"]
    text, _ = run(core.on_menu(a, "77", "m:prole:emp:77"))
    assert "последнего администратора" in text
    run(core.on_menu(a, "77", "m:proleok:emp:77"))
    assert a.admins == ["77"]
    # администратор из настроек в списке «из настроек» и кнопки смены роли у него нет
    b = admin_adapter("max", approvers=("5",), admins=("5",))
    text, rows = run(core.on_menu(b, "5", "m:people"))
    assert "id 5) — из настроек" in text and not any("m:pset:5" == x[1] for r in rows for x in r)
    assert run(core.on_menu(b, "5", "m:proleok:emp:5"))[0].startswith("👥")  # такого человека нет в базе


def test_new_admin_is_not_blocked_by_invites():
    a = admin_adapter()
    make_person(a, "77", "Иван", "adm")

    class L(FakeAdapter):
        async def invite_link(self, token):
            return f"https://t.me/b?start=inv_{token}"
    a.invite_link = L.invite_link.__get__(a)
    token = run(core.on_menu(a, "77", "m:invn:emp"))[0].split("inv_")[1].split()[0]
    assert run(core.redeem_invite(a, "77", "Иван", token)) == "emp"  # у админа доступ выше: ссылка не тратится
    assert core.db.execute("SELECT used_by FROM invites").fetchone()["used_by"] is None


# ---------- обновление ----------
def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    """origin (как GitHub) и рабочая копия (как /app/code на сервере)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    (origin / "main.py").write_text("print('v1')\n")
    (origin / "requirements.txt").write_text("httpx\n")
    git(origin, "add", "."), git(origin, "commit", "-qm", "первая версия")
    code = tmp_path / "code"
    git(tmp_path, "clone", "-q", str(origin), str(code))
    return origin, code


def push_version(origin, text, msg):
    (origin / "main.py").write_text(text)
    git(origin, "add", "."), git(origin, "commit", "-qm", msg)


def run_updater(code, data, test_cmd, *extra, env=None):
    p = subprocess.run([sys.executable, str(ROOT / "updater.py"), *extra], capture_output=True, text=True,
                       env={**os.environ, "CODE_DIR": str(code), "DATA_DIR": str(data), "UPDATE_TEST_CMD": test_cmd,
                            "GITHUB_BRANCH": "main", **(env or {})})
    assert p.returncode == 0, p.stderr
    f = data / "update_result.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def test_updater_applies_new_version_when_tests_pass(repos, tmp_path):
    origin, code = repos
    data = tmp_path / "data"
    data.mkdir()
    (data / "update_request.json").write_text(json.dumps({"platform": "tg", "uid": "1"}))
    push_version(origin, "print('v2')\n", "вторая версия")
    r = run_updater(code, data, f"{sys.executable} -c pass")
    assert r["status"] == "ok" and "вторая версия" in r["commit"] and (r["platform"], r["uid"]) == ("tg", "1")
    assert (code / "main.py").read_text() == "print('v2')\n"
    state = json.loads((data / "update_state.json").read_text())
    assert state["status"] == "probation" and state["prev"] != state["new"]
    assert not (data / "update_request.json").exists()


def test_updater_keeps_old_version_when_tests_fail(repos, tmp_path):
    origin, code = repos
    data = tmp_path / "data"
    data.mkdir()
    push_version(origin, "сломано(\n", "плохая версия")
    r = run_updater(code, data, f"{sys.executable} -c \"import sys; print('FAILED test_x'); sys.exit(1)\"")
    assert r["status"] == "tests_failed" and "FAILED test_x" in r["detail"]
    assert (code / "main.py").read_text() == "print('v1')\n"  # осталась прежняя версия
    assert not (data / "update_state.json").exists()


def test_updater_nochange_and_unreachable_github(repos, tmp_path):
    origin, code = repos
    data = tmp_path / "data"
    data.mkdir()
    assert run_updater(code, data, "true")["status"] == "nochange"
    git(code, "remote", "set-url", "origin", "https://user:SECRETTOKEN@127.0.0.1:1/x.git")
    r = run_updater(code, data, "true")
    assert r["status"] == "failed" and "SECRETTOKEN" not in json.dumps(r)  # токен в ответ не попадает


def test_updater_rollback_restores_previous_version(repos, tmp_path):
    origin, code = repos
    data = tmp_path / "data"
    data.mkdir()
    push_version(origin, "print('v2')\n", "вторая версия")
    run_updater(code, data, "true")
    r = run_updater(code, data, "true", "--rollback")
    assert r["status"] == "rolled_back" and (code / "main.py").read_text() == "print('v1')\n"
    assert json.loads((data / "update_state.json").read_text())["status"] == "rolled_back"


def test_bot_side_update_flow(repos, tmp_path, monkeypatch):
    origin, code = repos
    monkeypatch.setattr(ops, "REPO_DIR", code)
    monkeypatch.setenv("GITHUB_REPO", str(origin))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    a = admin_adapter()
    assert "последняя версия" in run(core.on_menu(a, "1", "m:upd"))[0]
    push_version(origin, "print('v2')\n", "вторая версия")
    text, rows = run(core.on_menu(a, "1", "m:upd"))
    assert "вторая версия" in text and rows[0][0][1] == "m:updgo"
    assert "только администратор" in run(core.on_menu(a, "2", "m:upd"))[0]
    assert ops.RESTART_CODE is None
    text, _ = run(core.on_menu(a, "1", "m:updgo"))
    assert "Перезапускаюсь" in text and ops.RESTART_CODE == 42
    assert json.loads((tmp_path / "data" / "update_request.json").read_text())["uid"] == "1"


def test_update_refused_while_publishing_and_when_not_configured(repos, tmp_path, monkeypatch, tg, llm, png):
    origin, code = repos
    monkeypatch.setattr(ops, "REPO_DIR", code)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    a = admin_adapter()
    assert "не настроено" in run(core.on_menu(a, "1", "m:upd"))[0]  # GITHUB_REPO не задан
    monkeypatch.setenv("GITHUB_REPO", str(origin))
    run(core.submit(tg, "9", "Анна", "Заменили масло и фильтры", png))
    core._set(draft_id(), status="publishing")
    text, _ = run(core.on_menu(a, "1", "m:updgo"))
    assert "идёт публикация" in text and ops.RESTART_CODE is None


def test_update_result_is_announced_to_requester(tmp_path, monkeypatch):
    a = admin_adapter()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "update_result.json").write_text(json.dumps(
        {"status": "tests_failed", "platform": "tg", "uid": "1", "commit": "abc1234", "detail": "FAILED x"}))
    run(ops.announce_update_result())
    sent = a.sent("notify")
    assert sent[-1][1] == "1" and "Обновление отменено" in sent[-1][2] and "abc1234" in sent[-1][2]
    assert not (tmp_path / "update_result.json").exists()  # сообщаем один раз


def test_probation_marks_version_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "update_state.json").write_text(json.dumps({"status": "probation", "prev": "a"}))
    run(ops.probation_watch(0))
    assert json.loads((tmp_path / "update_state.json").read_text())["status"] == "ok"


def test_scrub_hides_github_token():
    assert "TOKEN" not in ops.scrub("fatal: unable to access 'https://x-access-token:TOKEN@github.com/o/r.git/'")


def test_restart_event_fires_with_code():
    async def scenario():
        waiter = asyncio.create_task(ops.wait_restart())
        await asyncio.sleep(0)
        ops.RESTART_DELAY = 0
        ops.request_restart(42)
        return await asyncio.wait_for(waiter, 2)
    assert asyncio.run(scenario()) == 42


def test_status_shows_version_and_pause(monkeypatch):
    a = admin_adapter()
    ops.set_paused(True)

    async def ok(*args, **kw):
        return "ОК"
    monkeypatch.setattr(core, "_complete", ok)
    text = run(core.status_text(a))
    assert "НА ПАУЗЕ" in text and "Версия:" in text


# ---------- entrypoint.sh с подставным python ----------
@pytest.fixture
def sandbox(tmp_path):
    bin_dir, app = tmp_path / "bin", tmp_path / "app"
    bin_dir.mkdir()
    app.mkdir()
    log = tmp_path / "calls.log"
    # подставной «python»: main.py завершается кодами из файла exits (по одному на запуск), updater.py просто пишет в лог
    (bin_dir / "python").write_text(
        "#!/bin/sh\n"
        f"echo \"$@\" >> {log}\n"
        "case \"$1\" in\n"
        f"  main.py) c=$(head -n1 {tmp_path}/exits); sed -i 1d {tmp_path}/exits; exit ${{c:-0}} ;;\n"
        "esac\nexit 0\n")
    (bin_dir / "python").chmod(0o755)
    (bin_dir / "pip").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "pip").chmod(0o755)
    (app / "main.py").write_text("")
    return S(tmp=tmp_path, bin=bin_dir, app=app, log=log)


def run_entry(sb, exits, env=None):
    (sb.tmp / "exits").write_text("\n".join(map(str, exits)) + "\n")
    e = {**os.environ, "PATH": f"{sb.bin}:{os.environ['PATH']}", "APP_DIR": str(sb.app),
         "DATA_DIR": str(sb.tmp / "data"), "REQ_STAMP": str(sb.tmp / "stamp"), **(env or {})}
    e.pop("GITHUB_REPO", None)
    e.update(env or {})
    p = subprocess.run(["sh", str(ROOT / "entrypoint.sh")], capture_output=True, text=True, env=e, timeout=30)
    calls = sb.log.read_text().splitlines() if sb.log.exists() else []
    return p, calls


def test_entrypoint_without_github_just_runs_main(sandbox):
    p, calls = run_entry(sandbox, [0])
    assert p.returncode == 0 and calls == ["main.py"]


def test_entrypoint_update_code_runs_updater_then_restarts(sandbox):
    (sandbox.app / "code").mkdir()
    (sandbox.app / "code" / "main.py").write_text("")
    (sandbox.app / "code" / "requirements.txt").write_text("x\n")
    p, calls = run_entry(sandbox, [42, 0], {"GITHUB_REPO": "file:///x", "CODE_DIR": str(sandbox.app / "code")})
    assert p.returncode == 0
    assert calls == ["main.py", f"{sandbox.app}/updater.py", "main.py"]


def test_entrypoint_rolls_back_when_new_version_crashes_on_start(sandbox):
    code = sandbox.app / "code"
    code.mkdir()
    (code / "main.py").write_text("")
    (code / "requirements.txt").write_text("x\n")
    data = sandbox.tmp / "data"
    data.mkdir()
    (data / "update_state.json").write_text(json.dumps({"status": "probation", "prev": "abc"}))
    p, calls = run_entry(sandbox, [1, 0], {"GITHUB_REPO": "file:///x", "CODE_DIR": str(code)})
    assert calls == ["main.py", f"{sandbox.app}/updater.py --rollback", "main.py"] and p.returncode == 0


def test_entrypoint_does_not_loop_on_crash_without_update(sandbox):
    code = sandbox.app / "code"
    code.mkdir()
    (code / "main.py").write_text("")
    (code / "requirements.txt").write_text("x\n")
    p, calls = run_entry(sandbox, [3], {"GITHUB_REPO": "file:///x", "CODE_DIR": str(code)})
    assert p.returncode == 3 and calls == ["main.py"]  # обычный сбой: пусть решает Docker (restart: unless-stopped)


# ---------- main.serve: остановка ради обновления ----------
def test_serve_stops_everything_and_exits_with_code_42():
    import main
    stopped = []

    class Worker:
        name = "w"

        async def run(self):
            try:
                await asyncio.Event().wait()  # работает «вечно»
            except asyncio.CancelledError:
                stopped.append(1)
                raise

    async def scenario():
        ops.RESTART_DELAY = 0
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, ops.request_restart, 42)
        await main.serve([Worker(), Worker()])
    with pytest.raises(SystemExit) as e:
        asyncio.run(scenario())
    assert e.value.code == 42 and len(stopped) == 2  # оба мессенджера остановлены аккуратно
    ops.RESTART_DELAY = 3


def test_failed_update_names_the_failing_test_and_reason(repos, tmp_path):
    """Сообщение «обновление отменено» должно называть упавший тест, а не только «тесты не прошли»."""
    origin, code = repos
    data = tmp_path / "data"
    data.mkdir()
    (origin / "tests").mkdir()
    (origin / "tests" / "test_demo.py").write_text(
        "def test_ok():\n    assert True\n\n\ndef test_settings_are_listed():\n"
        "    missing = ['HOUSEKEEPING_SEC']\n    assert not missing, f'нет настроек: {missing}'\n")
    git(origin, "add", "."), git(origin, "commit", "-qm", "добавили тесты")
    r = run_updater(code, data, f"{sys.executable} -m pytest -q -x -p no:cacheprovider")
    assert r["status"] == "tests_failed"
    assert "FAILED tests/test_demo.py::test_settings_are_listed" in r["detail"]
    assert "HOUSEKEEPING_SEC" in r["detail"] and "PASSED" not in r["detail"]
    assert len(r["detail"].splitlines()) <= 8  # коротко: тест и причина, без полотна вывода
