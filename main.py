import asyncio, logging, os

from dotenv import load_dotenv

load_dotenv()
import core  # noqa: E402  (после load_dotenv, чтобы подхватить настройки)
import ops  # noqa: E402
from max_ import Max  # noqa: E402
from tg import Telegram  # noqa: E402

log = logging.getLogger("main")


def ids(name):
    """Список числовых id из настройки. @username не подходит: бот не сможет написать такому человеку."""
    out = [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]
    bad = [x for x in out if not x.lstrip("-").isdigit()]
    if bad:
        raise SystemExit(f"{name}: нужны числовые id, а не «{bad[0]}». Свой id покажет бот @userinfobot "
                         "(для MAX — id из события или профиля).")
    return out


def need(name):
    v = os.getenv(name, "").strip()
    if not v:
        raise SystemExit(f"Не задано {name} в настройках")
    return v


async def serve(workers):
    """Запускает мессенджеры и фоновые задачи. Админ нажал «Обновить» -> останавливаем всё и выходим с кодом 42:
    его понимает entrypoint.sh (запускает updater.py и поднимает бота заново)."""
    tasks = {asyncio.create_task(supervise(w)) for w in workers}
    waiter = asyncio.create_task(ops.wait_restart())
    tasks.add(waiter)
    while tasks:
        done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            if t is waiter:
                for other in tasks:
                    other.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise SystemExit(t.result())
            if t.exception():
                raise t.exception()


async def supervise(adapter):
    """Если один мессенджер упал (например, сбой сети), перезапускаем только его, второй продолжает работать."""
    delay = 5
    while True:
        try:
            await adapter.run()
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Адаптер %s остановился, перезапуск через %s с", adapter.name, delay)
            try:  # администратору: бот споткнулся (если сеть лежит, сообщение может не дойти)
                await core.alert_staff(adapter, f"⚠️ {adapter.name}: сбой ({type(e).__name__}), "
                                                f"перезапуск через {delay} с")
            except Exception:
                log.warning("не удалось сообщить о сбое", exc_info=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # без ключа нейросети бот принимает фото и падает на каждом — лучше не стартовать
    if core.PROVIDER == "openai" and not os.getenv("LLM_API_KEY"):
        raise SystemExit("Не задан LLM_API_KEY (LLM_PROVIDER=openai)")
    if (problem := core.llm_config_problem()):
        raise SystemExit(problem)
    if core.PROVIDER == "anthropic" and not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit("Не задан ANTHROPIC_API_KEY (LLM_PROVIDER=anthropic)")
    log.info("Нейросеть: %s / %s", core.PROVIDER, core.MODEL)
    if os.getenv("TG_TOKEN"):
        core.ADAPTERS["tg"] = Telegram(need("TG_TOKEN"), need("TG_CHANNEL"), ids("TG_APPROVERS"), ids("TG_EMPLOYEES"),
                                     ids("TG_ADMINS"))
        if ids("TG_ADMINS"):
            log.info("TG_ADMINS задан: новые люди добавляются кнопками (заявки и приглашения)")
        elif not ids("TG_EMPLOYEES"):
            log.warning("TG_EMPLOYEES пуст: фото может прислать кто угодно (защищает только лимит фото в час)")
    if os.getenv("MAX_TOKEN"):
        core.ADAPTERS["max"] = Max(need("MAX_TOKEN"), need("MAX_CHANNEL"), ids("MAX_APPROVERS"), ids("MAX_EMPLOYEES"),
                                    ids("MAX_ADMINS"))
        if ids("MAX_ADMINS"):
            log.info("MAX_ADMINS задан: новые люди добавляются кнопками (заявки)")
        elif not ids("MAX_EMPLOYEES"):
            log.warning("MAX_EMPLOYEES пуст: фото может прислать кто угодно (защищает только лимит фото в час)")
    if not core.ADAPTERS:
        raise SystemExit("Задайте TG_TOKEN и/или MAX_TOKEN в .env")
    ap = os.getenv("APPROVAL_PLATFORM")
    if ap and ap not in core.ADAPTERS:
        raise SystemExit(f"APPROVAL_PLATFORM={ap}, но этот мессенджер не настроен")
    for a in core.ADAPTERS.values():
        if not (core.ADAPTERS[ap] if ap else a).approvers:
            raise SystemExit("Не заданы согласующие (TG_APPROVERS / MAX_APPROVERS)")
    core.startup()
    await core.announce_start()
    await ops.announce_update_result()
    await serve([*core.ADAPTERS.values(), core.Reminder()])


if __name__ == "__main__":
    asyncio.run(main())