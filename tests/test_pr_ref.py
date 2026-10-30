import json

import pytest

from shuin.cli import main
from shuin.errors import LedgerError
from tests.conftest import open_card


def test_bare_pr_number_when_the_board_has_one_repo(env, capsys):
    task = open_card(env)
    assert env.ledger.resolve_pr_ref("128") == ("acme/app", 128)
    assert env.ledger.resolve_pr_ref("#128") == ("acme/app", 128)
    assert env.ledger.resolve_pr_ref("  #128  ") == ("acme/app", 128)
    assert env.ledger.resolve_pr_ref("acme/app#128") == ("acme/app", 128)

    code = main(["--db", str(env.db), "seal-status", "--pr", "128"])
    assert code == 0
    assert capsys.readouterr().out.strip()
    code = main(["--db", str(env.db), "seal-status", "--pr", "#128"])
    assert code == 0
    assert capsys.readouterr().out.strip()

    code = main(
        [
            "--db",
            str(env.db),
            "--token",
            env.tokens["worker-1"],
            "--json",
            "pr",
            "link",
            task["id"],
            "--pr",
            "128",
        ]
    )
    assert code == 0
    linked = json.loads(capsys.readouterr().out)
    assert linked["repo"] == "acme/app"
    assert linked["number"] == 128

    other = env.ledger.add_task(env.actors["eng-lead"], "Elsewhere", repo="other/lib")
    with pytest.raises(LedgerError) as raised:
        env.ledger.resolve_pr_ref("128")
    assert raised.value.code == "bad_pr"
    assert "more than one repo" in raised.value.message
    assert env.ledger.resolve_pr_ref("acme/app#128") == ("acme/app", 128)
    code = main(["--db", str(env.db), "--json", "seal-status", "--pr", "#128"])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "bad_pr"
    assert "more than one repo" in payload["message"]
    code = main(["--db", str(env.db), "seal-status", "--pr", "other/lib#1"])
    assert code == 1
    assert "not on a card" in capsys.readouterr().err
    assert other["id"]

    help_code = main(["seal", "--help"])
    assert help_code == 0
    assert "bare number" in capsys.readouterr().out


def test_bare_pr_number_needs_a_repo(env, capsys):
    with pytest.raises(LedgerError) as raised:
        env.ledger.resolve_pr_ref("12")
    assert raised.value.code == "bad_pr"
    assert "No repo" in raised.value.message
    code = main(["--db", str(env.db), "--json", "seal-status", "--pr", "12"])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["error"] == "bad_pr"

    task = env.ledger.add_task(env.actors["eng-lead"], "Only a task", repo="acme/app")
    env.ledger.claim_task(env.actors["eng-lead"], task["id"], assignee="worker-1")
    code = main(
        [
            "--db",
            str(env.db),
            "--token",
            env.tokens["worker-1"],
            "--json",
            "pr",
            "link",
            task["id"],
            "--pr",
            "#15",
            "--node-id",
            "PR_node_15",
        ]
    )
    assert code == 0
    body = json.loads(capsys.readouterr().out)
    assert body["repo"] == "acme/app"
    assert body["number"] == 15
    with pytest.raises(LedgerError) as bad:
        env.ledger.resolve_pr_ref("#0")
    assert bad.value.code == "bad_pr"
    with pytest.raises(LedgerError):
        env.ledger.resolve_pr_ref("nope")
