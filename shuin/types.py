from dataclasses import dataclass


@dataclass(frozen=True)
class CheckItem:
    context: str
    check_id: int | None
    state: str
    source: str


@dataclass(frozen=True)
class HeadRead:
    head_sha: str | None = None
    branch: str | None = None
    base_branch: str | None = None
    node_id: str | None = None
    state: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ChecksRead:
    checks: tuple[CheckItem, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class PrSnapshot:
    repo: str
    number: int
    node_id: str | None
    branch: str | None
    base_branch: str | None
    head_sha: str | None
    state: str | None
    review_state: str | None
    ci_state: str | None
    files: tuple[str, ...] = ()
    patch_id: str | None = None
    tree_sha: str | None = None
