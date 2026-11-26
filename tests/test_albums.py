"""Альбомы: весь набор проходит согласование и публикацию как один черновик."""
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from conftest import FakeAdapter, run

import core
import max_
import ops
import tg as telegram


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    path = tmp_path_factory.mktemp("album") / "clip.mp4"
    subprocess.run([core._ffmpeg(), "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=red:s=160x120:d=1", "-pix_fmt", "yuv420p", "-y", str(path)], check=True)
    return path.read_bytes()


def photos(png, count=2):
    return [{"type": "image", "data": png} for _ in range(count)]


def files():
    return set(core.PHOTOS.iterdir()) | set(core.VIDEOS.iterdir())


class RecordingBot:
    def __init__(self, fail_text=None):
        self.groups, self.texts = [], []
        self.fail_text = fail_text

    async def send_media_group(self, **kw):
        self.groups.append(kw)
        return [SimpleNamespace(chat=SimpleNamespace(id=-200)) for _ in kw["media"]]

    async def send_message(self, chat, text):
        if self.fail_text is TimeoutError:
            raise TimeoutError("lost reply")
        if self.fail_text is TelegramBadRequest:
            raise TelegramBadRequest(method=SendMessage(chat_id=chat, text=text), message="refused")
        self.texts.append(text)


def test_mixed_album_max_approval_and_both_channels(png, clip, llm, monkeypatch):
    mx = max_.Max("test", "-100", ["5"], ["9"], ["5"])
    tg = telegram.Telegram("123456:ABCDEF", "@channel")
    tg.bot = RecordingBot()
    core.ADAPTERS.update(max=mx, tg=tg)
    messages = []

    async def upload(path):
        assert Path(path).is_file()
        return Path(path).name

    async def call(method, path, **kw):
        assert path == "/messages"
        messages.append(kw)
        return {}

    monkeypatch.setattr(mx, "_upload_image", upload)
    monkeypatch.setattr(mx, "_upload_video", upload)
    monkeypatch.setattr(mx, "call", call)
    did = run(core.submit_album(mx, "9", "Анна", "Установили новый насос",
                               [*photos(png, 1), {"type": "video", "data": clip}, *photos(png, 1)]))
    d = core.get(did)
    media = core.draft_media(d)
    assert [i["type"] for i in media] == ["image", "video", "image"]
    assert len(llm.calls) == 1 and "АЛЬБОМ из 3" in llm.calls[0]["text"] and llm.calls[0]["b64"]
    assert messages[0]["params"] == {"user_id": 5} and not tg.bot.groups
    assert [a["type"] for a in messages[0]["json"]["attachments"]] == ["image", "video", "image", "inline_keyboard"]
    assert run(core.on_action(mx, "5", "okyes", did, 1))[0]
    assert messages[1]["params"] == {"chat_id": -100}
    assert [a["type"] for a in messages[1]["json"]["attachments"]] == ["image", "video", "image"]
    group = tg.bot.groups[0]["media"]
    assert [i.type for i in group] == ["photo", "video", "photo"]
    assert [str(i.media.path) for i in group] == [i["path"] for i in media]
    assert group[0].caption == d["post"] and group[1].caption is None
    assert core.get(did)["status"] == "published" and core.get(did)["published_to"] == ""
    assert all(not Path(p).exists() for p in core.draft_files(d))
    run(core.on_action(mx, "5", "okyes", did, 1))
    assert len(tg.bot.groups) == 1


def test_album_edit_regenerate_show_deferred_and_delete(png, llm):
    mx = FakeAdapter("max", ["5"], admins=["5"])
    core.ADAPTERS["max"] = mx
    did = run(core.submit_album(mx, "9", "Анна", "Заменили насос", photos(png)))
    media = core.draft_media(core.get(did))
    run(core.on_action(mx, "5", "regen", did, 1))
    assert "АЛЬБОМ из 2" in llm.calls[-1]["text"]
    run(core.on_action(mx, "5", "edit", did, 2))
    run(core.on_text(mx, "5", "Исправленный общий текст"))
    run(core.on_action(mx, "5", "show", did, 1))
    assert mx.sent("approval_media")[-1][3] == media
    assert mx.sent("approval")[-1][3] == "Исправленный общий текст"
    ops.enqueue(mx, "5", did)
    assert run(ops.flush_deferred()) == 1
    assert mx.sent("approval_media")[-1][3] == media
    paths = core.draft_files(core.get(did))
    assert all(Path(p).exists() for p in paths)
    run(core.on_action(mx, "5", "delyes", did, 3))
    assert all(not Path(p).exists() for p in paths)


@pytest.mark.parametrize("failure", ["invalid_file", "llm", "approval"])
def test_failed_album_leaves_no_files_or_draft(png, llm, failure):
    mx = FakeAdapter("max", [] if failure == "approval" else ["5"], admins=[] if failure == "approval" else ["5"])
    core.ADAPTERS["max"] = mx
    if failure == "approval":
        async def broken(*a, **kw):
            raise RuntimeError("delivery failed")
        mx.send_for_approval = broken
    items = photos(png)
    if failure == "invalid_file":
        items[-1]["data"] = b"not an image"
    if failure == "llm":
        llm.reply = RuntimeError("LLM failed")
    before = files()
    with pytest.raises((core.BadImage, RuntimeError)):
        run(core.submit_album(mx, "9", "Анна", "Заменили насос", items))
    assert files() == before
    assert core.db.execute("SELECT count(*) FROM drafts").fetchone()[0] == 0


def test_album_housekeeping_protects_active_media_then_expires(png, llm):
    mx = FakeAdapter("max", ["5"], admins=["5"])
    core.ADAPTERS["max"] = mx
    did = run(core.submit_album(mx, "9", "Анна", "Заменили насос", photos(png)))
    paths = core.draft_files(core.get(did))
    for p in paths:
        os.utime(p, (time.time() - 7200,) * 2)
    core.housekeeping()
    assert all(Path(p).exists() for p in paths)
    core._set(did, created=1, last_sent=1)
    assert core.housekeeping() == 1
    assert all(not Path(p).exists() for p in paths)


@pytest.mark.parametrize("failure", [TelegramBadRequest, TimeoutError])
def test_partial_telegram_album_resumes_text_without_resending_media_or_max(png, llm, failure):
    mx = FakeAdapter("max", ["5"], admins=["5"])
    tg = telegram.Telegram("123456:ABCDEF", "@channel")
    tg.bot = RecordingBot(fail_text=failure)
    core.ADAPTERS.update(max=mx, tg=tg)
    llm.reply = "Пост " * 300
    did = run(core.submit_album(mx, "9", "Анна", "Заменили насос", photos(png)))
    assert not run(core.on_action(mx, "5", "okyes", did, 1))[0]
    assert core._parse_marks(core.get(did)["published_to"]) == {"max": "done", "tg": "unknown"}
    if failure is TimeoutError:
        assert not run(core.on_action(mx, "5", "okyes", did, 1))[0]
        assert any(data == f"d:okforce:{did}:1" for row in mx.sent("menu")[-1][3] for _, data in row)
    tg.bot.fail_text = None
    assert run(core.on_action(mx, "5", "okforce" if failure is TimeoutError else "sendtext", did, 1))[0]
    assert len(tg.bot.groups) == len(mx.sent("publish_media")) == 1 and tg.bot.texts == [llm.reply]


@pytest.mark.parametrize("count", [0, 1, 11])
def test_album_count_limits(count, png, llm):
    with pytest.raises(core.BadImage):
        run(core.submit_album(FakeAdapter("max", ["5"], admins=["5"]), "9", "Анна", "Описание", photos(png, count)))
    assert not llm.calls


def test_album_total_size_limit(png, llm, monkeypatch):
    monkeypatch.setattr(core, "MAX_ALBUM_BYTES", len(png))
    with pytest.raises(core.BadImage):
        run(core.submit_album(FakeAdapter("max", ["5"], admins=["5"]), "9", "Анна", "Описание", photos(png)))
    assert not llm.calls


def test_max_receives_all_media_in_order_and_checks_count_before_download(png, monkeypatch):
    mx = max_.Max("test", "-100", ["5"], ["9"], ["5"])
    downloaded, submitted, notified = [], [], []

    async def download(att, limit, what):
        downloaded.append(att["type"])
        return png

    async def submit(*args):
        submitted.append(args[-1])

    async def notify(uid, text):
        notified.append(text)

    monkeypatch.setattr(mx, "_download", download)
    monkeypatch.setattr(mx, "notify", notify)
    monkeypatch.setattr(core, "submit_album", submit)
    msg = {"recipient": {"chat_type": "dialog"}, "sender": {"user_id": 9},
           "body": {"text": "Описание работы", "attachments": [{"type": t} for t in ["image", "video", "image"]]}}
    run(mx.on_message(msg))
    assert downloaded == ["image", "video", "image"]
    assert [i["type"] for i in submitted[0]] == downloaded
    msg["body"]["attachments"] *= 4
    run(mx.on_message(msg))
    assert len(submitted) == 1 and len(downloaded) == 3
    assert "до 10" in notified[-1]


def test_old_database_migrates_without_losing_single_draft(tmp_path):
    conn = sqlite3.connect(tmp_path / "drafts.db")
    conn.execute("CREATE TABLE drafts(id INTEGER PRIMARY KEY,platform TEXT,author_id TEXT,author_name TEXT,source_text TEXT,photo TEXT,post TEXT,status TEXT,attempts INTEGER,created REAL)")
    conn.execute("INSERT INTO drafts VALUES(1,'max','9','Анна','Описание','old.jpg','Пост','pending',1,1)")
    conn.commit()
    conn.close()
    code = "import core; d=core.get(1); assert d['post']=='Пост'; assert core.draft_media(d)==[]; assert core.media_kw(d)=={}"
    result = subprocess.run([os.sys.executable, "-c", code], env={**os.environ, "DATA_DIR": str(tmp_path)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_ten_photos_are_kept_and_sent_as_one_telegram_group(png, llm):
    mx = FakeAdapter("max", ["5"], admins=["5"])
    tg = telegram.Telegram("123456:ABCDEF", "@channel")
    tg.bot = RecordingBot()
    core.ADAPTERS.update(max=mx, tg=tg)
    did = run(core.submit_album(mx, "9", "Анна", "Результаты работы", photos(png, 10)))
    assert len(core.draft_media(core.get(did))) == 10
    assert run(core.on_action(mx, "5", "okyes", did, 1))[0]
    assert len(tg.bot.groups) == 1 and len(tg.bot.groups[0]["media"]) == 10


def test_converted_album_size_limit_also_cleans_files(png, llm, monkeypatch):
    monkeypatch.setattr(core, "MAX_ALBUM_BYTES", len(png) * 2 + 1)
    before = files()
    with pytest.raises(core.BadImage, match="После обработки"):
        run(core.submit_album(FakeAdapter("max", ["5"], admins=["5"]), "9", "Анна", "Описание", photos(png)))
    assert files() == before and not llm.calls
