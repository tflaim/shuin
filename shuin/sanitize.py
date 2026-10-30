"""Length caps and control-character stripping for agent free text.

Free text is allowed only in update bodies, tried-and-failed lines, and
verification notes. It is stored as an agent claim, never as instructions.
"""

import re

from shuin.errors import LedgerError

_BIDI = (
    "\u202a\u202b\u202c\u202d\u202e"
    "\u2066\u2067\u2068\u2069"
    "\u200b\u200c\u200d\u200e\u200f"
    "\ufeff"
)

_AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TASK_ID = re.compile(r"^T-[0-9]{4,}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{7,64}$")

# One-line tried-and-failed note on review request. Matches tried_failed.entry.
TRIED_FAILED_MAX = 300

# Identity and confirmed-fact fields. Clients do not get to set these.
FORBIDDEN_FIELDS = frozenset(
    {
        "agent",
        "agent_id",
        "actor",
        "head_sha",
        "ci_state",
        "review_state",
        "last_synced_at",
        "files_json",
        "token",
        "token_hash",
        "label",
        "claimed_by",
    }
)


def reject_forbidden_fields(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN_FIELDS:
                raise LedgerError(
                    400,
                    "agent_field_rejected",
                    "Identity is stamped from the token.",
                )
            reject_forbidden_fields(item)
    elif isinstance(value, list):
        for item in value:
            reject_forbidden_fields(item)


def _strip_controls(text: str) -> str:
    cleaned = "".join(ch for ch in text if ch >= " " and ch != "\x7f")
    for mark in _BIDI:
        cleaned = cleaned.replace(mark, "")
    return cleaned.strip()


def clean_text(value, max_len: int, *, one_line: bool = False) -> str:
    if not isinstance(value, str):
        raise LedgerError(400, "bad_text", "Text required.")
    if one_line and ("\n" in value or "\r" in value):
        raise LedgerError(400, "one_line", "Tried-and-failed must be one line.")
    cleaned = _strip_controls(value)
    if len(cleaned) > max_len:
        raise LedgerError(
            400,
            "too_long",
            f"Text is too long (max {max_len} chars, got {len(cleaned)}).",
        )
    if len(cleaned) < 1 or cleaned.isspace():
        raise LedgerError(400, "empty", "Text is empty.")
    return cleaned


def clean_agent_id(value: str) -> str:
    if not isinstance(value, str) or not _AGENT_ID.fullmatch(value):
        raise LedgerError(400, "bad_agent", "Agent id is not usable.")
    return value


def clean_task_id(value: str) -> str:
    if not isinstance(value, str) or not _TASK_ID.fullmatch(value):
        raise LedgerError(400, "bad_task", "Task id is not usable.")
    return value


def clean_repo(value: str) -> str:
    if not isinstance(value, str) or not _REPO.fullmatch(value):
        raise LedgerError(400, "bad_repo", "Repo must look like owner/name.")
    return value


def clean_sha(value: str) -> str:
    if not isinstance(value, str):
        raise LedgerError(400, "bad_sha", "Commit SHA is not usable.")
    sha = value.strip().lower()
    if not _SHA.fullmatch(sha):
        raise LedgerError(400, "bad_sha", "Commit SHA is not usable.")
    return sha


def same_sha(left, right) -> bool:
    """True when two SHAs are the same commit. Case and surrounding space do not count."""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    a = left.strip().lower()
    b = right.strip().lower()
    return bool(a) and a == b


_MODEL = re.compile(r"^[A-Za-z0-9._:@+/-]{1,80}$")


def clean_model(value: str) -> str:
    if not isinstance(value, str):
        raise LedgerError(400, "bad_model", "Model is not usable.")
    model = value.strip()
    if not _MODEL.fullmatch(model):
        raise LedgerError(400, "bad_model", "Model is not usable.")
    return model


def clean_pr_number(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 1_000_000_000:
        raise LedgerError(400, "bad_pr", "PR number is not usable.")
    return value


def parse_pr_ref(value: str) -> tuple[str, int]:
    if not isinstance(value, str) or "#" not in value:
        raise LedgerError(400, "bad_pr", "PR must look like owner/repo#123.")
    repo, _, num = value.partition("#")
    if not num.isdigit():
        raise LedgerError(400, "bad_pr", "PR must look like owner/repo#123.")
    return clean_repo(repo), clean_pr_number(int(num))
