import json

from shuin.cli import main
from shuin.errors import LedgerError
import pytest

from tests.conftest import SHA_A, open_card
from tests.test_fresh_run import _pass_and_seal


def _status(env, task_id):
    return env.ledger.conn.execute(
        "SELECT status, owner_agent, closed_by FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()


def _kinds(env, task_id):
    return [
        row["kind"]
        for row in env.ledger.conn.execute(
            "SELECT kind FROM updates WHERE task_id=? ORDER BY id",
            (task_id,),
        )
    ]


def test_worker_task_done_records_done_without_a_seal_or_a_note(env):
    task = open_card(env, head=SHA_A)
    body = env.ledger.complete_task(env.actors["worker-1"], task["id"])
    assert body["status"] == "in_review"
    assert body["closed_by"] is None
    assert _status(env, task["id"])["status"] == "in_review"
    assert _kinds(env, task["id"]) == ["done"]
    notes = env.ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM updates WHERE task_id=? AND kind='note'",
        (task["id"],),
    ).fetchone()["n"]
    assert notes == 0


def test_unassigned_worker_can_mark_task_done(env):
    task = env.ledger.add_task(env.actors["eng-lead"], "Unclaimed", repo="acme/app")
    body = env.ledger.complete_task(env.actors["worker-1"], task["id"])
    assert body["status"] == "in_review"
    row = _status(env, task["id"])
    assert row["status"] == "in_review"
    assert row["owner_agent"] == "worker-1"
    assert row["closed_by"] is None
    assert _kinds(env, task["id"]) == ["done"]


def test_unassigned_worker_cannot_finish_a_card_claim_refuses(env):
    parent = env.ledger.add_task(env.actors["eng-lead"], "Parent", repo="acme/app")
    child = env.ledger.add_task(
        env.actors["eng-lead"],
        "Child",
        repo="acme/app",
        depends_on=parent["id"],
    )
    with pytest.raises(LedgerError) as claim:
        env.ledger.claim_task(env.actors["worker-1"], child["id"])
    assert claim.value.code == "waiting_dep"
    assert claim.value.message == f"Blocked by {parent['id']}."
    with pytest.raises(LedgerError) as done:
        env.ledger.complete_task(env.actors["worker-1"], child["id"])
    assert done.value.code == "waiting_dep"
    assert done.value.message == claim.value.message
    row = _status(env, child["id"])
    assert row["owner_agent"] is None
    assert row["status"] == "blocked"
    assert row["closed_by"] is None
    assert _kinds(env, child["id"]) == []


def test_cli_task_done_succeeds_for_the_owning_worker(env, capsys):
    task = open_card(env, head=SHA_A)
    code = main(
        [
            "--db",
            str(env.db),
            "--token",
            env.tokens["worker-1"],
            "--json",
            "task",
            "done",
            task["id"],
        ]
    )
    assert code == 0
    body = json.loads(capsys.readouterr().out)
    assert body["status"] == "in_review"
    assert body["closed_by"] is None
    assert _kinds(env, task["id"]) == ["done"]


def test_a_fresh_seal_still_closes_the_card(env):
    task = open_card(env, head=SHA_A)
    assert _pass_and_seal(env)["ok"] is True
    body = env.ledger.complete_task(env.actors["worker-1"], task["id"])
    assert body["status"] == "done"
    assert body["closed_by"] == "manual"


def test_lead_without_a_pr_still_closes(env):
    task = env.ledger.add_task(env.actors["eng-lead"], "No pull request", repo="acme/app")
    body = env.ledger.complete_task(env.actors["eng-lead"], task["id"])
    assert body["status"] == "done"
    assert body["closed_by"] == "manual"


def test_a_closed_pr_is_still_refused(env):
    task = open_card(env, head=SHA_A)
    env.ledger.conn.execute("UPDATE prs SET state='closed' WHERE number=128")
    with pytest.raises(LedgerError) as raised:
        env.ledger.complete_task(env.actors["worker-1"], task["id"])
    assert raised.value.code == "seal_refused"
    assert _status(env, task["id"])["status"] != "done"


def test_another_worker_cannot_mark_someone_elses_card_done(env):
    task = open_card(env, head=SHA_A)
    token = env.ledger.mint_agent(env.actors["admin"], "worker-2", "codex", "worker")
    other = env.ledger.authenticate(token)
    with pytest.raises(LedgerError) as raised:
        env.ledger.complete_task(other, task["id"])
    assert raised.value.code == "role_mismatch"
