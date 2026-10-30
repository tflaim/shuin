import json
import warnings

import pytest

from shuin.cli import _format_board, main
from shuin.errors import LedgerError
from shuin.render import render_html, write_board
from tests.conftest import SHA_A, open_card, run_log, snap


class _GitHub:
    def __init__(self):
        self.states = {}
        self.fetched = []

    def fetch_pull(self, repo, number):
        self.fetched.append(number)
        return snap(
            number=number,
            node=f"PR_node_{number}",
            head=SHA_A,
            state=self.states.get(number, "open"),
        )


def _ids(body):
    return [task["id"] for task in body["tasks"]]


def test_claim_refuses_until_poll_sees_the_dependency_merge(env, capsys):
    parent = open_card(env, head=SHA_A)
    child = env.ledger.add_task(
        env.actors["eng-lead"],
        "Wait for the parent",
        repo="acme/app",
        depends_on=parent["id"],
    )
    assert child["status"] == "blocked"
    assert child["depends_on"] == parent["id"]
    with pytest.raises(LedgerError) as blocked:
        env.ledger.claim_task(env.actors["worker-1"], child["id"])
    assert blocked.value.code == "waiting_dep"
    assert parent["id"] in blocked.value.message
    assert child["id"] not in _ids(env.ledger.ready_tasks(env.actors["worker-1"]))

    code = main(
        ["--db", str(env.db), "--token", env.tokens["worker-1"], "--json", "task", "claim", child["id"]]
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "waiting_dep"
    assert parent["id"] in payload["message"]

    github = _GitHub()
    github.states[128] = "merged"
    env.ledger.poll_once(github)
    ready = env.ledger.ready_tasks(env.actors["eng-lead"])
    assert child["id"] in _ids(ready)
    claimed = env.ledger.claim_task(env.actors["worker-1"], child["id"])
    assert claimed["status"] == "active"
    assert child["id"] not in _ids(env.ledger.ready_tasks(env.actors["worker-1"]))

    code = main(
        ["--db", str(env.db), "--token", env.tokens["eng-lead"], "task", "ready"]
    )
    assert code == 0
    assert "No tasks ready." in capsys.readouterr().out


def test_pr_dependency_blocks_claim_until_poll_merges_it(env):
    parent = open_card(env, head=SHA_A)
    child = env.ledger.add_task(env.actors["eng-lead"], "Wait on two things", repo="acme/app")
    env.ledger.depend(env.actors["eng-lead"], child["id"], depends_on=parent["id"], depends_on_pr="55")
    with pytest.raises(LedgerError) as blocked:
        env.ledger.claim_task(env.actors["worker-1"], child["id"])
    assert blocked.value.code == "waiting_dep"
    assert parent["id"] in blocked.value.message
    assert "acme/app#55" in blocked.value.message
    assert child["id"] not in _ids(env.ledger.ready_tasks(env.actors["worker-1"]))

    github = _GitHub()
    github.states[128] = "merged"
    env.ledger.poll_once(github)
    with pytest.raises(LedgerError) as still:
        env.ledger.claim_task(env.actors["worker-1"], child["id"])
    assert "acme/app#55" in still.value.message
    assert parent["id"] not in still.value.message
    assert child["id"] not in _ids(env.ledger.ready_tasks(env.actors["worker-1"]))

    github.states[55] = "merged"
    env.ledger.poll_once(github)
    assert child["id"] in _ids(env.ledger.ready_tasks(env.actors["worker-1"]))
    assert env.ledger.claim_task(env.actors["worker-1"], child["id"])["status"] == "active"


def test_unlinked_pr_dependency_and_a_bare_number(env, capsys):
    task = env.ledger.add_task(
        env.actors["eng-lead"],
        "Wait for a pull request",
        repo="acme/app",
        depends_on_pr="#9",
    )
    assert task["status"] == "blocked"
    assert task["dependencies"] == [
        {"kind": "pr", "repo": "acme/app", "number": 9, "state": "open", "open": True}
    ]
    code = main(
        [
            "--db",
            str(env.db),
            "--token",
            env.tokens["eng-lead"],
            "--json",
            "task",
            "depend",
            task["id"],
            "--pr",
            "9",
        ]
    )
    assert code == 0
    again = json.loads(capsys.readouterr().out)
    assert len(again["dependencies"]) == 1

    github = _GitHub()
    github.states[9] = "merged"
    env.ledger.poll_once(github)
    ready = env.ledger.ready_tasks(env.actors["worker-1"])
    assert task["id"] in _ids(ready)
    assert ready["tasks"][0]["dependencies"][0]["open"] is False
    assert ready["tasks"][0]["dependencies"][0]["state"] == "merged"


def test_dependency_cycles_are_refused(env):
    first = env.ledger.add_task(env.actors["eng-lead"], "First", repo="acme/app")
    second = env.ledger.add_task(
        env.actors["eng-lead"],
        "Second",
        repo="acme/app",
        depends_on=first["id"],
    )
    with pytest.raises(LedgerError) as cycle:
        env.ledger.depend(env.actors["eng-lead"], first["id"], depends_on=second["id"])
    assert cycle.value.code == "cycle"
    assert first["id"] in cycle.value.message
    assert second["id"] in cycle.value.message
    with pytest.raises(LedgerError) as self_cycle:
        env.ledger.depend(env.actors["eng-lead"], first["id"], depends_on=first["id"])
    assert self_cycle.value.code == "cycle"

    third = env.ledger.add_task(env.actors["eng-lead"], "Third", repo="acme/app", depends_on=second["id"])
    with pytest.raises(LedgerError) as long_cycle:
        env.ledger.depend(env.actors["eng-lead"], first["id"], depends_on=third["id"])
    assert long_cycle.value.code == "cycle"

    card = open_card(env, head=SHA_A)
    waiting = env.ledger.add_task(env.actors["eng-lead"], "Waits on the PR", repo="acme/app", depends_on_pr="acme/app#128")
    with pytest.raises(LedgerError) as pr_cycle:
        env.ledger.depend(env.actors["eng-lead"], card["id"], depends_on=waiting["id"])
    assert pr_cycle.value.code == "cycle"
    with pytest.raises(LedgerError) as own_pr:
        env.ledger.depend(env.actors["worker-1"], card["id"], depends_on_pr="128")
    assert own_pr.value.code == "cycle"


def test_dependencies_show_on_the_board(env, tmp_path):
    parent = env.ledger.add_task(env.actors["eng-lead"], "Parent", repo="acme/app")
    child = env.ledger.add_task(
        env.actors["eng-lead"],
        "Child",
        repo="acme/app",
        depends_on=parent["id"],
        depends_on_pr="acme/app#12",
    )
    board = env.ledger.list_board(env.actors["eng-lead"])
    card = next(item for item in board["tasks"] if item["id"] == child["id"])
    assert card["depends_on"] == parent["id"]
    assert card["dependencies"] == [
        {"kind": "task", "id": parent["id"], "state": "open", "open": True},
        {"kind": "pr", "repo": "acme/app", "number": 12, "state": "open", "open": True},
    ]
    text = _format_board(board)
    assert f"deps {parent['id']} open, acme/app#12 open" in text
    page = render_html(board)
    assert f"deps {parent['id']} open, acme/app#12 open" in page
    _html_path, json_path = write_board(env.ledger, tmp_path)
    public = json.loads(json_path.read_text(encoding="utf-8"))
    stored = next(item for item in public["tasks"] if item["id"] == child["id"])
    assert stored["dependencies"][1]["repo"] == "acme/app"
    assert stored["dependencies"][1]["number"] == 12


def test_a_done_task_releases_a_waiting_card(env):
    parent = env.ledger.add_task(env.actors["eng-lead"], "Parent", repo="acme/app")
    child = env.ledger.add_task(env.actors["eng-lead"], "Child", repo="acme/app", depends_on=parent["id"])
    assert child["id"] not in _ids(env.ledger.ready_tasks(env.actors["worker-1"]))
    with pytest.raises(LedgerError) as dropped:
        env.ledger.drop_task(env.actors["eng-lead"], parent["id"])
    assert dropped.value.code == "has_children"
    env.ledger.complete_task(env.actors["eng-lead"], parent["id"])
    assert child["id"] in _ids(env.ledger.ready_tasks(env.actors["worker-1"]))


def test_blocked_for_a_person_cannot_be_claimed(env):
    task = env.ledger.add_task(env.actors["eng-lead"], "Needs a person", repo="acme/app")
    env.ledger.claim_task(env.actors["worker-1"], task["id"])
    env.ledger.set_blocked(env.actors["worker-1"], task["id"], "needs_human")
    with pytest.raises(LedgerError) as blocked:
        env.ledger.claim_task(env.actors["worker-1"], task["id"])
    assert blocked.value.code == "blocked"
    assert "needs_human" in blocked.value.message


def test_claim_model_sticks_to_later_rows_by_that_agent(env):
    env.ledger.conn.execute("UPDATE agents SET model=? WHERE id='admin'", ("gpt-5.4",))
    task = open_card(env, head=SHA_A)
    env.ledger.claim_task(env.actors["admin"], task["id"], model="composer-2.5")
    env.ledger.set_blocked(env.actors["admin"], task["id"], "needs_human")
    env.ledger.request_review(env.actors["admin"], "acme/app", 128, "nothing")
    env.ledger.resolve_blocked(env.actors["admin"], task["id"])
    queued = env.ledger.enqueue(env.actors["admin"], "acme/app", 128, SHA_A)
    env.ledger.claim_slot(env.actors["admin"])
    env.ledger.add_evidence(
        env.actors["verifier-1"],
        "acme/app",
        128,
        "test_log",
        "file://bench/128.log",
        SHA_A,
        result="pass",
    )
    env.ledger.stamp(env.actors["admin"], queued["queue_id"])
    rows = env.ledger.conn.execute(
        "SELECT agent_id, status, model FROM task_runs WHERE task_id=? AND agent_id='admin' ORDER BY attempt",
        (task["id"],),
    ).fetchall()
    assert [row["status"] for row in rows] == ["started", "blocked", "review_requested", "completed"]
    assert {row["model"] for row in rows} == {"composer-2.5"}

    other = env.ledger.add_task(env.actors["eng-lead"], "Verifier model", repo="acme/app")
    env.ledger.claim_task(env.actors["worker-1"], other["id"], model="composer-2.5")
    env.ledger.link_pr(env.actors["worker-1"], other["id"], "acme/app", 130, node_id="PR_node_130")
    env.ledger.apply_github_snapshot(snap(number=130, node="PR_node_130", head=SHA_A))
    env.ledger.conn.execute("UPDATE agents SET model=? WHERE id='verifier-1'", ("verifier-default",))
    queued = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 130, SHA_A)
    env.ledger.claim_slot(env.actors["verifier-1"])
    run_log(env, number=130)
    env.ledger.stamp(env.actors["verifier-1"], queued["queue_id"])
    verifier = env.ledger.conn.execute(
        "SELECT model FROM task_runs WHERE task_id=? AND agent_id='verifier-1'",
        (other["id"],),
    ).fetchone()
    assert verifier["model"] == "verifier-default"
    worker = env.ledger.conn.execute(
        "SELECT model FROM task_runs WHERE task_id=? AND status='started' AND agent_id='worker-1'",
        (other["id"],),
    ).fetchone()
    assert worker["model"] == "composer-2.5"


def test_closed_pr_is_visible_then_removed_and_claimable(env, capsys, tmp_path):
    task = env.ledger.add_task(
        env.actors["eng-lead"],
        "Wait for a pull request",
        repo="acme/app",
        depends_on_pr="acme/app#9",
    )
    github = _GitHub()
    github.states[9] = "closed"
    env.ledger.poll_once(github)

    board = env.ledger.list_board(env.actors["eng-lead"])
    card = next(item for item in board["tasks"] if item["id"] == task["id"])
    assert card["dependencies"] == [
        {"kind": "pr", "repo": "acme/app", "number": 9, "state": "closed", "open": True}
    ]
    text = _format_board(board)
    assert "deps acme/app#9 closed" in text
    page = render_html(board)
    assert "deps acme/app#9 closed" in page
    _html_path, json_path = write_board(env.ledger, tmp_path)
    public = json.loads(json_path.read_text(encoding="utf-8"))
    stored = next(item for item in public["tasks"] if item["id"] == task["id"])
    assert stored["dependencies"][0]["state"] == "closed"

    code = main(
        ["--db", str(env.db), "--token", env.tokens["worker-1"], "--json", "task", "claim", task["id"]]
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "waiting_dep"
    assert payload["message"] == (
        "Blocked by acme/app#9 (closed without merge; remove with task depend --remove)."
    )
    assert task["id"] not in _ids(env.ledger.ready_tasks(env.actors["worker-1"]))

    code = main(
        [
            "--db",
            str(env.db),
            "--token",
            env.tokens["worker-1"],
            "--json",
            "task",
            "depend",
            task["id"],
            "--remove",
            "--pr",
            "9",
        ]
    )
    assert code == 1
    denied = json.loads(capsys.readouterr().out)
    assert denied["error"] == "role_mismatch"
    assert task["id"] not in _ids(env.ledger.ready_tasks(env.actors["worker-1"]))

    code = main(
        [
            "--db",
            str(env.db),
            "--token",
            env.tokens["eng-lead"],
            "--json",
            "task",
            "depend",
            task["id"],
            "--remove",
            "--pr",
            "9",
        ]
    )
    assert code == 0
    removed = json.loads(capsys.readouterr().out)
    assert removed["status"] == "active"
    assert removed["dependencies"] == []

    code = main(
        ["--db", str(env.db), "--token", env.tokens["worker-1"], "task", "ready"]
    )
    assert code == 0
    assert task["id"] in capsys.readouterr().out
    claimed = env.ledger.claim_task(env.actors["worker-1"], task["id"])
    assert claimed["status"] == "active"
    assert task["id"] not in _ids(env.ledger.ready_tasks(env.actors["worker-1"]))


def test_unblock_clears_a_closed_pr_and_the_card_can_be_claimed(env, capsys):
    task = env.ledger.add_task(
        env.actors["eng-lead"],
        "Closed pull request",
        repo="acme/app",
        depends_on_pr="acme/app#9",
    )
    github = _GitHub()
    github.states[9] = "closed"
    env.ledger.poll_once(github)
    assert task["id"] not in _ids(env.ledger.ready_tasks(env.actors["worker-1"]))

    code = main(
        ["--db", str(env.db), "--token", env.tokens["eng-lead"], "--json", "task", "unblock", task["id"]]
    )
    assert code == 0
    body = json.loads(capsys.readouterr().out)
    assert body["status"] == "active"
    assert task["id"] in _ids(env.ledger.ready_tasks(env.actors["worker-1"]))
    assert env.ledger.claim_task(env.actors["worker-1"], task["id"])["status"] == "active"

    parent = env.ledger.add_task(env.actors["eng-lead"], "Still open", repo="acme/app")
    mixed = env.ledger.add_task(
        env.actors["eng-lead"],
        "Two blockers",
        repo="acme/app",
        depends_on=parent["id"],
        depends_on_pr="acme/app#11",
    )
    github.states[11] = "closed"
    env.ledger.poll_once(github)
    with pytest.raises(LedgerError) as stuck:
        env.ledger.resolve_blocked(env.actors["eng-lead"], mixed["id"])
    assert stuck.value.code == "waiting_dep"
    assert parent["id"] in stuck.value.message
    row = env.ledger.conn.execute("SELECT status, blocked_reason FROM tasks WHERE id=?", (mixed["id"],)).fetchone()
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "waiting_dep"
    assert mixed["id"] not in _ids(env.ledger.ready_tasks(env.actors["worker-1"]))


def test_poll_skips_dependency_prs_that_are_merged_or_closed(env):
    open_card(env, head=SHA_A)
    closed = env.ledger.add_task(
        env.actors["eng-lead"],
        "Closed dep",
        repo="acme/app",
        depends_on_pr="acme/app#9",
    )
    merged = env.ledger.add_task(
        env.actors["eng-lead"],
        "Merged dep",
        repo="acme/app",
        depends_on_pr="acme/app#10",
    )
    github = _GitHub()
    github.states[9] = "closed"
    github.states[10] = "merged"
    env.ledger.poll_once(github)
    assert {9, 10, 128} <= set(github.fetched)
    assert closed["id"] not in _ids(env.ledger.ready_tasks(env.actors["worker-1"]))
    assert merged["id"] in _ids(env.ledger.ready_tasks(env.actors["worker-1"]))

    github.fetched.clear()
    github.states[9] = "merged"
    env.ledger.poll_once(github)
    assert 128 in github.fetched
    assert 9 not in github.fetched
    assert 10 not in github.fetched
    assert closed["id"] not in _ids(env.ledger.ready_tasks(env.actors["worker-1"]))


def test_missing_dependency_task_is_resolved(env, tmp_path):
    parent = env.ledger.add_task(env.actors["eng-lead"], "Gone", repo="acme/app")
    child = env.ledger.add_task(
        env.actors["eng-lead"],
        "Child",
        repo="acme/app",
        depends_on=parent["id"],
    )
    other = env.ledger.add_task(
        env.actors["eng-lead"],
        "Other child",
        repo="acme/app",
        depends_on=parent["id"],
    )
    env.ledger.conn.execute("PRAGMA foreign_keys=OFF")
    env.ledger.conn.execute("DELETE FROM tasks WHERE id=?", (parent["id"],))
    env.ledger.conn.execute("PRAGMA foreign_keys=ON")

    with pytest.warns(RuntimeWarning, match=f"Dependency task {parent['id']} is missing"):
        board = env.ledger.list_board(env.actors["eng-lead"])
    card = next(item for item in board["tasks"] if item["id"] == child["id"])
    assert card["status"] == "active"
    assert card["dependencies"] == [
        {"kind": "task", "id": parent["id"], "state": "missing", "open": False}
    ]
    assert f"deps {parent['id']} missing" in _format_board(board)
    assert f"deps {parent['id']} missing" in render_html(board)
    _html_path, json_path = write_board(env.ledger, tmp_path)
    public = json.loads(json_path.read_text(encoding="utf-8"))
    stored = next(item for item in public["tasks"] if item["id"] == child["id"])
    assert stored["dependencies"][0]["state"] == "missing"
    assert child["id"] in _ids(env.ledger.ready_tasks(env.actors["worker-1"]))
    assert env.ledger.claim_task(env.actors["worker-1"], child["id"])["status"] == "active"

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        claimed = env.ledger.claim_task(env.actors["worker-1"], other["id"])
    assert claimed["status"] == "active"
