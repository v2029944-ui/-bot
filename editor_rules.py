"""Bounded active editorial rules for generation and learning."""
import json
import time

import command_context

ACTIVE_COUNT = 30
ACTIVE_CHARS = 12000
RULE_CHARS = 1000
STALE = "Эта кнопка устарела. Используйте обновлённый список правил."


def table(common: bool) -> str:
    return "common_rules" if common else "rules"


def _database(common: bool):
    import core
    import spaces

    return spaces.root().db if common and spaces.INSTALLED else core.db


def _mode_key(common: bool) -> str:
    return "common_editor_rules_mode" if common else "editor_rules_mode"


def mode(common: bool = False) -> tuple[bool, int]:
    row = _database(common).execute("SELECT value FROM settings WHERE key=?", (_mode_key(common),)).fetchone()
    if row is None:
        return True, 0
    enabled, revision = json.loads(row[0])
    return enabled, revision


def set_mode(enabled: bool, revision: int, common: bool = False) -> str:
    import core

    def save():
        current, latest = mode(common)
        if revision != latest or current == enabled:
            return STALE
        core.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                        (_mode_key(common), json.dumps([enabled, revision + 1])))
        return "Правила стиля включены." if enabled else "Правила стиля отключены."

    return command_context.mutate("editor_rules_mode", save)


def set_rule(rule_id: int, enabled: bool, revision: int, common: bool = False) -> str:
    import core

    def save():
        changed = core.db.execute(
            f"UPDATE {table(common)} SET enabled=?, revision=revision+1 WHERE id=? AND revision=? AND enabled!=?",
            (enabled, rule_id, revision, enabled),
        ).rowcount
        if not changed:
            return STALE
        return "Правило включено." if enabled else "Правило отключено."

    return command_context.mutate("editor_rule_state", save)


def active(common: bool = False) -> list[str]:
    if not mode(common)[0]:
        return []
    rows = _database(common).execute(
        f"SELECT text FROM {table(common)} WHERE enabled=1 ORDER BY id DESC LIMIT ?", (ACTIVE_COUNT,),
    ).fetchall()
    result = []
    remaining = ACTIVE_CHARS
    for row in rows:
        text = row[0][:RULE_CHARS]
        if len(text) > remaining:
            break
        result.append(text)
        remaining -= len(text)
    return result[::-1]


def add(text: str, operation: str = "rule_add", *, common: bool = False) -> str:
    import core
    text = text.strip()
    if not text or len(text.encode("utf-16-le")) // 2 > RULE_CHARS:
        return "Пришлите правило от 1 до 1000 знаков."
    def insert():
        if core.db.execute(f"SELECT 1 FROM {table(common)} WHERE text=?", (text,)).fetchone():
            return "Это правило уже сохранено."
        core.db.execute(f"INSERT INTO {table(common)}(text,created) VALUES(?,?)", (text, time.time()))
        return "✅ Правило добавлено."
    return command_context.mutate(operation, insert)
