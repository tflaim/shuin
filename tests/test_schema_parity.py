"""The schema a new ledger gets is the schema migrations produce."""

import re
import sqlite3
from pathlib import Path

from shuin.ledger import open_ledger

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "schema_v0.sql"


def _norm(sql: str | None) -> str | None:
    if sql is None:
        return None
    return " ".join(sql.split())


def _canon_check(text: str) -> str:
    text = " ".join(text.split())
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r",\s+", ",", text)
    return text


def _checks(sql: str | None) -> tuple[str, ...]:
    text = _norm(sql) or ""
    found = []
    needle = "CHECK("
    start = 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            break
        depth = 0
        for end in range(index + len("CHECK"), len(text)):
            char = text[end]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    found.append(_canon_check(text[index : end + 1]))
                    start = end + 1
                    break
        else:
            break
    return tuple(sorted(found))


def _where(sql: str | None) -> str | None:
    text = _norm(sql)
    if not text or " WHERE " not in text:
        return None
    return text.split(" WHERE ", 1)[1]


def shape(path: Path) -> dict:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        tables = [
            row["name"]
            for row in conn.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table' AND name NOT LIKE 'sqlite_%'
                   ORDER BY name"""
            )
        ]
        body = {}
        for table in tables:
            columns = tuple(
                sorted(
                    (
                        row["name"],
                        row["type"],
                        row["notnull"],
                        row["dflt_value"],
                        row["pk"],
                    )
                    for row in conn.execute(f"PRAGMA table_info({table})")
                )
            )
            foreign_keys = tuple(
                sorted(
                    (row["table"], row["from"], row["to"], row["on_update"], row["on_delete"])
                    for row in conn.execute(f"PRAGMA foreign_key_list({table})")
                )
            )
            indexes = []
            for row in conn.execute(f"PRAGMA index_list({table})"):
                cols = tuple(
                    item["name"] for item in conn.execute(f"PRAGMA index_info({row['name']})")
                )
                sql = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name=?",
                    (row["name"],),
                ).fetchone()
                indexes.append((row["name"], row["unique"], cols, _where(sql["sql"] if sql else None)))
            create = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            body[table] = {
                "columns": columns,
                "foreign_keys": foreign_keys,
                "indexes": tuple(sorted(indexes)),
                "checks": _checks(create["sql"] if create else None),
            }
        views = tuple(
            (row["name"], _norm(row["sql"]))
            for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='view' ORDER BY name"
            )
        )
        return {"tables": body, "views": views}
    finally:
        conn.close()


def test_migrated_schema_matches_the_schema_the_code_expects(tmp_path):
    expected = tmp_path / "expected.db"
    migrated = tmp_path / "migrated.db"
    fresh = open_ledger(expected)
    fresh.close()
    conn = sqlite3.connect(migrated)
    try:
        conn.executescript(FIXTURE.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (1, 1)")
        conn.commit()
    finally:
        conn.close()
    opened = open_ledger(migrated)
    opened.close()
    assert shape(migrated) == shape(expected)


def test_v10_ledger_gains_empty_trust_events(tmp_path):
    path = tmp_path / "v10.db"
    ledger = open_ledger(path)
    ledger.close()
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP TABLE IF EXISTS trust_events")
        conn.execute("DELETE FROM schema_migrations")
        conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (10, 1)")
        conn.commit()
    finally:
        conn.close()

    upgraded = open_ledger(path)
    try:
        assert upgraded.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 11
        assert upgraded.conn.execute("SELECT * FROM trust_events").fetchall() == []
    finally:
        upgraded.close()

    fresh = tmp_path / "fresh.db"
    ledger = open_ledger(fresh)
    ledger.close()
    assert shape(path) == shape(fresh)
