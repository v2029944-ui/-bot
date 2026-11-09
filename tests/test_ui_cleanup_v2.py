import httpx
from conftest import run
from test_admin_menu_button import Api, make

import core
import max_


def test_failed_delete_is_queued_and_retried():
    async def scenario():
        calls = []
        failed = True

        async def handler(request):
            nonlocal failed
            calls.append((request.method, request.url.path, dict(request.url.params)))
            if request.method == "DELETE" and failed:
                failed = False
                return httpx.Response(503, request=request, text="temporary")
            return httpx.Response(200, request=request, json={})

        ad = max_.Max("token", "-1", [], [], admins=["1"])
        await ad.http.aclose()
        ad.http = httpx.AsyncClient(base_url=max_.API, transport=httpx.MockTransport(handler))
        assert not await ad._delete("m1", "1")
        row = core.db.execute("SELECT op,mid,uid FROM max_ui_cleanup").fetchone()
        assert tuple(row) == ("delete", "m1", "1")
        await ad._process_ui_cleanup()
        assert not core.db.execute("SELECT 1 FROM max_ui_cleanup").fetchone()
        assert [c[0] for c in calls].count("DELETE") == 2
        await ad.http.aclose()

    run(scenario())


def test_delete_404_is_success_and_not_requeued():
    async def scenario():
        async def handler(request):
            return httpx.Response(404, request=request, text="gone")

        ad = max_.Max("token", "-1", [], [], admins=["1"])
        await ad.http.aclose()
        ad.http = httpx.AsyncClient(base_url=max_.API, transport=httpx.MockTransport(handler))
        assert await ad._delete("gone", "1")
        assert not core.db.execute("SELECT 1 FROM max_ui_cleanup").fetchone()
        await ad.http.aclose()

    run(scenario())


def test_new_draft_attempt_queues_only_older_version_for_same_recipient(monkeypatch, png, llm):
    async def scenario():
        api = Api()
        ad = make(api)
        core.ADAPTERS["max"] = ad
        ad.allowed = {"9"}

        async def upload(path):
            return "photo-token"

        monkeypatch.setattr(ad, "_upload_image", upload)
        did = await core.submit(ad, "9", "Author", "Description", png)
        first_mid = f"m{api.n}"
        first = core.db.execute("""SELECT u.did,u.attempt,r.uid FROM max_draft_ui u
                                   JOIN max_draft_recipient r ON r.mid=u.mid WHERE u.mid=?""",
                                (first_mid,)).fetchone()
        assert tuple(first) == (did, 1, "9")

        core._set(did, attempts=2, post="Second version")
        await ad.send_for_approval("9", did, core.get(did)["photo"], "Second version")
        second_mid = f"m{api.n}"
        assert second_mid != first_mid
        queued = core.db.execute("SELECT op,mid,uid FROM max_ui_cleanup WHERE op='delete'").fetchall()
        assert [tuple(r) for r in queued] == [("delete", first_mid, "9")]

        # A different recipient gets their own history; delivery to one user must not delete another user's post.
        await ad.send_for_approval("1", did, core.get(did)["photo"], "Second version")
        assert not core.db.execute(
            "SELECT 1 FROM max_ui_cleanup WHERE uid='1' AND mid=?", (first_mid,)).fetchone()
        await ad.http.aclose()

    run(scenario())


def test_cleanup_deletes_old_draft_after_new_delivery(monkeypatch, png, llm):
    async def scenario():
        api = Api()
        ad = make(api)
        core.ADAPTERS["max"] = ad
        ad.allowed = {"9"}

        async def upload(path):
            return "photo-token"

        monkeypatch.setattr(ad, "_upload_image", upload)
        did = await core.submit(ad, "9", "Author", "Description", png)
        first_mid = f"m{api.n}"
        core._set(did, attempts=2, post="Second version")
        await ad.send_for_approval("9", did, core.get(did)["photo"], "Second version")
        await ad._process_ui_cleanup()
        deletes = [c for c in api.of("DELETE") if c[2].get("message_id") == first_mid]
        assert len(deletes) == 1
        assert core.db.execute("SELECT 1 FROM max_draft_ui WHERE mid=?", (first_mid,)).fetchone() is None
        assert core.db.execute("SELECT 1 FROM max_draft_recipient WHERE mid=?", (first_mid,)).fetchone() is None
        await ad.http.aclose()

    run(scenario())


def test_material_progress_message_is_removed_and_no_redundant_ready_notice(monkeypatch, png, llm):
    async def scenario():
        api = Api()
        ad = make(api)
        core.ADAPTERS["max"] = ad
        ad.allowed = {"9"}

        async def upload(path):
            return "photo-token"

        async def download(*args, **kwargs):
            return png

        monkeypatch.setattr(ad, "_upload_image", upload)
        monkeypatch.setattr(max_.max_queue, "download", download)
        message = {
            "sender": {"user_id": 9, "name": "Author"},
            "recipient": {"chat_type": "dialog"},
            "body": {"mid": "source", "text": "Description", "attachments": [
                {"type": "image", "payload": {"url": "https://example.invalid/p.jpg"}}
            ]},
        }
        await ad.on_message(message)
        texts = [entry[3].get("text", "") for entry in api.of("POST")]
        assert "Принято, готовлю пост…" in texts
        assert all("Пост готов к редактированию" not in t for t in texts)
        progress_mid = next(mid for mid, body in api.messages.items() if body.get("text") == "Принято, готовлю пост…")
        assert any(c[2].get("message_id") == progress_mid for c in api.of("DELETE"))
        await ad.http.aclose()

    run(scenario())


def test_legacy_max_draft_ui_shape_stays_rollback_compatible():
    columns = [row["name"] for row in core.db.execute("PRAGMA table_info(max_draft_ui)")]
    assert columns == ["mid", "did", "attempt"]
    assert [row["name"] for row in core.db.execute("PRAGMA table_info(max_draft_recipient)")] == ["mid", "uid"]


def test_deferred_strip_draft_clears_local_tracking(monkeypatch):
    async def scenario():
        ad = make(Api())
        core.db.execute("INSERT INTO max_draft_ui VALUES('old',99,1)")
        core.db.execute("INSERT INTO max_draft_recipient VALUES('old','1')")
        core.db.execute("INSERT INTO max_button_cache VALUES('old','[]')")
        ad._queue_ui_cleanup("strip_draft", "old")

        async def strip(*args, **kwargs):
            return True

        monkeypatch.setattr(ad, "_strip_admin_button", strip)
        await ad._process_ui_cleanup()
        assert core.db.execute("SELECT 1 FROM max_draft_ui WHERE mid='old'").fetchone() is None
        assert core.db.execute("SELECT 1 FROM max_draft_recipient WHERE mid='old'").fetchone() is None
        assert core.db.execute("SELECT 1 FROM max_button_cache WHERE mid='old'").fetchone() is None
        assert core.db.execute("SELECT 1 FROM max_ui_cleanup WHERE mid='old'").fetchone() is None
        await ad.http.aclose()

    run(scenario())


def test_stale_scan_respects_cleanup_backoff(monkeypatch):
    async def scenario():
        ad = make(Api())
        core.db.execute("INSERT INTO max_draft_ui VALUES('backoff',999999,1)")
        core.db.execute("""INSERT INTO max_ui_cleanup(op,mid,next_try,created)
                           VALUES('strip_draft','backoff',99999999999,0)""")
        core.db.commit()
        visited = []

        async def strip(prev, **kwargs):
            visited.append(prev["mid"])
            return True

        monkeypatch.setattr(ad, "_strip_admin_button", strip)
        await ad.cleanup_stale_buttons()
        assert "backoff" not in visited
        assert core.db.execute("SELECT 1 FROM max_ui_cleanup WHERE mid='backoff'").fetchone()
        await ad.http.aclose()

    run(scenario())


def test_partial_comparison_delivery_does_not_retire_previous_version(monkeypatch, png, llm):
    async def scenario():
        api = Api()
        ad = make(api)
        core.ADAPTERS["max"] = ad
        ad.allowed = {"9"}

        async def upload(path):
            return "photo-token"

        monkeypatch.setattr(ad, "_upload_image", upload)
        did = await core.submit(ad, "9", "Author", "Description", png)
        first_mid = f"m{api.n}"

        options = [
            {"model": "one", "post": "Option one"},
            {"model": "two", "post": "Option two"},
        ]
        core._set(did, attempts=2, candidates=__import__("json").dumps(options), post="Option one")

        original_send = ad._send
        calls = 0

        async def flaky_send(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("second option failed")
            return await original_send(*args, **kwargs)

        monkeypatch.setattr(ad, "_send", flaky_send)
        try:
            await ad.send_for_approval("9", did, core.get(did)["photo"], core.get(did)["post"], resume=True)
        except RuntimeError as exc:
            assert "second option failed" in str(exc)
        else:
            raise AssertionError("comparison delivery must fail")

        assert core.db.execute(
            "SELECT 1 FROM max_draft_recipient WHERE mid=? AND uid='9'", (first_mid,)).fetchone()
        assert not core.db.execute(
            "SELECT 1 FROM max_ui_cleanup WHERE op='delete' AND mid=?", (first_mid,)).fetchone()
        await ad.http.aclose()

    run(scenario())


def test_comparison_retry_sends_only_missing_option(monkeypatch, png, llm):
    async def scenario():
        api = Api()
        ad = make(api)
        core.ADAPTERS["max"] = ad
        ad.allowed = {"9"}

        async def upload(path):
            return "photo-token"

        monkeypatch.setattr(ad, "_upload_image", upload)
        did = await core.submit(ad, "9", "Author", "Description", png)
        options = [
            {"model": "one", "post": "Option one"},
            {"model": "two", "post": "Option two"},
        ]
        core._set(did, attempts=2, candidates=__import__("json").dumps(options), post="Option one")

        original_send = ad._send
        calls = 0

        async def flaky_send(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("second option failed")
            return await original_send(*args, **kwargs)

        monkeypatch.setattr(ad, "_send", flaky_send)
        try:
            await ad.send_for_approval("9", did, core.get(did)["photo"], core.get(did)["post"], resume=True)
        except RuntimeError:
            pass
        saved = core.db.execute(
            "SELECT slot,mid FROM max_approval_delivery WHERE did=? AND attempt=2 AND uid='9' ORDER BY slot",
            (did,)).fetchall()
        assert len(saved) == 1 and saved[0]["slot"] == 0
        first_option_mid = saved[0]["mid"]

        monkeypatch.setattr(ad, "_send", original_send)
        before = len(api.of("POST"))
        await ad.send_for_approval("9", did, core.get(did)["photo"], core.get(did)["post"], resume=True)
        assert len(api.of("POST")) == before + 1
        assert core.db.execute(
            "SELECT COUNT(*) FROM max_approval_delivery WHERE did=? AND attempt=2 AND uid='9'", (did,)).fetchone()[0] == 2
        assert core.db.execute(
            "SELECT 1 FROM max_draft_recipient WHERE mid=? AND uid='9'", (first_option_mid,)).fetchone()

        await ad.http.aclose()

    run(scenario())


def test_delete_success_false_is_not_treated_as_cleaned():
    async def scenario():
        async def handler(request):
            return httpx.Response(200, request=request, json={"success": False, "message": "denied"})

        ad = max_.Max("token", "-1", [], [], admins=["1"])
        await ad.http.aclose()
        ad.http = httpx.AsyncClient(base_url=max_.API, transport=httpx.MockTransport(handler))
        assert not await ad._delete("m1", "1")
        assert core.db.execute(
            "SELECT 1 FROM max_ui_cleanup WHERE op='delete' AND mid='m1'").fetchone()
        await ad.http.aclose()

    run(scenario())
