import asyncio

import pytest
from conftest import run

import core


def draft(mx, png):
    return run(core.submit(mx, "9", "Анна", "Почистили форсунки на стенде", png))


def test_change_menu_length_and_back_do_not_regenerate(mx, llm, png):
    did = draft(mx, png)
    run(core.on_action(mx, "5", "edit", did, 1))
    run(core.on_action(mx, "5", "change", did, 1))
    assert ("max", "5") not in core.WAITING_EDIT
    buttons = [b for row in mx.sent("menu")[-1][3] for b in row]
    assert [b[1] for b in buttons] == [f"d:{a}:{did}:1" for a in ("redo", "redoc", "edit", "length", "back")]
    run(core.on_action(mx, "5", "length", did, 1))
    buttons = [b for row in mx.sent("menu")[-1][3] for b in row]
    assert [b[1] for b in buttons] == [f"d:{a}:{did}:1" for a in ("len_short", "len_medium", "len_long", "change")]
    run(core.on_action(mx, "5", "back", did, 1))
    assert len([b for row in mx.sent("menu")[-1][3] for b in row]) == 3
    assert len(llm.calls) == 1 and core.get(did)["attempts"] == 1


@pytest.mark.parametrize("length,bounds", [("short", "150–300"), ("medium", "300–800"), ("long", "800–1500")])
def test_length_regenerates_only_this_post_and_persists(mx, llm, png, length, bounds):
    mx.approvers.append("6")
    did = draft(mx, png)
    other = draft(mx, png)
    before = core.get(did)
    llm.reply = "Обновлённый текст"
    assert run(core.on_action(mx, "5", "len_" + length, did, 1))[0]
    after = core.get(did)
    assert after["post_length"] == length and after["attempts"] == 2
    assert after["photo"] == before["photo"] and after["post"] == llm.reply
    assert bounds in llm.calls[-1]["system"] and "не выдумывай" in llm.calls[-1]["system"]
    assert core.get(other)["post_length"] == "" and core.get(other)["attempts"] == 1
    assert {a[1] for a in mx.sent("approval")[-2:]} == {"5", "9"}
    assert not mx.sent("publish")
    assert "устаревший" in run(core.on_action(mx, "5", "okyes", did, 1))[1]
    run(core.on_action(mx, "5", "regen", did, 2))
    assert bounds in llm.calls[-1]["system"]
    run(core._regenerate(mx, "5", did, comment="Добавь акцент на результате"))
    assert bounds in llm.calls[-1]["system"]
    assert core.get(did)["post_length"] == length


@pytest.mark.parametrize("failure", ["llm", "delivery"])
def test_length_failure_preserves_previous_draft(mx, llm, png, monkeypatch, failure):
    did = draft(mx, png)
    core._set(did, post_length="medium")
    before = core.get(did)
    if failure == "llm":
        llm.reply = RuntimeError("LLM unavailable")
    else:
        async def fail(*args, **kwargs):
            raise RuntimeError("MAX unavailable")
        monkeypatch.setattr(mx, "send_for_approval", fail)
    assert not run(core.on_action(mx, "5", "len_short", did, 1))[0]
    after = core.get(did)
    for key in ("post", "post_length", "attempts", "photo", "status"):
        assert after[key] == before[key]


@pytest.mark.parametrize("action", ["change", "length", "len_short", "len_medium", "len_long"])
def test_new_controls_enforce_permissions_version_and_partial_publication(mx, llm, png, action):
    did = draft(mx, png)
    assert "нет прав" in run(core.on_action(mx, "99", action, did, 1))[1]
    assert "устаревший" in run(core.on_action(mx, "5", action, did, 9))[1]
    core._set(did, published_to="max?")
    assert "Публикация уже началась" in run(core.on_action(mx, "5", action, did, 1))[1]
    assert len(llm.calls) == 1 and not mx.sent("menu")


def test_length_cannot_overwrite_concurrent_manual_edit(mx, llm, png):
    did = draft(mx, png)

    async def scenario():
        return await asyncio.gather(core.on_action(mx, "5", "len_short", did, 1),
                                    core._apply_edit(mx, "5", did, 1, "Ручной вариант"))

    results = run(scenario())
    assert "устаревший" in results[0][1]
    assert core.get(did)["post"] == "Ручной вариант"
    assert core.get(did)["post_length"] == "" and len(llm.calls) == 1


def test_length_choice_cancels_waiting_for_comment(mx, llm, png):
    did = draft(mx, png)
    run(core.on_action(mx, "5", "redoc", did, 1))
    assert ("max", "5") in core.WAITING
    run(core.on_action(mx, "5", "len_short", did, 1))
    assert ("max", "5") not in core.WAITING
    assert not run(core.on_comment(mx, "5", "Обычное сообщение"))
    assert core.get(did)["attempts"] == 2
