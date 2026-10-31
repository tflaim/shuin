import json

import pytest

from shuin.cli import main
from shuin.github import GhClient
from tests.conftest import SHA_A, SHA_B, open_card, snap


@pytest.fixture
def posts(monkeypatch):
    calls = []

    def runner(args):
        calls.append(args)
        return 0, "{}", ""

    monkeypatch.setattr("shuin.cli.GhClient", lambda: GhClient(runner))
    return calls


def invoke(env, capsys, command, *args, actor="verifier-1"):
    code = main([
        "--db", str(env.db), "--token", env.tokens[actor], "--json",
        "gauntlet", command, "--pr", "acme/app#128", "--commit-sha", SHA_A,
        "--run", "gauntlet-7", *args,
    ])
    captured = capsys.readouterr()
    return code, json.loads(captured.out) if captured.out else {"error": captured.err}


def test_start_posts_pending_on_the_cached_head(env, capsys, posts):
    open_card(env, head=SHA_A)
    code, body = invoke(env, capsys, "start")
    assert code == 0
    assert posts == [[
        "gh", "api", f"repos/acme/app/statuses/{SHA_A}", "--method", "POST",
        "-f", "state=pending", "-f", "context=shuin/gauntlet",
        "-f", "description=gauntlet-7: running",
    ]]
    assert body == {
        "sha": SHA_A, "state": "pending", "context": "shuin/gauntlet",
        "description": "gauntlet-7: running",
    }


@pytest.mark.parametrize("result,passed,failed,state,ledger_state,description", [
    ("pass", "41", "0", "success", "passed", "gauntlet-7: 41 passed, 0 failed"),
    ("fail", "40", "1", "failure", "failed", "gauntlet-7: 40 passed, 1 failed"),
])
def test_finish_posts_verdict_and_records_the_same_head(
    env, capsys, posts, result, passed, failed, state, ledger_state, description,
):
    open_card(env, head=SHA_A)
    env.ledger.add_evidence(
        env.actors["verifier-1"], "acme/app", 128, "test_log",
        "https://logs.example/run/7", SHA_A, result=result,
    )
    code, body = invoke(
        env, capsys, "finish", "--result", result, "--passed", passed,
        "--failed", failed, "--log-url", "https://logs.example/run/7",
    )
    assert code == 0
    assert posts == [[
        "gh", "api", f"repos/acme/app/statuses/{SHA_A}", "--method", "POST",
        "-f", f"state={state}", "-f", "context=shuin/gauntlet",
        "-f", f"description={description}",
        "-f", "target_url=https://logs.example/run/7",
    ]]
    assert body == {
        "sha": SHA_A, "state": state, "context": "shuin/gauntlet",
        "description": description, "target_url": "https://logs.example/run/7",
    }
    row = env.ledger.conn.execute(
        "SELECT commit_sha, state FROM verification_queue"
    ).fetchone()
    assert dict(row) == {"commit_sha": SHA_A, "state": ledger_state}


@pytest.mark.parametrize("command", ["start", "finish"])
def test_stale_head_refuses_before_posting(env, capsys, posts, command):
    open_card(env, head=SHA_A)
    args = ["--commit-sha", SHA_B]
    if command == "finish":
        args += ["--result", "pass", "--passed", "41", "--failed", "0"]
    code, body = invoke(env, capsys, command, *args)
    assert code == 1
    assert body["error"] == "stale_head"
    assert posts == []
    assert env.ledger.conn.execute("SELECT COUNT(*) FROM verification_queue").fetchone()[0] == 0


@pytest.mark.parametrize("command", ["start", "finish"])
@pytest.mark.parametrize("args", [
    ["--log-url", "https://u:p@x/y"],
    ["--log-url", "https://x/y?token=z"],
    ["--log-url", "http://logs.example/run/7"],
    ["--run", "secret token"],
    ["--run", "x" * 65],
    ["--commit-sha", "a" * 7],
])
def test_invalid_status_metadata_never_posts(env, capsys, posts, command, args):
    open_card(env, head=SHA_A)
    if command == "finish":
        args = ["--result", "pass", "--passed", "41", "--failed", "0", *args]
    code, body = invoke(env, capsys, command, *args)
    assert code == 1
    assert "error" in body
    assert posts == []
    assert env.ledger.conn.execute("SELECT COUNT(*) FROM verification_queue").fetchone()[0] == 0


@pytest.mark.parametrize("args", [
    ["--passed", "-1", "--failed", "0"],
    ["--passed", "41", "--failed", "-1"],
    ["--passed", "not-a-count", "--failed", "0"],
    ["--passed", "1" * 120, "--failed", "0"],
])
def test_finish_refuses_invalid_counts(env, capsys, posts, args):
    open_card(env, head=SHA_A)
    code, body = invoke(env, capsys, "finish", "--result", "pass", *args)
    assert code == 1
    assert "error" in body
    assert posts == []


@pytest.mark.parametrize("command", ["start", "finish"])
def test_only_a_verifier_can_post(env, capsys, posts, command):
    open_card(env, head=SHA_A)
    args = [] if command == "start" else ["--result", "pass", "--passed", "41", "--failed", "0"]
    code, body = invoke(env, capsys, command, *args, actor="worker-1")
    assert code == 1
    assert body["error"] == "role_mismatch"
    assert posts == []


def test_finish_requires_run_evidence_before_posting(env, capsys, posts):
    open_card(env, head=SHA_A)
    code, body = invoke(env, capsys, "finish", "--result", "pass", "--passed", "41", "--failed", "0")
    assert code == 1
    assert body["error"] == "run_evidence_required"
    assert posts == []


def github_runner(statuses, calls, head=SHA_A):
    def runner(args):
        calls.append(args)
        path = args[2]
        if path == "repos/acme/app/pulls/128":
            return 0, json.dumps({
                "node_id": "PR_node_128", "state": "open", "merged": False,
                "head": {"ref": "feat/sheet", "sha": head}, "base": {"ref": "main"},
            }), ""
        if "/protection" in path or "rulesets" in path:
            return 1, "", "404 Not Found"
        if path == f"repos/acme/app/commits/{head}/check-runs?per_page=100&page=1":
            return 0, '{"total_count":0,"check_runs":[]}', ""
        if path == f"repos/acme/app/commits/{head}/status":
            return 0, json.dumps({"sha": head, "statuses": statuses}), ""
        raise AssertionError(args)
    return runner


def test_seal_requires_gauntlet_success_and_a_verifier_pass(env):
    open_card(env, head=SHA_A)
    statuses, calls = [], []
    github = GhClient(github_runner(statuses, calls))
    env.ledger.add_evidence(
        env.actors["verifier-1"], "acme/app", 128, "test_log",
        "https://logs.example/run/7", SHA_A, result="pass",
    )
    env.ledger.verify(env.actors["verifier-1"], "acme/app", 128, SHA_A, "pass")
    missing = env.ledger.seal(env.actors["eng-lead"], "acme/app", 128, github)
    assert missing["ok"] is False
    assert missing["classification"] == "missing"
    for state in ("pending", "failure", "success"):
        statuses[:] = [{"id": 7, "sha": SHA_A, "context": "shuin/gauntlet", "state": state}]
        body = env.ledger.seal(env.actors["eng-lead"], "acme/app", 128, github)
        assert body["ok"] is (state == "success")
        assert body["classification"] == state
    assert body["voice"] == "Seal holds."
    env.ledger.conn.execute("DELETE FROM verification_queue")
    assert env.ledger.seal(env.actors["eng-lead"], "acme/app", 128, github)["ok"] is False


def test_new_head_does_not_inherit_the_old_status(env):
    open_card(env, head=SHA_A)
    old = [{"id": 7, "sha": SHA_A, "context": "shuin/gauntlet", "state": "success"}]
    calls = []
    first = GhClient(github_runner(old, calls)).read_checks("acme/app", "main", SHA_A)
    assert {item.context: item.state for item in first.checks} == {"shuin/gauntlet": "success"}
    env.ledger.apply_github_snapshot(snap(head=SHA_B))
    calls.clear()
    new = GhClient(github_runner(old, calls, head=SHA_B)).read_checks("acme/app", "main", SHA_B)
    assert {item.context: item.state for item in new.checks} == {"shuin/gauntlet": "missing"}
    assert ["gh", "api", f"repos/acme/app/commits/{SHA_B}/status"] in calls
    assert ["gh", "api", f"repos/acme/app/commits/{SHA_A}/status"] not in calls
    assert old == [{"id": 7, "sha": SHA_A, "context": "shuin/gauntlet", "state": "success"}]
