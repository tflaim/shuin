import json
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "findings.py"
FIRST = {
    "id": "f-0001",
    "date": "2026-10-28",
    "repo": "acme/app",
    "pr": 128,
    "class": "missing-check",
    "finding": "Repeated review finding",
    "found_by": "human",
    "enforcer": None,
}


def run(log, command, *args):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), command, "--log", str(log), *args],
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def add(log, **changes):
    values = {
        "repo": "acme/app", "pr": "128", "class": "missing-check",
        "finding": "Repeated review finding", "found-by": "human", "date": "2026-10-28",
    }
    values.update(changes)
    args = [arg for key, value in values.items() for arg in (f"--{key}", value)]
    return run(log, "add", *args)


def test_add_to_empty_log(tmp_path):
    log = tmp_path / "log.jsonl"
    log.touch()
    assert add(log) == (0, "f-0001\n", "")
    assert log.read_text() == (
        '{"id": "f-0001", "date": "2026-10-28", "repo": "acme/app", "pr": 128, '
        '"class": "missing-check", "finding": "Repeated review finding", '
        '"found_by": "human", "enforcer": null}\n'
    )


def test_second_add_preserves_first_line(tmp_path):
    log = tmp_path / "log.jsonl"
    assert add(log) == (0, "f-0001\n", "")
    first_line = log.read_bytes()
    assert add(log) == (0, "f-0002\n", "")
    assert log.read_bytes() == first_line + (
        b'{"id": "f-0002", "date": "2026-10-28", "repo": "acme/app", "pr": 128, '
        b'"class": "missing-check", "finding": "Repeated review finding", '
        b'"found_by": "human", "enforcer": null}\n'
    )


@pytest.mark.parametrize(("field", "value", "message"), [
    ("class", "Bad_slug", "class must be a kebab-case slug"),
    ("finding", "", "finding must contain 1-200 characters"),
    ("finding", "x" * 201, "finding must contain 1-200 characters"),
    ("found-by", "unknown", "found_by must be forge, codex, gauntlet or human"),
    ("repo", "", "repo must be a nonempty string"),
    ("pr", "0", "pr must be a positive integer"),
    ("pr", "abc", "pr must be a positive integer"),
    ("date", "2026-02-30", "date must be a valid YYYY-MM-DD date"),
    ("date", "20261028", "date must be a valid YYYY-MM-DD date"),
])
def test_invalid_add_leaves_log_unchanged(tmp_path, field, value, message):
    log = tmp_path / "log.jsonl"
    log.write_text(json.dumps(FIRST) + "\n")
    before = log.read_bytes()
    assert add(log, **{field: value}) == (2, "", message + "\n")
    assert log.read_bytes() == before


@pytest.mark.parametrize(("count", "status", "expected"), [
    (0, None, (0, "", "")),
    (1, None, (0, "", "")),
    (2, None, (1, "", "class missing-check has 2 findings and no shipped check: add a lint, test or hook, then set its enforcer (f-0001, f-0002)\n")),
    (2, "proposed", (1, "", "class missing-check has 2 findings and no shipped check: add a lint, test or hook, then set its enforcer (f-0001, f-0002)\n")),
    (2, "shipped", (0, "", "")),
])
def test_check_repeated_class(tmp_path, count, status, expected):
    rows = [dict(FIRST, id=f"f-{number:04d}") for number in range(1, count + 1)]
    if status:
        rows[0]["enforcer"] = {
            "kind": "test", "path": "tests/test_checks.py", "status": status, "pr": 129,
        }
    log = tmp_path / "log.jsonl"
    log.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert run(log, "check") == expected


def test_malformed_line(tmp_path):
    log = tmp_path / "log.jsonl"
    log.write_text(json.dumps(FIRST) + "\n{broken\n")
    assert run(log, "check") == (1, "", "line 2: invalid JSON\n")


@pytest.mark.parametrize(("change", "message"), [
    ({"id": "f-0002"}, "id must be f-0001"),
    ({"id": "f-001"}, "id must be f-0001"),
    ({"date": None}, "date must be a valid YYYY-MM-DD date"),
    ({"repo": 1}, "repo must be a nonempty string"),
    ({"pr": True}, "pr must be a positive integer"),
    ({"pr": "128"}, "pr must be a positive integer"),
    ({"class": "bad--slug"}, "class must be a kebab-case slug"),
    ({"class": None}, "class must be a kebab-case slug"),
    ({"finding": "   "}, "finding must contain 1-200 characters"),
    ({"finding": 5}, "finding must contain 1-200 characters"),
    ({"found_by": "unknown"}, "found_by must be forge, codex, gauntlet or human"),
    ({"extra": True}, "row must contain id, date, repo, pr, class, finding, found_by and enforcer"),
    ({"enforcer": "shipped"}, "enforcer must contain kind, path, status and pr"),
    ({"enforcer": {"status": "shipped"}}, "enforcer must contain kind, path, status and pr"),
    ({"enforcer": {"kind": "ci", "path": "check.py", "status": "shipped", "pr": None}}, "enforcer.kind must be lint, test or hook"),
    ({"enforcer": {"kind": "lint", "path": "", "status": "shipped", "pr": None}}, "enforcer.path must be a nonempty string"),
    ({"enforcer": {"kind": "lint", "path": "check.py", "status": "done", "pr": None}}, "enforcer.status must be proposed or shipped"),
    ({"enforcer": {"kind": "lint", "path": "check.py", "status": "shipped", "pr": True}}, "enforcer.pr must be null or a positive integer"),
])
@pytest.mark.parametrize(("command", "code"), [("add", 2), ("check", 1)])
def test_invalid_row_is_rejected(tmp_path, change, message, command, code):
    log = tmp_path / "log.jsonl"
    log.write_text(json.dumps(dict(FIRST, **change)) + "\n")
    before = log.read_bytes()
    result = add(log) if command == "add" else run(log, "check")
    assert result == (code, "", "line 1: " + message + "\n")
    assert log.read_bytes() == before


@pytest.mark.parametrize("row", [[], {}, None])
def test_row_requires_all_fields(tmp_path, row):
    log = tmp_path / "log.jsonl"
    log.write_text(json.dumps(row) + "\n")
    assert run(log, "check") == (1, "", "line 1: row must contain id, date, repo, pr, class, finding, found_by and enforcer\n")


@pytest.mark.parametrize("second_id", ["f-0001", "f-0003"])
def test_duplicate_or_skipped_id(tmp_path, second_id):
    log = tmp_path / "log.jsonl"
    log.write_text(json.dumps(FIRST) + "\n" + json.dumps(dict(FIRST, id=second_id)) + "\n")
    assert run(log, "check") == (1, "", "line 2: id must be f-0002\n")


def test_add_after_line_without_newline(tmp_path):
    log = tmp_path / "log.jsonl"
    first_line = json.dumps(FIRST).encode()
    log.write_bytes(first_line)
    assert add(log) == (0, "f-0002\n", "")
    assert log.read_bytes().startswith(first_line + b"\n")
    assert run(log, "check") == (1, "", "class missing-check has 2 findings and no shipped check: add a lint, test or hook, then set its enforcer (f-0001, f-0002)\n")


@pytest.mark.parametrize("kind", ["lint", "test", "hook"])
def test_shipped_enforcer_only_covers_its_class(tmp_path, kind):
    rows = [
        dict(FIRST, enforcer={"kind": kind, "path": "checks.py", "status": "shipped", "pr": None}),
        dict(FIRST, id="f-0002", **{"class": "other-class"}),
        dict(FIRST, id="f-0003", **{"class": "other-class"}),
    ]
    log = tmp_path / "log.jsonl"
    log.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert run(log, "check") == (1, "", "class other-class has 2 findings and no shipped check: add a lint, test or hook, then set its enforcer (f-0002, f-0003)\n")


@pytest.mark.parametrize("who", ["forge", "codex", "gauntlet", "human"])
def test_add_defaults_to_utc_date(tmp_path, who):
    log = tmp_path / "log.jsonl"
    before = datetime.now(timezone.utc).date().isoformat()
    assert run(log, "add", "--repo", "acme/app", "--pr", "128", "--class", "missing-check",
               "--finding", "x" * 200, "--found-by", who) == (0, "f-0001\n", "")
    after = datetime.now(timezone.utc).date().isoformat()
    row = json.loads(log.read_text())
    assert row["date"] in (before, after)
    assert row == dict(FIRST, date=row["date"], finding="x" * 200, found_by=who)


def test_invalid_add_argument_has_one_line_error(tmp_path):
    log = tmp_path / "log.jsonl"
    log.touch()
    assert run(log, "add", "--unknown") == (2, "", "the following arguments are required: --repo, --pr, --class, --finding, --found-by\n")
    assert log.read_bytes() == b""
