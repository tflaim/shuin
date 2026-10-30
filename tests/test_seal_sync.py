"""Seal syncs GitHub state before it gates, and every refusal names a next command."""

from pathlib import Path

import pytest

from shuin.errors import LedgerError
from shuin.seal import refusal, seal_command, task_drop_command, task_merged_command
from shuin.types import HeadRead
from tests.conftest import SHA_A, SHA_B, open_card, run_log
from tests.test_gate import FakeGH, _check, _head

# The head both reads returned for example/repo#135.
HEAD = "39d70c0" + "0" * 33


def _blank(env, number=128):
    env.ledger.conn.execute(
        "UPDATE prs SET state=NULL, last_synced_at=NULL WHERE number=?",
        (number,),
    )


def _verify_pass(env, sha=SHA_A, number=128):
    run_log(env, sha, number)
    return env.ledger.verify(env.actors["verifier-1"], "acme/app", number, sha, "pass")


def _seal(env, github, number=128):
    return env.ledger.seal(env.actors["eng-lead"], "acme/app", number, github)


def _named(voice: str) -> None:
    assert voice != "Seal refused."
    assert "Seal refused." not in voice
    reason, sep, command = voice.partition(". ")
    assert sep == ". "
    assert reason
    assert command.startswith(("shuin ", "gh "))


def test_merged_pr_with_an_empty_cache_names_the_reason(env):
    """The #135 shape: empty state, empty last_synced_at, verifier pass, merged upstream."""
    task = open_card(env, number=135, node="PR_node_135", head=HEAD, title="Example sheet")
    recorded = _verify_pass(env, HEAD, 135)
    assert recorded["verifier_state"] == "pass"
    _blank(env, 135)
    cached = env.ledger.pr_by_repo_number("acme/app", 135)
    assert cached["state"] is None
    assert cached["last_synced_at"] is None
    assert cached["head_sha"] == HEAD

    body = _seal(
        env,
        FakeGH([_head(HEAD, state="merged"), _head(HEAD, state="merged")], checks=()),
        135,
    )
    _named(body["voice"])
    assert body["ok"] is False
    assert body["classification"] == "failure"
    assert body["verifier_state"] == "pass"
    assert body["head_sha"] == HEAD
    assert body["head_sha_reread"] == HEAD
    assert body["checks"] == []
    assert body["voice"] == "PR is already merged. shuin poll"
    synced = env.ledger.pr_by_repo_number("acme/app", 135)
    assert synced["state"] == "merged"
    assert synced["head_sha"] == HEAD
    assert synced["last_synced_at"] == env.clock["t"]
    status = env.ledger.conn.execute(
        "SELECT status, closed_by FROM tasks WHERE id=?",
        (task["id"],),
    ).fetchone()
    assert status["status"] != "done"
    assert status["closed_by"] is None


def test_unsynced_open_pr_seals_on_the_local_fallback(env):
    open_card(env, head=SHA_A)
    _verify_pass(env)
    _blank(env)
    body = _seal(env, FakeGH([_head(SHA_A), _head(SHA_A)], checks=()))
    assert body["ok"] is True
    assert body["classification"] == "success"
    assert body["verifier_state"] == "pass"
    assert body["voice"] == "Seal holds."
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    assert pr["state"] == "open"
    assert pr["head_sha"] == SHA_A
    assert pr["last_synced_at"] == env.clock["t"]


def test_a_closed_cache_follows_an_open_github_head(env):
    open_card(env, head=SHA_A)
    _verify_pass(env)
    env.ledger.conn.execute("UPDATE prs SET state='closed' WHERE number=128")
    body = _seal(env, FakeGH([_head(SHA_A), _head(SHA_A)], checks=()))
    assert body["ok"] is True
    assert body["classification"] == "success"
    assert body["voice"] == "Seal holds."
    assert env.ledger.pr_by_repo_number("acme/app", 128)["state"] == "open"


def test_a_failing_check_on_a_merged_pr_still_names_the_merge(env):
    open_card(env, head=SHA_A)
    _verify_pass(env)
    body = _seal(
        env,
        FakeGH(
            [_head(SHA_A, state="merged"), _head(SHA_A, state="merged")],
            checks=(_check("failure"),),
        ),
    )
    _named(body["voice"])
    assert body["ok"] is False
    assert body["classification"] == "failure"
    assert body["voice"] == "PR is already merged. shuin poll"
    assert body["checks"][0]["state"] == "failure"
    assert env.ledger.pr_by_repo_number("acme/app", 128)["state"] == "merged"


def test_each_seal_refusal_names_a_reason_and_a_next_command(env):
    again = seal_command("acme/app", 128)
    verify = f"shuin verify --pr acme/app#128 --commit-sha {SHA_A} --result pass"

    open_card(env, head=SHA_A)
    auth = _seal(env, FakeGH([HeadRead(error="auth"), HeadRead(error="auth")]))
    assert auth["voice"] == refusal("GitHub auth failed.", "gh auth login")
    assert auth["classification"] == "auth"

    infra = _seal(env, FakeGH([_head(SHA_A), _head(SHA_A)], check_error="infra"))
    assert infra["voice"] == refusal("GitHub read failed.", again)
    assert infra["classification"] == "infra"

    stale = _seal(env, FakeGH([_head(SHA_A), _head(SHA_B)], checks=(_check("success"),)))
    assert stale["voice"] == refusal("Seal broken: head moved.", again)
    assert stale["classification"] == "stale"

    pending = _seal(env, FakeGH([_head(SHA_A), _head(SHA_A)], checks=(_check("pending"),)))
    assert pending["voice"] == refusal("Checks still running.", again)
    assert pending["classification"] == "pending"

    failed = _seal(env, FakeGH([_head(SHA_A), _head(SHA_A)], checks=(_check("failure"),)))
    assert failed["voice"] == refusal("Required checks failed.", again)
    assert failed["classification"] == "failure"

    skipped = _seal(env, FakeGH([_head(SHA_A), _head(SHA_A)], checks=(_check("skipped"),)))
    assert skipped["voice"] == refusal("Required checks failed.", again)

    neutral = _seal(env, FakeGH([_head(SHA_A), _head(SHA_A)], checks=(_check("neutral"),)))
    assert neutral["voice"] == refusal("Required checks failed.", again)

    missing_check = _seal(env, FakeGH([_head(SHA_A), _head(SHA_A)], checks=(_check("missing"),)))
    assert missing_check["voice"] == refusal("Required checks missing.", again)
    assert "shuin verify" not in missing_check["voice"]

    missing_verifier = _seal(env, FakeGH([_head(SHA_A), _head(SHA_A)], checks=()))
    assert missing_verifier["voice"] == f"Verifier pass is missing. {verify}"
    assert missing_verifier["classification"] == "missing"

    env.ledger.add_evidence(
        env.actors["verifier-1"],
        "acme/app",
        128,
        "test_log",
        "file://bench/fail.log",
        SHA_A,
        result="fail",
    )
    env.ledger.verify(env.actors["verifier-1"], "acme/app", 128, SHA_A, "fail")
    verifier_fail = _seal(env, FakeGH([_head(SHA_A), _head(SHA_A)], checks=()))
    assert verifier_fail["voice"] == refusal("Verifier recorded a fail.", verify)
    assert verifier_fail["classification"] == "failure"
    assert verifier_fail["verifier_state"] == "fail"

    merged = _seal(
        env,
        FakeGH([_head(SHA_A, state="merged"), _head(SHA_A, state="merged")], checks=()),
    )
    assert merged["voice"] == refusal("PR is already merged.", "shuin poll")
    assert merged["classification"] == "failure"

    closed = _seal(
        env,
        FakeGH([_head(SHA_A, state="closed"), _head(SHA_A, state="closed")], checks=()),
    )
    assert closed["voice"] == refusal("PR is closed.", "shuin poll")
    assert closed["classification"] == "failure"

    _verify_pass(env)
    _blank(env)
    unknown = HeadRead(SHA_A, "feat/sheet", "main", "PR_node_128", None)
    unsynced = _seal(env, FakeGH([unknown, unknown], checks=()))
    assert unsynced["voice"] == refusal("PR has not been synced.", "shuin poll")
    assert unsynced["classification"] == "missing"
    assert unsynced["verifier_state"] == "pass"
    assert env.ledger.pr_by_repo_number("acme/app", 128)["last_synced_at"] is None

    disagreed = _seal(
        env,
        FakeGH([_head(SHA_A, state="open"), _head(SHA_A, state="merged")], checks=()),
    )
    assert disagreed["voice"] == refusal("Seal broken: head moved.", again)
    assert disagreed["classification"] == "stale"

    for body in (
        auth,
        infra,
        stale,
        pending,
        failed,
        skipped,
        neutral,
        missing_check,
        missing_verifier,
        verifier_fail,
        merged,
        closed,
        unsynced,
        disagreed,
    ):
        _named(body["voice"])


def test_task_done_refusals_name_the_next_command(env):
    closed = open_card(env, head=SHA_A)
    env.ledger.conn.execute("UPDATE prs SET state='closed' WHERE number=128")
    with pytest.raises(LedgerError) as closed_err:
        env.ledger.complete_task(env.actors["eng-lead"], closed["id"])
    assert closed_err.value.code == "seal_refused"
    assert closed_err.value.message == task_drop_command(closed["id"])
    _named(closed_err.value.message)

    merged = open_card(env, number=129, node="PR_node_129", head=SHA_A, title="Merged sheet")
    env.ledger.conn.execute("UPDATE prs SET state='merged' WHERE number=129")
    with pytest.raises(LedgerError) as merged_err:
        env.ledger.complete_task(env.actors["eng-lead"], merged["id"])
    assert merged_err.value.code == "seal_refused"
    assert merged_err.value.message == task_merged_command(merged["id"])
    _named(merged_err.value.message)


def test_seal_verdict_names_each_reason(env):
    task = open_card(env, head=SHA_A)
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    missing = env.ledger._seal_verdict(pr)
    assert missing.holds is False
    assert missing.code == "seal_refused"
    assert missing.voice == refusal("Seal is missing.", seal_command("acme/app", 128))
    _named(missing.voice)

    _blank(env)
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    unsynced = env.ledger._seal_verdict(pr)
    assert unsynced.holds is False
    assert unsynced.voice == refusal("PR has not been synced.", "shuin poll")
    _named(unsynced.voice)

    env.ledger.conn.execute(
        "UPDATE prs SET state='closed', last_synced_at=1, head_sha=? WHERE number=128",
        (SHA_A,),
    )
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    closed = env.ledger._seal_verdict(pr)
    assert closed.holds is False
    assert closed.voice == refusal("PR is closed.", "shuin poll")
    _named(closed.voice)

    env.ledger.conn.execute("UPDATE prs SET state='merged' WHERE number=128")
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    merged = env.ledger._seal_verdict(pr)
    assert merged.holds is False
    assert merged.voice == refusal("PR is already merged.", "shuin poll")
    _named(merged.voice)

    env.ledger.conn.execute(
        "UPDATE prs SET state='open', head_sha=?, last_synced_at=1 WHERE number=128",
        (SHA_A,),
    )
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    env.ledger.conn.execute(
        """INSERT INTO pr_acceptance(
             pr_id, task_id, head_sha, head_sha_reread, classification,
             required_checks_json, verifier_state, receipt_json, created_at
           ) VALUES (?, ?, ?, ?, 'success', '[]', 'pass', '{}', 1)""",
        (pr["id"], task["id"], SHA_B, SHA_B),
    )
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    moved = env.ledger._seal_verdict(pr)
    assert moved.holds is False
    assert moved.code == "stale_head"
    assert moved.voice == refusal("Seal broken: head moved.", "shuin poll")
    _named(moved.voice)


def test_source_has_no_bare_seal_refusal():
    root = Path("shuin")
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "Seal refused." not in text, path


def test_readme_records_the_merged_pr_decision():
    text = Path("README.md").read_text(encoding="utf-8")
    assert "An already-merged pull request is refused." in text
    assert "sealed_merge" in text
    assert "PR is already merged. shuin poll" in text
    assert "the local fallback seals an open pull request" in text
    assert "Every seal refusal names the reason and the next command." in text
