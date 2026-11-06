"""Выбор четырёх vision-моделей по публичному каталогу AITunnel."""
import contextvars
import json
import math

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
                              ("claude-haiku", []),
                              ("gemini-", ["gemini-2.5-flash", "gemini-2.0-flash"]),
                              ("grok-", [])]:
        choices = [n for n in usable if n.startswith(prefix) and (prefix != "gemini-" or "flash" in n)
                   and (prefix != "grok-" or "fast" in n)]
        prices = {}
        for name in choices:
            try:
                prompt = float(usable[name]["prompt_cost"])
                completion = float(usable[name]["completion_cost"])
                if all(math.isfinite(value) and value >= 0 for value in (prompt, completion, prompt + completion)):
                    prices[name] = prompt + completion
            except (KeyError, TypeError, ValueError):
                continue
        choices = [name for name in choices if name in prices]
        choices.sort(key=lambda n: (preferred.index(n) if n in preferred else len(preferred),
                     prices[n], n))
        if not choices:
            raise ValueError("Нет подходящей vision-модели с известной ценой " + prefix)
        result.append(choices[0])
    return result


async def menu(toggle=False, enabled=None, refresh=False):
    async with resources.slot(f"comparison:{spaces.active_id()}", 1):
        return await _menu(toggle, enabled, refresh)


async def _menu(toggle=False, enabled=None, refresh=False):
    import core
    import ops
    c = config()
    if enabled is not None:
        toggle = bool(c["enabled"]) != enabled
    if toggle or refresh:
        if c["enabled"] and not refresh:
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
                return "Не удалось подобрать четыре vision-модели с ценами, включая Claude Haiku и Grok Fast. Настройки сохранены без изменений; попробуйте позже.", [[("Повторить", "m:compare_refresh" if c["enabled"] else "m:compare_set:1")]]
            c = {"enabled": True, "models": models}
        ops.set_setting("model_comparison", json.dumps(c))
    return ("🧪 Сравнение моделей: " + ("включено" if c["enabled"] else "выключено") + "\n" +
            "\n".join(c["models"]) + f"\nОдин материал — {len(c['models'])} генерации и столько же запросов к провайдеру. Выберите один вариант перед публикацией.",
            [[("Выключить" if c["enabled"] else "Включить и выбрать модели", f"m:compare_set:{0 if c['enabled'] else 1}")]] +
            ([[("🔄 Подобрать недорогие модели", "m:compare_refresh")]] if c["enabled"] else []))
