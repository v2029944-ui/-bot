import asyncio

import core
import max_actions
from conftest import run
from test_background_actions import event


def test_large_emoji_rules_list_fits_both_platform_limits(tg):
    for n in range(50):
        core.db.execute("INSERT INTO rules(text,created) VALUES(?,0)", ("😀" * 100 + str(n),))
    core.db.commit()
    for text in (core.rules_view()[0], core.rules_command(tg, "1", "/rules")):
        assert len(text.encode("utf-16-le")) // 2 <= 3900


def test_background_failure_is_reported_and_not_stuck_processing():
    async def scenario():
        notices = []

        class Adapter:
            async def handle(self, update):
                raise RuntimeError("Unexpected failure")

            async def notify(self, uid, text):
                notices.append((uid, text))

        max_actions.enqueue(event(1, "failure"))
        worker = max_actions.Worker(Adapter())
        worker.schedule()
        await asyncio.gather(*worker.active.values())
        row = core.db.execute("SELECT state,payload FROM max_actions").fetchone()
        assert row["state"] == "failed" and row["payload"] == ""
        assert notices and notices[0][0] == "1"
    run(scenario())


def test_backlog_from_one_user_does_not_hide_other_users():
    async def scenario():
        calls = []

        class Adapter:
            async def handle(self, update):
                calls.append(update["callback"]["user"]["user_id"])

        for n in range(100):
            max_actions.enqueue(event(1, f"user1-{n}"))
        max_actions.enqueue(event(2, "user2"))
        worker = max_actions.Worker(Adapter())
        worker.schedule()
        await asyncio.gather(*worker.active.values())
        assert calls == [1, 2]
    run(scenario())
