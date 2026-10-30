import sqlite3

import pytest

from shuin.errors import LedgerError
from tests.conftest import SHA_A, open_card


def test_review_requires_tried_and_failed(env):
    open_card(env, head=SHA_A)
    with pytest.raises(LedgerError) as raised:
        env.ledger.request_review(env.actors["worker-1"], "acme/app", 128, None)
    assert raised.value.status == 400
    assert raised.value.code == "tried_failed_required"
    for bad in ("", "   ", "tried\nfailed", "x" * 301):
        with pytest.raises(LedgerError):
            env.ledger.request_review(env.actors["worker-1"], "acme/app", 128, bad)
    assert env.ledger.conn.execute("SELECT COUNT(*) AS n FROM review_requests").fetchone()["n"] == 0


def test_review_accepts_nothing_and_stamps_actor(env):
    open_card(env, head=SHA_A)
    body = env.ledger.request_review(env.actors["worker-1"], "acme/app", 128, "nothing")
    assert body["actor"] == "worker-1"
    row = env.ledger.conn.execute("SELECT * FROM review_requests WHERE id=?", (body["review_request_id"],)).fetchone()
    tried = env.ledger.conn.execute("SELECT * FROM tried_failed WHERE id=?", (row["tried_failed_id"],)).fetchone()
    assert tried["entry"] == "nothing"
    assert tried["agent_id"] == "worker-1"
    run = env.ledger.conn.execute("SELECT * FROM task_runs WHERE task_id=?", (tried["task_id"],)).fetchall()
    assert any(item["status"] == "review_requested" and item["summary"] == "nothing" for item in run)
    event = env.ledger.conn.execute("SELECT * FROM events WHERE type='review.requested'").fetchone()
    assert event["enum_state"] == "open"
    assert "nothing" not in (event["enum_state"] or "")


def test_tried_failed_rejects_a_single_space(env):
    task = open_card(env, head=SHA_A)
    with pytest.raises(LedgerError) as raised:
        env.ledger.request_review(env.actors["worker-1"], "acme/app", 128, " ")
    assert raised.value.code == "empty"
    pr = env.ledger.conn.execute("SELECT id FROM prs").fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        env.ledger.conn.execute(
            """INSERT INTO tried_failed(task_id, pr_id, agent_id, entry, created_at)
               VALUES (?,?,?,?,1)""",
            (task["id"], pr["id"], "worker-1", " "),
        )
    assert env.ledger.conn.execute("SELECT COUNT(*) AS n FROM tried_failed").fetchone()["n"] == 0


def test_schema_rejects_review_without_tried_and_failed(env):
    task = open_card(env, head=SHA_A)
    pr = env.ledger.conn.execute("SELECT id FROM prs").fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        env.ledger.conn.execute(
            """INSERT INTO review_requests(pr_id, requested_by, tried_failed_id, state, created_at)
               VALUES (?,?,NULL,'open',1)""",
            (pr["id"], "worker-1"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        env.ledger.conn.execute(
            """INSERT INTO tried_failed(task_id, pr_id, agent_id, entry, created_at)
               VALUES (?,?,?,?,1)""",
            (task["id"], pr["id"], "worker-1", ""),
        )
    with pytest.raises(sqlite3.IntegrityError):
        env.ledger.conn.execute(
            """INSERT INTO updates(task_id, agent_id, kind, body, label, created_at)
               VALUES (?,?,'note','hello','instruction',1)""",
            (task["id"], "worker-1"),
        )
