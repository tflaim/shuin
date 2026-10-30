import pytest

from shuin.errors import LedgerError
from tests.conftest import SHA_A, open_card, run_log

LOG_HASH = "ab" * 32


def test_stamp_rejects_a_log_written_before_the_claim(env):
    open_card(env, head=SHA_A)
    env.ledger.add_evidence(
        env.actors["verifier-1"],
        "acme/app",
        128,
        "test_log",
        "file://bench/early.log",
        SHA_A,
        result="pass",
        sha256=LOG_HASH,
    )
    queued = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    slot = env.ledger.claim_slot(env.actors["verifier-1"])
    claimed = env.ledger.conn.execute(
        "SELECT claimed_at FROM verification_queue WHERE id=?",
        (queued["queue_id"],),
    ).fetchone()["claimed_at"]
    env.ledger.conn.execute(
        "UPDATE evidence SET created_at=? WHERE uri='file://bench/early.log'",
        (claimed - 10,),
    )
    with pytest.raises(LedgerError) as missing:
        env.ledger.stamp(env.actors["verifier-1"], slot["queue_id"])
    assert missing.value.code == "run_evidence_required"
    still = env.ledger.conn.execute(
        "SELECT state FROM verification_queue WHERE id=?",
        (queued["queue_id"],),
    ).fetchone()["state"]
    assert still == "running"
    run_log(env)
    stamped = env.ledger.stamp(env.actors["verifier-1"], slot["queue_id"])
    assert stamped["voice"] == "Stamped. Not sealed yet: shuin seal --pr acme/app#128"
    assert stamped["result"] == "pass"
    assert stamped["run_at"] == env.clock["t"]
    assert stamped["output_sha256"] is None
    assert stamped["run_id"] == env.ledger.conn.execute(
        "SELECT id FROM evidence WHERE uri='file://bench/128.log'"
    ).fetchone()["id"]


def test_stamp_records_the_log_hash(env):
    open_card(env, head=SHA_A)
    queued = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    env.ledger.claim_slot(env.actors["verifier-1"])
    evidence = env.ledger.add_evidence(
        env.actors["verifier-1"],
        "acme/app",
        128,
        "test_log",
        "file://bench/hashed.log",
        SHA_A,
        result="pass",
        sha256=LOG_HASH,
    )
    stamped = env.ledger.stamp(env.actors["verifier-1"], queued["queue_id"])
    assert stamped["run_id"] == evidence["evidence_id"]
    assert stamped["output_sha256"] == LOG_HASH
    assert stamped["run_at"] == env.clock["t"]


def test_queue_row_cites_evidence_written_for_its_commit(env):
    open_card(env, head=SHA_A)
    queued = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    assert queued["evidence_id"] is None
    assert queued["evidence_at"] is None
    assert queued["output_sha256"] is None
    assert env.ledger.queue()[0]["evidence_id"] is None
    evidence = env.ledger.add_evidence(
        env.actors["verifier-1"],
        "acme/app",
        128,
        "test_log",
        "file://bench/ready.log",
        SHA_A,
        result="pass",
        sha256=LOG_HASH,
    )
    row = env.ledger.queue()[0]
    assert row["state"] == "evidence_ready"
    assert row["evidence_id"] == evidence["evidence_id"]
    assert row["evidence_at"] == env.clock["t"]
    assert row["output_sha256"] == LOG_HASH


def test_only_the_verifier_can_upload_a_proof_log(env):
    open_card(env, head=SHA_A)
    with pytest.raises(LedgerError) as worker:
        env.ledger.add_evidence(
            env.actors["worker-1"],
            "acme/app",
            128,
            "test_log",
            "file://bench/worker.log",
            SHA_A,
            result="pass",
        )
    assert worker.value.code == "role_mismatch"
    with pytest.raises(LedgerError) as lead:
        env.ledger.add_evidence(
            env.actors["eng-lead"],
            "acme/app",
            128,
            "test_log",
            "file://bench/lead.log",
            SHA_A,
            result="pass",
        )
    assert lead.value.code == "role_mismatch"
    shot = env.ledger.add_evidence(
        env.actors["worker-1"],
        "acme/app",
        128,
        "screenshot",
        "https://example.com/sheet.png",
        SHA_A,
        result="info",
    )
    assert shot["actor"] == "worker-1"
    evidence = env.ledger.add_evidence(
        env.actors["verifier-1"],
        "acme/app",
        128,
        "test_log",
        "file://bench/verifier.log",
        SHA_A,
        result="pass",
    )
    assert evidence["actor"] == "verifier-1"
