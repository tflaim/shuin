from collections.abc import Sequence
from typing import Literal


PrClass = Literal["docs", "test_only", "small_fix", "other"]

MAX_FILES = 5
MAX_LINES = 80
GUARDED_PREFIXES = ("githooks", ".githooks", ".github", ".husky", "skills", ".cursor")
GUARDED_FILES = (
    "agents.md", "claude.md", "install.sh", "security.md", "license", "pyproject.toml",
)


def classify_pr(files: Sequence[str], additions: int, deletions: int) -> PrClass:
    if not files or any(type(count) is not int or count < 0 for count in (additions, deletions)):
        return "other"

    docs = tests = source = 0
    for path in files:
        if type(path) is not str:
            return "other"
        parts = path.split("/")
        if (
            not path
            or path.startswith("/")
            or "\\" in path
            or ":" in path
            or ".." in parts
            or any(part.casefold() in GUARDED_PREFIXES for part in parts)
            or parts[-1].casefold() in GUARDED_FILES
            or parts[-1].casefold().startswith("license.")
        ):
            return "other"

        is_doc = path.endswith(".md") or path.startswith("docs/")
        is_test = not is_doc and (
            path.startswith(("tests/", "Tests/"))
            or parts[-1].startswith("test_")
            or parts[-1].endswith(("_test.py", "Tests.swift"))
        )
        docs += is_doc
        tests += is_test
        source += not is_doc and not is_test

    if docs == len(files):
        return "docs"
    if tests == len(files):
        return "test_only"
    if tests and source and len(files) <= MAX_FILES and additions + deletions <= MAX_LINES:
        return "small_fix"
    return "other"
