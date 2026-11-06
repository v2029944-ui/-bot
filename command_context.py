"""Exactly-once local mutations during replay of a durable MAX command."""
import json
import time
from collections.abc import Callable
from contextvars import ContextVar

EVENT: ContextVar[str | None] = ContextVar("command_event", default=None)


def mutate(operation: str, action: Callable[[], str]) -> str:
    import core
    key = EVENT.get()
    if key:
        row = core.db.execute("SELECT result FROM command_results WHERE event_key=? AND operation=?", (key, operation)).fetchone()
        if row:
            return json.loads(row[0])
    core.db.execute("SAVEPOINT command_mutation")
    try:
        result = action()  # synchronous, no commits: result and mutation share one transaction
        if key:
            core.db.execute("INSERT INTO command_results VALUES(?,?,?,?)", (key, operation, json.dumps(result), time.time()))
        core.db.execute("RELEASE command_mutation")
        core.db.commit()
    except BaseException:
        core.db.rollback()
        raise
    return result
