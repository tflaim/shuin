import json
import threading
import time

import pytest

from shuin.errors import GhProblem
from shuin.github import (
    GhClient,
    aggregate_ci,
    aggregate_reviews,
    clean_files,
    contexts_from_protection,
    contexts_from_ruleset,
    head_from_pr,
    match_required,
    ruleset_applies,
)
from shuin.types import PrSnapshot


@pytest.fixture
def pull_responses():
    sha = "0123456789abcdef0123456789abcdef01234567"
    payloads = {
        "repos/o/r/pulls/1": {
            "node_id": "PR_1",
            "merged": False,
            "state": "open",
            "head": {"ref": "feature", "sha": sha},
            "base": {"ref": "main"},
        },
        "repos/o/r/pulls/1/reviews": [{"user": {"id": 1}, "state": "APPROVED"}],
        "repos/o/r/pulls/1/files": [{"filename": "shuin/github.py"}],
        f"repos/o/r/commits/{sha}/check-runs": {
            "total_count": 1,
            "check_runs": [{"name": "ci", "head_sha": sha, "status": "completed", "conclusion": "success"}],
        },
        f"repos/o/r/commits/{sha}/status": {
            "statuses": [{"context": "lint", "sha": sha, "state": "success"}],
        },
    }
    return {path: (0, json.dumps(payload), "") for path, payload in payloads.items()}


def test_fetch_pull_overlaps_follow_up_reads(pull_responses):
    barrier = threading.Barrier(4, timeout=2)

    def runner(args):
        path = args[2].split("?", 1)[0]
        if path != "repos/o/r/pulls/1":
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                return 1, "", "barrier timeout"
        return pull_responses[path]

    snapshot = GhClient(runner).fetch_pull("o/r", 1)
    assert snapshot == PrSnapshot(
        repo="o/r",
        number=1,
        node_id="PR_1",
        branch="feature",
        base_branch="main",
        head_sha="0123456789abcdef0123456789abcdef01234567",
        state="open",
        review_state="approved",
        ci_state="passing",
        files=("shuin/github.py",),
    )


def test_fetch_pull_reads_head_before_follow_ups(pull_responses):
    events = []

    def runner(args):
        path = args[2].split("?", 1)[0]
        events.append(("start", path))
        response = pull_responses[path]
        if path == "repos/o/r/pulls/1":
            events.append(("return", path))
        return response

    GhClient(runner).fetch_pull("o/r", 1)
    assert events[:2] == [("start", "repos/o/r/pulls/1"), ("return", "repos/o/r/pulls/1")]
    assert len(events[2:]) == 4
    assert {path for event, path in events[2:] if event == "start"} == set(pull_responses) - {"repos/o/r/pulls/1"}


@pytest.mark.parametrize("reviews_fail, expected_kind", [(True, "auth"), (False, "infra")])
def test_fetch_pull_propagates_first_error_in_read_order(pull_responses, reviews_fail, expected_kind):
    if reviews_fail:
        pull_responses["repos/o/r/pulls/1/reviews"] = (1, "", "HTTP 401")
    pull_responses["repos/o/r/commits/0123456789abcdef0123456789abcdef01234567/status"] = (1, "", "HTTP 500")

    def runner(args):
        return pull_responses[args[2].split("?", 1)[0]]

    with pytest.raises(GhProblem) as exc:
        GhClient(runner).fetch_pull("o/r", 1)
    assert exc.value.kind == expected_kind


def test_fetch_pull_finishes_in_two_rounds(pull_responses):
    def runner(args):
        time.sleep(0.2)
        return pull_responses[args[2].split("?", 1)[0]]

    start = time.perf_counter()
    snapshot = GhClient(runner).fetch_pull("o/r", 1)
    elapsed = time.perf_counter() - start
    assert snapshot.head_sha == "0123456789abcdef0123456789abcdef01234567"
    assert elapsed < 0.7


def test_head_and_required_checks_parse():
    head = head_from_pr(
        {
            "node_id": "PR_kwDO1",
            "merged": False,
            "state": "open",
            "head": {"ref": "feat/sheet", "sha": "A" * 40},
            "base": {"ref": "main"},
        }
    )
    assert head.head_sha == "a" * 40
    assert head.branch == "feat/sheet"
    contexts = contexts_from_protection(
        {"required_status_checks": {"contexts": ["ci"], "checks": [{"context": "verify"}]}}
    )
    assert contexts == ["ci", "verify"]
    ruleset = {
        "enforcement": "active",
        "target": "branch",
        "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
        "rules": [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "lint"}]}}],
    }
    assert ruleset_applies(ruleset, "main") is True
    assert ruleset_applies(ruleset, "feature") is False
    assert contexts_from_ruleset(ruleset) == ["lint"]
    matched = match_required(
        ["ci", "lint"],
        [{"id": 4, "name": "ci", "status": "completed", "conclusion": "success"}],
        [{"id": 9, "context": "lint", "state": "pending"}],
    )
    by_name = {item.context: item.state for item in matched}
    assert by_name == {"ci": "success", "lint": "pending"}
    assert aggregate_reviews(
        [
            {"user": {"id": 1}, "state": "CHANGES_REQUESTED", "submitted_at": "2026-10-01"},
            {"user": {"id": 1}, "state": "APPROVED", "submitted_at": "2026-10-02"},
            {"user": {"id": 2}, "state": "COMMENTED", "submitted_at": "2026-10-02"},
        ]
    ) == "approved"
    assert aggregate_ci([{"name": "ci", "status": "completed", "conclusion": "success"}], []) == "passing"
    assert aggregate_ci([{"name": "ci", "status": "completed", "conclusion": "failure"}], []) == "failing"
    assert clean_files(["ios/Sheet.swift", "../etc/passwd", "/tmp/x", "ios/Sheet.swift"]) == ["ios/Sheet.swift"]


def test_check_run_sha_match_ignores_case():
    sha = "a" * 40
    matched = match_required(
        ["ci"],
        [{"id": 1, "name": "ci", "head_sha": sha.upper(), "status": "completed", "conclusion": "success"}],
        [{"id": 2, "context": "ci", "state": "success", "sha": f"  {sha.upper()}  "}],
        sha,
    )
    assert [(item.context, item.state) for item in matched] == [("ci", "success")]


def test_carried_over_and_patch_identity_runs_do_not_pass():
    sha = "a" * 40
    other = "b" * 40
    matched = match_required(
        ["ci", "lint", "build"],
        [
            {"id": 1, "name": "ci", "head_sha": other, "status": "completed", "conclusion": "success"},
            {"id": 2, "name": "ci", "head_sha": sha, "status": "completed", "conclusion": "success", "patch_identity": True},
            {"id": 3, "name": "lint", "head_sha": sha, "status": "completed", "conclusion": "success", "carried_over": True},
            {"id": 4, "name": "lint", "head_sha": sha, "status": "completed", "conclusion": "success", "check_suite": {"head_sha": other}},
            {"id": 5, "name": "build", "head_sha": sha, "status": "completed", "conclusion": "success"},
            {"id": 6, "name": "build", "head_sha": sha, "status": "completed", "conclusion": "success", "carried_over": True},
        ],
        [{"id": 9, "context": "ci", "state": "success", "sha": other, "patch_identity": True}],
        sha,
    )
    by_name = {item.context: item.state for item in matched}
    assert by_name == {"ci": "missing", "lint": "missing", "build": "success"}
    assert aggregate_ci(
        [{"name": "ci", "head_sha": other, "status": "completed", "conclusion": "success"}],
        [],
        sha,
    ) == "none"


def test_read_checks_keeps_only_a_full_run_on_the_exact_head():
    from shuin.github import GhClient

    sha = "c" * 40
    other = "d" * 40

    def runner(args):
        path = args[2]
        if "/protection" in path:
            return 0, '{"required_status_checks":{"contexts":["ci","build"]}}', ""
        if "rulesets" in path:
            return 1, "", "404 Not Found"
        if "check-runs" in path:
            body = (
                '{"total_count":2,"check_runs":['
                f'{{"id":1,"name":"ci","head_sha":"{other}","status":"completed","conclusion":"success"}},'
                f'{{"id":2,"name":"build","head_sha":"{sha}","status":"completed","conclusion":"success"}}'
                "]}"
            )
            return 0, body, ""
        if path.endswith("/status") or "/status?" in path:
            return 0, f'{{"sha":"{sha}","statuses":[]}}', ""
        return 1, "", "404 Not Found"

    checks = GhClient(runner).read_checks("acme/app", "main", sha)
    by_name = {item.context: item.state for item in checks.checks}
    assert by_name == {"ci": "missing", "build": "success"}


def test_reviews_with_changes_requested_stay_requested():
    assert aggregate_reviews(
        [
            {"user": {"id": 1}, "state": "APPROVED", "submitted_at": "2026-10-01"},
            {"user": {"id": 2}, "state": "CHANGES_REQUESTED", "submitted_at": "2026-10-02"},
        ]
    ) == "changes_requested"
