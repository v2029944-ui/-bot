"""Выбор моделей AITunnel для пространства: одна модель или сравнение нескольких."""
import contextvars
import json
import math
import secrets

import httpx

import dialog_state
import resources
import spaces

MODEL: contextvars.ContextVar[str | None] = contextvars.ContextVar("generation_model", default=None)
MAX_MODELS = 4
PAGE_SIZE = 8
POLICY_VERSION = 1  # Bump when changing the allowlist to expire stored picker snapshots.
DEFAULT_MODEL = "gpt-4.1-mini"
BUDGET_MODELS = frozenset({"gpt-4.1-mini", "gpt-4.1-nano", "gpt-4o-mini",
                           "gemini-2.5-flash-lite", "gemini-3.1-flash-lite"})


class Options(str):
    options: list[dict]
    def __new__(cls, options):
        obj = super().__new__(cls, next(o["post"] for o in options if o.get("post")))
        obj.options = options
        return obj


def config():
    import ops
    c = json.loads(ops.get_setting("model_comparison", '{"enabled":false,"models":[]}'))
    models = list(dict.fromkeys(name for name in c["models"] if name in BUDGET_MODELS))[:MAX_MODELS]
    if c["enabled"] and not models:
        models = [DEFAULT_MODEL]
    if models != c["models"]:
        c = {**c, "models": models}
        ops.set_setting("model_comparison", json.dumps(c))
    return c


def default_model():
    import core
    c = config()
    model = c["models"][0] if c["enabled"] and c["models"] else core.MODEL
    return DEFAULT_MODEL if budget_provider() and model not in BUDGET_MODELS else model


def budget_provider():
    import core
    return core.PROVIDER == "openai" and core.env("LLM_BASE_URL").rstrip("/") == "https://api.aitunnel.ru/v1"


def request_model(model=None):
    model = model or default_model()
    return default_model() if budget_provider() and model not in BUDGET_MODELS else model


def draft_selection(post):
    """A single configured model needs no extra candidate-selection step."""
    options = post.options if isinstance(post, Options) else []
    return {"candidates": json.dumps(options if len(options) > 1 else [], ensure_ascii=False),
            "selected_model": options[0]["model"] if len(options) == 1 else ""}


def eligible(catalog):
    import core
    if not isinstance(catalog, dict):
        raise ValueError("Некорректный каталог")
    result = {}
    for name, info in catalog.items():
        if name not in BUDGET_MODELS or not isinstance(info, dict):
            continue
        modalities = info.get("modalities") or {}
        if (not isinstance(modalities, dict) or "text" not in (modalities.get("output") or [])
                or "text" not in (modalities.get("input") or [])):
            continue
        if core.env("LLM_VISION", "1") != "0" and "image" not in (modalities.get("input") or []):
            continue
        result[name] = info
    return result


def price(info):
    try:
        values = [float(info[key]) for key in ("prompt_cost", "completion_cost")]
        total = sum(values)
        return total if all(math.isfinite(v) and v >= 0 for v in [*values, total]) else math.inf
    except (KeyError, TypeError, ValueError):
        return math.inf


def select(catalog):
    usable = eligible(catalog)
    result = []
    for choices in (("gpt-4.1-mini", "gpt-4o-mini"),
                    ("gemini-2.5-flash-lite", "gemini-3.1-flash-lite")):
        found = next((name for name in choices if name in usable), None)
        if found:
            result.append(found)
    # If a recommended model disappears, prefer a known lower tariff over an unknown one.
    rest = sorted((name for name in usable if name not in result), key=lambda name: (price(usable[name]), name))
    result += rest[:2 - len(result)]
    if len(result) != 2:
        raise ValueError("В каталоге недостаточно подходящих моделей")
    return result


async def fetch_catalog():
    import core
    if core.PROVIDER != "openai" or core.env("LLM_BASE_URL").rstrip("/") != "https://api.aitunnel.ru/v1":
        raise ValueError("Для выбора моделей настройте AITunnel: LLM_PROVIDER=openai и LLM_BASE_URL=https://api.aitunnel.ru/v1.")
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get("https://api.aitunnel.ru/public/aitunnel/models/chat")
        response.raise_for_status()
        catalog = eligible(response.json())
    if not catalog:
        raise ValueError("В каталоге нет моделей с нужной поддержкой текста и изображений.")
    return catalog


def picker_state():
    import core
    return dialog_state.PendingMap(core.db, "model_picker")


def view():
    c = config()
    models = c["models"] if c["enabled"] else []
    text = ("🤖 Модели генерации\n" + ("\n".join(models) if models else f"По умолчанию: {default_model()}") +
            "\n\nДоступны компактные GPT mini/nano и Gemini Flash Lite. " +
            f"Один материал — {len(models) or 1} запрос(а) генерации. "
            "Одна модель — обычный пост; несколько — варианты для выбора перед публикацией.")
    rows = [[("☑ Выбрать модели", "m:compare_pick")], [("⭐ Рекомендованная пара", "m:compare_refresh")]]
    if models:
        rows.append([("Модель по умолчанию", "m:compare_set:0")])
    return text, rows


def picker_view(state, notice=""):
    selected, names, token, revision = state["selected"], state["names"], state["token"], state["revision"]
    page = min(max(0, state["page"]), (len(names) - 1) // PAGE_SIZE)
    state["page"] = page
    text = ("☑ Выбор моделей\nВыберите от 1 до 4 моделей.\n" +
            ("\n".join(selected) if selected else "Пока ничего не выбрано.") +
            f"\n\nВыбрано: {len(selected)}. Каждый материал — столько же запросов генерации.\n"
            "Изменения вступят в силу после «Сохранить».\n"
            f"Страница {page + 1} из {(len(names) - 1) // PAGE_SIZE + 1}.")
    rows = []
    for index in range(page * PAGE_SIZE, min((page + 1) * PAGE_SIZE, len(names))):
        name = names[index]
        marked = name in selected
        rows.append([(("☑ " if marked else "☐ ") + name[:70], f"m:models_set:{token}:{revision}:{index}:{0 if marked else 1}")])
    nav = []
    if page:
        nav.append(("← Назад", f"m:models_page:{token}:{revision}:{page - 1}"))
    if (page + 1) * PAGE_SIZE < len(names):
        nav.append(("Далее →", f"m:models_page:{token}:{revision}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([("✅ Сохранить", f"m:models_save:{token}:{revision}"), ("Отмена", "m:compare")])
    return (notice + "\n\n" if notice else "") + text, rows


async def menu(toggle=False, enabled=None, refresh=False, *, adapter=None, uid=None, action=None):
    async with resources.slot(f"comparison:{spaces.active_id()}", 1):
        return await _menu(toggle, enabled, refresh, adapter=adapter, uid=uid, action=action)


async def _menu(toggle=False, enabled=None, refresh=False, *, adapter=None, uid=None, action=None):
    import ops
    key = (adapter.name, str(uid)) if adapter is not None else ("max", str(uid))
    current = config()
    previous = picker_state().get(key)
    if previous and previous.get("policy") != POLICY_VERSION:
        picker_state().pop(key, None)
        previous = None
    if (enabled is True or action == "m:compare_pick") and previous and previous["before"] == current:
        return picker_view(previous)
    if action and action.startswith("m:models_"):
        state = picker_state().get(key)
        parts = action.split(":")
        if (not state or len(parts) < 4 or parts[2] != state["token"] or parts[3] != str(state["revision"])):
            return picker_view(state, "Эта кнопка устарела. Используйте текущий список.") if state else (
                "Этот список уже закрыт. Откройте выбор моделей заново.", view()[1])
        if current != state["before"]:
            picker_state().pop(key, None)
            return "Настройки уже изменены другим администратором. Откройте список заново.", view()[1]
        if parts[1] == "models_set" and len(parts) == 6 and parts[4].isdigit() and len(parts[4]) < 8 and parts[5] in ("0", "1"):
            index = int(parts[4])
            if index >= len(state["names"]):
                return picker_view(state, "Неизвестная модель.")
            name = state["names"][index]
            selected = state["selected"]
            if parts[5] == "1" and name not in selected:
                if len(selected) >= MAX_MODELS:
                    return picker_view(state, "Можно выбрать не больше четырёх моделей.")
                selected.append(name)
            elif parts[5] == "0" and name in selected:
                selected.remove(name)
            state["revision"] += 1
        elif parts[1] == "models_page" and len(parts) == 5 and parts[4].isdigit() and len(parts[4]) < 8:
            state["page"] = int(parts[4])
        elif parts[1] == "models_save" and len(parts) == 4:
            if not 1 <= len(state["selected"]) <= MAX_MODELS:
                return picker_view(state, "Выберите хотя бы одну модель.")
            try:
                catalog = await fetch_catalog()
                if any(name not in catalog for name in state["selected"]):
                    return picker_view(state, "Одна из моделей больше не доступна с нужными возможностями. Нажмите «Отмена» и откройте список заново.")
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                return picker_view(state, "Не удалось проверить каталог. Выбор не сохранён; попробуйте позже.")
            if config() != state["before"]:
                picker_state().pop(key, None)
                return "Настройки изменились. Откройте список заново.", view()[1]
            ops.set_setting("model_comparison", json.dumps({"enabled": True, "models": state["selected"]}))
            picker_state().pop(key, None)
            text, rows = view()
            return "✅ Модели сохранены для этого пространства.\n\n" + text, rows
        else:
            return picker_view(state, "Неизвестное действие.")
        result = picker_view(state)
        picker_state()[key] = state
        return result
    picker_state().pop(key, None)
    if enabled is False or toggle and current["enabled"]:
        ops.set_setting("model_comparison", json.dumps({**current, "enabled": False}))
        return view()
    opening = refresh or enabled is True or toggle or action == "m:compare_pick"
    if not opening:
        return view()
    try:
        catalog = await fetch_catalog()
        recommended = select(catalog) if len(catalog) >= 2 else list(catalog)
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return "Не удалось получить список подходящих моделей AITunnel. Настройки не изменены.", [[("Повторить", "m:compare_pick")]]
    selected = recommended if refresh or not current["models"] else [name for name in current["models"] if name in catalog][:MAX_MODELS]
    ordered = list(dict.fromkeys([*recommended, *selected, *sorted(catalog)]))
    state = {"policy": POLICY_VERSION, "token": secrets.token_urlsafe(8), "revision": 0, "names": ordered,
             "selected": selected, "page": 0, "before": current}
    picker_state()[key] = state
    return picker_view(state)
