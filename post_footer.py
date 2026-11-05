"""Управляемая администратором ссылка в опубликованных постах."""
import html
import json
from urllib.parse import urlsplit

WAITING = {}


class LinkedPost(str):
    def __new__(cls, markup, plain):
        obj = super().__new__(cls, markup)
        obj.plain = plain
        return obj


def config():
    import ops
    value = json.loads(ops.get_setting("post_footer", '{"enabled":false,"text":"Подробнее","max":"","tg":""}'))
    value.setdefault("marker", "{bot}")
    return value


def save(value):
    import ops
    ops.set_setting("post_footer", json.dumps(value, ensure_ascii=False))


def for_draft(did):
    import core
    d = core.get(did)
    if d.get("footer") is not None:
        return json.loads(d["footer"])
    settings = config()
    core._set(did, footer=json.dumps(settings, ensure_ascii=False))
    return settings


def view():
    c = config()
    return (f"🔗 Ссылка в постах: {'включена' if c['enabled'] else 'выключена'}.\n"
            f"Маркер: {c['marker']}\nТекст: {c['text']}\nMAX: {c['max'] or 'не задан'}\nTelegram: {c['tg'] or 'не задан'}\n"
            "Маркер в тексте заменяется ссылкой. Если его нет, ссылка добавляется в конце.\n"
            "Ссылка видна в варианте на проверке и добавляется в публикации. Для одного адреса в обоих каналах укажите его в обоих полях.",
            [[("✏️ Текст ссылки", "m:footer_text"), ("Маркер", "m:footer_marker")], [("Адрес MAX", "m:footer_max"), ("Адрес Telegram", "m:footer_tg")],
             [("Выключить" if c['enabled'] else "Включить", "m:footer_toggle")]])


def validate(field, value):
    value = value.strip()
    if field == "marker":
        if not value or len(value.encode("utf-16-le")) // 2 > 32 or any(ch.isspace() for ch in value):
            raise ValueError("Маркер должен содержать от 1 до 32 знаков без пробелов, например {bot}.")
    elif field == "text":
        if not value or len(value.encode("utf-16-le")) // 2 > 80 or "\n" in value:
            raise ValueError("Текст ссылки должен занимать одну строку от 1 до 80 знаков.")
    elif value == "-":
        value = ""
    else:
        try:
            url = urlsplit(value)
            if url.scheme != "https" or not url.hostname or url.username or url.password or len(value) > 300 or any(ch.isspace() for ch in value):
                raise ValueError()
        except ValueError:
            raise ValueError("Пришлите HTTPS-ссылку до 300 знаков или «-», чтобы убрать её.")
    return value


def render(post, channel, settings):
    marker = settings.get("marker", "{bot}")
    label = settings.get("text", "Подробнее")
    has_marker = bool(marker) and marker in post
    if not settings.get("enabled") or not settings.get(channel):
        result = post.replace(marker, label) if has_marker else post
        if len(result.encode("utf-16-le")) // 2 > 4000:
            raise ValueError("Пост после замены маркера слишком длинный. Сократите текст.")
        return result
    link = '<a href="' + html.escape(settings[channel], quote=True) + '">' + html.escape(label) + '</a>'
    if has_marker:
        plain = post.replace(marker, label)
        markup = link.join(html.escape(part) for part in post.split(marker))
    else:
        plain = post + "\n\n" + label
        markup = html.escape(post) + '\n\n' + link
    if len(markup.encode("utf-16-le")) // 2 > 4000:
        raise ValueError("Пост со ссылкой слишком длинный. Сократите текст и подтвердите публикацию заново.")
    return LinkedPost(markup, plain)


def restore_copied_marker(text, original, settings):
    """Восстановить неизменённый скопированный заключительный абзац без угадывания ссылки."""
    marker = settings.get("marker", "{bot}")
    if not settings.get("enabled") or not (settings.get("max") or settings.get("tg")) or marker in text:
        return text
    label = settings["text"]
    if marker in original:
        tail = original.strip().split("\n\n")[-1]
        visible = tail.replace(marker, label)
        if marker in tail and text.endswith(visible):
            return text[:-len(visible)] + tail
    elif text == label or text.endswith("\n" + label):
        return text[:-len(label)] + marker
    raise ValueError(f"Чтобы сохранить ссылку в предложении, замените её слово на {marker} и пришлите текст снова. "
                     "Бот сам сделает его кликабельным. Отмена: /cancel")
