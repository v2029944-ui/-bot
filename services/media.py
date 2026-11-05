"""Фото, видео и альбомы."""
import core
import io
import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from PIL import Image
from PIL import ImageOps
from PIL import ImageDraw
from PIL import UnidentifiedImageError


def caption_ok(text: str | None) -> bool:
    """Без описания нейросеть выдаёт «воду», поэтому такое фото не принимаем (REQUIRE_CAPTION=0 — отключить)."""
    if core.env("REQUIRE_CAPTION", "1") == "0":
        return True
    return len((text or "").strip()) >= int(core.env("MIN_CAPTION_CHARS", "8"))


def _ffmpeg() -> str:
    try:
        import imageio_ffmpeg  # ffmpeg в виде Python-пакета: в образе Docker ничего ставить не нужно
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg") or "ffmpeg"


def _probe(path: Path) -> tuple[float | None, bool]:
    """(длительность в секундах, есть ли видеодорожка). ffmpeg -i без выходного файла печатает сведения в stderr."""
    try:
        p = subprocess.run([core._ffmpeg(), "-nostdin", "-hide_banner", "-protocol_whitelist", "file", "-i", str(path)],
                           capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return None, False
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", p.stderr)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None
    return dur, "Video:" in p.stderr


def _contact_sheet(video: Path, times: list[float]) -> str:
    """Склеиваем кадры слева направо в одну картинку: нейросеть получает один JPEG, как и с обычным фото."""
    frames = []
    for t in times:
        tmp = core.PHOTOS / f"frame_{uuid.uuid4().hex}.jpg"
        try:
            subprocess.run([core._ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "file",
                            "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", "-vf", "scale=640:-2", "-y", str(tmp)],
                           capture_output=True, timeout=60)
            if tmp.exists() and tmp.stat().st_size:
                with Image.open(tmp) as im:
                    frames.append(im.convert("RGB"))
        except (subprocess.TimeoutExpired, OSError, UnidentifiedImageError):
            pass
        finally:
            tmp.unlink(missing_ok=True)
    if not frames:
        raise core.BadImage("Не получилось прочитать кадры из видео")
    sheet = Image.new("RGB", (sum(f.width for f in frames), max(f.height for f in frames)))
    x = 0
    for f in frames:
        sheet.paste(f, (x, 0))
        x += f.width
    if sheet.width > 1600:
        sheet = sheet.resize((1600, max(1, sheet.height * 1600 // sheet.width)))
    path = core.PHOTOS / f"{uuid.uuid4().hex}.jpg"
    sheet.save(path, "JPEG", quality=85)
    return str(path)


def save_video(data: bytes, filename: str = "") -> tuple[str, str]:
    """Сохраняет видео и склейку из трёх кадров (начало, середина, конец) для нейросети и превью.
    Возвращает (путь к видео, путь к картинке). Бросает BadImage, если это не видео или оно слишком большое/длинное."""
    if len(data) > core.MAX_VIDEO_BYTES:
        raise core.BadImage(f"Видео больше {core.MAX_VIDEO_BYTES // 1024 // 1024} МБ")
    ext = Path(filename).suffix.lower()
    path = core.VIDEOS / f"{uuid.uuid4().hex}{ext if ext in core._VIDEO_EXT else '.mp4'}"
    output = core.VIDEOS / f"{uuid.uuid4().hex}.mp4"
    path.write_bytes(data)
    try:
        dur, has_video = core._probe(path)
        if dur is None or not has_video:
            raise core.BadImage("Не получилось открыть файл — это не видео")
        if dur > core.MAX_VIDEO_SEC:
            raise core.BadImage(f"Видео длиннее {core.MAX_VIDEO_SEC} с")
        # В Telegram sendVideo поддерживает MPEG4. Нормализуем также MOV/HEVC
        # с телефонов: одинаковый MP4/H.264 отправляется в оба канала.
        try:
            converted = subprocess.run([
                core._ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error",
                "-protocol_whitelist", "file", "-threads", "2", "-i", str(path),
                "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
                "-vf", "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-crf", "23",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", "-y", str(output),
            ], capture_output=True, timeout=300)
        except (subprocess.TimeoutExpired, OSError) as e:
            raise core.BadImage("Не получилось подготовить видео для публикации") from e
        if converted.returncode or not output.exists() or not output.stat().st_size:
            raise core.BadImage("Не получилось подготовить видео для публикации")
        if output.stat().st_size > core.MAX_VIDEO_BYTES:
            raise core.BadImage(f"После обработки видео больше {core.MAX_VIDEO_BYTES // 1024 // 1024} МБ")
        times = [dur * 0.15, dur * 0.5, dur * 0.85] if dur > 2 else [0.0]
        sheet = core._contact_sheet(output, times)
        path.unlink(missing_ok=True)
        return str(output), sheet
    except BaseException:
        path.unlink(missing_ok=True)
        output.unlink(missing_ok=True)
        raise


def draft_media(d: dict) -> list[dict]:
    return json.loads(d.get("media") or "[]")


def draft_files(d: dict) -> set[str]:
    paths = {d.get("photo"), d.get("video")}
    for item in core.draft_media(d):
        paths.update((item["path"], item["preview"]))
    return {p for p in paths if p}


def media_kw(d: dict) -> dict:
    media = core.draft_media(d)
    if media:
        return {"media": media}
    return {"video": d["video"]} if d.get("video") else {}


def album_preview(media: list[dict]) -> str:
    """Все вложения в одном пронумерованном превью для LLM; исходники сохраняются отдельно."""
    sheet = Image.new("RGB", (1600, 450 * ((len(media) + 1) // 2)), "white")
    draw = ImageDraw.Draw(sheet)
    for i, item in enumerate(media):
        with Image.open(item["preview"]) as im:
            tile = ImageOps.contain(im.convert("RGB"), (780, 410))
        x, y = (i % 2) * 800, (i // 2) * 450
        sheet.paste(tile, (x + 10, y + 30))
        draw.text((x + 10, y + 8), f"{i + 1}: {item['type']}", fill="black")
        tile.close()
    path = core.PHOTOS / f"{uuid.uuid4().hex}.jpg"
    try:
        sheet.save(path, "JPEG", quality=88)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        sheet.close()
    return str(path)


def _pixel_limit(fmt: str | None) -> int:
    return {"JPEG": core.MAX_PIXELS_JPEG, "MPO": core.MAX_PIXELS_JPEG, "HEIF": core.MAX_PIXELS_HEIF}.get(fmt or "", core.MAX_PIXELS_OTHER)


def save_photo(data: bytes) -> str:
    """Приводим любое фото к JPEG до 1600px: так оно проходит лимиты и нейросети, и мессенджеров.
    Ошибки содержимого — ValueError: это ошибка пользователя, а не сбой бота."""
    if len(data) > core.MAX_PHOTO_BYTES:
        raise core.BadImage("Файл слишком большой (больше 25 МБ)")
    try:
        img = Image.open(io.BytesIO(data))  # читает только заголовок: память пока не тратится
        w, h = img.size
        if w * h > core._pixel_limit(img.format):
            raise core.BadImage(f"Слишком большое разрешение ({w}×{h}). Пришлите фото поменьше")
        if img.format == "JPEG":
            img.draft("RGB", (1600, 1600))  # JPEG сразу читается уменьшенным: даже огромный кадр не раздувает память
        img = ImageOps.exif_transpose(img)  # поворот по EXIF, как снято на телефон
        img = img.convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise core.BadImage("Не получилось открыть файл — это не изображение") from e
    img.thumbnail((1600, 1600))
    # sendPhoto Telegram: отношение сторон не должно превышать 20.
    w, h = img.size
    if max(w, h) > 20 * min(w, h):
        img = ImageOps.pad(img, (max(w, (h + 19) // 20), max(h, (w + 19) // 20)), color="white")
    core.PHOTOS.mkdir(parents=True, exist_ok=True)
    path = core.PHOTOS / f"{uuid.uuid4().hex}.jpg"  # уникальное имя: альбомы приходят одновременно
    img.save(path, "JPEG", quality=88)
    return str(path)
