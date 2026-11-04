import asyncio, logging, os, signal

from dotenv import load_dotenv

load_dotenv()
import core  # noqa: E402  (после load_dotenv, чтобы подхватить настройки)
import ops  # noqa: E402
from max_ import Max  # noqa: E402
from tg import Telegram  # noqa: E402
import max_queue  # noqa: E402

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
    loop = asyncio.get_running_loop()
    try:  # docker stop шлёт SIGTERM: останавливаемся аккуратно, а не по таймауту с SIGKILL
        loop.add_signal_handler(signal.SIGTERM, lambda: (log.info("получен SIGTERM, останавливаюсь"),
                                                         ops.request_restart(0, delay=0)))
        sigterm = True
    except (NotImplementedError, RuntimeError, ValueError):  # Windows, не главный поток
        sigterm = False
    tasks = {asyncio.create_task(supervise(w)) for w in workers}
    waiter = asyncio.create_task(ops.wait_restart())
    tasks.add(waiter)
    try:
        while tasks:
            done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t is waiter:
                    core.SHUTTING_DOWN = True  # новые фото больше не берём, о перезапуске скажем
                    left = await core.drain_inflight(float(core.env("SHUTDOWN_DRAIN_SEC", "30")))
                    if left:
                        log.warning("не успели обработать фото при остановке: %s", left)
                    for other in tasks:
                        other.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise SystemExit(t.result())
                if t.exception():
                    raise t.exception()
    finally:
        if sigterm:
            loop.remove_signal_handler(signal.SIGTERM)
        core.SHUTTING_DOWN = False


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
    for h in logging.getLogger().handlers:  # токены и ключи не должны попадать в docker-логи, даже в трассировках
        h.setFormatter(core.RedactingFormatter(h.formatter._fmt if h.formatter else None))
    if (problem := ops.config_problem()):
        raise SystemExit(problem)
    # без ключа нейросети бот принимает фото и падает на каждом — лучше не стартовать
    if core.PROVIDER == "openai" and not os.getenv("LLM_API_KEY"):
        raise SystemExit("Не задан LLM_API_KEY (LLM_PROVIDER=openai)")
    if (problem := core.llm_config_problem()):
        raise SystemExit(problem)
    if core.PROVIDER == "anthropic" and not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit("Не задан ANTHROPIC_API_KEY (LLM_PROVIDER=anthropic)")
    log.info("Нейросеть: %s / %s", core.PROVIDER, core.MODEL)
    if not os.getenv("MAX_TOKEN"):
        raise SystemExit("Для приёма и согласования нужен MAX_TOKEN")
    if os.getenv("TG_TOKEN"):
        core.ADAPTERS["tg"] = Telegram(need("TG_TOKEN"), need("TG_CHANNEL"))
    core.ADAPTERS["max"] = Max(need("MAX_TOKEN"), need("MAX_CHANNEL"), ids("MAX_APPROVERS"), ids("MAX_EMPLOYEES"),
                                ids("MAX_ADMINS"))
    if ids("MAX_ADMINS"):
        log.info("MAX_ADMINS задан: новые люди добавляются кнопками (заявки)")
    elif not ids("MAX_EMPLOYEES"):
        log.warning("MAX_EMPLOYEES пуст: материалы может прислать кто угодно")
    working = [core.ADAPTERS["max"]]
    ap = os.getenv("APPROVAL_PLATFORM")
    if ap and ap not in core.ADAPTERS:
        raise SystemExit(f"APPROVAL_PLATFORM={ap}, но этот мессенджер не настроен")
    if ap and core.ADAPTERS[ap].publish_only:
        raise SystemExit(f"APPROVAL_PLATFORM={ap}: этот мессенджер работает только на публикацию")
    for a in working:
        if not (core.ADAPTERS[ap] if ap else a).approvers:
            raise SystemExit("Не заданы согласующие (MAX_APPROVERS / MAX_ADMINS)")
    core.startup()
    await core.announce_start()
    await ops.announce_update_result()
    try:
        await serve([*working, max_queue.Worker(core.ADAPTERS["max"]), core.Reminder()])
    finally:
        await core.ADAPTERS["max"].http.aclose()
        if "tg" in core.ADAPTERS:
            await core.ADAPTERS["tg"].bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
