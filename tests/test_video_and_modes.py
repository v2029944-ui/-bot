"""Видео (приём из MAX, согласование, публикация в MAX и Telegram) и режим Telegram «только публикация»."""
import asyncio
import os
import subprocess
import types

import pytest
from conftest import FakeAdapter, draft_id, run
from PIL import Image

import core
import max_
import spaces
import tg as tgmod

S = types.SimpleNamespace


@pytest.fixture(scope="module")
def mp4(tmp_path_factory):
    """Настоящий короткий ролик 320×240, 3 секунды (ffmpeg из imageio-ffmpeg)."""
    path = tmp_path_factory.mktemp("v") / "t.mp4"
    subprocess.run([core._ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc=duration=3:size=320x240:rate=10", "-pix_fmt", "yuv420p", "-y", str(path)], check=True)
    return path.read_bytes()


# ---------- сохранение видео ----------
def test_save_video_makes_contact_sheet_from_three_frames(mp4):
    vpath, sheet = core.save_video(mp4, "ролик.MOV")
    assert vpath.endswith(".mp4") and os.path.getsize(vpath) > 0
    probe = subprocess.run([core._ffmpeg(), "-hide_banner", "-i", vpath], capture_output=True, text=True)
    assert "Video: h264" in probe.stderr and "yuv420p" in probe.stderr
    with Image.open(sheet) as im:
        assert im.format == "JPEG" and im.size == (1600, 400)  # три кадра рядом, ужато до 1600 по ширине
    for f in (vpath, sheet):
        os.unlink(f)


def test_save_video_rejects_garbage_big_and_long(mp4, monkeypatch):
    before = set(core.VIDEOS.iterdir()) | set(core.PHOTOS.iterdir())
    with pytest.raises(core.BadImage, match="не видео"):
        core.save_video("это точно не видео".encode() * 100)
    monkeypatch.setattr(core, "MAX_VIDEO_BYTES", 1000)
    with pytest.raises(core.BadImage, match="Видео больше"):
        core.save_video(mp4)
    monkeypatch.setattr(core, "MAX_VIDEO_BYTES", 10**9)
    monkeypatch.setattr(core, "MAX_VIDEO_SEC", 2)
    with pytest.raises(core.BadImage, match="длиннее 2 с"):
        core.save_video(mp4)
    assert set(core.VIDEOS.iterdir()) | set(core.PHOTOS.iterdir()) == before  # ничего не осталось на диске


# ---------- путь видео: согласование → публикация ----------
def test_video_goes_through_approval_and_publish_with_the_file(tg, llm, mp4):
    run(core.submit(tg, "9", "Анна", "Установили новый насос", mp4, kind="video", filename="v.mp4"))
    did = draft_id()
    d = core.get(did)
    assert d["video"] and os.path.exists(d["video"]) and os.path.exists(d["photo"])
    assert [o[1] for o in tg.sent("approval_video")] == ["1", "2"]  # видео ушло администраторам
    call = llm.calls[-1]
    assert call["b64"] and "ВИДЕО" in call["text"] and "три кадра" in call["text"]  # нейросеть видит кадры
    assert "на видео" in call["system"]  # и ей запрещено писать «на видео»
    ok, msg = run(core.on_action(tg, "1", "okyes", did, 1))
    assert ok and tg.sent("publish_video")[0][1] == d["video"]


def test_regenerate_and_show_keep_the_video(tg, llm, mp4):
    run(core.submit(tg, "9", "Анна", "Установили новый насос", mp4, kind="video"))
    did = draft_id()
    run(core.on_action(tg, "1", "regen", did, 1))
    assert "ВИДЕО" in llm.calls[-1]["text"]
    before = len(tg.sent("approval_video"))
    run(core.on_action(tg, "2", "show", did, 2))
    assert len(tg.sent("approval_video")) == before + 1


def test_delete_and_housekeeping_remove_video_files(tg, llm, mp4):
    run(core.submit(tg, "9", "Анна", "Установили новый насос", mp4, kind="video"))
    did = draft_id()
    d = core.get(did)
    run(core.on_action(tg, "1", "del", did, 1))
    run(core.on_action(tg, "1", "delyes", did, 1))
    assert not os.path.exists(d["video"]) and not os.path.exists(d["photo"])
    # сироты в папке видео убираются, ссылочные файлы остаются
    keep = core.VIDEOS / "keep.mp4"
    orphan = core.VIDEOS / "orphan.mp4"
    keep.write_bytes(b"x")
    orphan.write_bytes(b"x")
    import time
    os.utime(orphan, (time.time() - 7200,) * 2)
    core.housekeeping()
    assert keep.exists() and not orphan.exists()
    keep.unlink()


def test_failed_video_submit_leaves_no_files(tg, llm, mp4):
    before = set(core.VIDEOS.iterdir()) | set(core.PHOTOS.iterdir())
    llm.reply = RuntimeError("нейросеть недоступна")
    with pytest.raises(RuntimeError):
        run(core.submit(tg, "9", "Анна", "Установили новый насос", mp4, kind="video"))
    assert set(core.VIDEOS.iterdir()) | set(core.PHOTOS.iterdir()) == before


# ---------- Telegram: видео файлом и режим «только публикация» ----------
class RecBot:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        async def f(*a, **k):
            self.calls.append((name, a, k))
            return S(chat=S(id=-200))
        return f


def test_telegram_sends_video_file_not_the_preview(tmp_path):
    t = tgmod.Telegram("123456:ABCDEF", "@chan")
    t.bot = RecBot()
    photo, video = tmp_path / "p.jpg", tmp_path / "v.mp4"
    photo.write_bytes(b"x")
    video.write_bytes(b"x")
    run(t.publish(str(photo), "Короткий пост", video=str(video)))
    name, args, kw = t.bot.calls[0]
    assert name == "send_video" and kw["caption"] == "Короткий пост" and kw["supports_streaming"]
    t.bot.calls.clear()
    run(t.publish(str(photo), "Фото-пост"))
    assert t.bot.calls[0][0] == "send_photo"
    t.bot.calls.clear()
    run(t.publish(str(photo), "д" * 1500, video=str(video)))  # длинный пост: видео, затем отдельное сообщение
    assert [c[0] for c in t.bot.calls] == ["send_video", "send_message"]
    assert t.bot.calls[1][1][0] == -200


def test_publish_only_telegram_does_not_poll_and_gets_every_post(llm, png, monkeypatch):
    t = tgmod.Telegram("123456:ABCDEF", "@chan")
    t.bot = RecBot()
    mx = FakeAdapter("max", ["5"], admins=["5"])
    core.ADAPTERS.update(tg=t, max=mx)
    run(asyncio.wait_for(t.run(), 1))  # сразу возвращается: getUpdates не запускается, конфликта токена нет
    assert t.publish_only and not [c for c in t.bot.calls if c[0] in ("get_updates", "set_my_commands")]
    # сотрудник пишет в MAX → согласует MAX → публикация идёт и в MAX, и в Telegram (PUBLISH_TO_ALL не нужен)
    run(core.submit(mx, "9", "Анна", "Заменили масло и фильтры", png))
    did = draft_id()
    assert mx.sent("approval") and not t.bot.calls  # Telegram не доставляет черновики
    ok, msg = run(core.on_action(mx, "5", "okyes", did, 1))
    assert ok and mx.sent("publish") and [c[0] for c in t.bot.calls] == ["send_photo"]
    assert core.get(did)["status"] == "published" and core.get(did)["published_to"] == ""


def test_telegram_failure_is_reported_in_max_and_retry_does_not_duplicate_max(llm, png):
    class BrokenBot(RecBot):
        async def send_photo(self, *a, **k):
            raise RuntimeError("таймаут")
    t = tgmod.Telegram("123456:ABCDEF", "@chan")
    t.bot = BrokenBot()
    mx = FakeAdapter("max", ["5"], admins=["5"])
    core.ADAPTERS.update(tg=t, max=mx)
    run(core.submit(mx, "9", "Анна", "Заменили масло и фильтры", png))
    did = draft_id()
    ok, msg = run(core.on_action(mx, "5", "okyes", did, 1))
    assert not ok and "tg" in msg and len(mx.sent("publish")) == 1  # в MAX вышло, в Telegram нет
    assert core._parse_marks(core.get(did)["published_to"])["max"] == "done"
    # Обычный повтор требует подтверждения, а подтверждённый не дублирует MAX.
    run(core.on_action(mx, "5", "okyes", did, 1))
    assert len(mx.sent("publish")) == 1 and "оборвалась" in mx.sent("menu")[-1][2]
    t.bot = RecBot()
    assert run(core.on_action(mx, "5", "okforce", did, 1))[0]
    assert len(mx.sent("publish")) == 1
    assert [c[0] for c in t.bot.calls] == ["send_photo"]


def test_video_from_max_is_approved_then_sent_to_both_channels(llm, mp4):
    t = tgmod.Telegram("123456:ABCDEF", "@chan")
    t.bot = RecBot()
    mx = FakeAdapter("max", ["5"], admins=["5"])
    core.ADAPTERS.update(tg=t, max=mx)
    did = run(core.submit(mx, "9", "Анна", "Установили новый насос", mp4, kind="video"))
    d = core.get(did)
    assert mx.sent("approval_video") and not mx.sent("publish") and not t.bot.calls
    assert run(core.on_action(mx, "5", "okyes", did, 1))[0]
    assert mx.sent("publish_video") == [("publish_video", d["video"])]
    name, args, kw = t.bot.calls[0]
    assert name == "send_video" and str(args[1].path) == d["video"]
    assert core.get(did)["status"] == "published" and core.get(did)["published_to"] == ""


def test_publish_only_ignores_old_telegram_roles_and_sends_no_service_messages():
    core.db.execute("INSERT INTO people(platform,uid,name,role,created) VALUES('tg','77','Старый админ','adm',0)")
    core.db.commit()
    t = tgmod.Telegram("123456:ABCDEF", "@chan")
    t.bot = RecBot()
    core.ADAPTERS["tg"] = t
    core.refresh_people(t)
    assert t.approvers == [] and t.admins == [] and core.staff(t) == []
    assert not hasattr(t, "dp")
    run(core.announce_start())
    run(t.notify("77", "Служебное сообщение"))
    run(t.send_menu("77", "Меню", []))
    run(t.send_for_approval("77", 1, "photo.jpg", "Черновик"))
    assert t.bot.calls == []


def test_old_telegram_draft_is_approved_in_max_and_published_to_both(llm, png):
    t = tgmod.Telegram("123456:ABCDEF", "@chan")
    t.bot = RecBot()
    mx = FakeAdapter("max", ["5"], admins=["5"])
    core.ADAPTERS.update(tg=t, max=mx)
    did = run(core.submit(mx, "9", "Анна", "Заменили масло и фильтры", png))
    core._set(did, platform="tg")  # черновик, оставшийся до смены режима
    assert core._approval_adapter(core.get(did), None) is mx
    assert run(core.on_action(mx, "5", "okyes", did, 1))[0]
    assert mx.sent("publish") and [c[0] for c in t.bot.calls] == ["send_photo"]


def _env(monkeypatch, **kw):
    for k in ("TG_TOKEN", "TG_CHANNEL", "MAX_TOKEN", "MAX_CHANNEL", "MAX_APPROVERS", "TG_APPROVERS", "APPROVAL_PLATFORM",
              "TG_MODE"):
        monkeypatch.delenv(k, raising=False)
    base = {"LLM_API_KEY": "k", "LLM_BASE_URL": "http://x", "STARTUP_NOTIFY": "0"}
    for k, v in {**base, **kw}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(core, "PROVIDER", "openai")


@pytest.fixture
def main_state():
    """A simulated main() must not leave workspace proxies installed for later tests."""
    try:
        yield
    finally:
        if spaces.INSTALLED:
            root = spaces.root()
            run(spaces.close())
            for name, original in root.memories.items():
                if name != "footer_waiting":
                    setattr(core, name, original)
            core.post_footer.WAITING = root.memories["footer_waiting"]
            core.WAITING_RULE = core.WAITING_RULE.root
            core.db = root.db
            core.DATA, core.PHOTOS, core.VIDEOS = root.folder, root.folder / "photos", root.folder / "videos"
            spaces.INSTALLED, spaces.ROOT = False, None


def test_main_builds_publish_only_telegram_with_max(monkeypatch, main_state):
    import main
    _env(monkeypatch, TG_MODE="publish_only", TG_TOKEN="123456:ABCDEF", TG_CHANNEL="@c", MAX_TOKEN="mt",
         MAX_CHANNEL="-5", MAX_APPROVERS="5")
    seen = {}

    async def fake_serve(workers):
        seen["workers"] = workers
    monkeypatch.setattr(main, "serve", fake_serve)
    run(main.main())
    assert core.ADAPTERS["tg"].publish_only and core.ADAPTERS["tg"].approvers == []
    assert not core.ADAPTERS["max"].publish_only and core.ADAPTERS["max"].allowed == {"5"}


def test_main_defaults_to_max_workflow(monkeypatch, main_state):
    import main
    _env(monkeypatch, TG_TOKEN="123456:ABCDEF", TG_CHANNEL="@c", MAX_TOKEN="mt",
         MAX_CHANNEL="-5", MAX_APPROVERS="5", TG_APPROVERS="не числовой id")

    async def fake_serve(workers):
        pass
    monkeypatch.setattr(main, "serve", fake_serve)
    run(main.main())
    assert core.ADAPTERS["tg"].publish_only and core.ADAPTERS["tg"].approvers == []


@pytest.mark.parametrize("env, text", [
    (dict(TG_MODE="publish_only", TG_TOKEN="123456:ABCDEF", TG_CHANNEL="@c"), "нужен MAX_TOKEN"),
    (dict(TG_MODE="publish_only", TG_TOKEN="123456:ABCDEF", TG_CHANNEL="@c", MAX_TOKEN="m", MAX_CHANNEL="-5",
          MAX_APPROVERS="5", APPROVAL_PLATFORM="tg"), "только на публикацию"),
])
def test_main_rejects_wrong_publish_only_setups(monkeypatch, env, text):
    import main
    _env(monkeypatch, **env)
    with pytest.raises(SystemExit, match=text):
        run(main.main())


# ---------- MAX: приём и отправка видео ----------
class FakeHttp:
    """Подставной httpx.AsyncClient: stream() отдаёт заданные байты, post() запоминает загрузку."""
    payload, length, posts, downloads = b"VIDEO", None, [], []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    def stream(self, method, url):
        FakeHttp.downloads.append((method, url))
        outer = self

        class R:
            headers = {"content-length": str(outer.length)} if outer.length is not None else {}

            async def __aenter__(s):
                return s

            async def __aexit__(s, *a):
                pass

            def raise_for_status(s):
                pass

            async def aiter_bytes(s):
                yield outer.payload
        return R()

    async def post(self, url, files=None):
        FakeHttp.posts.append((url, files["data"][0]))
        return S(raise_for_status=lambda: None, json=lambda: {})


@pytest.fixture
def mx(monkeypatch):
    m = max_.Max("token", "-100", ["5"], ["9"], ["5"])
    core.ADAPTERS["max"] = m
    m.out = []

    async def notify(uid, text):
        m.out.append(("notify", str(uid), text))
    monkeypatch.setattr(m, "notify", notify)
    async def video_details(method, path, **kw):
        assert method == "GET" and path == "/videos/t"
        return {"urls": {"mp4_720": "http://cdn/v.mp4"}}
    monkeypatch.setattr(m, "call", video_details)
    FakeHttp.payload, FakeHttp.length, FakeHttp.posts, FakeHttp.downloads = b"VIDEO", None, [], []
    monkeypatch.setattr(max_.httpx, "AsyncClient", FakeHttp)
    return m


def video_msg(text="Установили новый насос", url="http://cdn/v.mp4"):
    return {"recipient": {"chat_type": "dialog"}, "sender": {"user_id": 9, "name": "Вася"},
            "body": {"text": text, "attachments": [{"type": "video", "payload": {"url": url, "token": "t"}}]}}


def test_max_video_message_is_submitted_as_video(mx, monkeypatch):
    got = []

    async def fake_submit(adapter, uid, name, text, data, **kw):
        got.append((uid, text, data, kw))
        return 1
    monkeypatch.setattr(core, "submit", fake_submit)
    run(mx.on_message(video_msg()))
    assert got == [("9", "Установили новый насос", b"VIDEO", {"kind": "video", "filename": "video.mp4"})]
    assert ("notify", "9", "Принято, готовлю пост…") in mx.out
    assert not any(o[0] == "notify" and "Пост готов к редактированию" in o[2] for o in mx.out)
    assert FakeHttp.downloads == [("GET", "http://cdn/v.mp4")]


def test_max_video_resolves_direct_mp4_instead_of_payload_url(mx, monkeypatch):
    calls = []

    async def details(method, path, **kw):
        calls.append((method, path))
        return {"urls": {"mp4_1080": None, "mp4_720": "http://cdn/direct.mp4", "hls": "http://cdn/stream.m3u8"}}
    monkeypatch.setattr(mx, "call", details)
    data = run(mx._download(video_msg(url="http://watch/not-a-file")["body"]["attachments"][0], 100, "video"))
    assert data == b"VIDEO" and calls == [("GET", "/videos/t")]
    assert FakeHttp.downloads == [("GET", "http://cdn/direct.mp4")]


def test_max_video_waits_for_direct_links_and_checks_streamed_size(mx, monkeypatch):
    results = iter([{"urls": None}, {"urls": {"mp4_480": "http://cdn/ready.mp4"}}])

    async def details(*a, **kw):
        return next(results)
    monkeypatch.setattr(mx, "call", details)
    att = video_msg()["body"]["attachments"][0]
    # Без Content-Length лимит всё равно применяется к полученным байтам.
    with pytest.raises(core.BadImage, match="больше"):
        run(mx._download(att, 2, "video"))
    assert FakeHttp.downloads == [("GET", "http://cdn/ready.mp4")]


def test_max_video_without_token_cannot_download_arbitrary_payload_url(mx):
    att = {"payload": {"url": "http://watch/not-a-file"}}
    with pytest.raises(RuntimeError, match="токен"):
        run(mx._download(att, 100, "video"))
    assert FakeHttp.downloads == []


def test_max_video_too_big_missing_url_and_no_caption(mx, monkeypatch):
    async def fake_submit(*a, **k):
        raise AssertionError("не должно дойти до обработки")
    monkeypatch.setattr(core, "submit", fake_submit)
    monkeypatch.setenv("EMPLOYEE_NOTIFY", "1")
    FakeHttp.length = core.MAX_VIDEO_BYTES + 1
    run(mx.on_message(video_msg()))
    assert any("Видео" not in o[2] and "МБ" in o[2] or "больше" in o[2] for o in mx.out if o[0] == "notify")
    mx.out.clear()
    async def no_video_urls(*a, **kw):
        return {"urls": None}
    monkeypatch.setattr(mx, "call", no_video_urls)
    run(mx.on_message(video_msg(url=None)))  # MAX ещё не подготовил прямые ссылки
    assert any("Не получилось обработать" in o[2] for o in mx.out)
    mx.out.clear()
    run(mx.on_message(video_msg(text="")))
    assert ("notify", "9", core.CAPTION_ASK) in mx.out


def test_max_uploads_video_and_waits_until_it_is_processed(mx, monkeypatch, tmp_path):
    calls = []
    errors = [0]

    async def call(method, path, **kw):
        calls.append((method, path, kw.get("params"), kw.get("json")))
        if path == "/uploads":
            return {"url": "http://upload/v", "token": "VTOKEN"}
        if errors[0] < 2:  # «attachment.not.ready» дважды, потом успех
            errors[0] += 1
            req = max_.httpx.Request("POST", "http://x")
            raise max_.httpx.HTTPStatusError("x", request=req, response=max_.httpx.Response(400, text="attachment.not.ready", request=req))
        return {}
    monkeypatch.setattr(mx, "call", call)
    monkeypatch.setattr(max_.httpx, "AsyncClient", FakeHttp)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    run(mx.publish("preview.jpg", "Пост", video=str(video)))
    msgs = [c for c in calls if c[1] == "/messages"]
    assert len(msgs) == 3  # два «ещё не готово» и успех
    assert msgs[-1][3]["attachments"] == [{"type": "video", "payload": {"token": "VTOKEN"}}]
    assert msgs[-1][2] == {"chat_id": -100} and FakeHttp.posts[0][0] == "http://upload/v"
    assert calls[0][2] == {"type": "video"}  # превью-картинка в этом случае не загружается


def test_max_upload_video_reports_missing_token(mx, monkeypatch, tmp_path):
    async def call(method, path, **kw):
        return {"url": "http://upload/v"}  # токена нет нигде
    monkeypatch.setattr(mx, "call", call)
    monkeypatch.setattr(max_.httpx, "AsyncClient", FakeHttp)
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="токен"):
        run(mx._upload_video(str(video)))


def test_max_uploads_photo_through_real_file_handling(mx, monkeypatch, tmp_path):
    """Регрессия: загрузка картинки раньше падала (open() внутри async with), а в тестах её ни разу не проверяли."""
    calls = []

    async def call(method, path, **kw):
        calls.append((path, kw.get("params"), kw.get("json")))
        return {"url": "http://upload/p"} if path == "/uploads" else {}

    class Http(FakeHttp):
        async def post(self, url, files=None):
            data = files["data"][1].read()  # файл открыт и читается
            FakeHttp.posts.append((url, data))
            return S(raise_for_status=lambda: None, json=lambda: {"photos": {"x": {"token": "PTOKEN"}}})
    monkeypatch.setattr(mx, "call", call)
    monkeypatch.setattr(max_.httpx, "AsyncClient", Http)
    photo = tmp_path / "p.jpg"
    photo.write_bytes(b"JPEGDATA")
    run(mx.publish(str(photo), "Пост"))
    assert FakeHttp.posts == [("http://upload/p", b"JPEGDATA")]
    msg = [c for c in calls if c[0] == "/messages"][0]
    assert msg[2]["attachments"] == [{"type": "image", "payload": {"token": "PTOKEN"}}]


@pytest.mark.parametrize("stopping", [False, True])
def test_max_legacy_pause_does_not_block_but_shutdown_rejects_media(mx, monkeypatch, stopping):
    import ops
    if stopping:
        monkeypatch.setattr(core, "SHUTTING_DOWN", True)
    else:
        ops.set_setting("paused", "1")
    run(mx.on_message(video_msg()))
    if stopping:
        assert not FakeHttp.downloads
        assert any("перезапускается" in o[2] for o in mx.out)
    else:
        assert FakeHttp.downloads
        assert not any("паузе" in o[2] for o in mx.out)


def test_sigterm_cancels_max_worker_and_exits_cleanly(monkeypatch):
    import signal

    import main
    stopped = []

    class Worker:
        name = "max"

        async def run(self):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.append(True)

    async def scenario():
        asyncio.get_running_loop().call_later(0.05, lambda: os.kill(os.getpid(), signal.SIGTERM))
        with pytest.raises(SystemExit) as result:
            await main.serve([Worker()])
        assert result.value.code == 0
    run(scenario())
    assert stopped == [True]
