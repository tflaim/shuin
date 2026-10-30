import pytest

from shuin.errors import LedgerError
from shuin.ledger import open_ledger
from tests.conftest import SHA_A, open_card


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [
        ([], {"n": 0, "clean": 0, "edited": 0, "reverted": 0, "reworked": 0, "score": None, "eligible": False}),
        (["merged_clean"] * 9, {"n": 9, "clean": 9, "edited": 0, "reverted": 0, "reworked": 0, "score": 1.0, "eligible": False}),
        (["merged_clean"] * 10, {"n": 10, "clean": 10, "edited": 0, "reverted": 0, "reworked": 0, "score": 1.0, "eligible": True}),
        (["merged_clean"] * 9 + ["merged_edited"], {"n": 10, "clean": 9, "edited": 1, "reverted": 0, "reworked": 0, "score": 0.9, "eligible": True}),
        (["merged_clean"] * 8 + ["merged_edited"] * 2, {"n": 10, "clean": 8, "edited": 2, "reverted": 0, "reworked": 0, "score": 0.8, "eligible": False}),
        (["merged_clean"] * 10 + ["reverted"], {"n": 11, "clean": 10, "edited": 0, "reverted": 1, "reworked": 0, "score": 0.9090909090909091, "eligible": False}),
        (["reverted"] * 5 + ["merged_clean"] * 20, {"n": 20, "clean": 20, "edited": 0, "reverted": 0, "reworked": 0, "score": 1.0, "eligible": True}),
        (["merged_clean"] * 9 + ["reworked"], {"n": 10, "clean": 9, "edited": 0, "reverted": 0, "reworked": 1, "score": 0.9, "eligible": True}),
    ],
    ids=["empty", "nine-clean", "ten-clean", "one-edited", "two-edited", "recent-revert", "old-reverts", "reworked"],
)
def test_trust_summary(env, kinds, expected):
    for kind in kinds:
        env.ledger.record_trust_event("worker-1", "acme/app", kind)

    assert env.ledger.trust_summary("worker-1", "acme/app") == expected


def test_trust_pairs_are_separate(env):
    for _ in range(10):
        env.ledger.record_trust_event("worker-1", "acme/app", "merged_clean")

    assert env.ledger.trust_summary("worker-1", "acme/other") == {
        "n": 0, "clean": 0, "edited": 0, "reverted": 0, "reworked": 0, "score": None, "eligible": False,
    }
    assert env.ledger.trust_summary("verifier-1", "acme/app") == {
        "n": 0, "clean": 0, "edited": 0, "reverted": 0, "reworked": 0, "score": None, "eligible": False,
    }


def test_record_trust_event_keeps_optional_details_and_history(env):
    task = open_card(env, head=SHA_A)
    env.ledger.record_trust_event(
        "worker-1", "acme/app", "merged_edited", task_id=task["id"], pr_id=1, head_sha=SHA_A,
    )
    env.clock["t"] += 1
    env.ledger.record_trust_event("worker-1", "acme/app", "reworked")

    reopened = open_ledger(env.db)
    try:
        rows = reopened.conn.execute(
            "SELECT agent_id, repo, kind, task_id, pr_id, head_sha, created_at FROM trust_events ORDER BY id"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("worker-1", "acme/app", "merged_edited", "T-0001", 1, SHA_A, 1_700_000_000),
            ("worker-1", "acme/app", "reworked", None, None, None, 1_700_000_001),
        ]
        assert reopened.trust_summary("worker-1", "acme/app") == {
            "n": 2, "clean": 0, "edited": 1, "reverted": 0, "reworked": 1, "score": 0.0, "eligible": False,
        }
    finally:
        reopened.close()


def test_record_trust_event_rejects_unknown_kind(env):
    with pytest.raises(LedgerError) as raised:
        env.ledger.record_trust_event("worker-1", "acme/app", "unknown")
    assert raised.value.code == "bad_kind"
    assert env.ledger.trust_summary("worker-1", "acme/app") == {
        "n": 0, "clean": 0, "edited": 0, "reverted": 0, "reworked": 0, "score": None, "eligible": False,
    }


@pytest.mark.parametrize("method", ["record_trust_event", "trust_summary"])
def test_trust_rejects_unknown_agent(env, method):
    with pytest.raises(LedgerError) as raised:
        if method == "record_trust_event":
            env.ledger.record_trust_event("missing", "acme/app", "merged_clean")
        else:
            env.ledger.trust_summary("missing", "acme/app")
    assert raised.value.code == "not_found"
