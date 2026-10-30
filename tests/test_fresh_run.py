import pytest

from shuin.errors import LedgerError
from shuin.seal import Freshness, fresh, fresh_at
from tests.conftest import SHA_A, SHA_B, open_card, run_log, snap
from tests.test_gate import FakeGH, _check, _head


def _pass_and_seal(env, sha=SHA_A, number=128):
    env.ledger.enqueue(env.actors["worker-1"], "acme/app", number, sha)
    slot = env.ledger.claim_slot(env.actors["verifier-1"])
    run_log(env, sha, number)
    env.ledger.stamp(env.actors["verifier-1"], slot["queue_id"])
    return env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        number,
        FakeGH([_head(sha), _head(sha)], checks=(_check("success"),)),
    )


def test_full_run_on_the_exact_head_seals(env):
    open_card(env, head=SHA_A)
    body = _pass_and_seal(env)
    assert body["ok"] is True
    assert body["verifier_state"] == "pass"
    row = env.ledger.conn.execute(
        "SELECT run_kind, opened_sha, opened_epoch, state FROM verification_queue"
    ).fetchone()
    assert row["state"] == "passed"
    assert row["run_kind"] == "full"
    assert row["opened_sha"] == SHA_A
    assert row["opened_epoch"] == 1


def test_patch_identity_does_not_carry_a_pass(env):
    open_card(env, head=SHA_A)
    env.ledger.apply_github_snapshot(snap(head=SHA_A, patch_id="same-patch"))
    sealed = _pass_and_seal(env)
    assert sealed["ok"] is True
    env.ledger.apply_github_snapshot(snap(head=SHA_B, patch_id="same-patch"))
    stored = env.ledger.conn.execute("SELECT patch_id, head_sha FROM prs").fetchone()
    assert stored["patch_id"] == "same-patch"
    assert stored["head_sha"] == SHA_B
    moved = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        FakeGH([_head(SHA_B), _head(SHA_B)], checks=(_check("success"),)),
    )
    assert moved["ok"] is False
    assert moved["verifier_state"] == "missing"


def test_carried_over_and_patch_identity_rows_do_not_count(env):
    open_card(env, head=SHA_A)
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    for kind in ("carried_over", "patch_identity"):
        env.ledger.conn.execute("DELETE FROM verification_queue")
        env.ledger.conn.execute(
            """INSERT INTO verification_queue(
                 pr_id, commit_sha, requested_by, state, enqueued_at, claimed_by, finished_at,
                 run_kind, opened_sha, opened_epoch
               ) VALUES (?, ?, 'verifier-1', 'passed', 1, 'verifier-1', 1, ?, ?, 1)""",
            (pr["id"], SHA_A, kind, SHA_A),
        )
        body = env.ledger.seal(
            env.actors["eng-lead"],
            "acme/app",
            128,
            FakeGH([_head(SHA_A), _head(SHA_A)], checks=(_check("success"),)),
        )
        assert body["verifier_state"] == "missing"
        assert body["ok"] is False
        status = env.ledger.seal_status("acme/app", 128)
        assert status["seal"] != "stamped"
        assert status["voice"] != "Stamped."


def test_a_pass_from_before_the_head_landed_does_not_count(env):
    open_card(env, head=SHA_A)
    assert _pass_and_seal(env)["ok"] is True
    env.ledger.apply_github_snapshot(snap(head=SHA_B))
    env.ledger.apply_github_snapshot(snap(head=SHA_A))
    again = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        FakeGH([_head(SHA_A), _head(SHA_A)], checks=(_check("success"),)),
    )
    assert again["ok"] is False
    assert again["verifier_state"] == "missing"
    task_id = env.ledger.conn.execute("SELECT id FROM tasks").fetchone()["id"]
    finished = env.ledger.complete_task(env.actors["eng-lead"], task_id)
    assert finished["status"] != "done"
    assert env.ledger.conn.execute(
        "SELECT kind FROM updates WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()["kind"] == "done"
    fresh = _pass_and_seal(env)
    assert fresh["ok"] is True
    assert fresh["verifier_state"] == "pass"


def test_stamp_rejects_a_patch_identity_run(env):
    open_card(env, head=SHA_A)
    queued = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    env.ledger.claim_slot(env.actors["verifier-1"])
    env.ledger.conn.execute(
        "UPDATE verification_queue SET run_kind='patch_identity' WHERE id=?",
        (queued["queue_id"],),
    )
    with pytest.raises(LedgerError) as raised:
        env.ledger.stamp(env.actors["verifier-1"], queued["queue_id"])
    assert raised.value.code == "stale_run"
    assert raised.value.message == "Verification is not fresh."
    state = env.ledger.conn.execute(
        "SELECT state FROM verification_queue WHERE id=?",
        (queued["queue_id"],),
    ).fetchone()["state"]
    assert state == "superseded"


def test_pass_kinds_and_stamp_needs_run_evidence(env):
    open_card(env, head=SHA_A)
    env.ledger.apply_github_snapshot(snap(head=SHA_A, patch_id="patch-a", tree_sha=SHA_A))
    fresh = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A, require_run=True)
    assert fresh["pass_kind"] == "fresh"
    assert env.ledger.queue()[0]["pass_kind"] == "fresh"
    slot = env.ledger.claim_slot(env.actors["verifier-1"])
    with pytest.raises(LedgerError) as missing:
        env.ledger.stamp(env.actors["verifier-1"], slot["queue_id"])
    assert missing.value.code == "run_evidence_required"
    assert missing.value.message == "Run evidence is missing."
    still = env.ledger.conn.execute(
        "SELECT state FROM verification_queue WHERE id=?",
        (slot["queue_id"],),
    ).fetchone()["state"]
    assert still == "running"
    run_log(env)
    env.ledger.stamp(env.actors["verifier-1"], slot["queue_id"])
    sealed = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        FakeGH([_head(SHA_A), _head(SHA_A)], checks=(_check("success"),)),
    )
    assert sealed["ok"] is True

    env.ledger.apply_github_snapshot(snap(head=SHA_B, patch_id="patch-b", tree_sha=SHA_A))
    tree = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_B)
    assert tree["pass_kind"] == "tree_match"
    assert env.ledger.queue()[0]["pass_kind"] == "tree_match"
    run_log(env, SHA_B)
    claimed = env.ledger.claim_slot(env.actors["verifier-1"])
    env.ledger.stamp(env.actors["verifier-1"], claimed["queue_id"])
    tree_seal = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        FakeGH([_head(SHA_B), _head(SHA_B)], checks=(_check("success"),)),
    )
    assert tree_seal["ok"] is True

    carried_sha = "c" * 40
    env.ledger.apply_github_snapshot(snap(head=carried_sha, patch_id="patch-a", tree_sha=SHA_B))
    carried = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, carried_sha)
    assert carried["pass_kind"] == "patch_carried"
    assert env.ledger.queue()[0]["pass_kind"] == "patch_carried"
    run_log(env, carried_sha)
    carried_slot = env.ledger.claim_slot(env.actors["verifier-1"])
    env.ledger.stamp(env.actors["verifier-1"], carried_slot["queue_id"])
    promoted = env.ledger.conn.execute(
        "SELECT pass_kind, state FROM verification_queue WHERE id=?",
        (carried_slot["queue_id"],),
    ).fetchone()
    assert promoted["state"] == "passed"
    assert promoted["pass_kind"] == "fresh"
    carried_seal = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        FakeGH([_head(carried_sha), _head(carried_sha)], checks=(_check("success"),)),
    )
    assert carried_seal["ok"] is True

    forced_sha = "d" * 40
    env.ledger.apply_github_snapshot(snap(head=forced_sha, patch_id="patch-a", tree_sha=SHA_A))
    forced = env.ledger.enqueue(
        env.actors["worker-1"],
        "acme/app",
        128,
        forced_sha,
        require_run=True,
    )
    assert forced["pass_kind"] == "fresh"


def test_unpromoted_patch_carried_pass_does_not_seal(env):
    open_card(env, head=SHA_A)
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    epoch = pr["head_epoch"]
    env.ledger.conn.execute(
        """INSERT INTO verification_queue(
             pr_id, commit_sha, requested_by, state, enqueued_at, claimed_by, finished_at,
             run_kind, opened_sha, opened_epoch, pass_kind
           ) VALUES (?, ?, 'verifier-1', 'passed', 1, 'verifier-1', 1, 'full', ?, ?, 'patch_carried')""",
        (pr["id"], SHA_A, SHA_A, epoch),
    )
    body = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        FakeGH([_head(SHA_A), _head(SHA_A)], checks=(_check("success"),)),
    )
    assert body["ok"] is False
    assert body["verifier_state"] == "missing"


def test_stamp_requires_a_non_failing_test_log(env):
    open_card(env, head=SHA_A)
    queued = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    env.ledger.claim_slot(env.actors["verifier-1"])
    env.ledger.add_evidence(
        env.actors["worker-1"],
        "acme/app",
        128,
        "screenshot",
        "file://bench/sheet.png",
        SHA_A,
        result="pass",
    )
    with pytest.raises(LedgerError) as shot:
        env.ledger.stamp(env.actors["verifier-1"], queued["queue_id"])
    assert shot.value.code == "run_evidence_required"
    env.ledger.add_evidence(
        env.actors["verifier-1"],
        "acme/app",
        128,
        "test_log",
        "file://bench/sheet-fail.log",
        SHA_A,
        result="fail",
    )
    with pytest.raises(LedgerError) as failed:
        env.ledger.stamp(env.actors["verifier-1"], queued["queue_id"])
    assert failed.value.code == "run_evidence_required"
    still = env.ledger.conn.execute(
        "SELECT state FROM verification_queue WHERE id=?",
        (queued["queue_id"],),
    ).fetchone()["state"]
    assert still == "running"
    env.ledger.add_evidence(
        env.actors["verifier-1"],
        "acme/app",
        128,
        "test_log",
        "file://bench/sheet-info.log",
        SHA_A,
        result="info",
    )
    stamped = env.ledger.stamp(env.actors["verifier-1"], queued["queue_id"])
    assert stamped["voice"] == "Stamped. Not sealed yet: shuin seal --pr acme/app#128"


def test_claim_refuses_a_row_that_is_not_the_head(env):
    open_card(env, head=SHA_A)
    queued = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    env.ledger.conn.execute("UPDATE prs SET head_sha=? WHERE number=128", (SHA_B,))
    with pytest.raises(LedgerError) as raised:
        env.ledger.claim_slot(env.actors["verifier-1"])
    assert raised.value.code == "stale_head"
    state = env.ledger.conn.execute(
        "SELECT state FROM verification_queue WHERE id=?",
        (queued["queue_id"],),
    ).fetchone()["state"]
    assert state == "queued"


def test_inserted_pass_without_an_opened_run_does_not_count(env):
    open_card(env, head=SHA_A)
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    env.ledger.conn.execute(
        """INSERT INTO verification_queue(
             pr_id, commit_sha, requested_by, state, enqueued_at, claimed_by, finished_at
           ) VALUES (?, ?, 'verifier-1', 'passed', 1, 'verifier-1', 1)""",
        (pr["id"], SHA_A),
    )
    body = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        FakeGH([_head(SHA_A), _head(SHA_A)], checks=(_check("success"),)),
    )
    assert body["verifier_state"] == "missing"
    assert body["classification"] != "success"


def test_a_pass_needs_a_full_run_and_a_real_epoch(env):
    open_card(env, head=SHA_A)
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    env.ledger.conn.execute(
        """INSERT INTO verification_queue(
             pr_id, commit_sha, requested_by, state, enqueued_at, claimed_by, finished_at,
             run_kind, opened_sha, opened_epoch
           ) VALUES (?, ?, 'verifier-1', 'passed', 1, 'verifier-1', 1, 'full', ?, NULL)""",
        (pr["id"], SHA_A, SHA_A),
    )
    assert env.ledger.verifier_state(pr["id"], SHA_A) == "missing"
    env.ledger.conn.execute("UPDATE verification_queue SET opened_epoch=0")
    env.ledger.conn.execute("UPDATE prs SET head_epoch=0 WHERE id=?", (pr["id"],))
    assert env.ledger.verifier_state(pr["id"], SHA_A) == "pass"
    env.ledger.conn.execute("UPDATE verification_queue SET run_kind='carried_over'")
    assert env.ledger.verifier_state(pr["id"], SHA_A) == "missing"
    assert fresh(Freshness(SHA_A, SHA_A, run=True, opened_sha=SHA_A, opened_epoch=0, head_epoch=None, run_kind="full")) is False
    assert fresh(Freshness(SHA_A, SHA_A, run=True, opened_sha=SHA_A, opened_epoch=0, head_epoch="", run_kind="full")) is False
    assert fresh(Freshness(SHA_A, SHA_A, run=True, opened_sha=SHA_A, opened_epoch=0, head_epoch=0, run_kind=None)) is False
    assert fresh(Freshness(SHA_A, SHA_A, run=True, opened_sha=SHA_A, opened_epoch=0, head_epoch=0, run_kind="full")) is True


def test_a_timestamp_cannot_skip_the_sha_check():
    with pytest.raises(TypeError):
        Freshness(head=None, since=1)
    assert fresh(Freshness(None, SHA_A)) is False
    assert fresh(Freshness(SHA_A, None)) is False
    with pytest.raises(ValueError) as raised:
        fresh(Freshness(SHA_A, SHA_A, run_kind="full"))
    assert str(raised.value) == "run fields require run=True"
    assert fresh_at(5, 5) is True
    assert fresh_at(4, 5) is False
    assert fresh_at(None, 5) is False
    with pytest.raises(ValueError) as missing:
        fresh_at(5, None)
    assert str(missing.value) == "since is required"


def test_enqueue_keeps_mixed_case_and_supersedes_an_empty_sha(env):
    open_card(env, head=SHA_A)
    queued = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A.upper())
    assert queued["commit_sha"] == SHA_A
    env.ledger.conn.execute(
        "UPDATE verification_queue SET commit_sha=? WHERE id=?",
        (SHA_A.upper(), queued["queue_id"]),
    )
    again = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    assert again["queue_id"] == queued["queue_id"]
    assert again["state"] == "queued"
    stored = env.ledger.conn.execute(
        "SELECT commit_sha, state FROM verification_queue ORDER BY id"
    ).fetchall()
    assert [(row["commit_sha"], row["state"]) for row in stored] == [(SHA_A.upper(), "queued")]

    env.ledger.conn.execute(
        "UPDATE verification_queue SET commit_sha='' WHERE id=?",
        (queued["queue_id"],),
    )
    fresh_row = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    assert fresh_row["queue_id"] != queued["queue_id"]
    assert fresh_row["commit_sha"] == SHA_A
    rows = env.ledger.conn.execute(
        "SELECT commit_sha, state FROM verification_queue ORDER BY id"
    ).fetchall()
    assert [(row["commit_sha"], row["state"]) for row in rows] == [
        ("", "superseded"),
        (SHA_A, "queued"),
    ]


def test_head_move_keeps_mixed_case_and_supersedes_an_empty_sha(env):
    open_card(env, head=SHA_A)
    queued = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    env.ledger.conn.execute(
        "UPDATE verification_queue SET commit_sha=? WHERE id=?",
        (SHA_B.upper(), queued["queue_id"]),
    )
    env.ledger.conn.execute(
        """INSERT INTO verification_queue(
             pr_id, commit_sha, requested_by, state, enqueued_at
           ) VALUES (?, '', 'worker-1', 'queued', 1)""",
        (pr["id"],),
    )
    env.ledger.apply_github_snapshot(snap(head=SHA_B))
    rows = env.ledger.conn.execute(
        "SELECT commit_sha, state FROM verification_queue ORDER BY id"
    ).fetchall()
    assert [(row["commit_sha"], row["state"]) for row in rows] == [
        (SHA_B.upper(), "queued"),
        ("", "superseded"),
    ]


def test_release_treats_mixed_case_as_the_same_commit(env):
    open_card(env, head=SHA_A)
    queued = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    env.ledger.claim_slot(env.actors["verifier-1"])
    run_log(env)
    env.ledger.conn.execute(
        "UPDATE verification_queue SET commit_sha=? WHERE id=?",
        (f"  {SHA_A.upper()}  ", queued["queue_id"]),
    )
    released = env.ledger.stamp(env.actors["verifier-1"], queued["queue_id"])
    assert released["result"] == "pass"
    row = env.ledger.conn.execute(
        "SELECT state, run_kind FROM verification_queue WHERE id=?",
        (queued["queue_id"],),
    ).fetchone()
    assert row["run_kind"] == "full"
    assert row["state"] == "passed"


def test_verify_matches_a_mixed_case_test_log(env):
    open_card(env, head=SHA_A)
    env.ledger.add_evidence(
        env.actors["verifier-1"],
        "acme/app",
        128,
        "test_log",
        "file://bench/128.log",
        SHA_A,
        result="fail",
    )
    env.ledger.conn.execute(
        "UPDATE evidence SET commit_sha=? WHERE kind='test_log'",
        (f"  {SHA_A.upper()}  ",),
    )
    failed = env.ledger.verify(env.actors["verifier-1"], "acme/app", 128, SHA_A, "fail")
    assert failed["voice"] == "Verifier fail."
    assert failed["verifier_state"] == "fail"
    env.ledger.conn.execute("UPDATE evidence SET result='pass' WHERE kind='test_log'")
    passed = env.ledger.verify(env.actors["verifier-1"], "acme/app", 128, SHA_A, "pass")
    assert passed["voice"] == "Verifier pass."
    assert passed["verifier_state"] == "pass"
