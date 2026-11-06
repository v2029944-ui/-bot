"""Bounded active editorial rules for generation and learning."""
import time

import command_context

ACTIVE_COUNT = 30
ACTIVE_CHARS = 12000
RULE_CHARS = 1000


def active() -> list[str]:
    import core
    rows = core.db.execute("SELECT text FROM rules ORDER BY id DESC LIMIT ?", (ACTIVE_COUNT,)).fetchall()
    result = []
    remaining = ACTIVE_CHARS
    for row in rows:
        text = row[0][:RULE_CHARS]
        if len(text) > remaining:
            break
        result.append(text)
        remaining -= len(text)
    return result[::-1]


def add(text: str, operation: str = "rule_add") -> str:
    import core
    text = text.strip()
    if not text or len(text.encode("utf-16-le")) // 2 > RULE_CHARS:
        return "Пришлите правило от 1 до 1000 знаков."
    def insert():
        if core.db.execute("SELECT 1 FROM rules WHERE text=?", (text,)).fetchone():
            return "Это правило уже сохранено."
        core.db.execute("INSERT INTO rules(text,created) VALUES(?,?)", (text, time.time()))
        return "✅ Правило добавлено."
    return command_context.mutate(operation, insert)
