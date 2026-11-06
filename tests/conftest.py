"""Общие заготовки для тестов: подставные мессенджеры и нейросеть. Сеть и реальные токены не нужны."""
import asyncio
import io
import os
import re
import sys
import tempfile
from pathlib import Path

REAL_SLEEP = asyncio.sleep

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _settings_names() -> list[str]:
    """Все настройки бота из образца env.example: тесты не должны зависеть от настоящего .env сервера
    (при обновлении кнопкой тесты идут на сервере, где в окружении лежат ваши значения)."""
    text = (Path(__file__).resolve().parent.parent / "env.example").read_text(encoding="utf-8")
    return re.findall(r"^#? ?([A-Z][A-Z0-9_]+)=", text, flags=re.M)


for _k in _settings_names():
    os.environ.pop(_k, None)  # до импорта core: часть настроек читается при импорте
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="postbot_tests_")  # до импорта core: база создаётся при импорте

import pytest  # noqa: E402
from PIL import Image  # noqa: E402

import core  # noqa: E402
import max_ as max_  # noqa: E402 — initialize adapter/queue tables for isolated test modules


def run(coro):
    return asyncio.run(coro)


class FakeAdapter(core.Adapter):
    """Мессенджер-пустышка: всё, что бот «отправил», складывается в self.out."""

    def __init__(self, name="tg", approvers=("1",), fail=None, sure=(), admins=()):
        self.name, self.approvers, self.out = name, list(approvers), []
        self.admins = list(admins)
        self.fail = list(fail or [])  # исключения для публикаций по очереди
        self.sure = tuple(sure)       # какие исключения считать «точно не ушло»

    def not_sent(self, e):
        return isinstance(e, self.sure)

    async def send_for_approval(self, approver, did, photo, post, silent=False, video=None, media=None):
        self.out.append(("approval", approver, did, post))
        if media:
            self.out.append(("approval_media", approver, did, media))
        if video:
            self.out.append(("approval_video", approver, did, video))
        if silent:
            self.out.append(("silent", approver, did))

    async def send_menu(self, user_id, text, rows, silent=False):
        self.out.append(("menu", str(user_id), text, rows))
        if silent:
            self.out.append(("silent", str(user_id), text))

    async def publish(self, photo, post, video=None, media=None):
        if self.fail:
            raise self.fail.pop(0)
        self.out.append(("publish", post))
        if media:
            self.out.append(("publish_media", media))
        if video:
            self.out.append(("publish_video", video))

    async def notify(self, user_id, text):
        self.out.append(("notify", str(user_id), text))

    def sent(self, kind):
        return [o for o in self.out if o[0] == kind]


class FakeLLM:
    """Подставная нейросеть: reply — строка или функция (system, text) -> строка/исключение."""

    def __init__(self):
        self.calls, self.reply = [], "Почистили форсунки на стенде."

    async def __call__(self, system, text, b64=None):
        self.calls.append({"system": system, "text": text, "b64": b64})
        r = self.reply(system, text) if callable(self.reply) else self.reply
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Каждый тест начинает с чистой базы и чистой памяти бота."""
    core.db.execute("DELETE FROM drafts")
    core.db.execute("DELETE FROM rules")
    for t in ("people", "requests", "settings", "prefs", "deferred", "max_inbox", "max_actions", "max_draft_ui", "max_notices", "command_results"):
        core.db.execute(f"DELETE FROM {t}")
    core.db.commit()
    for d in (core.WAITING, core._HITS, core._RL_ALERT, core._ALERT_LAST, core.ADAPTERS):
        d.clear()
    core.WAITING_RULE.clear()
    core.WAITING_EDIT.clear()
    core.WAITING_REJECT.clear()
    core.db.execute("DELETE FROM dialog_state")
    core.db.execute("DELETE FROM max_button_cache")
    core.db.commit()
    core.post_footer.WAITING.clear()
    core.spaces.WAITING_MEMBER.clear()
    core.shutil.rmtree(core.DATA / "inbox", ignore_errors=True)
    core.ops.RESTART_CODE = None
    for k in _settings_names():
        if k != "DATA_DIR":
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NOTIFY_DEFAULT", "normal")
    for k in ("EMPLOYEE_NOTIFY", "EMPLOYEE_CONFIRM", "PUBLISH_TO_ALL", "APPROVAL_PLATFORM",
              "RATE_LIMIT_PER_HOUR", "ALERT_THROTTLE_SEC", "LLM_VISION"):
        monkeypatch.delenv(k, raising=False)

    async def no_sleep(*a, **k):
        await REAL_SLEEP(0)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)  # повторы с паузами не тормозят тесты


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(core, "_complete", fake)
    return fake


@pytest.fixture
def tg():
    a = FakeAdapter("tg", ["1", "2"], admins=["1", "2"])
    core.ADAPTERS["tg"] = a
    return a


@pytest.fixture
def mx():
    a = FakeAdapter("max", ["5"], admins=["5"])
    core.ADAPTERS["max"] = a
    return a


@pytest.fixture
def png():
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, "PNG")
    return buf.getvalue()


def draft_id():
    return core.db.execute("SELECT MAX(id) FROM drafts").fetchone()[0]
