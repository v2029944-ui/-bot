"""Выбор трёх vision-моделей по публичному каталогу AITunnel."""
import contextvars
import json

import httpx

import resources
import spaces

MODEL = contextvars.ContextVar("generation_model", default=None)


class Options(str):
    options: list[dict]
    def __new__(cls, options):
        obj = super().__new__(cls, next(o["post"] for o in options if o.get("post")))
        obj.options = options
        return obj


def config():
    import ops
    return json.loads(ops.get_setting("model_comparison", '{"enabled":false,"models":[]}'))


def select(catalog):
    usable = {n: i for n, i in catalog.items() if isinstance(i, dict)
              and "image" in i.get("modalities", {}).get("input", [])
              and "text" in i.get("modalities", {}).get("output", [])}
    result = []
    for prefix, preferred in [("gpt-", ["gpt-4.1-mini", "gpt-4o-mini"]),
                              ("claude-sonnet", ["claude-sonnet-4.6", "claude-sonnet-4.5"]),
                              ("gemini-", ["gemini-2.5-flash", "gemini-2.0-flash"])]:
        choices = [n for n in usable if n.startswith(prefix) and (prefix != "gemini-" or "flash" in n)]
        choices.sort(key=lambda n: (preferred.index(n) if n in preferred else len(preferred),
                     float(usable[n].get("prompt_cost", 1e9)) + float(usable[n].get("completion_cost", 1e9)), n))
        if not choices:
            raise ValueError("Нет подходящей vision-модели " + prefix)
        result.append(choices[0])
    return result


async def menu(toggle=False, enabled=None):
    async with resources.slot(f"comparison:{spaces.active_id()}", 1):
        return await _menu(toggle, enabled)


async def _menu(toggle=False, enabled=None):
    import core
    import ops
    c = config()
    if enabled is not None:
        toggle = bool(c["enabled"]) != enabled
    if toggle:
        if c["enabled"]:
            c["enabled"] = False
        else:
            if core.PROVIDER != "openai" or core.env("LLM_BASE_URL").rstrip("/") != "https://api.aitunnel.ru/v1":
                return "Для сравнения настройте AITunnel: LLM_PROVIDER=openai и LLM_BASE_URL=https://api.aitunnel.ru/v1.", None
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.get("https://api.aitunnel.ru/public/aitunnel/models/chat")
                    response.raise_for_status()
                    models = select(response.json())
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                return "Не удалось выбрать три vision-модели из каталога. Режим не включён; попробуйте позже.", [[("Повторить", "m:compare_set:1")]]
            c = {"enabled": True, "models": models}
        ops.set_setting("model_comparison", json.dumps(c))
    return ("🧪 Сравнение моделей: " + ("включено" if c["enabled"] else "выключено") + "\n" +
            "\n".join(c["models"]) + "\nОдин материал — три генерации и три списания. Выберите один вариант перед публикацией.",
            [[("Выключить" if c["enabled"] else "Включить и выбрать модели", f"m:compare_set:{0 if c['enabled'] else 1}")]])
