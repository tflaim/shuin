import json

from shuin.ledger import sign_webhook
from tests.conftest import SHA_A, open_card


def test_events_are_ids_only_and_inbox_cursor_advances(env):
    task = open_card(env, head=SHA_A)
    env.ledger.request_review(env.actors["worker-1"], "acme/app", 128, "nothing")
    env.ledger.set_blocked(env.actors["worker-1"], task["id"], "needs_human")
    first = env.ledger.my_inbox(env.actors["worker-1"], ack=False)
    assert first["events"]
    for event in first["events"]:
        assert set(event) == {"id", "type", "task_id", "pr", "queue_id", "state", "ts"}
        assert "nothing" not in json.dumps(event)
    assert env.ledger.my_inbox(env.actors["worker-1"], ack=False)["events"]
    acked = env.ledger.my_inbox(env.actors["worker-1"], ack=True)
    assert acked["events"]
    assert env.ledger.my_inbox(env.actors["worker-1"], ack=False)["events"] == []


def test_webhook_signature_and_retries(env):
    task = open_card(env, head=SHA_A)
    sub = env.ledger.notify_me(
        env.actors["worker-1"],
        "review.requested",
        "webhook",
        channel_target="http://1.1.1.1/hook",
        filt={"task_id": task["id"]},
    )
    env.ledger.notify_me(env.actors["worker-1"], "review.requested", "poll")
    env.ledger.notify_me(
        env.actors["eng-lead"],
        "review.requested",
        "cursor_followup",
        channel_target="lead-session",
        filt={"task_id": "T-9999"},
    )
    env.ledger.request_review(env.actors["worker-1"], "acme/app", 128, "nothing")
    deliveries = env.ledger.conn.execute("SELECT * FROM deliveries").fetchall()
    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "pending"
    captured = {}

    def send(url, body, headers):
        captured["url"] = url
        captured["body"] = body
        captured["headers"] = headers

    result = env.ledger.deliver_due(send)
    assert result["sent"] == 1
    payload = json.loads(captured["body"])
    assert payload["type"] == "review.requested"
    assert payload["pr"] == 128
    expected = sign_webhook(sub["secret"], captured["headers"]["webhook-id"], int(captured["headers"]["webhook-timestamp"]), captured["body"])
    assert captured["headers"]["webhook-signature"] == expected
    assert "nothing" not in captured["body"].decode()

    def boom(url, body, headers):
        raise OSError("down")

    env.ledger.request_review(env.actors["worker-1"], "acme/app", 128, "still nothing")
    for delay in (60, 300, 1800):
        outcome = env.ledger.deliver_due(boom)
        assert outcome["failed"] == 1
        env.clock["t"] += delay
    outcome = env.ledger.deliver_due(boom)
    assert outcome["dead"] == 1
    dead = env.ledger.conn.execute("SELECT status FROM deliveries ORDER BY id DESC LIMIT 1").fetchone()
    assert dead["status"] == "dead"
    # The mismatched filter did not create a follow-up. A matching one does.
    env.ledger.notify_me(
        env.actors["eng-lead"],
        "agent.blocked",
        "cursor_followup",
        channel_target="lead-session",
    )
    env.ledger.set_blocked(env.actors["eng-lead"], task["id"], "waiting_verifier")
    box = env.ledger.followups(env.actors["eng-lead"])
    assert box["items"]
    assert box["items"][0]["event"]["state"] == "waiting_verifier"
    assert "text" not in box["items"][0]["event"]


def test_inbox_honors_poll_subscriptions(env):
    task = open_card(env, head=SHA_A)
    env.ledger.notify_me(env.actors["worker-1"], "agent.blocked", "poll")
    env.ledger.request_review(env.actors["worker-1"], "acme/app", 128, "nothing")
    env.ledger.set_blocked(env.actors["worker-1"], task["id"], "needs_human")
    inbox = env.ledger.my_inbox(env.actors["worker-1"])
    assert [event["type"] for event in inbox["events"]] == ["agent.blocked"]
    env.ledger.notify_me(
        env.actors["eng-lead"],
        "agent.blocked",
        "poll",
        filt={"task_id": "T-9999"},
    )
    lead_inbox = env.ledger.my_inbox(env.actors["eng-lead"])
    assert lead_inbox["events"] == []


def test_inbox_read_releases_an_expired_lease(env):
    open_card(env, head=SHA_A)
    env.ledger.notify_me(env.actors["worker-1"], "verification_slot.freed", "poll")
    env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    claimed = env.ledger.claim_slot(env.actors["verifier-1"])
    env.clock["t"] = claimed["lease_expires_at"]
    inbox = env.ledger.my_inbox(env.actors["worker-1"])
    assert any(
        event["type"] == "verification_slot.freed" and event["state"] == "expired"
        for event in inbox["events"]
    )
    running = env.ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM verification_queue WHERE state='running'"
    ).fetchone()["n"]
    assert running == 0


def test_board_json_omits_claim_text(env):
    task = open_card(env, head=SHA_A)
    env.ledger.post_update(env.actors["worker-1"], task["id"], "progress", "secret claim text")
    board = env.ledger.list_board(env.actors["worker-1"])
    blob = json.dumps(board)
    assert "secret claim text" not in blob
    assert "claims" not in board
