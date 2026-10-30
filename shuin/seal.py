"""The Seal: does a PR's seal hold at its head, and if not, why not.

One pure module. :func:`fresh` decides whether a seal or verification claim
is still the head. Callers pass the PR row, its latest acceptance receipt and
the verifier state at the PR head; :func:`verdict` answers with ``holds`` or
one named refusal and the next command. The Board, merge closing, task
completion and the gate all read their answer and their words from here.
"""

from dataclasses import dataclass

from shuin.sanitize import same_sha


@dataclass(frozen=True)
class Freshness:
    """A seal or verification claim checked against a head.

    ``head`` is the ledger head. ``sha`` is the commit the claim names.
    A run counts only when ``opened_sha`` is that commit, ``opened_epoch``
    equals a non-null ``head_epoch``, and ``run_kind`` is ``full``.
    """

    head: str | None
    sha: str | None = None
    run: bool = False
    opened_sha: str | None = None
    opened_epoch: int | None = None
    head_epoch: int | None = None
    run_kind: str | None = None


def _epoch(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def fresh(check: Freshness) -> bool:
    """Whether this claim is still the head.

    A SHA is fresh when it is the head. A missing head is not a fresh SHA.
    A run is fresh only when ``run_kind`` is ``full``, ``head_epoch`` is
    present, and the run was opened on that SHA at that epoch. Run fields
    on a plain SHA check are rejected.
    """
    if not check.run and any(
        value is not None
        for value in (check.opened_sha, check.opened_epoch, check.head_epoch, check.run_kind)
    ):
        raise ValueError("run fields require run=True")
    if not same_sha(check.sha, check.head):
        return False
    if not check.run:
        return True
    if check.run_kind != "full":
        return False
    if not same_sha(check.opened_sha, check.sha):
        return False
    opened = _epoch(check.opened_epoch)
    head_epoch = _epoch(check.head_epoch)
    if opened is None or head_epoch is None:
        return False
    return opened == head_epoch


def fresh_at(at: int | None, since: int) -> bool:
    """Whether a timestamp is present and at or after ``since``."""
    if isinstance(since, bool) or not isinstance(since, int):
        raise ValueError("since is required")
    if isinstance(at, bool) or not isinstance(at, int):
        return False
    return at >= since


LIVE_STATES = ("open", "merged", "closed")


def verifier_pass_command(repo: str, number: int, sha: str) -> str:
    """The one command a verifier runs to record a pass at a head."""
    return f"shuin verify --pr {repo}#{number} --commit-sha {sha} --result pass"


def seal_command(repo: str, number: int) -> str:
    return f"shuin seal --pr {repo}#{number}"


def refusal(reason: str, command: str) -> str:
    """One sentence of reason, then the next command. Never a bare refusal."""
    return f"{reason} {command}"


def missing_verifier_voice(repo: str, number: int, sha: str) -> str:
    return f"Verifier pass is missing. {verifier_pass_command(repo, number, sha)}"


def task_drop_command(task_id: str) -> str:
    return refusal("PR is closed.", f"shuin task drop {task_id}")


def task_merged_next(task_id: str) -> str:
    return f"shuin task done {task_id} --merged-externally --reason landed"


def task_merged_command(task_id: str) -> str:
    return refusal("PR is already merged.", task_merged_next(task_id))


@dataclass(frozen=True)
class Verdict:
    """``holds``, or one refusal: its error code, reason name, voice and next command."""

    holds: bool
    code: str | None = None
    reason: str | None = None
    voice: str = "Seal holds."
    command: str | None = None


HOLDS = Verdict(True)


def _refuse(reason: str, text: str, command: str, code: str = "seal_refused") -> Verdict:
    return Verdict(False, code, reason, refusal(text, command), command)


def not_sealed_yet(command: str) -> str:
    """A stamp's reply when the verifier passed but the Seal does not hold yet."""
    return f"Stamped. Not sealed yet: {command}"


def head_moved(receipt, head) -> bool:
    """A seal is a moved head only when a recorded SHA differs from the ledger head.

    Equal commits are not a move, including when only the letter case differs.
    A receipt whose two reads both match the ledger head is not 'head moved',
    even if an earlier classification said stale.
    """
    if not receipt or not head:
        return False
    left = receipt["head_sha"]
    right = receipt["head_sha_reread"]
    left_same = fresh(Freshness(head, left))
    right_same = fresh(Freshness(head, right))
    if left and right and left_same and right_same:
        return False
    if left and not left_same:
        return True
    if right and not right_same:
        return True
    return receipt["classification"] == "stale" and not (left and right)


def verdict(pr, receipt, verifier_state: str) -> Verdict:
    """Whether the Seal holds for ``pr`` at its cached head.

    It holds when the PR is not closed, the latest receipt is a success whose
    two head reads are the PR head, and the verifier passed at that head.
    Otherwise the first matching refusal wins: closed, merged, head moved,
    not synced, missing.
    """
    head = pr["head_sha"]
    if (
        pr["state"] != "closed"
        and head
        and receipt is not None
        and receipt["classification"] == "success"
        and fresh(Freshness(head, receipt["head_sha"]))
        and fresh(Freshness(head, receipt["head_sha_reread"]))
        and verifier_state == "pass"
    ):
        return HOLDS
    if pr["state"] == "closed":
        return _refuse("closed", "PR is closed.", "shuin poll")
    if pr["state"] == "merged":
        return _refuse("merged", "PR is already merged.", "shuin poll")
    if head and head_moved(receipt, head):
        return _refuse("head_moved", "Seal broken: head moved.", "shuin poll", code="stale_head")
    if not head or pr["last_synced_at"] is None or pr["state"] not in LIVE_STATES:
        return _refuse("unsynced", "PR has not been synced.", "shuin poll")
    return _refuse("missing", "Seal is missing.", seal_command(pr["repo"], pr["number"]))
