import json
import sqlite3

from shuin.db import LATEST_VERSION
from shuin.github import GhClient
from shuin.ledger import open_ledger
from tests.conftest import SHA_A, open_card, run_log
from tests.test_gate import FakeGH, _check, _head


def _pr_payload(sha: str) -> str:
    return json.dumps(
        {
            "node_id": "PR_node_128",
            "merged": False,
            "state": "open",
            "head": {"ref": "feat/sheet", "sha": sha},
            "base": {"ref": "main"},
        }
    )


def test_no_required_checks_seal_on_a_local_verifier_pass(env):
    open_card(env, head=SHA_A)
    env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    slot = env.ledger.claim_slot(env.actors["verifier-1"])
    run_log(env)
    env.ledger.stamp(env.actors["verifier-1"], slot["queue_id"])
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
    assert body["checks"] == []


def test_no_required_checks_without_a_verifier_pass_stay_missing(env):
    open_card(env, head=SHA_A)
    body = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        FakeGH([_head(SHA_A), _head(SHA_A)], checks=()),
    )
    assert body["ok"] is False
    assert body["classification"] == "missing"
    assert body["verifier_state"] == "missing"
    assert body["voice"] == (
        "Verifier pass is missing. "
        f"shuin verify --pr acme/app#128 --commit-sha {SHA_A} --result pass"
    )
    assert "Required checks missing" not in body["voice"]


def test_protection_403_does_not_read_checks_and_seals_locally(env):
    open_card(env, head=SHA_A)
    env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    slot = env.ledger.claim_slot(env.actors["verifier-1"])
    run_log(env)
    env.ledger.stamp(env.actors["verifier-1"], slot["queue_id"])
    seen = []

    def runner(args):
        path = args[2]
        seen.append(path)
        if "/pulls/" in path:
            return 0, _pr_payload(SHA_A), ""
        if "/protection" in path or "rulesets" in path:
            return 1, "", "HTTP 403 Upgrade to GitHub Pro or make this repository public"
        if "check-runs" in path:
            return 0, '{"total_count":0,"check_runs":[]}', ""
        return 1, "", "404 Not Found"

    body = env.ledger.seal(env.actors["eng-lead"], "acme/app", 128, GhClient(runner))
    assert body["ok"] is True
    assert body["classification"] == "success"
    assert body["verifier_state"] == "pass"
    assert any("check-runs" in path for path in seen)

    def moved(args):
        path = args[2]
        if "/pulls/" in path:
            return 0, _pr_payload("b" * 40), ""
        if "check-runs" in path:
            return 0, '{"total_count":0,"check_runs":[]}', ""
        return 1, "", "403 Forbidden"

    bare = env.ledger.seal(env.actors["eng-lead"], "acme/app", 128, GhClient(moved))
    assert bare["ok"] is False
    assert bare["classification"] != "auth"
    assert bare["classification"] != "success"


def test_protection_403_without_a_local_pass_is_not_an_auth_failure(env):
    open_card(env, head=SHA_A)

    def runner(args):
        path = args[2]
        if "/pulls/" in path:
            return 0, _pr_payload(SHA_A), ""
        if "/protection" in path or "rulesets" in path:
            return 1, "", "403 Forbidden"
        return 1, "", "404 Not Found"

    body = env.ledger.seal(env.actors["eng-lead"], "acme/app", 128, GhClient(runner))
    assert body["ok"] is False
    assert body["classification"] == "missing"
    assert body["classification"] != "auth"
    assert body["voice"] == (
        "Verifier pass is missing. "
        f"shuin verify --pr acme/app#128 --commit-sha {SHA_A} --result pass"
    )
    assert "Required checks missing" not in body["voice"]


def _local_pass(env):
    open_card(env, head=SHA_A)
    env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    slot = env.ledger.claim_slot(env.actors["verifier-1"])
    run_log(env)
    env.ledger.stamp(env.actors["verifier-1"], slot["queue_id"])


def _failing_run():
    return {
        "id": 9,
        "name": "ci",
        "head_sha": SHA_A,
        "status": "completed",
        "conclusion": "failure",
    }


def _github(protection_error: str, runs: list) -> GhClient:
    def runner(args):
        path = args[2]
        if "/pulls/" in path:
            return 0, _pr_payload(SHA_A), ""
        if "/protection" in path or "rulesets" in path:
            return 1, "", protection_error
        if "check-runs" in path:
            return 0, json.dumps({"total_count": len(runs), "check_runs": runs}), ""
        return 1, "", "404 Not Found"

    return GhClient(runner)


def test_401_plus_a_failing_check_refuses(env):
    _local_pass(env)
    body = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        _github("HTTP 401 Bad credentials", [_failing_run()]),
    )
    assert body["ok"] is False
    assert body["classification"] == "auth"


def test_403_plus_a_failing_check_refuses(env):
    _local_pass(env)
    body = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        _github("HTTP 403 Upgrade to GitHub Pro or make this repository public", [_failing_run()]),
    )
    assert body["ok"] is False
    assert body["classification"] == "failure"
    assert body["checks"] == [
        {"context": "ci", "check_id": 9, "state": "failure", "source": "check_run"}
    ]


def test_403_with_no_failing_checks_and_a_local_pass_seals(env):
    _local_pass(env)
    body = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        _github(
            "HTTP 403 Forbidden",
            [
                {
                    "id": 4,
                    "name": "ci",
                    "head_sha": SHA_A,
                    "status": "completed",
                    "conclusion": "success",
                }
            ],
        ),
    )
    assert body["ok"] is True
    assert body["classification"] == "success"
    assert body["verifier_state"] == "pass"
    assert body["voice"] == "Seal holds."


def _split_github(*, protection: tuple[int, str, str], rulesets: tuple[int, str, str], runs: list) -> GhClient:
    def runner(args):
        path = args[2]
        if "/pulls/" in path:
            return 0, _pr_payload(SHA_A), ""
        if "/protection" in path:
            return protection
        if "rulesets" in path:
            return rulesets
        if "check-runs" in path:
            return 0, json.dumps({"total_count": len(runs), "check_runs": runs}), ""
        return 1, "", "404 Not Found"

    return GhClient(runner)


def test_protection_404_and_rulesets_403_refuse_a_failing_run(env):
    _local_pass(env)
    body = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        _split_github(
            protection=(1, "", "404 Not Found"),
            rulesets=(1, "", "HTTP 403 Forbidden"),
            runs=[_failing_run()],
        ),
    )
    assert body["ok"] is False
    assert body["classification"] == "failure"
    assert body["verifier_state"] == "pass"


def test_protection_404_and_no_rules_refuse_a_failing_run(env):
    _local_pass(env)
    body = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        _split_github(
            protection=(1, "", "404 Not Found"),
            rulesets=(0, "[]", ""),
            runs=[_failing_run()],
        ),
    )
    assert body["ok"] is False
    assert body["classification"] == "failure"
    assert body["verifier_state"] == "pass"


def test_a_failing_required_check_still_refuses(env):
    open_card(env, head=SHA_A)
    env.ledger.enqueue(env.actors["worker-1"], "acme/app", 128, SHA_A)
    slot = env.ledger.claim_slot(env.actors["verifier-1"])
    run_log(env)
    env.ledger.stamp(env.actors["verifier-1"], slot["queue_id"])
    body = env.ledger.seal(
        env.actors["eng-lead"],
        "acme/app",
        128,
        FakeGH([_head(SHA_A), _head(SHA_A)], checks=(_check("failure"),)),
    )
    assert body["ok"] is False
    assert body["classification"] == "failure"


def test_current_ledger_repairs_mac_state_and_a_missing_column(tmp_path):
    path = tmp_path / "ledger.db"
    ledger = open_ledger(path)
    ledger.close()

    conn = sqlite3.connect(path)
    try:
        conn.execute("ALTER TABLE pr_acceptance RENAME COLUMN verifier_state TO mac_state")
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        assert version == LATEST_VERSION
        conn.commit()
    finally:
        conn.close()

    ledger = open_ledger(path)
    try:
        columns = {row["name"] for row in ledger.conn.execute("PRAGMA table_info(pr_acceptance)")}
        assert "verifier_state" in columns
        assert "mac_state" not in columns
    finally:
        ledger.close()

    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("ALTER TABLE pr_acceptance DROP COLUMN verifier_state")
        conn.commit()
    finally:
        conn.close()

    ledger = open_ledger(path)
    try:
        columns = {row["name"] for row in ledger.conn.execute("PRAGMA table_info(pr_acceptance)")}
        assert "verifier_state" in columns
        token = ledger.mint_agent(None, "admin", "human", "owner")
        owner = ledger.authenticate(token)
        lead = ledger.authenticate(ledger.mint_agent(owner, "eng-lead", "lead", "lead"))
        ledger.conn.execute(
            """INSERT INTO tasks(id, title, status, created_at, updated_at)
               VALUES ('T-0001', 'Legacy card', 'active', 1, 1)"""
        )
        ledger.conn.execute(
            """INSERT INTO prs(task_id, repo, number, head_sha, state, base_branch, head_epoch, last_synced_at)
               VALUES ('T-0001', 'acme/app', 7, ?, 'open', 'main', 1, 1)""",
            (SHA_A,),
        )
        body = ledger.seal(lead, "acme/app", 7, FakeGH([_head(SHA_A), _head(SHA_A)], checks=()))
        assert body["classification"] == "missing"
        stored = ledger.conn.execute(
            "SELECT verifier_state FROM pr_acceptance ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert stored["verifier_state"] == "missing"
    finally:
        ledger.close()
