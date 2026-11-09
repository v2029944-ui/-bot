import json
import time

import pytest
from conftest import FakeAdapter, run

import core
import updater
from tg import Telegram


def test_telegram_long_post_resumes_text_without_resending_media(tmp_path):
    class Bot:
        def __init__(self):
            self.photos = 0
            self.messages = 0

        async def send_photo(self, *args, **kwargs):
            self.photos += 1

        async def send_message(self, *args, **kwargs):
            self.messages += 1
            if self.messages == 1:
                raise RuntimeError("lost text")

    async def scenario():
        adapter = Telegram.__new__(Telegram)
        adapter.channel = "-100"
        adapter.bot = Bot()
        adapter.admins, adapter.approvers, adapter.allowed = [], [], set()
        photo = tmp_path / "p.jpg"
        photo.write_bytes(b"x")
        stages = []
        with pytest.raises(RuntimeError):
            await adapter.publish(str(photo), "x" * 1200, progress=stages.append)
        assert stages == ["media"]
        await adapter.publish(str(photo), "x" * 1200, resume={"media": True}, progress=stages.append)
        assert adapter.bot.photos == 1
        assert adapter.bot.messages == 2
        assert stages == ["media", "text"]

    run(scenario())


def test_publication_persists_partial_telegram_stage_and_resumes(monkeypatch, llm, png):
    class Resumable(FakeAdapter):
        publish_only = True
        supports_publish_resume = True
        channel = "-200"

        def __init__(self):
            super().__init__("tg", admins=())
            self.calls = []

        async def publish(self, photo, post, video=None, media=None, resume=None, progress=None):
            self.calls.append(dict(resume or {}))
            if not (resume or {}).get("media"):
                progress("media")
                raise RuntimeError("text send failed")
            progress("text")

    mx = FakeAdapter("max", ["5"], admins=["5"])
    mx.channel = "-100"
    tg = Resumable()
    core.ADAPTERS.update(max=mx, tg=tg)
    did = run(core.submit(mx, "9", "Author", "Caption", png))

    ok, _ = run(core.on_action(mx, "5", "okyes", did, 1))
    assert not ok
    d = core.get(did)
    assert json.loads(d["publish_progress"])["tg"] == {"media": True}
    assert core._parse_marks(d["published_to"]) == {"max": "done", "tg": "unknown"}
    text, rows = core.publication_view(d, mx)
    assert "медиа опубликовано, текст не отправлен" in text
    assert not any("Проверил: пост есть в Telegram" in label for row in rows for label, _ in row)

    ok, _ = run(core.on_action(mx, "5", "okforce", did, 1))
    assert ok
    assert tg.calls == [{}, {"media": True}]


def test_rate_limit_survives_in_memory_reset(monkeypatch, tg, llm, png):
    monkeypatch.setenv("RATE_LIMIT_PER_HOUR", "1")
    run(core.submit(tg, "9", "Author", "one", png))
    core._HITS.clear()
    with pytest.raises(core.RateLimited):
        run(core.submit(tg, "9", "Author", "two", png))


def test_candidate_tests_do_not_receive_production_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("MAX_TOKEN", "max-secret")
    monkeypatch.setenv("TG_TOKEN", "tg-secret")
    monkeypatch.setenv("LLM_API_KEY", "llm-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    env = updater.candidate_test_env("candidate", tmp_path)
    assert env["PYTHONPATH"] == "candidate"
    assert env["DATA_DIR"] == str(tmp_path)
    for name in ("MAX_TOKEN", "TG_TOKEN", "LLM_API_KEY", "ANTHROPIC_API_KEY"):
        assert name not in env


def test_housekeeping_removes_only_old_terminal_queue_history(monkeypatch):
    monkeypatch.setenv("DRAFT_TTL_DAYS", "0")
    old = time.time() - 31 * 86400
    core.db.execute("INSERT INTO max_inbox(event_key,payload,state,created) VALUES('old-done','', 'done',?)", (old,))
    core.db.execute("INSERT INTO max_inbox(event_key,payload,state,created) VALUES('old-failed','', 'failed',?)", (old,))
    core.db.execute("INSERT INTO command_results(event_key,operation,result,created) VALUES('e','o','null',?)", (old,))
    core.db.execute("INSERT INTO max_actions(event_key,uid,payload,state,created) VALUES('a','1','', 'done',?)", (old,))
    core.db.commit()
    core.housekeeping()
    assert core.db.execute("SELECT 1 FROM max_inbox WHERE event_key='old-done'").fetchone() is None
    assert core.db.execute("SELECT 1 FROM command_results WHERE event_key='e'").fetchone() is None
    assert core.db.execute("SELECT 1 FROM max_actions WHERE event_key='a'").fetchone() is None
    assert core.db.execute("SELECT state FROM max_inbox WHERE event_key='old-failed'").fetchone()[0] == "failed"
