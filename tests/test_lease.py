from shuin.errors import LedgerError
from tests.conftest import SHA_A, open_card, run_log
import pytest


def _enqueue(env, sha=SHA_A, number=128):
    return env.ledger.enqueue(env.actors["worker-1"], "acme/app", number, sha)


def test_fifo_and_single_slot(env):
    open_card(env, head=SHA_A)
    env.ledger.add_task(env.actors["eng-lead"], "Second sheet", repo="acme/app")
    second = env.ledger.conn.execute("SELECT id FROM tasks WHERE title='Second sheet'").fetchone()["id"]
    env.ledger.claim_task(env.actors["eng-lead"], second, assignee="worker-1")
    env.ledger.link_pr(env.actors["worker-1"], second, "acme/app", 129, node_id="PR_node_129")
    from tests.conftest import snap

    env.ledger.apply_github_snapshot(snap(number=129, node="PR_node_129", head=SHA_A))
    first = _enqueue(env)
    second_item = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 129, SHA_A)
    assert first["position"] == 1
    assert second_item["position"] == 2
    claimed = env.ledger.claim_slot(env.actors["verifier-1"])
    assert claimed["pr"] == 128
    assert claimed["lease_expires_at"] == env.clock["t"] + 30 * 60
    with pytest.raises(LedgerError) as held:
        env.ledger.claim_slot(env.actors["verifier-1"])
    assert held.value.status == 409
    assert held.value.code == "slot_held"
    running = env.ledger.conn.execute("SELECT COUNT(*) AS n FROM verification_queue WHERE state='running'").fetchone()["n"]
    assert running == 1


def test_lease_expiry_frees_the_verifier(env):
    open_card(env, head=SHA_A)
    task = env.ledger.add_task(env.actors["eng-lead"], "Second sheet", repo="acme/app")
    env.ledger.claim_task(env.actors["eng-lead"], task["id"], assignee="worker-1")
    env.ledger.link_pr(env.actors["worker-1"], task["id"], "acme/app", 129, node_id="PR_node_129")
    from tests.conftest import snap

    env.ledger.apply_github_snapshot(snap(number=129, node="PR_node_129", head=SHA_A))
    _enqueue(env)
    env.ledger.enqueue(env.actors["worker-1"], "acme/app", 129, SHA_A)
    first = env.ledger.claim_slot(env.actors["verifier-1"])
    env.clock["t"] = first["lease_expires_at"] - 1
    renewed = env.ledger.heartbeat(env.actors["verifier-1"], first["queue_id"])
    assert renewed["lease_expires_at"] == env.clock["t"] + 30 * 60
    env.clock["t"] = renewed["lease_expires_at"]
    nxt = env.ledger.claim_slot(env.actors["verifier-1"])
    assert nxt["pr"] == 129
    expired = env.ledger.conn.execute(
        "SELECT state FROM verification_queue WHERE id=?",
        (first["queue_id"],),
    ).fetchone()["state"]
    assert expired == "expired"
    event = env.ledger.conn.execute(
        "SELECT enum_state FROM events WHERE type='verification_slot.freed' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert event["enum_state"] == "expired"
    with pytest.raises(LedgerError) as denied:
        env.ledger.heartbeat(env.actors["verifier-1"], first["queue_id"])
    assert denied.value.message == "Lease expired."


def test_worker_cannot_claim_the_slot(env):
    open_card(env, head=SHA_A)
    _enqueue(env)
    with pytest.raises(LedgerError) as raised:
        env.ledger.claim_slot(env.actors["worker-1"])
    assert raised.value.status == 403


def test_stamp_voice(env):
    open_card(env, head=SHA_A)
    item = _enqueue(env)
    env.ledger.claim_slot(env.actors["verifier-1"])
    run_log(env)
    stamped = env.ledger.stamp(env.actors["verifier-1"], item["queue_id"])
    assert stamped["voice"] == "Stamped. Not sealed yet: shuin seal --pr acme/app#128"
    row = env.ledger.conn.execute("SELECT state FROM verification_queue WHERE id=?", (item["queue_id"],)).fetchone()
    assert row["state"] == "passed"
