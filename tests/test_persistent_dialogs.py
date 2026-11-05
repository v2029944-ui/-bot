import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import core
import dialog_state
from conftest import run


def test_waiting_input_survives_a_new_python_process(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "DATA_DIR": str(tmp_path / "data"), "PYTHONPATH": str(root)}
    write = """import core
core.WAITING_EDIT[('max','11')] = (7, 3)
core.WAITING[('max','22')] = (8, 2)
core.WAITING_REJECT[('max','33')] = (9, 1)
core.WAITING_RULE.add(('max','44'))
core.post_footer.WAITING[('max','55')] = 'marker'
"""
    read = """import core, json
print(json.dumps([core.WAITING_EDIT[('max','11')],core.WAITING[('max','22')],
 core.WAITING_REJECT[('max','33')],('max','44') in core.WAITING_RULE,
 core.post_footer.WAITING[('max','55')]]))
"""
    subprocess.run([sys.executable, "-c", write], cwd=root, env=env, check=True, capture_output=True, timeout=20)
    result = subprocess.run([sys.executable, "-c", read], cwd=root, env=env, check=True, capture_output=True, text=True, timeout=20)
    assert json.loads(result.stdout) == [[7, 3], [8, 2], [9, 1], True, "marker"]


def test_pending_input_expires_without_saving_post_text():
    key = ("max", "11")
    core.WAITING_EDIT[key] = (7, 3)
    row = core.db.execute("SELECT value FROM dialog_state WHERE kind='edit'").fetchone()
    assert json.loads(row[0]) == [7, 3]
    core.db.execute("UPDATE dialog_state SET updated=?", (time.time() - 8 * 86400,))
    core.db.commit()
    assert key not in core.WAITING_EDIT


def test_restored_comment_cannot_change_a_newer_version(mx, png, llm):
    async def scenario():
        did = await core.submit(mx, "9", "Анна", "Исходное описание", png)
        await core.on_action(mx, "5", "redoc", did, 1)
        # Новый экземпляр отображения читает ожидание из SQLite без памяти процесса.
        saved = dialog_state.PendingMap(core.db, "comment")
        assert saved[("max", "5")] == (did, 1)
        core._set(did, attempts=2, post="Новый вариант другого проверяющего")
        calls = len(llm.calls)
        await core.on_text(mx, "5", "Сделай короче")
        assert core.get(did)["post"] == "Новый вариант другого проверяющего"
        assert len(llm.calls) == calls
        assert ("max", "5") not in saved
    run(scenario())


def test_interrupted_comment_keeps_target_for_queue_replay(mx, png, llm, monkeypatch):
    async def scenario():
        did = await core.submit(mx, "9", "Анна", "Исходное описание", png)
        await core.on_action(mx, "5", "redoc", did, 1)

        async def interrupted(*args, **kwargs):
            raise asyncio.CancelledError

        original = core.generate_post
        monkeypatch.setattr(core, "generate_post", interrupted)
        try:
            await core.on_text(mx, "5", "Сделай короче")
        except asyncio.CancelledError:
            pass
        assert core.WAITING[("max", "5")] == (did, 1)
        core.startup()
        monkeypatch.setattr(core, "generate_post", original)
        await core.on_text(mx, "5", "Сделай короче")
        assert core.get(did)["attempts"] == 2
        assert ("max", "5") not in core.WAITING
    run(scenario())
