"""Fail-closed PR acceptance.

Read the head, read required checks for that exact head, re-read the head,
write that state and head onto the PR row, then gate. Anything other than a
clean success is a refusal. A pull-request read failure is a refusal.

A missing or 403 branch-protection read, a 403 ruleset list, or no rules at
all falls back to the local verifier pass only when that head has no failing
check runs and the pull request is open. Those paths still fetch the check
runs. A 401 or any other auth error is a refusal. When that fallback has no
verifier pass, the refusal names
``shuin verify --pr owner/repo#N --commit-sha SHA --result pass``.

An already-merged pull request is refused even when the verifier passed and
the heads match. A success receipt after the merge would let a later poll
close the card as sealed_merge. The refusal names ``shuin poll``. Every
refusal names its reason and the next command.
"""

from shuin.seal import (
    LIVE_STATES as _LIVE,
    Freshness,
    fresh,
    missing_verifier_voice,
    refusal,
    seal_command,
    task_drop_command,
    task_merged_command,
    verifier_pass_command,
)
from shuin.types import CheckItem

# The gate's words live in shuin.seal; they are re-exported here for callers.
__all__ = [
    "assess",
    "classify",
    "missing_verifier_voice",
    "read_github",
    "refusal",
    "seal_command",
    "seal_voice",
    "task_drop_command",
    "task_merged_command",
    "verifier_pass_command",
]

_PASSING = "success"


def classify(checks: list[CheckItem], verifier_state: str, head1: str | None, head2: str | None, read_error: str | None) -> str:
    if read_error == "auth":
        return "auth"
    if read_error:
        return "infra"
    if not head1 or not head2 or not fresh(Freshness(head1, head2)):
        return "stale"
    if not checks:
        if verifier_state == "pass":
            return _PASSING
        if verifier_state == "fail":
            return "failure"
        return "missing"
    states = [item.state for item in checks]
    if any(state == "failure" for state in states):
        return "failure"
    if any(state in ("skipped", "neutral", "stale") for state in states):
        return "failure"
    if any(state == "pending" for state in states):
        return "pending"
    if any(state == "missing" for state in states):
        return "missing"
    if any(state != _PASSING for state in states):
        return "failure"
    if verifier_state == "pass":
        return _PASSING
    if verifier_state == "fail":
        return "failure"
    return "missing"


def seal_voice(
    *,
    classification: str,
    repo: str,
    number: int,
    sha: str | None,
    verifier_state: str,
    checks: list,
    gate_reason: str | None,
) -> str:
    """Every non-success names why, and the command that moves it forward."""
    again = seal_command(repo, number)
    if classification == "success":
        return "Seal holds."
    if gate_reason == "merged":
        return refusal("PR is already merged.", "shuin poll")
    if gate_reason == "closed":
        return refusal("PR is closed.", "shuin poll")
    if gate_reason == "unsynced":
        return refusal("PR has not been synced.", "shuin poll")
    if classification == "stale":
        return refusal("Seal broken: head moved.", again)
    if classification == "pending":
        return refusal("Checks still running.", again)
    if classification == "auth":
        return refusal("GitHub auth failed.", "gh auth login")
    if classification == "infra":
        return refusal("GitHub read failed.", again)
    if classification == "failure":
        if verifier_state == "fail" and not checks and sha:
            return refusal("Verifier recorded a fail.", verifier_pass_command(repo, number, sha))
        return refusal("Required checks failed.", again)
    if classification == "missing" and not checks and verifier_state != "pass" and sha:
        return missing_verifier_voice(repo, number, sha)
    if classification == "missing":
        return refusal("Required checks missing.", again)
    return refusal("GitHub read failed.", again)


def read_github(github, repo: str, number: int) -> dict:
    """Read the head, the required checks for that head, then the head again."""
    head1, err1 = _read_head(github, repo, number)
    checks, err_checks = _read_checks(github, repo, head1)
    head2, err2 = _read_head(github, repo, number)
    sha1 = _norm_sha(head1.head_sha) if head1 and not head1.error else None
    sha2 = _norm_sha(head2.head_sha) if head2 and not head2.error else None
    return {
        "head1": head1,
        "head2": head2,
        "checks": list(checks),
        "read_error": err1 or err_checks or err2,
        "head_read_error": err1 or err2,
        "sha1": sha1,
        "sha2": sha2,
        "agreed": _agreed_head(head1, head2, sha1, sha2),
    }


def assess(reads: dict, pr, verifier_state: str) -> tuple[str, str | None]:
    """Classify the GitHub reads against the cached PR. Returns (classification, gate_reason)."""
    head1, head2, sha1, sha2 = reads["head1"], reads["head2"], reads["sha1"], reads["sha2"]
    if reads["agreed"] and head2.state in ("merged", "closed"):
        return "failure", head2.state
    if _states_disagree(head1, head2, sha1, sha2) and not reads["head_read_error"]:
        return "stale", None
    classification = classify(reads["checks"], verifier_state, sha1, sha2, reads["read_error"])
    return _fail_closed_pr(classification, pr, head1, head2, sha1, sha2)


def _read_head(github, repo: str, number: int):
    try:
        head = github.read_head(repo, number)
    except Exception:
        return None, "infra"
    if getattr(head, "error", None):
        return head, head.error
    return head, None


def _read_checks(github, repo: str, head):
    if head is None or head.error or not head.head_sha:
        return [], None
    try:
        checks = github.read_checks(repo, head.base_branch, head.head_sha)
    except Exception:
        return [], "infra"
    if getattr(checks, "error", None):
        return [], checks.error
    return list(checks.checks), None


def _agreed_head(head1, head2, sha1: str | None, sha2: str | None) -> bool:
    """True when both GitHub reads saw the same commit and the same PR state."""
    if not sha1 or not fresh(Freshness(sha1, sha2)) or head1 is None or head2 is None:
        return False
    if getattr(head1, "error", None) or getattr(head2, "error", None):
        return False
    return head1.state == head2.state and head1.state in _LIVE


def _states_disagree(head1, head2, sha1: str | None, sha2: str | None) -> bool:
    if not sha1 or not fresh(Freshness(sha1, sha2)) or head1 is None or head2 is None:
        return False
    if getattr(head1, "error", None) or getattr(head2, "error", None):
        return False
    return head1.state != head2.state


def _fail_closed_pr(classification: str, pr, head1, head2, sha1: str | None, sha2: str | None):
    """A success requires an open PR whose cache matches this head.

    Returns (classification, reason). reason is set when the cache or the
    live state blocks a success that the checks would have allowed.
    """
    if classification != "success":
        return classification, None
    live = head1.state if _agreed_head(head1, head2, sha1, sha2) else None
    if live not in _LIVE or pr["last_synced_at"] is None or not pr["head_sha"] or pr["state"] not in _LIVE:
        return "missing", "unsynced"
    if not fresh(Freshness(pr["head_sha"], sha1)) or not fresh(Freshness(pr["head_sha"], sha2)):
        return "stale", None
    if live == "merged" or pr["state"] == "merged":
        return "failure", "merged"
    if live != "open" or pr["state"] != "open":
        return "failure", "closed"
    return classification, None


def _norm_sha(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text or None
