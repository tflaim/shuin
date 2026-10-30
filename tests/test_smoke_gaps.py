"""Mac smoke gaps: verifier pass command, board voice, empty PR heads."""

import json
from pathlib import Path

import pytest

from shuin.cli import main
from shuin.errors import GhProblem, LedgerError
from shuin.ledger import open_ledger
from shuin.render import render_html
from tests.conftest import SHA_A, SHA_B, open_card, run_log, snap
from tests.test_gate import FakeGH, _check, _head


def _receipt(env, sha, reread="same", number=128, classification="success"):
    pr = env.ledger.pr_by_repo_number("acme/app", number)
    if reread == "same":
        reread = sha
    env.ledger.conn.execute(
        """INSERT INTO pr_acceptance(
             pr_id, task_id, head_sha, head_sha_reread, classification,
             required_checks_json, verifier_state, receipt_json, created_at
           ) VALUES (?, ?, ?, ?, ?, '[]', 'pass', '{}', ?)""",
        (pr["id"], pr["task_id"], sha, reread, classification, env.clock["t"]),
    )


def _reopen(env, when=None):
    moment = env.clock["t"] if when is None else when
    return open_ledger(env.db, clock=lambda: moment)


class _Pull:
    def __init__(self, sha):
        self.sha = sha

    def fetch_pull(self, repo, number):
        return snap(number=number, node=f"PR_node_{number}", head=self.sha)


class _Down:
    def fetch_pull(self, repo, number):
        raise GhProblem("infra")


def test_fallback_names_the_verify_command(env):
    open_card(env, head=SHA_A)
    body = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        FakeGH([_head(SHA_A), _head(SHA_A)], checks=()),
    )
    command = f"shuin verify --pr acme/app#128 --commit-sha {SHA_A} --result pass"
    assert body["ok"] is False
    assert body["classification"] == "missing"
    assert body["verifier_state"] == "missing"
    assert body["voice"] == f"Verifier pass is missing. {command}"
    assert "Required checks missing" not in body["voice"]


def test_a_missing_required_check_keeps_its_own_voice(env):
    open_card(env, head=SHA_A)
    body = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        FakeGH([_head(SHA_A), _head(SHA_A)], checks=(_check("missing"),)),
    )
    assert body["classification"] == "missing"
    assert body["voice"] == "Required checks missing. shuin seal --pr acme/app#128"
    assert "shuin verify" not in body["voice"]


def test_verify_pass_lets_the_fallback_seal_hold(env):
    open_card(env, head=SHA_A)
    run_log(env)
    recorded = env.ledger.verify(env.actors["verifier-1"], "acme/app", 128, SHA_A, "pass")
    assert recorded["voice"] == "Verifier pass."
    assert recorded["verifier_state"] == "pass"
    assert recorded["commit_sha"] == SHA_A
    assert recorded["run_id"] == env.ledger.conn.execute(
        "SELECT id FROM evidence WHERE uri='file://bench/128.log'"
    ).fetchone()["id"]
    assert recorded["run_at"] == env.clock["t"]
    body = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        FakeGH([_head(SHA_A), _head(SHA_A)], checks=()),
    )
    assert body["ok"] is True
    assert body["classification"] == "success"
    assert body["verifier_state"] == "pass"
    assert body["voice"] == "Seal holds."


def test_an_info_log_is_enough_for_a_pass(env):
    open_card(env, head=SHA_A)
    env.ledger.add_evidence(
        env.actors["verifier-1"],
        "acme/app",
        128,
        "test_log",
        "file://bench/info.log",
        SHA_A,
        result="info",
    )
    recorded = env.ledger.verify(env.actors["verifier-1"], "acme/app", 128, SHA_A, "pass")
    assert recorded["verifier_state"] == "pass"
    assert recorded["output_sha256"] is None


def test_verify_fail_refuses_the_fallback_seal(env):
    open_card(env, head=SHA_A)
    env.ledger.add_evidence(
        env.actors["verifier-1"],
        "acme/app",
        128,
        "test_log",
        "file://bench/fail.log",
        SHA_A,
        result="fail",
    )
    recorded = env.ledger.verify(env.actors["verifier-1"], "acme/app", 128, SHA_A, "fail")
    assert recorded["voice"] == "Verifier fail."
    assert recorded["verifier_state"] == "fail"
    body = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        FakeGH([_head(SHA_A), _head(SHA_A)], checks=()),
    )
    assert body["ok"] is False
    assert body["classification"] == "failure"
    assert body["verifier_state"] == "fail"
    assert "Verifier pass is missing" not in body["voice"]
    assert "Required checks missing" not in body["voice"]


def test_verify_needs_the_matching_run_proof(env):
    open_card(env, head=SHA_A)
    with pytest.raises(LedgerError) as missing:
        env.ledger.verify(env.actors["verifier-1"], "acme/app", 128, SHA_A, "pass")
    assert missing.value.code == "run_evidence_required"
    env.ledger.add_evidence(
        env.actors["verifier-1"],
        "acme/app",
        128,
        "test_log",
        "file://bench/fail.log",
        SHA_A,
        result="fail",
    )
    with pytest.raises(LedgerError) as still:
        env.ledger.verify(env.actors["verifier-1"], "acme/app", 128, SHA_A, "pass")
    assert still.value.code == "run_evidence_required"
    open_card(env, number=129, node="PR_node_129", head=SHA_A, title="Pass log only")
    run_log(env, number=129)
    with pytest.raises(LedgerError) as wrong_way:
        env.ledger.verify(env.actors["verifier-1"], "acme/app", 129, SHA_A, "fail")
    assert wrong_way.value.code == "run_evidence_required"
    with pytest.raises(LedgerError) as bad:
        env.ledger.verify(env.actors["verifier-1"], "acme/app", 128, SHA_A, "maybe")
    assert bad.value.code == "bad_result"


def test_verify_refuses_the_wrong_role_and_the_wrong_head(env):
    open_card(env, head=SHA_A)
    run_log(env)
    with pytest.raises(LedgerError) as worker:
        env.ledger.verify(env.actors["worker-1"], "acme/app", 128, SHA_A, "pass")
    assert worker.value.code == "role_mismatch"
    with pytest.raises(LedgerError) as moved:
        env.ledger.verify(env.actors["verifier-1"], "acme/app", 128, SHA_B, "pass")
    assert moved.value.code == "stale_head"
    assert moved.value.message == "Seal broken: head moved."
    bare = open_card(env, number=129, node="PR_node_129", title="No head yet")
    with pytest.raises(LedgerError) as unknown:
        env.ledger.verify(env.actors["verifier-1"], "acme/app", 129, SHA_A, "pass")
    assert unknown.value.code == "unknown_head"
    assert bare["id"]


def test_cli_verify(env, capsys):
    open_card(env, head=SHA_A)
    run_log(env)
    code = main(
        [
            "--db",
            str(env.db),
            "--token",
            env.tokens["verifier-1"],
            "--json",
            "verify",
            "--pr",
            "acme/app#128",
            "--commit-sha",
            SHA_A,
            "--result",
            "pass",
        ]
    )
    assert code == 0
    body = json.loads(capsys.readouterr().out)
    assert body["verifier_state"] == "pass"
    assert body["commit_sha"] == SHA_A
    assert body["voice"] == "Verifier pass."


def test_board_counts_stale_verification_and_skips_merged(env):
    open_card(env, head=SHA_A)
    open_card(env, number=129, node="PR_node_129", head=SHA_A, title="Second sheet")
    open_card(env, number=130, node="PR_node_130", head=SHA_A, title="Merged sheet")
    for number in (128, 129, 130):
        env.ledger.add_evidence(
            env.actors["worker-1"],
            "acme/app",
            number,
            "screenshot",
            f"file://bench/{number}.png",
            SHA_B,
            result="info",
        )
    env.ledger.apply_github_snapshot(snap(number=130, node="PR_node_130", head=SHA_A, state="merged"))
    body = env.ledger.snapshot()
    assert body["voice"] == "Stale verification: 2."
    cards = {card["pr"]["number"]: card for card in body["tasks"]}
    assert cards[128]["stale"] is True
    assert cards[128]["seal_broken"] is False
    assert cards[128]["voice"] == "Stale verification."
    assert cards[129]["stale_verification"] is True
    assert cards[130]["stale"] is False
    assert cards[130]["seal_broken"] is False
    assert cards[130]["stale_verification"] is False
    page = render_html(body)
    assert "Stale verification: 2." in page
    assert "Seal broken: head moved." not in page


def test_seal_broken_only_when_a_receipt_head_moved(env):
    open_card(env, head=SHA_A)
    open_card(env, number=129, node="PR_node_129", head=SHA_A, title="Second sheet")
    env.ledger.add_evidence(
        env.actors["worker-1"],
        "acme/app",
        129,
        "screenshot",
        "file://bench/129.png",
        SHA_B,
        result="info",
    )
    _receipt(env, SHA_B, reread=SHA_B, number=128)
    body = env.ledger.snapshot()
    assert body["voice"] == "Seal broken: head moved."
    cards = {card["pr"]["number"]: card for card in body["tasks"]}
    assert cards[128]["seal_broken"] is True
    assert cards[128]["voice"] == "Seal broken: head moved."
    assert cards[129]["voice"] == "Stale verification."
    assert cards[129]["seal_broken"] is False


def test_a_merged_receipt_does_not_count_as_seal_broken(env):
    open_card(env, head=SHA_A)
    _receipt(env, SHA_A)
    env.ledger.apply_github_snapshot(snap(head=SHA_B, state="merged"))
    card = env.ledger.seal_status("acme/app", 128)
    assert card["stale"] is False
    assert card["seal_broken"] is False
    assert "Seal broken" not in card["voice"]
    assert "Seal broken" not in env.ledger.snapshot()["voice"]


def test_open_backfills_an_empty_head_from_the_receipt(env):
    open_card(env)
    assert env.ledger.pr_by_repo_number("acme/app", 128)["head_sha"] is None
    _receipt(env, SHA_A.upper(), reread=SHA_A.upper())
    first = _reopen(env)
    try:
        pr = first.pr_by_repo_number("acme/app", 128)
        assert pr["head_sha"] == SHA_A
        assert pr["head_epoch"] == 1
        assert pr["pending_head"] is None
        assert pr["head_seen_at"] == env.clock["t"]
    finally:
        first.close()
    second = _reopen(env, when=env.clock["t"] + 50)
    try:
        pr = second.pr_by_repo_number("acme/app", 128)
        assert pr["head_sha"] == SHA_A
        assert pr["head_epoch"] == 1
        assert pr["head_seen_at"] == env.clock["t"]
    finally:
        second.close()


def test_backfill_uses_the_reread_when_the_receipt_disagrees(env):
    open_card(env)
    _receipt(env, SHA_A, reread=SHA_B)
    ledger = _reopen(env)
    try:
        assert ledger.pr_by_repo_number("acme/app", 128)["head_sha"] == SHA_B
    finally:
        ledger.close()


def test_backfill_does_not_replace_a_head_or_invent_one(env):
    open_card(env, head=SHA_B)
    epoch = env.ledger.pr_by_repo_number("acme/app", 128)["head_epoch"]
    _receipt(env, SHA_A)
    ledger = _reopen(env)
    try:
        pr = ledger.pr_by_repo_number("acme/app", 128)
        assert pr["head_sha"] == SHA_B
        assert pr["head_epoch"] == epoch
    finally:
        ledger.close()

    bare = open_card(env, number=131, node="PR_node_131", title="No receipt")
    env.ledger.conn.execute(
        """INSERT INTO pr_acceptance(
             pr_id, task_id, head_sha, head_sha_reread, classification,
             required_checks_json, verifier_state, receipt_json, created_at
           ) VALUES (?, ?, 'nope', NULL, 'missing', '[]', 'missing', '{}', 1)""",
        (
            env.ledger.pr_by_repo_number("acme/app", 131)["id"],
            bare["id"],
        ),
    )
    again = _reopen(env)
    try:
        assert again.pr_by_repo_number("acme/app", 131)["head_sha"] is None
    finally:
        again.close()


def test_a_blank_head_is_filled_from_the_receipt(env):
    open_card(env, head=SHA_A)
    env.ledger.conn.execute("UPDATE prs SET head_sha='' WHERE number=128")
    _receipt(env, SHA_B)
    ledger = _reopen(env)
    try:
        assert ledger.pr_by_repo_number("acme/app", 128)["head_sha"] == SHA_B
    finally:
        ledger.close()


def test_poll_writes_an_empty_head_and_a_failed_poll_uses_the_receipt(env):
    open_card(env)
    open_card(env, number=129, node="PR_node_129", title="Live poll")
    _receipt(env, SHA_A, number=128)
    live = env.ledger.poll_pr(_Pull(SHA_B), "acme/app", 129)
    assert live["ok"] is True
    assert env.ledger.pr_by_repo_number("acme/app", 129)["head_sha"] == SHA_B

    missed = env.ledger.poll_pr(_Down(), "acme/app", 128)
    assert missed["ok"] is False
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    assert pr["head_sha"] == SHA_A
    epoch = pr["head_epoch"]
    again = env.ledger.poll_pr(_Down(), "acme/app", 128)
    assert again["ok"] is False
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    assert pr["head_sha"] == SHA_A
    assert pr["head_epoch"] == epoch


def test_a_live_poll_wins_over_an_older_receipt(env):
    open_card(env)
    _receipt(env, SHA_A)
    env.ledger.poll_pr(_Pull(SHA_B), "acme/app", 128)
    assert env.ledger.pr_by_repo_number("acme/app", 128)["head_sha"] == SHA_B


def test_poll_once_backfills_when_github_fails(env):
    open_card(env)
    _receipt(env, SHA_A)
    results = env.ledger.poll_once(_Down())
    assert results[0]["ok"] is False
    assert env.ledger.pr_by_repo_number("acme/app", 128)["head_sha"] == SHA_A
    epoch = env.ledger.pr_by_repo_number("acme/app", 128)["head_epoch"]
    env.ledger.poll_once(_Down())
    pr = env.ledger.pr_by_repo_number("acme/app", 128)
    assert pr["head_sha"] == SHA_A
    assert pr["head_epoch"] == epoch


def test_readme_documents_verify_and_the_dev_extra():
    text = Path("README.md").read_text(encoding="utf-8")
    assert "shuin verify --pr owner/repo#12 --commit-sha <head-sha> --result pass" in text
    assert "shuin verify --pr owner/repo#N --commit-sha SHA --result pass" in text
    assert "verifier pass is missing" in text
    assert '.venv/bin/pip install -e ".[dev]"' in text
    assert "The `dev` extra installs pytest into this virtualenv." in text
