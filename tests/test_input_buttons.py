import pytest
from conftest import run
from test_admin_menu_button import Api, buttons, make

import core
import dialog_state
import post_footer


@pytest.mark.parametrize("payload,target", [
    ("m:add", "m:rules"), ("m:member", "m:people"),
    ("m:footer_text", "m:footer"), ("m:footer_marker", "m:footer"),
    ("m:footer_max", "m:footer"), ("m:footer_tg", "m:footer"),
])
def test_input_prompts_have_cancel_button_and_no_text_commands(mx, payload, target):
    mx.admins = ["5"]
    text, rows = run(core.on_menu(mx, "5", payload))
    assert "/cancel" not in text and rows[0][0][0] == "↩️ Отмена"
    cancel = rows[0][0][1]
    text, result = run(core.on_menu(mx, "5", cancel))
    assert not core.input_snapshot(mx, "5")
    assert result
    assert "устарела" in run(core.on_menu(mx, "5", cancel))[0]


def test_old_cancel_cannot_clear_newer_input_or_another_user_input(mx):
    mx.admins = ["5", "6"]
    _, rows = run(core.on_menu(mx, "5", "m:add"))
    old = rows[0][0][1]
    _, rows = run(core.on_menu(mx, "5", "m:footer_text"))
    current = rows[0][0][1]
    text, retry = run(core.on_menu(mx, "5", old))
    assert "устарела" in text and retry[0][0][1] == current
    assert post_footer.WAITING[("max", "5")] == "text"
    assert "устарела" in run(core.on_menu(mx, "6", current))[0]
    assert post_footer.WAITING[("max", "5")] == "text"
    assert "устарела" in run(core.on_menu(mx, "5", "m:cancel"))[0]
    assert post_footer.WAITING[("max", "5")] == "text"
    run(core.on_menu(mx, "5", current))
    assert not post_footer.WAITING


def test_navigation_invalidates_cancel_and_cancel_survives_reload(mx, monkeypatch):
    mx.admins = ["5"]
    _, rows = run(core.on_menu(mx, "5", "m:add"))
    old = rows[0][0][1]
    run(core.on_menu(mx, "5", "m:rules"))
    assert "устарела" in run(core.on_menu(mx, "5", old))[0]
    _, rows = run(core.on_menu(mx, "5", "m:footer_text"))
    current = rows[0][0][1]
    # Re-open both durable maps; no process-local state is required for this prompt.
    monkeypatch.setattr(post_footer, "WAITING", dialog_state.PendingMap(core.db, "footer"))
    assert "устарела" not in run(core.on_menu(mx, "5", current))[0]
    assert not core.input_snapshot(mx, "5")


@pytest.mark.parametrize("action,state", [("edit", "WAITING_EDIT"), ("redoc", "WAITING"), ("rejectcustom", "WAITING_REJECT")])
def test_real_max_draft_input_cancel_keeps_post_and_restores_navigation(monkeypatch, png, llm, action, state):
    async def scenario():
        api = Api()
        ad = make(api)
        core.ADAPTERS["max"] = ad

        async def upload(path):
            return "photo-token"

        monkeypatch.setattr(ad, "_upload_image", upload)
        did = await core.submit(ad, "9", "Автор", "Описание", png)
        before = core.get(did)
        payload = f"d:{action}:{did}:1"
        if action == "rejectcustom":
            await ad.on_callback({"user": {"user_id": 1}, "callback_id": "del", "payload": f"d:del:{did}:1"})
            payload = next(value for value in ad._confirmations()[("max", "1")] if value.startswith("d:rejectcustom:"))
        await ad.on_callback({"user": {"user_id": 1}, "callback_id": action, "payload": payload})
        prompt = api.of("POST")[-1][3]
        assert "/cancel" not in prompt["text"]
        cancel = next(b["payload"] for b in buttons(prompt) if b["payload"].startswith("m:cancel:"))
        assert ("max", "1") in getattr(core, state)
        assert ad._ui("1")["menu"]  # input prompt participates in stale menu cleanup
        await ad.on_callback({"user": {"user_id": 1}, "callback_id": "cancel", "payload": cancel})
        assert ("max", "1") not in getattr(core, state)
        assert core.get(did)["post"] == before["post"] and core.get(did)["status"] == "pending"
        assert any(b["payload"] == f"d:show:{did}:1" for b in buttons(api.of("POST")[-1][3]))
        await ad.http.aclose()
    run(scenario())


def test_real_max_validation_reply_also_has_cancel_button():
    async def scenario():
        api = Api()
        ad = make(api)
        core.ADAPTERS["max"] = ad
        await core.on_menu(ad, "1", "m:footer_marker")
        await core.on_text(ad, "1", "invalid marker with spaces")
        reply = api.of("POST")[-1][3]
        assert any(b["payload"].startswith("m:cancel:") for b in buttons(reply))
        assert ("max", "1") in post_footer.WAITING
        await ad.http.aclose()
    run(scenario())


def test_older_rules_can_be_viewed_and_deleted_using_buttons(mx):
    mx.admins = ["5"]
    for n in range(45):
        core.db.execute("INSERT INTO rules(text,created) VALUES(?,0)", (f"Правило {n}",))
    core.db.commit()
    text, rows = run(core.on_menu(mx, "5", "m:rules"))
    assert "/rule_del" not in text
    older = next(p for row in rows for _, p in row if p == "m:rules:1")
    text, rows = run(core.on_menu(mx, "5", older))
    assert "1. Правило 0" in text
    deletion = next(p for row in rows for _, p in row if p.startswith("m:del:"))
    text, _ = run(core.on_menu(mx, "5", deletion))
    assert "Правило 0\n" not in text
    assert not core.db.execute("SELECT 1 FROM rules WHERE text='Правило 0'").fetchone()
