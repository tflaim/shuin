from shuin.errors import LedgerError
from tests.conftest import SHA_A, SHA_B, open_card, snap
import pytest


def test_evidence_goes_stale_when_head_moves(env):
    task = open_card(env, head=SHA_A)
    env.ledger.add_evidence(
        env.actors["worker-1"],
        "acme/app",
        128,
        "screenshot",
        "file://bench/sheet.png",
        SHA_A,
        result="pass",
    )
    fresh = env.ledger.conn.execute("SELECT stale FROM evidence_v").fetchone()["stale"]
    assert fresh == 0
    queued = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    env.ledger.claim_slot(env.actors["verifier-1"])
    env.ledger.apply_github_snapshot(snap(head=SHA_B, branch="feat/sheet-renamed"))
    stale = env.ledger.conn.execute("SELECT stale FROM evidence_v").fetchone()["stale"]
    assert stale == 1
    state = env.ledger.conn.execute(
        "SELECT state FROM verification_queue WHERE id=?",
        (queued["queue_id"],),
    ).fetchone()["state"]
    assert state == "superseded"
    status = env.ledger.seal_status("acme/app", 128)
    assert status["stale"] is True
    assert status["seal_broken"] is False
    assert status["stale_verification"] is True
    assert status["voice"] == "Stale verification."
    assert env.ledger.snapshot()["voice"] == "Stale verification: 1."
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    assert pr["task_id"] == task["id"]
    assert pr["branch"] == "feat/sheet-renamed"
    assert pr["github_node_id"] == "PR_node_128"
    with pytest.raises(LedgerError) as raised:
        env.ledger.stamp(env.actors["verifier-1"], queued["queue_id"])
    assert raised.value.message == "Seal broken: head moved."
    with pytest.raises(LedgerError) as again:
        env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    assert again.value.message == "Seal broken: head moved."


def test_evidence_stale_ignores_case_and_space(env):
    open_card(env, head=SHA_A)
    env.ledger.add_evidence(
        env.actors["worker-1"],
        "acme/app",
        128,
        "screenshot",
        "file://bench/sheet.png",
        SHA_A,
        result="pass",
    )
    env.ledger.conn.execute(
        "UPDATE evidence SET commit_sha=?",
        (f"  {SHA_A.upper()}  ",),
    )
    stale = env.ledger.conn.execute("SELECT stale FROM evidence_v").fetchone()["stale"]
    assert stale == 0
    blank = env.ledger.conn.execute("UPDATE evidence SET commit_sha=''")
    assert blank.rowcount == 1
    stale = env.ledger.conn.execute("SELECT stale FROM evidence_v").fetchone()["stale"]
    assert stale == 1


def test_first_pr_sticks_and_stack_flags_rebase(env):
    parent = open_card(env, number=120, node="PR_node_120", head=SHA_A, title="Parent sheet")
    with pytest.raises(LedgerError) as raised:
        env.ledger.link_pr(env.actors["worker-1"], parent["id"], "acme/app", 121, node_id="PR_node_121")
    assert raised.value.status == 409
    assert raised.value.code == "pr_bound"
    env.ledger.apply_github_snapshot(snap(number=120, node="PR_node_120", head=SHA_A, state="merged", ci="passing", review="approved"))
    child = env.ledger.add_task(env.actors["eng-lead"], "Child sheet", repo="acme/app", depends_on=parent["id"])
    env.ledger.claim_task(env.actors["eng-lead"], child["id"], assignee="worker-1")
    env.ledger.link_pr(env.actors["worker-1"], child["id"], "acme/app", 121, node_id="PR_node_121")
    env.ledger.apply_github_snapshot(
        snap(number=121, node="PR_node_121", head=SHA_B, files=("ios/Login/Sheet.swift",))
    )
    env.ledger.apply_github_snapshot(
        snap(number=120, node="PR_node_120", head=SHA_A, state="merged", files=("ios/Login/Sheet.swift",), ci="passing", review="approved")
    )
    board = env.ledger.list_board(env.actors["eng-lead"])
    child_card = next(item for item in board["tasks"] if item["id"] == child["id"])
    assert child_card["depth"] == 1
    assert child_card["pr"]["needs_rebase"] is True
    assert child_card["depends_on"] == parent["id"]
    # Merged parent is not an open-PR overlap partner.
    assert board["overlaps"] == []
