"""Additive SQLite migrations. Unexpected database errors must stop initialization."""
import sqlite3
from typing import Any, Protocol


class Connection(Protocol):
    row_factory: Any

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor: ...
    def executemany(self, sql: str, parameters: Any, /) -> sqlite3.Cursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


def add_columns(db: Connection, table: str, definitions: tuple[str, ...]) -> None:
    existing = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
    for definition in definitions:
        if definition.split()[0] not in existing:
            db.execute(f'ALTER TABLE "{table}" ADD COLUMN {definition}')


def audit_tables(db: Connection) -> None:
    add_columns(db, "drafts", ("cleaned INTEGER DEFAULT 0", "closed_at REAL DEFAULT 0", "published_at REAL DEFAULT 0"))
    add_columns(db, "drafts", ("publication_receipt TEXT NOT NULL DEFAULT '{}'",))
    add_columns(db, "rules", ("enabled INTEGER NOT NULL DEFAULT 1", "revision INTEGER NOT NULL DEFAULT 0"))
    db.execute("CREATE INDEX IF NOT EXISTS drafts_cleanup ON drafts(cleaned,status,id)")
    db.execute("CREATE INDEX IF NOT EXISTS drafts_pending_order ON drafts(status,created,id)")
    db.execute("CREATE INDEX IF NOT EXISTS drafts_author_pending ON drafts(status,platform,author_id,created,id)")
    db.execute("CREATE INDEX IF NOT EXISTS drafts_publication_date ON drafts(status,published_at)")
    db.execute("CREATE INDEX IF NOT EXISTS people_order ON people(platform,created,uid)")
    db.execute("""CREATE TABLE IF NOT EXISTS command_results (
        event_key TEXT, operation TEXT, result TEXT, created REAL,
        PRIMARY KEY(event_key,operation))""")
    if db.execute("PRAGMA user_version").fetchone()[0] < 1:
        db.execute("PRAGMA user_version=1")
    db.commit()
