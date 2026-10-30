import json

import pytest

from shuin.cli import main
from shuin.errors import LedgerError
from shuin.httpapi import Api
from shuin.mcp import call_tool
from tests.conftest import SHA_A, open_card, snap


def _other_worker(env):
    token = env.ledger.mint_agent(env.actors["admin"], "worker-2", "codex", "worker")
    env.tokens["worker-2"] = token
    env.actors["worker-2"] = env.ledger.authenticate(token)
    return env.actors["worker-2"]


def test_owning_worker_can_attach_evidence_and_request_review(env):
    task = env.ledger.add_task(env.actors["worker-1"], "My card", repo="acme/app")
    env.ledger.claim_task(env.actors["worker-1"], task["id"])
    env.ledger.link_pr(env.actors["worker-1"], task["id"], "acme/app", 12, node_id="PR_node_12")
    evidence = env.ledger.add_evidence(
        env.actors["worker-1"],
        "acme/app",
        12,
        "screenshot",
        "https://example.com/sheet.png",
        SHA_A,
        result="info",
    )
    assert evidence["actor"] == "worker-1"
    review = env.ledger.request_review(env.actors["worker-1"], "acme/app", 12, "nothing")
    assert review["actor"] == "worker-1"
    status = env.ledger.conn.execute("SELECT status, owner_agent FROM tasks WHERE id=?", (task["id"],)).fetchone()
    assert status["owner_agent"] == "worker-1"
    assert status["status"] == "in_review"

    other = _other_worker(env)
    with pytest.raises(LedgerError) as evidence_denied:
        env.ledger.add_evidence(
            other,
            "acme/app",
            12,
            "film",
            "https://example.com/other.png",
            SHA_A,
        )
    with pytest.raises(LedgerError) as review_denied:
        env.ledger.request_review(other, "acme/app", 12, "nothing")
    assert evidence_denied.value.status == review_denied.value.status == 403
    assert evidence_denied.value.code == review_denied.value.code == "role_mismatch"

    # A lead can still do both on that card.
    lead_evidence = env.ledger.add_evidence(
        env.actors["eng-lead"],
        "acme/app",
        12,
        "build_log",
        "https://example.com/lead.log",
        SHA_A,
        result="pass",
    )
    assert lead_evidence["actor"] == "eng-lead"


def test_worker_cli_http_and_mcp_on_their_own_card(env, capsys):
    task = open_card(env, head=SHA_A)
    code = main(
        [
            "--db",
            str(env.db),
            "--token",
            env.tokens["worker-1"],
            "--json",
            "evidence",
            "add",
            "--pr",
            "acme/app#128",
            "--kind",
            "screenshot",
            "--uri",
            "https://example.com/cli.png",
            "--commit-sha",
            SHA_A,
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["actor"] == "worker-1"
    code = main(
        [
            "--db",
            str(env.db),
            "--token",
            env.tokens["worker-1"],
            "--json",
            "review",
            "request",
            "--pr",
            "acme/app#128",
            "--tried",
            "nothing",
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["actor"] == "worker-1"

    other = _other_worker(env)
    code = main(
        [
            "--db",
            str(env.db),
            "--token",
            env.tokens["worker-2"],
            "--json",
            "review",
            "request",
            "--pr",
            "acme/app#128",
            "--tried",
            "nothing",
        ]
    )
    assert code == 1
    assert json.loads(capsys.readouterr().out)["error"] == "role_mismatch"
    assert other["id"] == "worker-2"

    api = Api(env.db)
    headers = {"Authorization": f"Bearer {env.tokens['worker-1']}"}
    status, body, _ctype = api.handle(
        "POST",
        "/v1/evidence",
        headers,
        json.dumps(
            {
                "pr": {"repo": "acme/app", "number": 128},
                "kind": "build_log",
                "uri": "https://example.com/api.log",
                "commit_sha": SHA_A,
            }
        ).encode(),
    )
    assert status == 201
    stranger = {"Authorization": f"Bearer {env.tokens['worker-2']}"}
    status, body, _ctype = api.handle(
        "POST",
        "/v1/evidence",
        stranger,
        json.dumps(
            {
                "pr": {"repo": "acme/app", "number": 128},
                "kind": "build_log",
                "uri": "https://example.com/nope.log",
                "commit_sha": SHA_A,
            }
        ).encode(),
    )
    assert status == 403
    assert b"role_mismatch" in body

    text, is_error = call_tool(
        "add_evidence",
        {
            "repo": "acme/app",
            "number": 128,
            "kind": "film",
            "uri": "https://example.com/mcp.png",
            "commit_sha": SHA_A,
        },
        db_path=env.db,
        token=env.tokens["worker-1"],
    )
    assert is_error is False
    assert "worker-1" in text
    text, is_error = call_tool(
        "request_review",
        {"repo": "acme/app", "number": 128, "tried_failed": "still nothing"},
        db_path=env.db,
        token=env.tokens["worker-2"],
    )
    assert is_error is True
    assert "role_mismatch" in text

    help_code = main(["evidence", "--help"])
    assert help_code == 0
    assert "card they own" in capsys.readouterr().out
    help_code = main(["review", "--help"])
    assert help_code == 0
    assert "card they own" in capsys.readouterr().out


def test_unassigned_worker_cannot_take_a_card_by_attaching_evidence(env):
    task = env.ledger.add_task(env.actors["eng-lead"], "Unclaimed", repo="acme/app")
    env.ledger.link_pr(env.actors["eng-lead"], task["id"], "acme/app", 44, node_id="PR_node_44")
    env.ledger.post_update(env.actors["worker-1"], task["id"], "note", "looking")
    with pytest.raises(LedgerError) as evidence_denied:
        env.ledger.add_evidence(
            env.actors["worker-1"],
            "acme/app",
            44,
            "screenshot",
            "https://example.com/unclaimed.png",
            SHA_A,
            result="info",
        )
    assert evidence_denied.value.code == "role_mismatch"
    with pytest.raises(LedgerError) as review_denied:
        env.ledger.request_review(env.actors["worker-1"], "acme/app", 44, "looked")
    assert review_denied.value.code == "role_mismatch"
    owner = env.ledger.conn.execute("SELECT owner_agent, status FROM tasks WHERE id=?", (task["id"],)).fetchone()
    assert owner["owner_agent"] is None
    assert owner["status"] == "todo"


def test_add_evidence_on_a_blocked_card_does_not_grant_ownership(env):
    parent = env.ledger.add_task(env.actors["eng-lead"], "Parent", repo="acme/app")
    child = env.ledger.add_task(
        env.actors["eng-lead"],
        "Child",
        repo="acme/app",
        depends_on=parent["id"],
    )
    env.ledger.link_pr(env.actors["eng-lead"], child["id"], "acme/app", 46, node_id="PR_node_46")
    with pytest.raises(LedgerError) as claim:
        env.ledger.claim_task(env.actors["worker-1"], child["id"])
    assert claim.value.code == "waiting_dep"
    assert claim.value.message == f"Blocked by {parent['id']}."
    with pytest.raises(LedgerError) as evidence_denied:
        env.ledger.add_evidence(
            env.actors["worker-1"],
            "acme/app",
            46,
            "screenshot",
            "https://example.com/blocked.png",
            SHA_A,
            result="info",
        )
    assert evidence_denied.value.code == "role_mismatch"
    row = env.ledger.conn.execute(
        "SELECT owner_agent, status, blocked_reason FROM tasks WHERE id=?",
        (child["id"],),
    ).fetchone()
    assert row["owner_agent"] is None
    assert row["status"] == "blocked"
    assert row["blocked_reason"] == "waiting_dep"
    stored = env.ledger.conn.execute("SELECT COUNT(*) AS n FROM evidence").fetchone()["n"]
    assert stored == 0


def test_stale_caller_role_is_read_from_the_agent_row(env):
    open_card(env)
    stale = dict(env.actors["worker-1"])
    stale["role"] = "codex"
    evidence = env.ledger.add_evidence(
        stale,
        "acme/app",
        128,
        "screenshot",
        "https://example.com/stale.png",
        SHA_A,
        result="info",
    )
    assert evidence["actor"] == "worker-1"
    review = env.ledger.request_review(stale, "acme/app", 128, "still mine")
    assert review["actor"] == "worker-1"


def test_unassigned_worker_cannot_enqueue_an_unowned_card(env):
    task = env.ledger.add_task(env.actors["eng-lead"], "Queue me", repo="acme/app")
    env.ledger.link_pr(env.actors["eng-lead"], task["id"], "acme/app", 45, node_id="PR_node_45")
    env.ledger.apply_github_snapshot(snap(number=45, node="PR_node_45", head=SHA_A))
    with pytest.raises(LedgerError) as denied:
        env.ledger.enqueue(env.actors["worker-1"], "acme/app", 45, SHA_A)
    assert denied.value.code == "role_mismatch"
    owner = env.ledger.conn.execute("SELECT owner_agent FROM tasks WHERE id=?", (task["id"],)).fetchone()
    assert owner["owner_agent"] is None
