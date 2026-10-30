import pytest

from shuin.errors import LedgerError
from shuin.gate import classify
from shuin.types import CheckItem, ChecksRead, HeadRead
from tests.conftest import SHA_A, SHA_B, open_card, run_log, snap


def _check(state, context="ci", check_id=1):
    return CheckItem(context, check_id, state, "check_run")


def test_classify_fail_closed():
    ok = [_check("success")]
    assert classify(ok, "pass", SHA_A, SHA_A, None) == "success"
    assert classify(ok, "missing", SHA_A, SHA_A, None) == "missing"
    assert classify(ok, "fail", SHA_A, SHA_A, None) == "failure"
    assert classify([], "pass", SHA_A, SHA_A, None) == "success"
    assert classify([], "fail", SHA_A, SHA_A, None) == "failure"
    assert classify([], "missing", SHA_A, SHA_A, None) == "missing"
    assert classify([_check("pending")], "pass", SHA_A, SHA_A, None) == "pending"
    assert classify([_check("failure")], "pass", SHA_A, SHA_A, None) == "failure"
    assert classify([_check("skipped")], "pass", SHA_A, SHA_A, None) == "failure"
    assert classify([_check("neutral")], "pass", SHA_A, SHA_A, None) == "failure"
    assert classify([_check("missing")], "pass", SHA_A, SHA_A, None) == "missing"
    assert classify(ok, "pass", SHA_A, SHA_B, None) == "stale"
    assert classify(ok, "pass", SHA_A.upper(), f"  {SHA_A}  ", None) == "success"
    assert classify(ok, "pass", SHA_A, SHA_A, "auth") == "auth"
    assert classify(ok, "pass", SHA_A, None, "infra") == "infra"
    assert classify(ok, "pass", None, None, None) == "stale"


class FakeGH:
    def __init__(self, heads, checks=(), check_error=None):
        self.heads = list(heads)
        self.checks = checks
        self.check_error = check_error
        self.n = 0

    def read_head(self, repo, number):
        head = self.heads[min(self.n, len(self.heads) - 1)]
        self.n += 1
        return head

    def read_checks(self, repo, base, sha):
        if self.check_error:
            return ChecksRead(error=self.check_error)
        return ChecksRead(checks=tuple(self.checks))


def _head(sha, state="open"):
    return HeadRead(sha, "feat/sheet", "main", "PR_node_128", state)


def test_gate_stores_receipt_and_done_tracks_head(env):
    task = open_card(env, head=SHA_A)
    env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    slot = env.ledger.claim_slot(env.actors["verifier-1"])
    run_log(env)
    env.ledger.stamp(env.actors["verifier-1"], slot["queue_id"])
    gh = FakeGH([_head(SHA_A), _head(SHA_A)], checks=(_check("success"),))
    body = env.ledger.seal(env.actors["eng-lead"], "acme/app", 128, gh)
    assert body["ok"] is True
    assert body["classification"] == "success"
    assert body["voice"] == "Seal holds."
    stored = env.ledger.conn.execute("SELECT classification, head_sha, head_sha_reread FROM pr_acceptance").fetchone()
    assert stored["classification"] == "success"
    assert stored["head_sha"] == stored["head_sha_reread"] == SHA_A
    assert env.ledger.complete_task(env.actors["eng-lead"], task["id"])["status"] == "done"

    other = env.ledger.add_task(env.actors["eng-lead"], "Sibling", repo="acme/app")
    env.ledger.claim_task(env.actors["eng-lead"], other["id"], assignee="worker-1")
    env.ledger.link_pr(env.actors["worker-1"], other["id"], "acme/app", 129, node_id="PR_node_129")
    from tests.conftest import snap

    env.ledger.apply_github_snapshot(snap(number=129, node="PR_node_129", head=SHA_A))
    env.ledger.enqueue(env.actors["worker-1"], "acme/app", 129, SHA_A)
    slot = env.ledger.claim_slot(env.actors["verifier-1"])
    run_log(env, number=129)
    env.ledger.stamp(env.actors["verifier-1"], slot["queue_id"])
    moved = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        129,
        FakeGH([_head(SHA_A), _head(SHA_B)], checks=(_check("success"),)),
    )
    assert moved["ok"] is False
    assert moved["classification"] == "stale"
    assert moved["voice"] == "Seal broken: head moved. shuin seal --pr acme/app#129"
    finished = env.ledger.complete_task(env.actors["worker-1"], other["id"])
    assert finished["status"] == "in_review"
    assert finished["closed_by"] is None
    kind = env.ledger.conn.execute(
        "SELECT kind FROM updates WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (other["id"],),
    ).fetchone()["kind"]
    assert kind == "done"
    assert env.ledger.conn.execute(
        "SELECT status FROM tasks WHERE id=?",
        (other["id"],),
    ).fetchone()["status"] != "done"


def test_complete_refuses_after_the_pr_closes(env):
    task = open_card(env, head=SHA_A)
    env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    slot = env.ledger.claim_slot(env.actors["verifier-1"])
    run_log(env)
    env.ledger.stamp(env.actors["verifier-1"], slot["queue_id"])
    sealed = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        FakeGH([_head(SHA_A), _head(SHA_A)], checks=(_check("success"),)),
    )
    assert sealed["ok"] is True
    env.ledger.apply_github_snapshot(snap(head=SHA_A, state="closed"))
    with pytest.raises(LedgerError) as raised:
        env.ledger.complete_task(env.actors["eng-lead"], task["id"])
    assert raised.value.code == "seal_refused"
    status = env.ledger.conn.execute(
        "SELECT status FROM tasks WHERE id=?",
        (task["id"],),
    ).fetchone()["status"]
    assert status != "done"


def test_gate_refuses_a_closed_pr(env):
    open_card(env, head=SHA_A)
    env.ledger.apply_github_snapshot(snap(head=SHA_A, state="closed"))
    env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    slot = env.ledger.claim_slot(env.actors["verifier-1"])
    run_log(env)
    env.ledger.stamp(env.actors["verifier-1"], slot["queue_id"])
    body = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        FakeGH([_head(SHA_A, state="closed"), _head(SHA_A, state="closed")], checks=(_check("success"),)),
    )
    assert body["ok"] is False
    assert body["classification"] != "success"


def test_gate_fails_closed_when_never_polled(env):
    open_card(env)
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    env.ledger.conn.execute(
        """INSERT INTO verification_queue(
             pr_id, commit_sha, requested_by, state, enqueued_at, claimed_by, finished_at
           ) VALUES (?,?, 'verifier-1', 'passed', 1, 'verifier-1', 1)""",
        (pr["id"], SHA_A),
    )
    body = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        FakeGH([_head(SHA_A), _head(SHA_A)], checks=(_check("success"),)),
    )
    assert body["ok"] is False
    assert body["classification"] != "success"


def test_unknown_head_cannot_be_enqueued_or_shown_stamped(env):
    open_card(env)
    with pytest.raises(LedgerError) as raised:
        env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, "deadbee")
    assert raised.value.code == "unknown_head"
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    env.ledger.conn.execute(
        """INSERT INTO verification_queue(
             pr_id, commit_sha, requested_by, state, enqueued_at, claimed_by, finished_at
           ) VALUES (?,?, 'verifier-1', 'passed', 1, 'verifier-1', 1)""",
        (pr["id"], "deadbee"),
    )
    status = env.ledger.seal_status("acme/app", 128)
    assert status["seal"] != "stamped"
    assert status["voice"] != "Stamped."
    env.ledger.conn.execute(
        """INSERT INTO verification_queue(
             pr_id, commit_sha, requested_by, state, enqueued_at, claimed_by, claimed_at, lease_expires_at
           ) VALUES (?,?, 'worker-1', 'running', 1, 'verifier-1', 1, ?)""",
        (pr["id"], "deadbee", env.clock["t"] + 1000),
    )
    queue_id = env.ledger.conn.execute(
        "SELECT id FROM verification_queue WHERE state='running'"
    ).fetchone()["id"]
    with pytest.raises(LedgerError) as stamped:
        env.ledger.stamp(env.actors["verifier-1"], queue_id)
    assert stamped.value.code == "unknown_head"
    state = env.ledger.conn.execute(
        "SELECT state FROM verification_queue WHERE id=?",
        (queue_id,),
    ).fetchone()["state"]
    assert state != "passed"


def test_worker_forged_evidence_does_not_seal(env):
    open_card(env, head=SHA_A)
    env.ledger.add_evidence(
        env.actors["worker-1"],
        "acme/app",
        128,
        "screenshot",
        "file://bench/forged.png",
        SHA_A,
        result="pass",
    )
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    env.ledger.conn.execute(
        """INSERT INTO verification_queue(
             pr_id, commit_sha, requested_by, state, enqueued_at, claimed_by, finished_at
           ) VALUES (?,?, 'worker-1', 'passed', 1, 'worker-1', 1)""",
        (pr["id"], SHA_A),
    )
    body = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        FakeGH([_head(SHA_A), _head(SHA_A)], checks=(_check("success"),)),
    )
    assert body["ok"] is False
    assert body["classification"] != "success"
    assert body["verifier_state"] == "missing"


def test_gate_refuses_auth_and_missing_verifier(env):
    open_card(env, head=SHA_A)
    auth = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        FakeGH([HeadRead(error="auth"), HeadRead(error="auth")]),
    )
    assert auth["classification"] == "auth"
    assert auth["ok"] is False
    missing_verifier = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        FakeGH([_head(SHA_A), _head(SHA_A)], checks=(_check("success"),)),
    )
    assert missing_verifier["classification"] == "missing"
    assert missing_verifier["verifier_state"] == "missing"
    zero = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        FakeGH([_head(SHA_A), _head(SHA_A)], checks=()),
    )
    assert zero["classification"] == "missing"
