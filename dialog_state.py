"""Состояния ожидания ввода: только ID/версия поста или имя поля, без текста постов."""
from collections.abc import MutableMapping, MutableSet
import json
import time

TTL = 7 * 86400


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS dialog_state (
        kind TEXT, platform TEXT, uid TEXT, value TEXT, updated REAL,
        PRIMARY KEY(kind,platform,uid))""")
    db.commit()


def cleanup(db):
    db.execute("DELETE FROM dialog_state WHERE updated<?", (time.time() - TTL,))
    db.commit()


class PendingMap(MutableMapping):
    def __init__(self, db, kind):
        self.db, self.kind = db, kind

    def __getitem__(self, key):
        platform, uid = key
        row = self.db.execute("SELECT value,updated FROM dialog_state WHERE kind=? AND platform=? AND uid=?",
                              (self.kind, platform, str(uid))).fetchone()
        if row is None:
            raise KeyError(key)
        if row["updated"] < time.time() - TTL:
            del self[key]
            raise KeyError(key)
        value = json.loads(row["value"])
        return tuple(value) if isinstance(value, list) else value

    def __setitem__(self, key, value):
        platform, uid = key
        self.db.execute("INSERT OR REPLACE INTO dialog_state(kind,platform,uid,value,updated) VALUES(?,?,?,?,?)",
                        (self.kind, platform, str(uid), json.dumps(value), time.time()))
        self.db.commit()

    def __delitem__(self, key):
        platform, uid = key
        count = self.db.execute("DELETE FROM dialog_state WHERE kind=? AND platform=? AND uid=?",
                                (self.kind, platform, str(uid))).rowcount
        self.db.commit()
        if not count:
            raise KeyError(key)

    def __iter__(self):
        cleanup(self.db)
        return iter([(r["platform"], r["uid"]) for r in self.db.execute(
            "SELECT platform,uid FROM dialog_state WHERE kind=?", (self.kind,)).fetchall()])

    def __len__(self):
        cleanup(self.db)
        return self.db.execute("SELECT COUNT(*) FROM dialog_state WHERE kind=?", (self.kind,)).fetchone()[0]

    def clear(self):
        self.db.execute("DELETE FROM dialog_state WHERE kind=?", (self.kind,))
        self.db.commit()


class PendingSet(MutableSet):
    def __init__(self, db, kind="rule"):
        self.values = PendingMap(db, kind)

    def __contains__(self, key): return key in self.values
    def __iter__(self): return iter(self.values)
    def __len__(self): return len(self.values)
    def add(self, key): self.values[key] = True
    def discard(self, key): self.values.pop(key, None)
    def clear(self): self.values.clear()
