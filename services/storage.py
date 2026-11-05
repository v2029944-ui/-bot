"""Хранилище и очистка черновиков."""
import core
import time
from pathlib import Path
import spaces
import dialog_state


def get(did: int):
    r = core.db.execute("SELECT * FROM drafts WHERE id=?", (did,)).fetchone()
    return dict(r) if r else None


def _claim(did: int, src: str, dst: str) -> bool:
    """Атомарно меняет статус: защита от двойных нажатий и гонки двух согласующих."""
    cur = core.db.execute("UPDATE drafts SET status=? WHERE id=? AND status=?", (dst, did, src))
    core.db.commit()
    return cur.rowcount == 1


def _set(did: int, **f):
    core.db.execute(f"UPDATE drafts SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), did))
    core.db.commit()


def _parse_marks(raw: str | None) -> dict[str, str]:
    """published_to: "tg" — опубликовано, "max?" — отправлено, но ответ потерялся (пост мог выйти)."""
    out: dict[str, str] = {}
    for x in (raw or "").split(","):
        x = x.strip()
        if not x:
            continue
        if x.endswith("?"):
            out[x[:-1]] = "unknown"
        else:
            out[x] = "done"
    return out


def _dump_marks(m: dict[str, str]) -> str:
    return ",".join(sorted(f"{k}?" if v == "unknown" else k for k, v in m.items()))


def housekeeping() -> int:
    """Уборка: закрываем черновики без решения старше DRAFT_TTL_DAYS (статус expired, файлы удаляем),
    стираем оставшиеся файлы опубликованных/удалённых/просроченных постов и файлы-сироты (которых нет ни в одном черновике).
    Вызывается при старте и периодически (HOUSEKEEPING_SEC). Возвращает число закрытых черновиков."""
    dialog_state.cleanup(core.db)
    expired = 0
    ttl = int(core.env("DRAFT_TTL_DAYS", "7"))  # 0 — не закрывать
    if ttl > 0:
        for r in core.db.execute("SELECT * FROM drafts WHERE status='pending' AND "
                            "COALESCE(NULLIF(last_sent,0), created) < ?", (time.time() - ttl * 86400,)).fetchall():
            for path in core.draft_files(dict(r)):
                core._unlink(path)
            core.clear_source_cache(dict(r))
            core.db.execute("UPDATE drafts SET status='expired' WHERE id=?", (r["id"],))
            expired += 1
        core.db.commit()
        if expired:
            core.log.warning("Закрыто черновиков без решения (старше %s дн.): %s", ttl, expired)
    for r in core.db.execute("SELECT * FROM drafts WHERE status IN ('published','deleted','expired')").fetchall():
        for path in core.draft_files(dict(r)):
            core._unlink(path)
        core.clear_source_cache(dict(r))
        if r["status"] == "published":
            core.scrub_published(dict(r))
    known = {p for r in core.db.execute("SELECT * FROM drafts") for p in core.draft_files(dict(r))}
    for folder in (core.PHOTOS, core.VIDEOS):
        for f in folder.glob("*"):  # файл моложе часа может принадлежать материалу, который сейчас обрабатывается
            try:
                if f.is_file() and str(f) not in known and time.time() - f.stat().st_mtime > 3600:
                    f.unlink(missing_ok=True)
                    core.log.info("удалён файл-сирота %s", f.name)
            except OSError:
                core.log.warning("не удалось проверить файл %s", f, exc_info=True)
    return expired


def _unlink(path):
    try:
        Path(path or "").unlink(missing_ok=True)
    except (OSError, IsADirectoryError):
        core.log.warning("не удалось удалить фото %s", path, exc_info=True)


def clear_source_cache(d):
    for adapter in core.ADAPTERS.values():
        if forget := getattr(adapter, "forget_uploads", None):
            forget(core.draft_files(d))
    if core.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='max_button_cache'").fetchone():
        core.db.execute("DELETE FROM max_button_cache WHERE mid IN (SELECT mid FROM max_draft_ui WHERE did=?)", (d["id"],))
        core.db.commit()
    if d.get("source_key"):
        import max_queue
        max_queue.clear_cache(d["source_key"])


def scrub_published(d):
    """Оставляем ID, источник, дату и конечный статус; содержимое завершённого поста больше не нужно."""
    core.db.execute("""UPDATE drafts SET author_id='',author_name='',source_text='',post='',
                  photo='',video='',media='[]',published_to='',publish_errors='{}',
                  post_length='',rejection_reason='',footer=NULL,candidates='[]',selected_model='',attempts=1,last_sent=0,reminded=0,expiry_warned=0
                  WHERE id=? AND status='published'""", (d["id"],))
    if d.get("source_key"):
        queue = spaces.queue_db()
        queue.execute("UPDATE max_inbox SET state='done',payload='',error='' WHERE event_key=?", (d["source_key"],))
        queue.commit()
    core.db.commit()


def startup():
    """При старте: возвращаем «зависшие» после сбоя черновики в работу и убираем старое."""
    n = core.db.execute("UPDATE drafts SET status='pending' WHERE status IN ('publishing','regenerating')").rowcount
    core.db.commit()
    if n:
        core.log.warning("Восстановлено зависших черновиков: %s", n)
    core.LAST_EXPIRED = core.housekeeping()
