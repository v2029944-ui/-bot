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
import signal
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
        with subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              start_new_session=os.name == "posix",
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}) as p:
            try:
                stdout, stderr = p.communicate(timeout=timeout)
            except BaseException:
                try:
                    if os.name == "posix":
                        os.killpg(p.pid, signal.SIGKILL)
                    elif p.poll() is None:
                        p.kill()
                except ProcessLookupError:
                    pass
                p.communicate()
                raise
            return p.returncode, scrub((stdout or "") + (stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 1, f"команда не завершилась за {timeout} с"
    except OSError as e:
        return 1, scrub(str(e))


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


def summarize_failure(out: str) -> str:
    """Из вывода pytest берём главное: какие тесты упали и в чём (строки FAILED/ERROR и первые строки «E …»)."""
    lines = [line.rstrip() for line in out.splitlines()]
    failed = [line for line in lines if line.startswith(("FAILED ", "ERROR "))][:5]
    reasons = [line.strip() for line in lines if line.startswith("E ")][:3]
    picked = failed + [r for r in reasons if r not in failed]
    return "\n".join(picked) if picked else "\n".join(lines[-12:])


def finish(status: str, who: dict, **kw):
    _write("update_result.json", {"status": status, "platform": who.get("platform"), "uid": who.get("uid"), **kw})
    print(f"[updater] {status} {kw.get('detail', '')[:200]}", flush=True)


def requirements_sha(folder: Path) -> str:
    digest = hashlib.sha256()
    for name in ("requirements.txt", "requirements.lock", "requirements-dev.txt", "requirements-dev.lock"):
        path = folder / name
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def validate_requirements(folder: Path) -> None:
    lock = folder / "requirements.lock"
    if not lock.exists():
        return
    def pins(path):
        return {re.sub(r"[-_.]+", "-", name).lower(): version
                for name, version in re.findall(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)", path.read_text(), re.M)}
    runtime = pins(lock)
    direct = pins(folder / "requirements.txt")
    if any(runtime.get(name) != version for name, version in direct.items()):
        raise ValueError("requirements.txt расходится с requirements.lock; обновление остановлено.")
    dev = folder / "requirements-dev.lock"
    if dev.exists():
        development = pins(dev)
        if any(development.get(name) != version for name, version in runtime.items()):
            raise ValueError("Runtime-зависимости двух lock-файлов расходятся.")
        if any(development.get(name) != version for name, version in pins(folder / "requirements-dev.txt").items()):
            raise ValueError("requirements-dev.txt расходится с requirements-dev.lock.")


def compatibility(folder: Path) -> dict | None:
    path = folder / "compatibility.json"
    if not path.exists():
        return None  # Legacy release: the one-time transition cannot promise rollback compatibility.
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or not isinstance(value.get("read_versions"), list)
            or not value["read_versions"] or any(type(v) is not int or v < 1 for v in value["read_versions"])
            or type(value.get("write_version")) is not int or value["write_version"] not in value["read_versions"]):
        raise ValueError("Некорректный compatibility.json")
    return value


def check_compatibility(current: Path, candidate: Path) -> None:
    old, new = compatibility(current), compatibility(candidate)
    if old is not None and (new is None or old["write_version"] not in new["read_versions"]
                            or new["write_version"] not in old["read_versions"]):
        raise ValueError("Версии формата данных несовместимы с обновлением и откатом; требуется отдельная миграция.")


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
        try:
            check_compatibility(CODE, wt)
            validate_requirements(wt)
        except ValueError as exc:
            return finish("failed", who, detail=scrub(str(exc)), commit=short())
        pypath = str(wt)
        if requirements_sha(wt) != requirements_sha(CODE):
            # новые библиотеки нужны только для проверки: ставим в отдельную папку, общее окружение не трогаем
            # (в рабочее окружение они попадут при запуске, entrypoint.sh сам сверяет requirements.txt)
            libs = tmp / "libs"
            req = "requirements-dev.lock" if (wt / "requirements-dev.lock").exists() else "requirements.lock" if (wt / "requirements.lock").exists() else "requirements.txt"
            command = [sys.executable, "-m", "pip", "install", "-q", "--target", str(libs), "-r", req]
            if req.endswith(".lock"):
                command.append("--require-hashes")
            rc, out = run(command, wt, 900)
            if rc:
                return finish("failed", who, detail="не удалось установить библиотеки: " + out[-300:], commit=short())
            pypath = os.pathsep.join([str(wt), str(libs)])
        rc, out = run(shlex.split(TEST_CMD), wt, 600, {"PYTHONPATH": pypath, "DATA_DIR": str(tmp / "data")})
        if rc:
            return finish("tests_failed", who, detail=summarize_failure(out)[-900:], commit=short())
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
    state["status"] = "rolled_back" if not rc else "rollback_failed"
    _write("update_state.json", state)
    finish("rolled_back" if not rc else "failed", who, detail=out[-300:], commit=short())


if __name__ == "__main__":
    try:
        rollback() if "--rollback" in sys.argv else update()
    except Exception as e:  # обновление не должно ронять запуск бота
        print(f"[updater] ошибка: {scrub(str(e))}", flush=True)
    sys.exit(0)
