import os
import threading
from pathlib import Path

import pytest

from shuin.errors import LedgerError
from shuin.ledger import open_ledger
from tests.conftest import SHA_A, SHA_B, open_card, snap


def test_enqueue_wakes_a_waiting_verifier(env):
    open_card(env, head=SHA_A)
    armed = threading.Event()
    found = {}

    def wait_for_row():
        waiter = open_ledger(env.db, clock=lambda: env.clock["t"])
        try:
            actor = waiter.authenticate(env.tokens["verifier-1"])
            found["slot"] = waiter.claim_slot(actor, wait=True, timeout=5, armed=armed)
        finally:
            waiter.close()

    thread = threading.Thread(target=wait_for_row)
    thread.start()
    assert armed.wait(2)
    queued = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    thread.join(5)
    assert not thread.is_alive()
    assert found["slot"]["queue_id"] == queued["queue_id"]
    assert found["slot"]["state"] == "running"
    event = env.ledger.conn.execute(
        "SELECT type, enum_state, queue_id, actor FROM events WHERE type='verification.queued'"
    ).fetchone()
    assert event["enum_state"] == "queued"
    assert event["queue_id"] == queued["queue_id"]
    assert event["actor"] == "worker-1"


def test_duplicate_enqueue_does_not_wake_again(env):
    open_card(env, head=SHA_A)
    env.ledger.notify_me(
        env.actors["verifier-1"],
        "verification.queued",
        "webhook",
        channel_target="http://1.1.1.1/hook",
    )
    first = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    again = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    assert again["queue_id"] == first["queue_id"]
    queued = env.ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE type='verification.queued'"
    ).fetchone()["n"]
    deliveries = env.ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM deliveries"
    ).fetchone()["n"]
    assert queued == 1
    assert deliveries == 2
    followups = env.ledger.conn.execute("SELECT COUNT(*) AS n FROM lead_outbox").fetchone()["n"]
    assert followups == 1


def test_matching_evidence_is_never_plain_queued(env):
    open_card(env, head=SHA_A)
    env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    assert env.ledger.queue()[0]["state"] == "queued"
    env.ledger.add_evidence(
        env.actors["worker-1"],
        "acme/app",
        128,
        "build_log",
        "file://bench/ready.log",
        SHA_A,
        result="pass",
    )
    assert env.ledger.queue()[0]["state"] == "evidence_ready"
    env.ledger.claim_slot(env.actors["verifier-1"])
    assert env.ledger.queue()[0]["state"] == "running"


def test_wait_stays_while_the_slot_is_held_and_wakes_when_it_is_released(env):
    open_card(env, head=SHA_A)
    second = env.ledger.add_task(env.actors["eng-lead"], "Second sheet", repo="acme/app")
    env.ledger.claim_task(env.actors["eng-lead"], second["id"], assignee="worker-1")
    env.ledger.link_pr(env.actors["worker-1"], second["id"], "acme/app", 129, node_id="PR_node_129")
    env.ledger.apply_github_snapshot(snap(number=129, node="PR_node_129", head=SHA_A))
    first = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    later = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 129, SHA_A)
    held = env.ledger.claim_slot(env.actors["verifier-1"])
    assert held["queue_id"] == first["queue_id"]
    armed = threading.Event()
    found = {}

    def wait_for_slot():
        waiter = open_ledger(env.db, clock=lambda: env.clock["t"])
        try:
            actor = waiter.authenticate(env.tokens["verifier-1"])
            found["slot"] = waiter.claim_slot(actor, wait=True, timeout=5, armed=armed)
        finally:
            waiter.close()

    thread = threading.Thread(target=wait_for_slot)
    thread.start()
    assert armed.wait(2)
    thread.join(0.2)
    assert thread.is_alive()
    env.ledger.release_slot(env.actors["verifier-1"], held["queue_id"], "fail")
    thread.join(5)
    assert not thread.is_alive()
    assert found["slot"]["queue_id"] == later["queue_id"]
    assert found["slot"]["state"] == "running"


def test_cancel_wakes_a_waiter_that_lost_the_byte(env):
    open_card(env, head=SHA_A)
    second = env.ledger.add_task(env.actors["eng-lead"], "Second sheet", repo="acme/app")
    env.ledger.claim_task(env.actors["eng-lead"], second["id"], assignee="worker-1")
    env.ledger.link_pr(env.actors["worker-1"], second["id"], "acme/app", 129, node_id="PR_node_129")
    env.ledger.apply_github_snapshot(snap(number=129, node="PR_node_129", head=SHA_A))
    held = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    later = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 129, SHA_A)
    env.ledger.claim_slot(env.actors["verifier-1"])
    armed = threading.Event()
    found = {}

    def wait_for_slot():
        waiter = open_ledger(env.db, clock=lambda: env.clock["t"])
        try:
            actor = waiter.authenticate(env.tokens["verifier-1"])
            found["slot"] = waiter.claim_slot(actor, wait=True, timeout=5, armed=armed)
        finally:
            waiter.close()

    thread = threading.Thread(target=wait_for_slot)
    thread.start()
    assert armed.wait(2)
    env.ledger.cancel_queue(env.actors["eng-lead"], held["queue_id"])
    thread.join(5)
    assert not thread.is_alive()
    assert found["slot"]["queue_id"] == later["queue_id"]


def _wait_for_claim(env, found, armed):
    waiter = open_ledger(env.db, clock=lambda: env.clock["t"])
    try:
        actor = waiter.authenticate(env.tokens["verifier-1"])
        found["slot"] = waiter.claim_slot(actor, wait=True, timeout=5, armed=armed)
    except Exception as exc:
        found["error"] = exc
    finally:
        waiter.close()


def test_wait_stays_on_stale_head_until_the_head_returns(env):
    open_card(env, head=SHA_A)
    queued = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    env.ledger.conn.execute("UPDATE prs SET head_sha=? WHERE number=128", (SHA_B,))
    with pytest.raises(LedgerError) as raised:
        env.ledger.claim_slot(env.actors["verifier-1"])
    assert raised.value.code == "stale_head"
    armed = threading.Event()
    found = {}
    thread = threading.Thread(target=_wait_for_claim, args=(env, found, armed))
    thread.start()
    assert armed.wait(2)
    thread.join(0.2)
    assert thread.is_alive()
    env.ledger.apply_github_snapshot(snap(head=SHA_A))
    thread.join(5)
    assert not thread.is_alive()
    assert found["slot"]["queue_id"] == queued["queue_id"]
    assert found["slot"]["state"] == "running"


def test_wait_stays_on_unknown_head_until_the_head_is_known(env):
    open_card(env, head=SHA_A)
    queued = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    env.ledger.conn.execute("UPDATE prs SET head_sha=NULL WHERE number=128")
    with pytest.raises(LedgerError) as raised:
        env.ledger.claim_slot(env.actors["verifier-1"])
    assert raised.value.code == "unknown_head"
    armed = threading.Event()
    found = {}
    thread = threading.Thread(target=_wait_for_claim, args=(env, found, armed))
    thread.start()
    assert armed.wait(2)
    thread.join(0.2)
    assert thread.is_alive()
    env.ledger.apply_github_snapshot(snap(head=SHA_A))
    thread.join(5)
    assert not thread.is_alive()
    assert found["slot"]["queue_id"] == queued["queue_id"]
    assert found["slot"]["state"] == "running"


def test_wait_refuses_a_regular_file_at_the_wake_path(env):
    open_card(env, head=SHA_A)
    path = Path(str(env.db.resolve()) + ".wake")
    path.write_text("keep", encoding="utf-8")
    with pytest.raises(LedgerError) as raised:
        env.ledger.claim_slot(env.actors["verifier-1"], wait=True, timeout=1)
    assert raised.value.code == "wake_blocked"
    assert raised.value.message == "Wake path is not a FIFO."
    assert path.read_text(encoding="utf-8") == "keep"


def test_wait_reports_when_fifos_are_unavailable(env, monkeypatch):
    open_card(env, head=SHA_A)
    monkeypatch.delattr(os, "mkfifo")
    with pytest.raises(LedgerError) as raised:
        env.ledger.claim_slot(env.actors["verifier-1"], wait=True, timeout=1)
    assert raised.value.code == "wake_unsupported"
    assert raised.value.message == "Waiting on the queue needs a POSIX FIFO."


def test_wait_times_out_without_polling_a_row_into_existence(env):
    open_card(env, head=SHA_A)
    with pytest.raises(LedgerError) as raised:
        env.ledger.claim_slot(env.actors["verifier-1"], wait=True, timeout=0.05)
    assert raised.value.code == "queue_empty"
    assert env.ledger.queue() == []


def test_enqueue_leaves_a_followup_for_each_verifier(env):
    open_card(env, head=SHA_A)
    queued = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    items = env.ledger.followups(env.actors["eng-lead"])["items"]
    assert len(items) == 1
    assert items[0]["target"]["text_quoted"] == "> verifier-1"
    assert items[0]["event"]["type"] == "verification.queued"
    assert items[0]["event"]["queue_id"] == queued["queue_id"]
    assert items[0]["event"]["state"] == "queued"
    again = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    assert again["queue_id"] == queued["queue_id"]
    assert len(env.ledger.followups(env.actors["eng-lead"])["items"]) == 1


def test_disabled_verifier_gets_no_followup(env):
    open_card(env, head=SHA_A)
    env.ledger.conn.execute(
        "UPDATE agents SET disabled_at=? WHERE id='verifier-1'",
        (env.clock["t"],),
    )
    env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    assert env.ledger.followups(env.actors["eng-lead"])["items"] == []


def test_a_verifier_already_on_the_event_is_not_notified_twice(env):
    open_card(env, head=SHA_A)
    env.ledger.notify_me(
        env.actors["verifier-1"],
        "verification.queued",
        "cursor_followup",
        channel_target="verifier-1",
    )
    env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    assert len(env.ledger.followups(env.actors["eng-lead"])["items"]) == 1


def test_waiting_claim_picks_up_a_row_when_the_wake_byte_is_missed(env, monkeypatch):
    open_card(env, head=SHA_A)
    monkeypatch.setattr("shuin.ledger.wake.wait_fd", lambda fd, timeout: False)
    armed = threading.Event()
    found = {}

    def wait_for_row():
        waiter = open_ledger(env.db, clock=lambda: env.clock["t"])
        try:
            actor = waiter.authenticate(env.tokens["verifier-1"])
            found["slot"] = waiter.claim_slot(actor, wait=True, timeout=3, armed=armed)
        except Exception as exc:
            found["error"] = exc
        finally:
            waiter.close()

    thread = threading.Thread(target=wait_for_row)
    thread.start()
    assert armed.wait(2)
    queued = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    thread.join(4)
    assert not thread.is_alive()
    assert "error" not in found
    assert found["slot"]["queue_id"] == queued["queue_id"]
    assert found["slot"]["state"] == "running"
