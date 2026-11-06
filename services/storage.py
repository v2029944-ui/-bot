"""Хранилище и очистка черновиков."""
import time
from pathlib import Path

import core
import dialog_state
import spaces

TERMINAL_TEXT_TTL = 7 * 86400
CLEANUP_BATCH = 200


def get(did: int):
    r = core.db.execute("SELECT * FROM drafts WHERE id=?", (did,)).fetchone()
    return dict(r) if r else None


def _claim(did: int, src: str, dst: str) -> bool:
    """Атомарно меняет статус: защита от двойных нажатий и гонки двух согласующих."""
    fields = "status=?"
    values: list[str | float] = [dst]
    if dst in ("deleted", "expired"):
        fields += ",closed_at=?,cleaned=0"
        values.append(time.time())
    cur = core.db.execute(f"UPDATE drafts SET {fields} WHERE id=? AND status=?", (*values, did, src))
    core.db.commit()
    return cur.rowcount == 1


def _set(did: int, **f):
    if "status" in f:
        f.setdefault("cleaned", 0)
        if f["status"] in ("deleted", "expired"):
            f.setdefault("closed_at", time.time())
        if f["status"] == "published":
            f.setdefault("published_at", time.time())
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
    core.db.execute("DELETE FROM requests WHERE created<?", (time.time() - core.REQ_TTL,))
    core.db.commit()
    expired = 0
    ttl = int(core.env("DRAFT_TTL_DAYS", "7"))  # 0 — не закрывать
    if ttl > 0:
        for r in core.db.execute("SELECT * FROM drafts WHERE status='pending' AND "
                            "COALESCE(published_to,'')='' AND COALESCE(NULLIF(last_sent,0), created) < ? ORDER BY id LIMIT ?", (time.time() - ttl * 86400, CLEANUP_BATCH)).fetchall():
            for path in core.draft_files(dict(r)):
                core._unlink(path)
            core.clear_source_cache(dict(r), commit=False)
            core.db.execute("UPDATE drafts SET status='expired',closed_at=?,cleaned=0 WHERE id=?", (time.time(), r["id"]))
            expired += 1
        core.db.commit()
        if expired:
            core.log.warning("Закрыто черновиков без решения (старше %s дн.): %s", ttl, expired)
    cutoff = time.time() - TERMINAL_TEXT_TTL
    rows = core.db.execute("SELECT * FROM drafts WHERE status IN ('published','deleted','expired') AND "
                           "(cleaned=0 OR (cleaned=1 AND COALESCE(NULLIF(closed_at,0),created)<?)) "
                           "ORDER BY id LIMIT ?", (cutoff, CLEANUP_BATCH)).fetchall()
    for row in rows:
        d = dict(row)
        for path in core.draft_files(d):
            core._unlink(path)
        core.clear_source_cache(d, commit=False)
        if d["status"] == "published" or (d.get("closed_at") or d["created"] or 0) < cutoff:
            _scrub_terminal(d)
        else:
            core.db.execute("UPDATE drafts SET cleaned=1,photo='',video='',media='[]' WHERE id=?", (d["id"],))
    core.db.commit()
    spaces.queue_db().commit()
    known = {p for r in core.db.execute("SELECT photo,video,media FROM drafts WHERE status IN ('pending','publishing','regenerating')") for p in core.draft_files(dict(r))}
    for folder in (core.PHOTOS, core.VIDEOS):
        for f in folder.glob("*"):  # файл моложе часа может принадлежать материалу, который сейчас обрабатывается
            try:
                if f.is_file() and str(f) not in known and time.time() - f.stat().st_mtime > 3600:
                    f.unlink(missing_ok=True)
                    core.log.info("удалён файл-сирота %s", f.name)
            except OSError:
                core.log.warning("не удалось проверить файл %s", f, exc_info=True)
    return expired


def cleanup_pending() -> bool:
    cutoff = time.time() - TERMINAL_TEXT_TTL
    if core.db.execute("SELECT 1 FROM drafts WHERE status IN ('published','deleted','expired') AND "
                       "(cleaned=0 OR (cleaned=1 AND COALESCE(NULLIF(closed_at,0),created)<?)) LIMIT 1", (cutoff,)).fetchone():
        return True
    ttl = int(core.env("DRAFT_TTL_DAYS", "7"))
    return ttl > 0 and core.db.execute("SELECT 1 FROM drafts WHERE status='pending' AND COALESCE(published_to,'')='' "
                                     "AND COALESCE(NULLIF(last_sent,0),created)<? LIMIT 1", (time.time() - ttl * 86400,)).fetchone() is not None


def _unlink(path):
    try:
        Path(path or "").unlink(missing_ok=True)
    except (OSError, IsADirectoryError):
        core.log.warning("не удалось удалить фото %s", path, exc_info=True)


def clear_source_cache(d, commit=True):
    for adapter in core.ADAPTERS.values():
        if forget := getattr(adapter, "forget_uploads", None):
            forget(core.draft_files(d))
    if core.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='max_button_cache'").fetchone():
        core.db.execute("DELETE FROM max_button_cache WHERE mid IN (SELECT mid FROM max_draft_ui WHERE did=?)", (d["id"],))
        if commit:
            core.db.commit()
    if d.get("source_key"):
        import max_queue
        max_queue.clear_cache(d["source_key"])


def scrub_published(d):
    _scrub_terminal(d)
    core.db.commit()
    spaces.queue_db().commit()


def _scrub_terminal(d):
    """Оставляем ID, источник, дату и конечный статус; содержимое завершённого поста больше не нужно."""
    core.db.execute("""UPDATE drafts SET author_id='',author_name='',source_text='',post='',
                  photo='',video='',media='[]',published_to='',publish_errors='{}',publication_channels='{}',
                  post_length='',rejection_reason='',footer=NULL,candidates='[]',selected_model='',attempts=1,last_sent=0,reminded=0,expiry_warned=0,cleaned=2
                  WHERE id=? AND status IN ('published','deleted','expired')""", (d["id"],))
    if d.get("source_key"):
        queue = spaces.queue_db()
        queue.execute("UPDATE max_inbox SET state='done',payload='',error='' WHERE event_key=?", (d["source_key"],))


def startup():
    """При старте: возвращаем «зависшие» после сбоя черновики в работу и убираем старое."""
    core.db.execute("UPDATE deferred SET lease_until=0 WHERE lease_until!=0")
    # The actual closing date of legacy rows is unknown. Give remaining text
    # a full retention period from this first recovery, without guessing a date.
    core.db.execute("UPDATE drafts SET closed_at=? WHERE status IN ('deleted','expired') "
                    "AND COALESCE(closed_at,0)=0 AND cleaned!=2", (time.time(),))
    n = core.db.execute("UPDATE drafts SET status='pending' WHERE status IN ('publishing','regenerating')").rowcount
    core.db.commit()
    if n:
        core.log.warning("Восстановлено зависших черновиков: %s", n)
    core.LAST_EXPIRED = core.housekeeping()
