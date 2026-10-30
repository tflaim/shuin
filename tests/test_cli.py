import json

import pytest

from shuin.cli import main
from shuin.errors import LedgerError
from shuin.mcp import tool_specs
from shuin.sanitize import TRIED_FAILED_MAX
from tests.conftest import SHA_A, open_card, run_log


def test_cli_rejects_review_without_tried(env, capsys):
    open_card(env, head=SHA_A)
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
        ]
    )
    assert code == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.err or captured.out)
    assert payload["error"] == "tried_failed_required"


def test_update_help_and_new_kinds(env, capsys):
    code = main(["update", "--help"])
    assert code == 0
    help_text = capsys.readouterr().out
    assert "done" in help_text
    assert "tried-failed" in help_text
    task = open_card(env)
    for kind in ("done", "tried-failed"):
        code = main(
            [
                "--db",
                str(env.db),
                "--token",
                env.tokens["worker-1"],
                "--json",
                "update",
                task["id"],
                "--kind",
                kind,
                "--body",
                f"claim {kind}",
            ]
        )
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["actor"] == "worker-1"
    status = env.ledger.conn.execute("SELECT status FROM tasks WHERE id=?", (task["id"],)).fetchone()["status"]
    assert status == "in_review"
    tried = env.ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM tried_failed WHERE task_id=?",
        (task["id"],),
    ).fetchone()["n"]
    assert tried == 1
    spec = next(tool for tool in tool_specs() if tool["name"] == "post_update")
    assert spec["inputSchema"]["properties"]["kind"]["enum"] == [
        "progress",
        "question",
        "handoff",
        "note",
        "done",
        "tried-failed",
    ]
    with pytest.raises(LedgerError) as raised:
        env.ledger.post_update(env.actors["worker-1"], task["id"], "shipped", "no")
    assert raised.value.code == "bad_kind"


def test_done_update_leaves_a_verifying_card(env):
    task = open_card(env, head=SHA_A)
    env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    status = env.ledger.conn.execute("SELECT status FROM tasks WHERE id=?", (task["id"],)).fetchone()["status"]
    assert status == "verifying"
    env.ledger.post_update(env.actors["worker-1"], task["id"], "done", "shipped")
    status = env.ledger.conn.execute("SELECT status FROM tasks WHERE id=?", (task["id"],)).fetchone()["status"]
    assert status == "verifying"
    kind = env.ledger.conn.execute(
        "SELECT kind FROM updates WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task["id"],),
    ).fetchone()["kind"]
    assert kind == "done"


def test_review_tried_help_states_the_length_cap(env, capsys):
    code = main(["review", "request", "--help"])
    assert code == 0
    assert str(TRIED_FAILED_MAX) in capsys.readouterr().out
    open_card(env, head=SHA_A)
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
            "x" * (TRIED_FAILED_MAX + 1),
        ]
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "too_long"
    assert payload["message"] == f"Text is too long (max {TRIED_FAILED_MAX} chars, got {TRIED_FAILED_MAX + 1})."
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
            "y" * TRIED_FAILED_MAX,
        ]
    )
    assert code == 0
    review = next(tool for tool in tool_specs() if tool["name"] == "request_review")
    assert str(TRIED_FAILED_MAX) in review["description"]


def test_cli_stamp_and_seal_status(env, capsys):
    open_card(env, head=SHA_A)
    item = env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    env.ledger.claim_slot(env.actors["verifier-1"])
    run_log(env)
    # CLI opens its own ledger on the wall clock. The fixture clock is in 2023.
    env.ledger.conn.execute(
        "UPDATE verification_queue SET lease_expires_at=? WHERE id=?",
        (2_000_000_000, item["queue_id"]),
    )
    code = main(["--db", str(env.db), "--token", env.tokens["verifier-1"], "stamp", "--id", str(item["queue_id"])])
    assert code == 0
    assert capsys.readouterr().out.strip() == "Stamped. Not sealed yet: shuin seal --pr acme/app#128"
    code = main(["--db", str(env.db), "seal-status", "--pr", "acme/app#128"])
    assert code == 0
    # A verifier stamp alone is not a Seal: the Board waits for shuin seal.
    assert capsys.readouterr().out.strip() == "Seal is missing. shuin seal --pr acme/app#128"
    code = main(["--db", str(env.db), "queue"])
    assert "128" in capsys.readouterr().out or code == 0


def test_cli_require_run_and_merged_externally(env, capsys):
    task = open_card(env, head=SHA_A)
    code = main(
        [
            "--db",
            str(env.db),
            "--token",
            env.tokens["worker-1"],
            "--json",
            "queue",
            "enqueue",
            "--pr",
            "acme/app#128",
            "--commit-sha",
            SHA_A,
            "--require-run",
        ]
    )
    assert code == 0
    queued = json.loads(capsys.readouterr().out)
    assert queued["pass_kind"] == "fresh"
    code = main(
        [
            "--db",
            str(env.db),
            "--token",
            env.tokens["eng-lead"],
            "--json",
            "queue",
        ]
    )
    assert code == 0
    listed = json.loads(capsys.readouterr().out)["queue"]
    assert listed[0]["pass_kind"] == "fresh"
    assert listed[0]["state"] == "queued"
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
            "--merged-externally",
            "--reason",
            "landed outside",
        ]
    )
    assert code == 1
    refused = capsys.readouterr()
    assert json.loads(refused.err or refused.out)["error"] == "role_mismatch"
    env.ledger.conn.execute("UPDATE prs SET state='merged' WHERE number=128")
    code = main(
        [
            "--db",
            str(env.db),
            "--token",
            env.tokens["eng-lead"],
            "--json",
            "task",
            "done",
            task["id"],
            "--merged-externally",
        ]
    )
    assert code == 1
    missing = capsys.readouterr()
    assert json.loads(missing.err or missing.out)["error"] == "reason_required"
    code = main(
        [
            "--db",
            str(env.db),
            "--token",
            env.tokens["eng-lead"],
            "--json",
            "task",
            "done",
            task["id"],
            "--merged-externally",
            "--reason",
            "landed outside",
        ]
    )
    assert code == 0
    body = json.loads(capsys.readouterr().out)
    assert body["closed_by"] == "merge"
    assert body["status"] == "done"
    assert body["reason"] == "landed outside"
