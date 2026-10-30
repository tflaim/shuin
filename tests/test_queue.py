import json

import pytest

from shuin.cli import main
from shuin.errors import LedgerError
from shuin.httpapi import Api
from shuin.mcp import call_tool
from tests.conftest import SHA_A, open_card, snap


def _enqueue(env):
    return env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)


def _state(env, queue_id):
    return env.ledger.conn.execute(
        "SELECT state FROM verification_queue WHERE id=?",
        (queue_id,),
    ).fetchone()["state"]


def test_lead_and_owner_can_cancel_or_drop_a_row(env, capsys):
    open_card(env, head=SHA_A)
    queued = _enqueue(env)
    before = env.ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE type='verification_slot.freed'"
    ).fetchone()["n"]
    body = env.ledger.cancel_queue(env.actors["eng-lead"], queued["queue_id"])
    assert body == {"queue_id": queued["queue_id"], "state": "cancelled", "reason": "cancelled"}
    assert _state(env, queued["queue_id"]) == "cancelled"
    assert env.ledger.queue() == []
    after = env.ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE type='verification_slot.freed'"
    ).fetchone()["n"]
    assert after == before
    cancelled = env.ledger.conn.execute(
        """SELECT actor, enum_state FROM events
           WHERE type='verification_queue.cancelled' AND queue_id=?""",
        (queued["queue_id"],),
    ).fetchone()
    assert cancelled["actor"] == "eng-lead"
    assert cancelled["enum_state"] == "cancelled"
    status = env.ledger.conn.execute("SELECT status FROM tasks WHERE repo='acme/app'").fetchone()["status"]
    assert status == "active"
    with pytest.raises(LedgerError) as closed:
        env.ledger.drop_queue(env.actors["admin"], queued["queue_id"])
    assert closed.value.status == 409
    assert closed.value.code == "closed"

    running = _enqueue(env)
    slot = env.ledger.claim_slot(env.actors["verifier-1"])
    assert slot["queue_id"] == running["queue_id"]
    with pytest.raises(LedgerError) as worker:
        env.ledger.cancel_queue(env.actors["worker-1"], running["queue_id"])
    assert worker.value.status == 403
    with pytest.raises(LedgerError) as verifier:
        env.ledger.drop_queue(env.actors["verifier-1"], running["queue_id"])
    assert verifier.value.status == 403
    dropped = env.ledger.drop_queue(env.actors["admin"], running["queue_id"])
    assert dropped["state"] == "cancelled"
    assert _state(env, running["queue_id"]) == "cancelled"
    event = env.ledger.conn.execute(
        "SELECT actor, enum_state FROM events WHERE type='verification_slot.freed' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert event["actor"] == "admin"
    assert event["enum_state"] == "cancelled"
    dropped_event = env.ledger.conn.execute(
        """SELECT actor, enum_state FROM events
           WHERE type='verification_queue.cancelled' AND queue_id=?""",
        (running["queue_id"],),
    ).fetchone()
    assert dropped_event["actor"] == "admin"
    assert dropped_event["enum_state"] == "cancelled"
    with pytest.raises(LedgerError) as missing:
        env.ledger.cancel_queue(env.actors["eng-lead"], 999)
    assert missing.value.status == 404

    code = main(
        [
            "--db",
            str(env.db),
            "--token",
            env.tokens["eng-lead"],
            "--json",
            "queue",
            "cancel",
            "--id",
            "1",
        ]
    )
    assert code == 1
    assert json.loads(capsys.readouterr().out)["error"] == "closed"

    again = _enqueue(env)
    code = main(
        [
            "--db",
            str(env.db),
            "--token",
            env.tokens["admin"],
            "--json",
            "queue",
            "drop",
            "--id",
            str(again["queue_id"]),
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["state"] == "cancelled"
    code = main(
        [
            "--db",
            str(env.db),
            "--token",
            env.tokens["worker-1"],
            "--json",
            "queue",
            "cancel",
            "--id",
            str(again["queue_id"]),
        ]
    )
    assert code == 1
    assert json.loads(capsys.readouterr().out)["error"] == "role_mismatch"

    fresh = _enqueue(env)
    api = Api(env.db)
    status, body, _ctype = api.handle(
        "POST",
        f"/v1/verification_queue/{fresh['queue_id']}/cancel",
        {"Authorization": f"Bearer {env.tokens['eng-lead']}"},
        b'{"reason":"stale head"}',
    )
    assert status == 200
    assert b'"cancelled"' in body
    http_event = env.ledger.conn.execute(
        """SELECT actor, enum_state FROM events
           WHERE type='verification_queue.cancelled' AND queue_id=?""",
        (fresh["queue_id"],),
    ).fetchone()
    assert http_event["actor"] == "eng-lead"
    assert http_event["enum_state"] == "stale head"
    status, body, _ctype = api.handle(
        "POST",
        f"/v1/verification_queue/{fresh['queue_id']}/drop",
        {"Authorization": f"Bearer {env.tokens['worker-1']}"},
        b"{}",
    )
    assert status == 403

    fresh = _enqueue(env)
    text, is_error = call_tool(
        "drop_queue",
        {"queue_id": fresh["queue_id"]},
        db_path=env.db,
        token=env.tokens["eng-lead"],
    )
    assert is_error is False
    assert "cancelled" in text
    help_code = main(["queue", "cancel", "--help"])
    assert help_code == 0
    help_text = capsys.readouterr().out
    assert "Lead or owner" in help_text
    assert "--reason" in help_text

    reasoned = _enqueue(env)
    code = main(
        [
            "--db",
            str(env.db),
            "--token",
            env.tokens["eng-lead"],
            "--json",
            "queue",
            "cancel",
            "--id",
            str(reasoned["queue_id"]),
            "--reason",
            "duplicate",
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "duplicate"
    reason_event = env.ledger.conn.execute(
        """SELECT actor, enum_state FROM events
           WHERE type='verification_queue.cancelled' AND queue_id=?""",
        (reasoned["queue_id"],),
    ).fetchone()
    assert reason_event["actor"] == "eng-lead"
    assert reason_event["enum_state"] == "duplicate"


def test_merged_pr_cancels_open_queue_rows(env):
    task = open_card(env, head=SHA_A)
    queued = _enqueue(env)
    env.ledger.apply_github_snapshot(snap(head=SHA_A, state="closed"))
    assert _state(env, queued["queue_id"]) == "queued"

    running = env.ledger.claim_slot(env.actors["verifier-1"])
    env.ledger.apply_github_snapshot(snap(head=SHA_A, state="merged"))
    assert _state(env, running["queue_id"]) == "cancelled"
    assert env.ledger.queue() == []
    event = env.ledger.conn.execute(
        "SELECT actor, enum_state FROM events WHERE type='verification_slot.freed' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert event["enum_state"] == "cancelled"
    assert event["actor"] == "system:merge"
    auto = env.ledger.conn.execute(
        """SELECT actor, enum_state FROM events
           WHERE type='verification_queue.cancelled' AND queue_id=?""",
        (running["queue_id"],),
    ).fetchone()
    assert auto["actor"] == "system:merge"
    assert auto["enum_state"] == "merged"
    status = env.ledger.conn.execute(
        "SELECT status, closed_by FROM tasks WHERE id=?",
        (task["id"],),
    ).fetchone()
    assert status["status"] == "done"
    assert status["closed_by"] == "merge"

    # A second poll of the merged PR does not revive the row.
    env.ledger.apply_github_snapshot(snap(head=SHA_A, state="merged"))
    assert _state(env, running["queue_id"]) == "cancelled"

    other = env.ledger.add_task(env.actors["eng-lead"], "Still open", repo="acme/app")
    env.ledger.claim_task(env.actors["eng-lead"], other["id"], assignee="worker-1")
    env.ledger.link_pr(env.actors["worker-1"], other["id"], "acme/app", 129, node_id="PR_node_129")
    env.ledger.apply_github_snapshot(snap(number=129, node="PR_node_129", head=SHA_A, state="open"))
    live = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 129, SHA_A)
    env.ledger.conn.execute("UPDATE prs SET state='merged' WHERE number=129")
    listed = env.ledger.queue()
    assert [item["queue_id"] for item in listed] == [live["queue_id"]]
    assert _state(env, live["queue_id"]) == "queued"
    assert env.ledger.queue(maintain=True) == []
    assert _state(env, live["queue_id"]) == "cancelled"
    assert _state(env, running["queue_id"]) == "cancelled"
    live_event = env.ledger.conn.execute(
        """SELECT actor, enum_state FROM events
           WHERE type='verification_queue.cancelled' AND queue_id=?""",
        (live["queue_id"],),
    ).fetchone()
    assert live_event["actor"] == "system:merge"
    assert live_event["enum_state"] == "merged"


def test_tokenless_queue_read_does_not_write(env, capsys):
    open_card(env, head=SHA_A)
    running = _enqueue(env)
    claimed = env.ledger.claim_slot(env.actors["verifier-1"])
    env.ledger.conn.execute(
        "UPDATE verification_queue SET lease_expires_at=? WHERE id=?",
        (1, claimed["queue_id"]),
    )
    other = env.ledger.add_task(env.actors["eng-lead"], "Merged neighbor", repo="acme/app")
    env.ledger.claim_task(env.actors["eng-lead"], other["id"], assignee="worker-1")
    env.ledger.link_pr(env.actors["worker-1"], other["id"], "acme/app", 129, node_id="PR_node_129")
    env.ledger.apply_github_snapshot(snap(number=129, node="PR_node_129", head=SHA_A, state="open"))
    queued = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 129, SHA_A)
    env.ledger.conn.execute("UPDATE prs SET state='merged' WHERE number=129")
    # enqueue does not cancel. The merged row is still queued, and the lease is already past.
    assert _state(env, running["queue_id"]) == "running"
    assert _state(env, queued["queue_id"]) == "queued"

    before = env.ledger.conn.execute("PRAGMA data_version").fetchone()[0]
    code = main(["--db", str(env.db), "queue"])
    assert code == 0
    assert "129" in capsys.readouterr().out
    after = env.ledger.conn.execute("PRAGMA data_version").fetchone()[0]
    assert after == before
    assert _state(env, running["queue_id"]) == "running"
    assert _state(env, queued["queue_id"]) == "queued"
    assert (
        env.ledger.conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE type='verification_queue.cancelled'"
        ).fetchone()["n"]
        == 0
    )

    code = main(
        [
            "--db",
            str(env.db),
            "--token",
            env.tokens["eng-lead"],
            "--json",
            "queue",
        ]
    )
    assert code == 0
    assert _state(env, running["queue_id"]) == "expired"
    assert _state(env, queued["queue_id"]) == "cancelled"
    kept = env.ledger.conn.execute(
        """SELECT actor, enum_state FROM events
           WHERE type='verification_queue.cancelled' AND queue_id=?""",
        (queued["queue_id"],),
    ).fetchone()
    assert kept["actor"] == "system:merge"
    assert kept["enum_state"] == "merged"
