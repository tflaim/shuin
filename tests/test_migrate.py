import sqlite3
import threading
from pathlib import Path

import pytest

from shuin.db import LATEST_VERSION, _migrate_update_kinds, _migrate_verifier_state
from shuin.errors import LedgerError
from shuin.ledger import hash_token, open_ledger
from shuin.types import CheckItem, ChecksRead, HeadRead

SHA = "a" * 40


def _legacy_db(path: Path) -> None:
    """A version-1 ledger from before verifier_state replaced mac_state."""
    sql = (Path(__file__).resolve().parents[1] / "shuin" / "schema.sql").read_text(encoding="utf-8")
    old = "verifier_state TEXT CHECK(verifier_state IS NULL OR verifier_state IN('pass','fail','missing'))"
    new = "mac_state TEXT CHECK(mac_state IS NULL OR mac_state IN('pass','fail','missing'))"
    assert old in sql
    sql = sql.replace(old, new)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(sql)
        conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (1, 1)")
        conn.execute(
            """INSERT INTO tasks(id, title, status, created_at, updated_at)
               VALUES ('T-0001', 'Legacy card', 'active', 1, 1)"""
        )
        conn.execute(
            """INSERT INTO prs(id, task_id, repo, number, head_sha, state, base_branch, last_synced_at)
               VALUES (1, 'T-0001', 'acme/app', 7, ?, 'open', 'main', 1)""",
            (SHA,),
        )
        conn.execute(
            """INSERT INTO pr_acceptance(
                 pr_id, task_id, head_sha, head_sha_reread, classification,
                 required_checks_json, mac_state, receipt_json, created_at
               ) VALUES (1, 'T-0001', ?, ?, 'missing', '[]', 'fail', '{}', 1)""",
            (SHA, SHA),
        )
        conn.commit()
    finally:
        conn.close()


class _GitHub:
    def read_head(self, repo, number):
        return HeadRead(SHA, "feat/sheet", "main", "PR_node_7", "open")

    def read_checks(self, repo, base, sha):
        return ChecksRead(checks=(CheckItem("ci", 1, "success", "check_run"),))


def test_existing_ledger_gains_verifier_state_and_seal_inserts(tmp_path):
    path = tmp_path / "ledger.db"
    _legacy_db(path)
    ledger = open_ledger(path)
    try:
        columns = {row["name"] for row in ledger.conn.execute("PRAGMA table_info(pr_acceptance)")}
        assert "verifier_state" in columns
        assert "mac_state" not in columns
        kept = ledger.conn.execute("SELECT verifier_state FROM pr_acceptance").fetchone()
        assert kept["verifier_state"] == "fail"
        versions = {
            row["version"]
            for row in ledger.conn.execute("SELECT version FROM schema_migrations")
        }
        assert 2 in versions
        card = ledger.snapshot()["tasks"][0]
        assert card["receipt"]["verifier_state"] == "fail"

        token = ledger.mint_agent(None, "admin", "human", "owner")
        owner = ledger.authenticate(token)
        lead_token = ledger.mint_agent(owner, "eng-lead", "lead", "lead")
        lead = ledger.authenticate(lead_token)
        body = ledger.seal(lead, "acme/app", 7, _GitHub())
        assert body["verifier_state"] == "missing"
        stored = ledger.conn.execute(
            "SELECT verifier_state FROM pr_acceptance ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert stored["verifier_state"] == "missing"
    finally:
        ledger.close()

    again = open_ledger(path)
    try:
        assert again.conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations").fetchone()["v"] >= 2
        columns = {row["name"] for row in again.conn.execute("PRAGMA table_info(pr_acceptance)")}
        assert "verifier_state" in columns
        assert "mac_state" not in columns
    finally:
        again.close()


def test_existing_ledger_accepts_done_and_tried_failed(tmp_path):
    path = tmp_path / "ledger.db"
    ledger = open_ledger(path, clock=lambda: 10)
    try:
        token = ledger.mint_agent(None, "admin", "human", "owner")
        owner = ledger.authenticate(token)
        worker_token = ledger.mint_agent(owner, "worker-1", "cursor", "worker")
        worker = ledger.authenticate(worker_token)
        task = ledger.add_task(worker, "Card", repo="acme/app")
        ledger.claim_task(worker, task["id"])
        ledger.post_update(worker, task["id"], "note", "before the migration")
    finally:
        ledger.close()

    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        conn.execute(
            """CREATE TABLE updates_old(
                 id INTEGER PRIMARY KEY,
                 task_id TEXT REFERENCES tasks(id),
                 agent_id TEXT NOT NULL REFERENCES agents(id),
                 kind TEXT NOT NULL CHECK(kind IN('progress','question','handoff','note')),
                 body TEXT NOT NULL CHECK(length(body)<=2000),
                 label TEXT NOT NULL DEFAULT 'agent_claim' CHECK(label='agent_claim'),
                 on_behalf_of TEXT,
                 reported_via TEXT CHECK(reported_via IS NULL OR reported_via='lead'),
                 created_at INTEGER NOT NULL
               )"""
        )
        conn.execute(
            """INSERT INTO updates_old(
                 id, task_id, agent_id, kind, body, label, on_behalf_of, reported_via, created_at
               )
               SELECT id, task_id, agent_id, kind, body, label, on_behalf_of, reported_via, created_at
               FROM updates"""
        )
        conn.execute("DROP TABLE updates")
        conn.execute("ALTER TABLE updates_old RENAME TO updates")
        conn.execute("DELETE FROM schema_migrations")
        conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (2, 1)")
        conn.execute("COMMIT")
    finally:
        conn.close()

    ledger = open_ledger(path, clock=lambda: 11)
    try:
        worker = ledger.authenticate(worker_token)
        done = ledger.post_update(worker, task["id"], "done", "shipped")
        tried = ledger.post_update(worker, task["id"], "tried-failed", "the sheet still overlaps")
        assert done["actor"] == tried["actor"] == "worker-1"
        rows = ledger.conn.execute("SELECT kind, body FROM updates ORDER BY id").fetchall()
        assert [(row["kind"], row["body"]) for row in rows] == [
            ("note", "before the migration"),
            ("done", "shipped"),
            ("tried-failed", "the sheet still overlaps"),
        ]
        status = ledger.conn.execute("SELECT status FROM tasks WHERE id=?", (task["id"],)).fetchone()["status"]
        assert status == "in_review"
        tried_rows = ledger.conn.execute(
            "SELECT COUNT(*) AS n FROM tried_failed WHERE task_id=?",
            (task["id"],),
        ).fetchone()["n"]
        assert tried_rows == 1
        versions = {row["version"] for row in ledger.conn.execute("SELECT version FROM schema_migrations")}
        assert versions == {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12}
    finally:
        ledger.close()


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in names}


def test_legacy_waiting_mac_ledger_migrates_with_its_rows(tmp_path):
    """A ledger built from the v0 schema, with rows in it."""
    path = tmp_path / "ledger.db"
    schema = (Path(__file__).resolve().parent / "fixtures" / "schema_v0.sql").read_text(encoding="utf-8")
    assert "waiting_mac" in schema
    assert "mac_state" in schema
    owner_token = "legacy-owner-token"
    worker_token = "legacy-worker-token"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(schema)
        conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (1, 1)")
        conn.execute(
            """INSERT INTO agents(id, kind, role, token_hash, created_at) VALUES
               ('admin', 'human', 'owner', ?, 1),
               ('worker-1', 'cursor', 'worker', ?, 1)""",
            (hash_token(owner_token), hash_token(worker_token)),
        )
        conn.execute(
            """INSERT INTO tasks(id, title, owner_agent, status, blocked_reason, repo, idempotency_key, created_at, updated_at)
               VALUES ('T-0001', 'Parent card', 'worker-1', 'active', NULL, 'acme/app', 'sheet', 1, 1)"""
        )
        conn.execute(
            """INSERT INTO tasks(
                 id, title, owner_agent, status, blocked_reason, depends_on, repo, created_at, updated_at
               ) VALUES ('T-0002', 'Child card', 'worker-1', 'blocked', 'waiting_mac', 'T-0001', 'acme/app', 1, 1)"""
        )
        conn.execute(
            """INSERT INTO tasks(id, title, status, blocked_reason, created_at, updated_at)
               VALUES ('T-0003', 'Needs a person', 'blocked', 'needs_human', 1, 1)"""
        )
        conn.execute(
            """INSERT INTO prs(id, task_id, repo, number, head_sha, state, base_branch, last_synced_at)
               VALUES (1, 'T-0002', 'acme/app', 7, ?, 'open', 'main', 1)""",
            (SHA,),
        )
        conn.execute(
            """INSERT INTO updates(task_id, agent_id, kind, body, created_at)
               VALUES ('T-0001', 'worker-1', 'note', 'still on the old kinds', 1)"""
        )
        conn.execute(
            """INSERT INTO tried_failed(task_id, pr_id, agent_id, entry, created_at)
               VALUES ('T-0002', 1, 'worker-1', 'the sheet still overlaps', 1)"""
        )
        conn.execute(
            """INSERT INTO pr_acceptance(
                 pr_id, task_id, head_sha, classification, required_checks_json, mac_state, receipt_json, created_at
               ) VALUES (1, 'T-0002', ?, 'missing', '[]', 'fail', '{}', 1)""",
            (SHA,),
        )
        conn.execute(
            """INSERT INTO verification_queue(pr_id, commit_sha, requested_by, state, enqueued_at)
               VALUES (1, ?, 'worker-1', 'queued', 1)""",
            (SHA,),
        )
        conn.commit()
        before = _table_counts(conn)
    finally:
        conn.close()

    ledger = open_ledger(path, clock=lambda: 20)
    try:
        after = _table_counts(ledger.conn)
        assert set(after) - {"task_deps", "trust_events", "process_runs"} == set(before)
        for name, count in before.items():
            if name == "schema_migrations":
                assert after[name] == count + (LATEST_VERSION - 1)
            else:
                assert after[name] == count
        assert after["task_deps"] == 1
        assert after["trust_events"] == 0
        assert after["process_runs"] == 0
        copied = ledger.conn.execute(
            "SELECT dep_task_id FROM task_deps WHERE task_id='T-0002'"
        ).fetchone()
        assert copied["dep_task_id"] == "T-0001"
        assert ledger.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert ledger.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        reasons = {
            row["id"]: row["blocked_reason"]
            for row in ledger.conn.execute("SELECT id, blocked_reason FROM tasks ORDER BY id")
        }
        assert reasons == {"T-0001": None, "T-0002": "waiting_verifier", "T-0003": "needs_human"}
        child = ledger.conn.execute("SELECT depends_on, owner_agent FROM tasks WHERE id='T-0002'").fetchone()
        assert child["depends_on"] == "T-0001"
        assert child["owner_agent"] == "worker-1"
        assert ledger.conn.execute("SELECT idempotency_key FROM tasks WHERE id='T-0001'").fetchone()[0] == "sheet"
        acceptance = ledger.conn.execute("SELECT verifier_state FROM pr_acceptance").fetchone()
        assert acceptance["verifier_state"] == "fail"
        task_sql = ledger.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()[0]
        assert "waiting_verifier" in task_sql
        assert "waiting_mac" not in task_sql
        worker = ledger.authenticate(worker_token)
        blocked = ledger.set_blocked(worker, "T-0002", "waiting_verifier")
        assert blocked["blocked_reason"] == "waiting_verifier"
        with pytest.raises(sqlite3.IntegrityError):
            ledger.conn.execute("UPDATE tasks SET blocked_reason='waiting_mac' WHERE id='T-0002'")
        with pytest.raises(sqlite3.IntegrityError):
            ledger.conn.execute(
                """INSERT INTO tasks(id, title, status, idempotency_key, created_at, updated_at)
                   VALUES ('T-0004', 'Duplicate key', 'todo', 'sheet', 2, 2)"""
            )
        assert ledger.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert ledger.conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert _table_counts(ledger.conn)["tasks"] == before["tasks"]
    finally:
        ledger.close()


def test_migrate_rechecks_version_after_taking_the_lock(tmp_path, monkeypatch):
    """A stale version read must not replay upgrades another connection already committed."""
    path = tmp_path / "ledger.db"
    ledger = open_ledger(path)
    ledger.close()
    import shuin.db as db

    real = db._schema_version
    seen = {"n": 0}

    def stale_once(conn):
        seen["n"] += 1
        if seen["n"] == 1:
            return 1
        return real(conn)

    monkeypatch.setattr(db, "_schema_version", stale_once)
    again = open_ledger(path)
    try:
        versions = [
            row["version"]
            for row in again.conn.execute("SELECT version FROM schema_migrations ORDER BY version")
        ]
        assert versions == [LATEST_VERSION]
        assert again.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        again.close()
    assert seen["n"] == 2


def test_newer_ledger_schema_is_refused(tmp_path):
    path = tmp_path / "ledger.db"
    ledger = open_ledger(path)
    ledger.close()
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 1)",
            (LATEST_VERSION + 1,),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(LedgerError) as raised:
        open_ledger(path)
    assert raised.value.message == f"ledger schema {LATEST_VERSION + 1}, code expects {LATEST_VERSION}"


def test_empty_migrations_table_opens_the_ledger(tmp_path):
    path = tmp_path / "ledger.db"
    ledger = open_ledger(path, clock=lambda: 10)
    try:
        token = ledger.mint_agent(None, "admin", "human", "owner")
        owner = ledger.authenticate(token)
        added = ledger.add_task(owner, "Keep this card", repo="acme/app")
        assert added["id"] == "T-0001"
        ledger.conn.execute(
            """INSERT INTO prs(task_id, repo, number, head_sha, head_epoch, state)
               VALUES ('T-0001', 'acme/app', 7, ?, 0, 'open')""",
            (SHA,),
        )
        ledger.conn.commit()
    finally:
        ledger.close()
    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM schema_migrations")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0
    finally:
        conn.close()
    again = open_ledger(path)
    try:
        version = again.conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()["v"]
        assert version == LATEST_VERSION
        assert again.conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
        kept = again.conn.execute("SELECT id, title FROM tasks").fetchone()
        assert kept["id"] == "T-0001"
        assert kept["title"] == "Keep this card"
        epoch = again.conn.execute("SELECT head_epoch FROM prs WHERE number=7").fetchone()["head_epoch"]
        assert epoch == 0
    finally:
        again.close()


def test_blank_file_with_an_empty_migrations_table_opens(tmp_path):
    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()
    ledger = open_ledger(path)
    try:
        version = ledger.conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()["v"]
        assert version == LATEST_VERSION
        assert ledger.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()[0] == 1
    finally:
        ledger.close()


def test_blank_file_install_holds_the_write_lock(tmp_path, monkeypatch):
    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()

    import shuin.db as db

    started = threading.Event()
    release = threading.Event()
    real = db._record_version

    def pause(conn, version):
        if version == LATEST_VERSION and not started.is_set():
            started.set()
            assert release.wait(5)
        real(conn, version)

    monkeypatch.setattr(db, "_record_version", pause)
    errors: list[BaseException] = []

    def opener():
        try:
            ledger = open_ledger(path)
            ledger.close()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=opener)
    worker.start()
    try:
        assert started.wait(5)
        other = sqlite3.connect(path, timeout=0.2)
        try:
            other.execute("PRAGMA busy_timeout=200")
            with pytest.raises(sqlite3.OperationalError) as raised:
                other.execute("BEGIN IMMEDIATE")
            assert "locked" in str(raised.value).lower()
        finally:
            other.close()
    finally:
        release.set()
        worker.join(5)
    assert errors == []
    assert worker.is_alive() is False
    check = sqlite3.connect(path)
    try:
        assert check.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == LATEST_VERSION
        assert check.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()[0] == 1
    finally:
        check.close()


def _load_v1(path: Path) -> sqlite3.Connection:
    schema = (Path(__file__).resolve().parent / "fixtures" / "schema_v0.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(schema)
    return conn


def _versions(conn: sqlite3.Connection) -> list[int]:
    return [row["version"] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def test_real_v1_ledger_infers_version_one(tmp_path):
    path = tmp_path / "ledger.db"
    conn = _load_v1(path)
    conn.commit()
    conn.close()
    ledger = open_ledger(path)
    try:
        assert _versions(ledger.conn) == list(range(1, LATEST_VERSION + 1))
    finally:
        ledger.close()


def test_real_v3_ledger_infers_version_three(tmp_path):
    path = tmp_path / "ledger.db"
    conn = _load_v1(path)
    _migrate_verifier_state(conn)
    _migrate_update_kinds(conn)
    conn.commit()
    conn.close()
    ledger = open_ledger(path)
    try:
        assert _versions(ledger.conn) == list(range(3, LATEST_VERSION + 1))
        columns = {row["name"] for row in ledger.conn.execute("PRAGMA table_info(pr_acceptance)")}
        assert "verifier_state" in columns
        assert "mac_state" not in columns
    finally:
        ledger.close()


def test_missing_waiting_mac_is_not_a_later_schema(tmp_path):
    path = tmp_path / "ledger.db"
    schema = (Path(__file__).resolve().parent / "fixtures" / "schema_v0.sql").read_text(encoding="utf-8")
    schema = schema.replace("'waiting_mac',", "")
    assert "waiting_mac" not in schema
    assert "waiting_verifier" not in schema
    conn = sqlite3.connect(path)
    try:
        conn.executescript(schema)
        conn.commit()
    finally:
        conn.close()
    ledger = open_ledger(path)
    try:
        assert _versions(ledger.conn) == list(range(1, LATEST_VERSION + 1))
    finally:
        ledger.close()


def test_fresh_ledger_is_at_the_latest_version(tmp_path):
    ledger = open_ledger(tmp_path / "ledger.db")
    try:
        version = ledger.conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()["v"]
        assert version == LATEST_VERSION
        columns = {row["name"] for row in ledger.conn.execute("PRAGMA table_info(pr_acceptance)")}
        assert "verifier_state" in columns
        assert "pending_head" in {row["name"] for row in ledger.conn.execute("PRAGMA table_info(prs)")}
        assert "model" in {row["name"] for row in ledger.conn.execute("PRAGMA table_info(task_runs)")}
    finally:
        ledger.close()


def test_pending_head_and_model_migration_is_idempotent(tmp_path):
    path = tmp_path / "ledger.db"
    ledger = open_ledger(path)
    ledger.close()
    conn = sqlite3.connect(path)
    try:
        conn.execute("ALTER TABLE prs DROP COLUMN pending_head")
        conn.execute("ALTER TABLE agents DROP COLUMN model")
        conn.execute("ALTER TABLE task_runs DROP COLUMN model")
        conn.execute("DELETE FROM schema_migrations")
        conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (7, 1)")
        conn.commit()
    finally:
        conn.close()

    ledger = open_ledger(path)
    try:
        for table, column in (("prs", "pending_head"), ("agents", "model"), ("task_runs", "model")):
            names = {row["name"] for row in ledger.conn.execute(f"PRAGMA table_info({table})")}
            assert column in names
        assert ledger.conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()["v"] == LATEST_VERSION
        assert ledger.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        ledger.close()

    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM schema_migrations WHERE version>=8")
        conn.commit()
    finally:
        conn.close()
    again = open_ledger(path)
    try:
        info = again.conn.execute("PRAGMA table_info(prs)").fetchall()
        assert sum(1 for row in info if row["name"] == "pending_head") == 1
        assert again.conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()["v"] == LATEST_VERSION
        assert again.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert again.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        again.close()


def test_task_deps_migration_is_idempotent(tmp_path):
    path = tmp_path / "ledger.db"
    ledger = open_ledger(path, clock=lambda: 5)
    try:
        token = ledger.mint_agent(None, "admin", "human", "owner")
        owner = ledger.authenticate(token)
        parent = ledger.add_task(owner, "Parent", repo="acme/app")
        ledger.add_task(owner, "Child", repo="acme/app", depends_on=parent["id"])
    finally:
        ledger.close()

    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP TABLE task_deps")
        conn.execute("DELETE FROM schema_migrations")
        conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (8, 1)")
        conn.commit()
    finally:
        conn.close()

    ledger = open_ledger(path)
    try:
        rows = ledger.conn.execute(
            "SELECT task_id, dep_task_id FROM task_deps ORDER BY task_id"
        ).fetchall()
        assert [(row["task_id"], row["dep_task_id"]) for row in rows] == [("T-0002", "T-0001")]
        assert ledger.conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()["v"] == LATEST_VERSION
        assert ledger.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        ledger.close()

    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM schema_migrations WHERE version>=9")
        conn.commit()
    finally:
        conn.close()
    again = open_ledger(path)
    try:
        count = again.conn.execute("SELECT COUNT(*) AS n FROM task_deps").fetchone()["n"]
        assert count == 1
        assert again.conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()["v"] == LATEST_VERSION
        assert again.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert again.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        again.close()


def test_partial_v8_schema_is_unknown(tmp_path):
    path = tmp_path / "ledger.db"
    ledger = open_ledger(path)
    ledger.close()
    conn = sqlite3.connect(path)
    try:
        conn.execute("ALTER TABLE agents DROP COLUMN model")
        conn.execute("DELETE FROM schema_migrations")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(LedgerError) as raised:
        open_ledger(path)
    assert raised.value.code == "schema_unknown"
    assert raised.value.message == "Ledger schema is not a known version. Restore schema_migrations from a backup."
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0
    finally:
        conn.close()


def test_unknown_extra_table_is_unknown(tmp_path):
    path = tmp_path / "ledger.db"
    ledger = open_ledger(path)
    ledger.close()
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY)")
        conn.execute("DELETE FROM schema_migrations")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(LedgerError) as raised:
        open_ledger(path)
    assert raised.value.code == "schema_unknown"
    assert raised.value.message == "Ledger schema is not a known version. Restore schema_migrations from a backup."
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 0
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='notes'"
        ).fetchone()[0] == 1
    finally:
        conn.close()
