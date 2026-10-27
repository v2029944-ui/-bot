"""Применяет обновление с GitHub. Запускается из entrypoint.sh, когда бот вышел с кодом 42 (кнопка «Обновить»).

Порядок: скачать новую версию -> проверить её тесты в отдельной папке -> только если всё зелёное, переключиться.
После переключения 2 минуты «испытательный срок»: упала при запуске — откат на прошлую версию (--rollback).
Результат кладётся в data/update_result.json, бот сообщает его админу при запуске."""
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CODE = Path(os.getenv("CODE_DIR", "/app/code"))
DATA = Path(os.getenv("DATA_DIR", "/app/data"))
BRANCH = os.getenv("GITHUB_BRANCH", "main")
TEST_CMD = os.getenv("UPDATE_TEST_CMD", "python -m pytest -q -x -p no:cacheprovider")


def scrub(text: str) -> str:
    """Токен GitHub в адресе репозитория не должен попасть в результат."""
    return re.sub(r"//[^/@\s]+@", "//***@", text or "")


def run(cmd: list[str], cwd: Path, timeout: int = 300, env: dict | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})})
        return p.returncode, scrub((p.stdout or "") + (p.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 1, f"команда не завершилась за {timeout} с"
    except OSError as e:
        return 1, str(e)


def git(*args: str, cwd: Path | None = None, timeout: int = 120) -> tuple[int, str]:
    return run(["git", *args], cwd or CODE, timeout)


def short(ref: str = "HEAD") -> str:
    return git("rev-parse", "--short", ref)[1]


def _read(name: str) -> dict:
    try:
        return json.loads((DATA / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(name: str, data: dict):
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def finish(status: str, who: dict, **kw):
    _write("update_result.json", {"status": status, "platform": who.get("platform"), "uid": who.get("uid"), **kw})
    print(f"[updater] {status} {kw.get('detail', '')[:200]}", flush=True)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def update():
    who = _read("update_request.json")
    (DATA / "update_request.json").unlink(missing_ok=True)
    rc, out = git("fetch", "origin", BRANCH)
    if rc:
        return finish("failed", who, detail="не удалось связаться с GitHub: " + out[-300:], commit=short())
    head, new = git("rev-parse", "HEAD")[1], git("rev-parse", "FETCH_HEAD")[1]
    if head == new:
        return finish("nochange", who, commit=short())

    tmp = Path(tempfile.mkdtemp(prefix="upd-"))
    wt = tmp / "src"
    try:
        rc, out = git("worktree", "add", "--detach", str(wt), new)
        if rc:
            return finish("failed", who, detail="не удалось подготовить новую версию: " + out[-300:], commit=short())
        if _sha(wt / "requirements.txt") != _sha(CODE / "requirements.txt"):  # новые библиотеки нужны для тестов
            rc, out = run([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"], wt, 900)
            if rc:
                return finish("failed", who, detail="не удалось установить библиотеки: " + out[-300:], commit=short())
        rc, out = run(shlex.split(TEST_CMD), wt, 600, {"PYTHONPATH": str(wt), "DATA_DIR": str(tmp / "data")})
        if rc:
            tail = "\n".join(out.splitlines()[-12:])
            return finish("tests_failed", who, detail=tail[-900:], commit=short())
        subject = git("log", "-1", "--format=%s", new)[1][:100]
        _write("update_state.json", {"prev": head, "new": new, "ts": time.time(), "status": "probation",
                                     "platform": who.get("platform"), "uid": who.get("uid")})
        rc, out = git("reset", "--hard", new)
        if rc:
            return finish("failed", who, detail="не удалось переключиться: " + out[-300:], commit=short())
        finish("ok", who, commit=f"{short()} {subject}")
    finally:
        git("worktree", "remove", "--force", str(wt))
        git("worktree", "prune")
        shutil.rmtree(tmp, ignore_errors=True)


def rollback():
    state = _read("update_state.json")
    prev = state.get("prev")
    who = {"platform": state.get("platform"), "uid": state.get("uid")}
    if not prev:
        return print("[updater] откатываться некуда", flush=True)
    rc, out = git("reset", "--hard", prev)
    state["status"] = "rolled_back"
    _write("update_state.json", state)
    finish("rolled_back" if not rc else "failed", who, detail=out[-300:], commit=short())


if __name__ == "__main__":
    try:
        rollback() if "--rollback" in sys.argv else update()
    except Exception as e:  # обновление не должно ронять запуск бота
        print(f"[updater] ошибка: {scrub(str(e))}", flush=True)
    sys.exit(0)
