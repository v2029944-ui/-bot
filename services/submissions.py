"""Приём материалов и доставка на согласование."""
import core
import json
import os
import time
from pathlib import Path
import comparison
import spaces
import resources
import ops


async def _check_rate(adapter: core.Adapter, author_id: str, author_name: str, source_key=None):
    """Защита бюджета: не больше RATE_LIMIT_PER_HOUR фото в час от сотрудника (согласующие без лимита)."""
    if str(author_id) in adapter.approvers:
        return
    if source_key:
        row = spaces.queue_db().execute("SELECT rate_counted FROM max_inbox WHERE event_key=?", (source_key,)).fetchone()
        if row and row[0]:
            return  # повтор обработки одного материала не расходует лимит сотрудника
    limit = int(core.env("RATE_LIMIT_PER_HOUR", "20"))
    key, now = f"{adapter.name}:{author_id}", time.time()
    hits = [t for t in core._HITS.get(key, []) if now - t < 3600]
    if len(hits) >= limit:
        core._HITS[key] = hits
        if now - core._RL_ALERT.get(key, 0) > 3600:  # согласующим пишем не чаще раза в час
            core._RL_ALERT[key] = now
            await core.alert(adapter, f"⚠️ {author_name} отправил больше {limit} фото за час. Новые фото игнорируются.")
        raise core.RateLimited(key)
    core._HITS[key] = hits + [now]
    if source_key:
        queue = spaces.queue_db()
        queue.execute("UPDATE max_inbox SET rate_counted=1 WHERE event_key=?", (source_key,))
        queue.commit()


def _approval_adapter(d: dict, fallback: core.Adapter) -> core.Adapter:
    """Кто согласует: APPROVAL_PLATFORM (если задан), иначе — мессенджер автора."""
    approval = core.ADAPTERS.get(os.getenv("APPROVAL_PLATFORM") or d["platform"])
    if approval is not None and approval.publish_only:
        # Сохраняем доступ к старым черновикам из Telegram после перехода в MAX.
        approval = core.ADAPTERS.get("max")
    return approval or fallback


async def _send_draft(d: dict, did: int, post: str, fallback: core.Adapter,
                      actor: str | None = None) -> tuple[int, Exception | None]:
    """Рассылаем черновик всем согласующим: сбой у одного не должен лишать черновика остальных.
    Режим уведомлений у каждого свой: без звука или придержать до рабочего времени. Тому, кто сам
    только что нажал кнопку (actor), новый вариант приходит сразу и обычным образом."""
    approval = core._approval_adapter(d, fallback)
    sent, last = 0, None
    for a in approval.approvers:
        mode = "normal" if a == actor else ops.delivery_for(approval, a)
        try:
            if mode == "defer":
                ops.enqueue(approval, a, did)  # придёт, когда начнётся рабочее время
                sent += 1
                continue
            kw = {"silent": True} if mode == "silent" else {}
            await approval.send_for_approval(a, did, d["photo"], post, **kw, **core.media_kw(d))
            sent += 1
        except Exception as e:
            last = e
            core.log.warning("не удалось отправить черновик %s согласующему %s: %s", did, a, e)
    if sent:  # от этого момента считаем, сколько черновик ждёт согласования
        core._set(did, last_sent=time.time(), reminded=0)
    return sent, last


async def submit(adapter: core.Adapter, author_id: str, author_name: str, text: str, photo_bytes: bytes,
                 kind: str = "photo", filename: str = "", source_key: str | None = None):
    """Сотрудник прислал фото + описание -> пост -> на согласование.
    Работа идёт в отдельной задаче под shield: при остановке бота main.serve дожидается её завершения
    (drain_inflight), а не обрывает на середине, когда фото уже принято, а черновика ещё нет."""
    return await core._guard(core._submit(adapter, author_id, author_name, text, photo_bytes, kind, filename, source_key))


async def resume_submission(adapter: core.Adapter, source_key: str | None):
    if not source_key:
        return None
    row = core.db.execute("SELECT * FROM drafts WHERE source_key=?", (source_key,)).fetchone()
    if not row:
        return None
    d = dict(row)
    if d["status"] == "pending" and not d["last_sent"]:
        sent, last = await core._send_draft(d, d["id"], d["post"], adapter)
        if not sent:
            raise RuntimeError(f"Не удалось отправить сохранённый черновик: {core.err(last)}")
    return d["id"]


async def _submit(adapter: core.Adapter, author_id: str, author_name: str, text: str, photo_bytes: bytes,
                  kind: str = "photo", filename: str = "", source_key: str | None = None):
    if (existing := await core.resume_submission(adapter, source_key)) is not None:
        return existing
    if kind == "video":  # path — картинка из кадров (для нейросети и превью), vpath — само видео
        vpath, path = await resources.media(core.save_video, photo_bytes, filename)
    else:
        vpath, path = "", await resources.media(core.save_photo, photo_bytes)  # BadImage — файл не картинка
    files = [path, vpath] if vpath else [path]
    rm = lambda: [Path(f).unlink(missing_ok=True) for f in files]
    try:
        await core._check_rate(adapter, author_id, author_name, source_key)  # лимит считаем только за валидные файлы
    except BaseException:
        rm()
        raise
    try:
        post = await core.generate_options(path, text, video=bool(vpath))
    except BaseException:  # в том числе отмена при остановке: иначе файлы висят на диске навсегда
        rm()
        raise
    cur = core.db.execute(
        "INSERT INTO drafts(platform,author_id,author_name,source_text,photo,video,post,created,source_key) VALUES(?,?,?,?,?,?,?,?,?)",
        (adapter.name, author_id, author_name, text, path, vpath, post, time.time(), source_key))
    core.db.commit()
    did = cur.lastrowid
    if isinstance(post, comparison.Options):
        core._set(did, candidates=json.dumps(post.options, ensure_ascii=False))
    d = core.get(did)
    sent, last = await core._send_draft(d, did, post, adapter)
    if not sent:  # иначе черновик-сирота: в базе есть, никто не видел
        if source_key:  # очередь повторит доставку уже готового черновика
            raise RuntimeError(f"Не удалось отправить черновик согласующим: {core.err(last)}")
        core.db.execute("DELETE FROM drafts WHERE id=?", (did,))
        core.db.commit()
        rm()
        raise RuntimeError(f"Не удалось отправить черновик согласующим: {core.err(last)}")
    return did


async def submit_album(adapter: core.Adapter, author_id: str, author_name: str, text: str, items: list[dict],
                       source_key: str | None = None):
    if not 2 <= len(items) <= core.MAX_ALBUM_ITEMS:
        raise core.BadImage(f"В одном посте должно быть от 2 до {core.MAX_ALBUM_ITEMS} вложений")
    if sum(len(item["data"]) for item in items) > core.MAX_ALBUM_BYTES:
        raise core.BadImage("Общий размер альбома больше 100 МиБ")
    return await core._guard(core._submit_album(adapter, author_id, author_name, text, items, source_key))


async def _submit_album(adapter, author_id, author_name, text, items, source_key=None):
    if (existing := await core.resume_submission(adapter, source_key)) is not None:
        return existing
    media, files, did = [], set(), None
    try:
        for item in items:
            if item["type"] == "video":
                path, preview = await resources.media(core.save_video, item["data"], "video.mp4")
            elif item["type"] == "image":
                path = preview = await resources.media(core.save_photo, item["data"])
            else:
                raise core.BadImage("Альбом может содержать только фото и видео")
            files.update((path, preview))
            media.append({"type": item["type"], "path": path, "preview": preview})
        if sum(Path(i["path"]).stat().st_size for i in media) > core.MAX_ALBUM_BYTES:
            raise core.BadImage("После обработки общий размер альбома больше 100 МиБ")
        await core._check_rate(adapter, author_id, author_name, source_key)  # один альбом — один пост
        preview = await resources.media(core.album_preview, media)
        files.add(preview)
        post = await core.generate_options(preview, text, media=media)
        cur = core.db.execute(
            "INSERT INTO drafts(platform,author_id,author_name,source_text,photo,media,post,created,source_key) VALUES(?,?,?,?,?,?,?,?,?)",
            (adapter.name, author_id, author_name, text, preview, json.dumps(media), post, time.time(), source_key))
        core.db.commit()
        did = cur.lastrowid
        if isinstance(post, comparison.Options):
            core._set(did, candidates=json.dumps(post.options, ensure_ascii=False))
        sent, last = await core._send_draft(core.get(did), did, post, adapter)
        if not sent:
            if source_key:
                raise RuntimeError(f"Не удалось отправить черновик согласующим: {core.err(last)}")
            core.db.execute("DELETE FROM drafts WHERE id=?", (did,))
            core.db.commit()
            did = None
            raise RuntimeError(f"Не удалось отправить черновик согласующим: {core.err(last)}")
        return did
    finally:
        if did is None:
            for path in files:
                core._unlink(path)
