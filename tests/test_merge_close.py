import pytest

from shuin.errors import LedgerError
from tests.conftest import SHA_A, open_card, snap
from tests.test_fresh_run import _pass_and_seal


def _status(env, task_id):
    return env.ledger.conn.execute(
        "SELECT status, blocked_reason, closed_by FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()


def test_merged_pr_with_a_fresh_seal_closes_the_task_and_advances_dependents(env):
    parent = open_card(env, head=SHA_A)
    assert _pass_and_seal(env)["ok"] is True
    child = env.ledger.add_task(
        env.actors["eng-lead"],
        "Child sheet",
        repo="acme/app",
        depends_on=parent["id"],
    )
    env.ledger.apply_github_snapshot(snap(head=SHA_A, state="merged"))
    assert _status(env, parent["id"])["status"] == "done"
    assert _status(env, parent["id"])["closed_by"] == "sealed_merge"
    child_row = _status(env, child["id"])
    assert child_row["status"] == "active"
    assert child_row["blocked_reason"] is None
    closed = env.ledger.conn.execute(
        "SELECT actor, enum_state FROM events WHERE type='task.closed'"
    ).fetchone()
    assert closed["actor"] == "system:merge"
    assert closed["enum_state"] == "done"
    advanced = env.ledger.conn.execute(
        "SELECT actor, enum_state, task_id FROM events WHERE type='task.advanced'"
    ).fetchone()
    assert advanced["actor"] == "system:merge"
    assert advanced["enum_state"] == "active"
    assert advanced["task_id"] == child["id"]

    env.ledger.apply_github_snapshot(snap(head=SHA_A, state="merged"))
    assert env.ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE type='task.closed'"
    ).fetchone()["n"] == 1


def test_merged_pr_without_a_fresh_seal_is_done_and_advances_dependents(env):
    task = open_card(env, head=SHA_A)
    child = env.ledger.add_task(
        env.actors["eng-lead"],
        "Still waiting",
        repo="acme/app",
        depends_on=task["id"],
    )
    env.ledger.apply_github_snapshot(snap(head=SHA_A, state="merged"))
    assert _status(env, task["id"])["status"] == "done"
    assert _status(env, task["id"])["closed_by"] == "merge"
    child_row = _status(env, child["id"])
    assert child_row["status"] == "active"
    assert child_row["blocked_reason"] is None
    assert env.ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE type='task.closed'"
    ).fetchone()["n"] == 1


def test_a_carried_pass_closes_as_merge_not_sealed_merge(env):
    task = open_card(env, head=SHA_A)
    assert _pass_and_seal(env)["ok"] is True
    env.ledger.apply_github_snapshot(snap(head="b" * 40))
    env.ledger.apply_github_snapshot(snap(head=SHA_A, state="merged"))
    row = _status(env, task["id"])
    assert row["status"] == "done"
    assert row["closed_by"] == "merge"


def test_closed_pr_does_not_finish_the_task(env):
    task = open_card(env, head=SHA_A)
    assert _pass_and_seal(env)["ok"] is True
    env.ledger.apply_github_snapshot(snap(head=SHA_A, state="closed"))
    assert _status(env, task["id"])["status"] != "done"


def test_poll_closes_a_pr_that_was_already_merged(env):
    task = open_card(env, head=SHA_A)
    env.ledger.conn.execute("UPDATE prs SET state='merged' WHERE number=128")
    child = env.ledger.add_task(
        env.actors["eng-lead"],
        "Waiting child",
        repo="acme/app",
        depends_on=task["id"],
    )
    class _Down:
        def fetch_pull(self, repo, number):
            raise RuntimeError("github down")

    env.ledger.poll_once(_Down())
    assert _status(env, task["id"])["status"] == "done"
    assert _status(env, task["id"])["closed_by"] == "merge"
    assert _status(env, child["id"])["status"] == "active"
    env.ledger.poll_once(_Down())
    assert env.ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE type='task.closed'"
    ).fetchone()["n"] == 1
    board = env.ledger.list_board(env.actors["eng-lead"])
    card = next(item for item in board["tasks"] if item["id"] == task["id"])
    assert card["closed_by"] == "merge"
    from shuin.cli import _format_board

    text = _format_board(board)
    assert "merge" in text
    assert task["id"] in text


def test_task_done_merged_externally(env):
    task = open_card(env, head=SHA_A)
    child = env.ledger.add_task(
        env.actors["eng-lead"],
        "Waiting child",
        repo="acme/app",
        depends_on=task["id"],
    )
    with pytest.raises(LedgerError) as worker:
        env.ledger.complete_task(
            env.actors["worker-1"],
            task["id"],
            merged_externally=True,
            reason="landed outside",
        )
    assert worker.value.code == "role_mismatch"
    with pytest.raises(LedgerError) as open_pr:
        env.ledger.complete_task(
            env.actors["eng-lead"],
            task["id"],
            merged_externally=True,
            reason="landed outside",
        )
    assert open_pr.value.code == "not_merged"
    env.ledger.conn.execute("UPDATE prs SET state='merged' WHERE number=128")
    with pytest.raises(LedgerError) as missing:
        env.ledger.complete_task(env.actors["eng-lead"], task["id"], merged_externally=True)
    assert missing.value.code == "reason_required"
    assert missing.value.message == "A reason is required."
    body = env.ledger.complete_task(
        env.actors["eng-lead"],
        task["id"],
        merged_externally=True,
        reason="landed outside",
    )
    assert body["status"] == "done"
    assert body["closed_by"] == "merge"
    assert body["reason"] == "landed outside"
    stored = env.ledger.conn.execute(
        "SELECT closed_by, closed_reason FROM tasks WHERE id=?",
        (task["id"],),
    ).fetchone()
    assert stored["closed_by"] == "merge"
    assert stored["closed_reason"] == "landed outside"
    summary = env.ledger.conn.execute(
        "SELECT summary FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task["id"],),
    ).fetchone()["summary"]
    assert summary == "landed outside"
    assert _status(env, child["id"])["status"] == "active"
    again = env.ledger.complete_task(env.actors["eng-lead"], task["id"], merged_externally=True)
    assert again["closed_by"] == "merge"
    assert again["reason"] == "landed outside"
    assert env.ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE type='task.closed'"
    ).fetchone()["n"] == 1


def test_owner_may_close_a_merged_card_with_a_reason(env):
    task = open_card(env, head=SHA_A)
    env.ledger.conn.execute("UPDATE prs SET state='merged' WHERE number=128")
    body = env.ledger.complete_task(
        env.actors["admin"],
        task["id"],
        merged_externally=True,
        reason="landed upstream",
    )
    assert body["status"] == "done"
    assert body["closed_by"] == "merge"
    assert body["reason"] == "landed upstream"


def test_manual_done_records_closed_by(env):
    task = open_card(env, head=SHA_A)
    assert _pass_and_seal(env)["ok"] is True
    body = env.ledger.complete_task(env.actors["eng-lead"], task["id"])
    assert body["closed_by"] == "manual"
    card = env.ledger.list_board(env.actors["eng-lead"])["tasks"][0]
    assert card["closed_by"] == "manual"


def test_dropped_task_stays_dropped_when_the_pr_merges(env):
    task = open_card(env, head=SHA_A)
    assert _pass_and_seal(env)["ok"] is True
    env.ledger.drop_task(env.actors["eng-lead"], task["id"])
    env.ledger.apply_github_snapshot(snap(head=SHA_A, state="merged"))
    assert _status(env, task["id"])["status"] == "dropped"


def test_poll_pr_closes_a_merged_pull_request(env):
    task = open_card(env, head=SHA_A)
    env.ledger.post_update(env.actors["worker-1"], task["id"], "done", "shipped")
    assert _status(env, task["id"])["status"] == "in_review"

    class _Merged:
        def fetch_pull(self, repo, number):
            return snap(state="merged")

    result = env.ledger.poll_pr(_Merged(), "acme/app", 128)
    assert result["ok"] is True
    assert _status(env, task["id"])["status"] == "done"
    assert _status(env, task["id"])["closed_by"] == "merge"


def test_poll_pr_closes_an_already_merged_card_when_github_is_down(env):
    task = open_card(env, head=SHA_A)
    env.ledger.conn.execute("UPDATE prs SET state='merged' WHERE number=128")

    class _Down:
        def fetch_pull(self, repo, number):
            raise RuntimeError("github down")

    result = env.ledger.poll_pr(_Down(), "acme/app", 128)
    assert result["ok"] is False
    assert result["error"] == "infra"
    assert _status(env, task["id"])["status"] == "done"
    assert _status(env, task["id"])["closed_by"] == "merge"
    env.ledger.poll_pr(_Down(), "acme/app", 128)
    assert env.ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE type='task.closed'"
    ).fetchone()["n"] == 1
