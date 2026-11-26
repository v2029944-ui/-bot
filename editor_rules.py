"""Bounded active editorial rules for generation and learning."""
import json
import time

import command_context

ACTIVE_COUNT = 30
ACTIVE_CHARS = 12000
RULE_CHARS = 1000
STALE = "Эта кнопка устарела. Используйте обновлённый список правил."


def mode() -> tuple[bool, int]:
    import core

    row = core.db.execute("SELECT value FROM settings WHERE key='editor_rules_mode'").fetchone()
    if row is None:
        return True, 0
    enabled, revision = json.loads(row[0])
    return enabled, revision


def set_mode(enabled: bool, revision: int) -> str:
    import core

    def save():
        current, latest = mode()
        if revision != latest or current == enabled:
            return STALE
        core.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('editor_rules_mode',?)",
                        (json.dumps([enabled, revision + 1]),))
        return "Правила стиля включены." if enabled else "Правила стиля отключены."

    return command_context.mutate("editor_rules_mode", save)


def set_rule(rule_id: int, enabled: bool, revision: int) -> str:
    import core

    def save():
        changed = core.db.execute(
            "UPDATE rules SET enabled=?, revision=revision+1 WHERE id=? AND revision=? AND enabled!=?",
            (enabled, rule_id, revision, enabled),
        ).rowcount
        if not changed:
            return STALE
        return "Правило включено." if enabled else "Правило отключено."

    return command_context.mutate("editor_rule_state", save)


def active() -> list[str]:
    import core
    if not mode()[0]:
        return []
    rows = core.db.execute("SELECT text FROM rules WHERE enabled=1 ORDER BY id DESC LIMIT ?", (ACTIVE_COUNT,)).fetchall()
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
