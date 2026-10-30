"""Hunk overlap and the semantic checks.

The v7 fixtures are the GRDB case: two diffs each call registerMigration("v7").
The enum fixtures are the missed break: one diff adds `case color`, another
changes a file that switches on that type.
"""

import json
import subprocess
from pathlib import Path

import pytest

from shuin.cli import main
from shuin.errors import LedgerError
from shuin.httpapi import Api
from shuin.mcp import call_tool
from shuin.overlap import (
    Check,
    DuplicateMigrationCheck,
    Registry,
    Subject,
    _DIFF_CAP,
    _ORIGIN,
    analyze,
    default_registry,
)
from shuin.types import PrSnapshot
from tests.conftest import SHA_A, open_card, snap

ROOT = Path(__file__).resolve().parent / "fixtures" / "overlap"
V7_A = (ROOT / "pr124_v7.diff").read_text(encoding="utf-8")
V7_B = (ROOT / "pr127_v7.diff").read_text(encoding="utf-8")
ENUM = (ROOT / "pr126_enum.diff").read_text(encoding="utf-8")
SWITCH = (ROOT / "pr127_switch.diff").read_text(encoding="utf-8")


def _git(repo, *args):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    return proc.stdout.strip()


def _repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "dev@example.com")
    _git(path, "config", "user.name", "Dev")
    (path / "AppDatabase.swift").write_text(
        "enum Mig {\n    func up() {\n        migrator.registerMigration(\"v6\") { _ in }\n    }\n}\n",
        encoding="utf-8",
    )
    _git(path, "add", "AppDatabase.swift")
    _git(path, "commit", "-m", "base")


def _branch(path: Path, name: str, text: str) -> None:
    _git(path, "checkout", "-b", name)
    (path / "AppDatabase.swift").write_text(text, encoding="utf-8")
    _git(path, "add", "AppDatabase.swift")
    _git(path, "commit", "-m", name)
    _git(path, "checkout", "main")


def _v7_text() -> str:
    return (
        "enum Mig {\n"
        "    func up() {\n"
        "        migrator.registerMigration(\"v6\") { _ in }\n"
        "        migrator.registerMigration(\"v7\") { _ in }\n"
        "    }\n"
        "}\n"
    )


def _cards(env, files_a, files_b, number_a=124, number_b=127):
    left = open_card(env, number=number_a, node=f"PR_node_{number_a}", title="left sheet")
    right = open_card(env, number=number_b, node=f"PR_node_{number_b}", title="right sheet")
    env.ledger.apply_github_snapshot(
        snap(number=number_a, node=f"PR_node_{number_a}", files=files_a, branch=f"feat/{number_a}")
    )
    env.ledger.apply_github_snapshot(
        snap(number=number_b, node=f"PR_node_{number_b}", files=files_b, branch=f"feat/{number_b}")
    )
    return left, right


def _run(env, *args):
    return main(
        ["--db", str(env.db), "--token", env.tokens["eng-lead"], "--json", "overlap", *args]
    )


def test_file_overlap_stays_the_same_shape(env):
    left, _right = _cards(env, ("AppDatabase.swift",), ("AppDatabase.swift",))
    body = env.ledger.check_overlap(env.actors["eng-lead"], left["id"])
    want = [
        item
        for item in env.ledger._overlaps()
        if any(pr["task_id"] == left["id"] for pr in item["prs"])
    ]
    assert body["task_id"] == left["id"]
    assert body["overlaps"] == want
    assert body["overlaps"] == [
        {
            "path": "AppDatabase.swift",
            "prs": [
                {"task_id": left["id"], "repo": "acme/app", "pr": 124},
                {"task_id": _right["id"], "repo": "acme/app", "pr": 127},
            ],
        }
    ]
    assert "branches" not in body["overlaps"][0]
    assert body["hunks"] == []
    assert body["findings"] == []
    board = env.ledger.list_board(env.actors["eng-lead"])
    assert board["overlaps"] == want
    assert "hunks" not in board
    assert "findings" not in board


def test_v7_fixture_reports_duplicate_migration_id(env):
    left, right = _cards(env, ("AppDatabase.swift",), ("AppDatabase.swift",))
    body = env.ledger.check_overlap(
        env.actors["eng-lead"],
        left["id"],
        diffs={
            ("pr", "acme/app", 124): V7_A,
            ("pr", "acme/app", 127): V7_B,
        },
    )
    finding = next(item for item in body["findings"] if item["message"] == "duplicate migration id")
    assert finding["check"] == "duplicate_migration_id"
    assert finding["id"] == "v7"
    assert {loc["pr"] for loc in finding["locations"]} == {124, 127}
    assert body["overlaps"][0]["path"] == "AppDatabase.swift"
    assert {pr["pr"] for pr in body["overlaps"][0]["prs"]} == {124, 127}
    hunks = body["hunks"]
    assert len(hunks) == 1
    assert hunks[0]["path"] == "AppDatabase.swift"
    assert {row["pr"] for row in hunks[0]["hunks"]} == {124, 127}
    for row in hunks[0]["hunks"]:
        assert row["start"] == 3
        assert row["end"] == 9
        assert row["old_start"] == 3
        assert row["old_end"] == 6
        assert "registerMigration" in row["snippet"]
        assert "v7" in row["snippet"]
    assert right["id"] != left["id"]


def test_enum_fixture_warns_when_files_do_not_overlap(env):
    left, _right = _cards(
        env,
        ("Sources/Theme.swift",),
        ("Sources/Painter.swift",),
        number_a=126,
        number_b=127,
    )
    body = env.ledger.check_overlap(
        env.actors["eng-lead"],
        left["id"],
        diffs={
            ("pr", "acme/app", 126): ENUM,
            ("pr", "acme/app", 127): SWITCH,
        },
    )
    assert body["overlaps"] == []
    finding = next(item for item in body["findings"] if item["check"] == "enum_switch")
    assert finding["severity"] == "warning"
    assert finding["type"] == "Appearance"
    assert finding["case"] == "color"
    assert ".color" in finding["message"]
    assert any(loc["pr"] == 126 and loc["path"] == "Sources/Theme.swift" for loc in finding["locations"])
    assert any(
        row["pr"] == 127 and row["path"] == "Sources/Painter.swift" and "switch appearance" in row["snippet"]
        for row in finding["switches"]
    )


def test_enum_case_in_hunk_header_and_full_file_switch():
    enum_diff = (
        "diff --git a/Theme.swift b/Theme.swift\n"
        "--- a/Theme.swift\n"
        "+++ b/Theme.swift\n"
        "@@ -1,3 +1,4 @@ enum Appearance {\n"
        "     case light\n"
        "+    case color\n"
        " }\n"
    )
    other_diff = (
        "diff --git a/Painter.swift b/Painter.swift\n"
        "--- a/Painter.swift\n"
        "+++ b/Painter.swift\n"
        "@@ -12,2 +12,3 @@\n"
        "     let flag = 1\n"
        "+    let flag = 2\n"
        "     return flag\n"
    )
    full = (
        "func paint(_ appearance: Appearance) {\n"
        "    switch appearance {\n"
        "    case .light:\n"
        "        return\n"
        "    case .dark:\n"
        "        return\n"
        "    }\n"
        "}\n"
    )
    report = analyze(
        [
            Subject(key="pr:acme/app#126", kind="pr", task_id="T-0001", repo="acme/app", pr=126, diff=enum_diff),
            Subject(
                key="pr:acme/app#127",
                kind="pr",
                task_id="T-0002",
                repo="acme/app",
                pr=127,
                files=("Painter.swift",),
                diff=other_diff,
                texts=(("Painter.swift", full),),
            ),
        ]
    )
    finding = next(item for item in report["findings"] if item["severity"] == "warning")
    assert finding["case"] == "color"
    assert finding["switches"][0]["path"] == "Painter.swift"
    assert "switch appearance" in finding["switches"][0]["snippet"]


def test_handled_switch_is_not_a_warning():
    enum_diff = ENUM
    handled = SWITCH.replace("case .dark:", "case .dark:\n     case .color:", 1)
    defaulted = SWITCH.replace("switch appearance {", "switch appearance {\n     default:\n         return", 1)
    add = Subject(key="branch:add", kind="branch", branch="add", diff=enum_diff)
    assert analyze([add, Subject(key="branch:ok", kind="branch", branch="ok", diff=handled)])["findings"] == []
    assert analyze([add, Subject(key="branch:def", kind="branch", branch="def", diff=defaulted)])["findings"] == []


def test_migration_filenames_and_custom_regex():
    def added(path: str) -> str:
        return (
            f"diff --git a/{path} b/{path}\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            f"+++ b/{path}\n"
            "@@ -0,0 +1 @@\n"
            "+select 1;\n"
        )

    pairs = [
        ("migrations/V7__color.sql", "db/v7_tint.sql", "v7"),
        ("migrations/V7.sql", "other/v7.sql", "v7"),
        ("migrations/0007_color.up.sql", "db/0007_tint.sql", "0007"),
    ]
    for left, right, ident in pairs:
        found = DuplicateMigrationCheck().run(
            [
                Subject(key="branch:a", kind="branch", branch="a", diff=added(left)),
                Subject(key="branch:b", kind="branch", branch="b", diff=added(right)),
            ]
        )
        assert found[0]["message"] == "duplicate migration id"
        assert found[0]["id"] == ident

    edited = (
        "diff --git a/migrations/V7__color.sql b/migrations/V7__color.sql\n"
        "--- a/migrations/V7__color.sql\n"
        "+++ b/migrations/V7__color.sql\n"
        "@@ -1 +1 @@\n"
        "-select 1;\n"
        "+select 2;\n"
    )
    assert (
        DuplicateMigrationCheck().run(
            [
                Subject(key="branch:a", kind="branch", branch="a", diff=edited),
                Subject(key="branch:b", kind="branch", branch="b", diff=edited),
            ]
        )
        == []
    )
    custom = DuplicateMigrationCheck(r"markMigration\('([^']+)'\)")
    marked = (
        "diff --git a/App.swift b/App.swift\n"
        "--- a/App.swift\n"
        "+++ b/App.swift\n"
        "@@ -1 +1,2 @@\n"
        " init()\n"
        "+markMigration('v9')\n"
    )
    found = custom.run(
        [
            Subject(key="branch:a", kind="branch", branch="a", diff=marked),
            Subject(key="branch:b", kind="branch", branch="b", diff=marked),
        ]
    )
    assert found[0]["id"] == "v9"
    assert DuplicateMigrationCheck().run(
        [
            Subject(key="branch:a", kind="branch", branch="a", diff=marked),
            Subject(key="branch:b", kind="branch", branch="b", diff=marked),
        ]
    ) == []
    with pytest.raises(LedgerError) as raised:
        DuplicateMigrationCheck("(")
    assert raised.value.code == "bad_regex"


def test_registry_runs_a_custom_check():
    class Boom(Check):
        name = "boom"

        def run(self, subjects):
            return [{"check": "boom", "severity": "warning", "message": "boom", "_keys": [subjects[0].key]}]

    registry = Registry()
    registry.register(Boom())
    left = Subject(key="branch:a", kind="branch", branch="a", files=("A.swift",))
    right = Subject(key="branch:b", kind="branch", branch="b", files=("A.swift",))
    report = analyze([left, right], registry=registry, focus={left.key})
    assert report["findings"] == [{"check": "boom", "severity": "warning", "message": "boom"}]
    assert [check.name for check in default_registry()] == ["duplicate_migration_id", "enum_switch"]
    with pytest.raises(LedgerError) as raised:
        Registry().register(object())
    assert raised.value.code == "bad_check"


def test_one_subject_does_not_flag_its_own_migration():
    found = DuplicateMigrationCheck().run(
        [Subject(key="branch:a", kind="branch", branch="a", diff=V7_A + "\n" + V7_A)]
    )
    assert found == []


def test_overlap_branch_without_a_pr(env, tmp_path, capsys):
    repo = tmp_path / "repo"
    _repo(repo)
    _branch(repo, "feat/v7", _v7_text())
    code = _run(env, "--branch", "feat/v7", "--repo", str(repo))
    assert code == 0
    body = json.loads(capsys.readouterr().out)
    assert body["task_id"] is None
    assert body["overlaps"] == []
    assert body["hunks"] == []
    assert body["findings"] == []
    sha = _git(repo, "rev-parse", "feat/v7")
    code = _run(env, "--sha", sha, "--repo", str(repo))
    assert code == 0
    body = json.loads(capsys.readouterr().out)
    assert body["task_id"] is None
    assert "overlaps" in body and "hunks" in body and "findings" in body


def test_two_branches_report_duplicate_migration_id(env, tmp_path, capsys):
    repo = tmp_path / "repo"
    _repo(repo)
    _branch(repo, "feat/a", _v7_text())
    _branch(repo, "feat/b", _v7_text())
    code = _run(env, "--branch", "feat/a", "--branch", "feat/b", "--repo", str(repo))
    assert code == 0
    body = json.loads(capsys.readouterr().out)
    assert body["task_id"] is None
    assert any(item["message"] == "duplicate migration id" and item["id"] == "v7" for item in body["findings"])
    overlap = body["overlaps"][0]
    assert overlap["path"] == "AppDatabase.swift"
    assert overlap["prs"] == []
    assert overlap["branches"] == ["feat/a", "feat/b"]
    assert any("registerMigration" in row["snippet"] and "v7" in row["snippet"] for row in body["hunks"][0]["hunks"])


def test_branch_overlaps_an_open_pr_file(env, tmp_path, capsys):
    left, _right = _cards(env, ("AppDatabase.swift",), ("Other.swift",))
    repo = tmp_path / "repo"
    _repo(repo)
    _branch(repo, "feat/local", _v7_text())
    code = _run(env, "--branch", "feat/local", "--repo", str(repo))
    assert code == 0
    body = json.loads(capsys.readouterr().out)
    hit = next(item for item in body["overlaps"] if item["path"] == "AppDatabase.swift")
    assert hit["branches"] == ["feat/local"]
    assert hit["prs"] == [{"task_id": left["id"], "repo": "acme/app", "pr": 124}]
    assert all(item["path"] != "Other.swift" for item in body["overlaps"])


def test_missing_branch_and_missing_target(env, tmp_path, capsys):
    repo = tmp_path / "repo"
    _repo(repo)
    code = _run(env, "--branch", "no-such", "--repo", str(repo))
    assert code == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.err or captured.out)
    assert payload["error"] == "bad_ref"
    code = _run(env)
    assert code == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.err or captured.out)
    assert payload["error"] == "overlap_target_required"


def test_migration_regex_setting_and_override(env):
    left, _right = _cards(env, ("App.swift",), ("App.swift",))
    marked = (
        "diff --git a/App.swift b/App.swift\n"
        "--- a/App.swift\n"
        "+++ b/App.swift\n"
        "@@ -1 +1,2 @@\n"
        " init()\n"
        "+markMigration('v9')\n"
    )
    diffs = {("pr", "acme/app", 124): marked, ("pr", "acme/app", 127): marked}
    quiet = env.ledger.check_overlap(env.actors["eng-lead"], left["id"], diffs=diffs)
    assert quiet["findings"] == []
    env.ledger.set_setting("overlap.migration_regex", r"markMigration\('([^']+)'\)")
    flagged = env.ledger.check_overlap(env.actors["eng-lead"], left["id"], diffs=diffs)
    assert flagged["findings"][0]["message"] == "duplicate migration id"
    assert flagged["findings"][0]["id"] == "v9"
    overridden = env.ledger.check_overlap(
        env.actors["eng-lead"],
        left["id"],
        diffs=diffs,
        migration_regex=r"otherMarker\(([^)]+)\)",
    )
    assert overridden["findings"] == []
    with pytest.raises(LedgerError) as raised:
        env.ledger.check_overlap(env.actors["eng-lead"], left["id"], migration_regex="(")
    assert raised.value.code == "bad_regex"


def _patch(path: str, added: list[str]) -> str:
    body = "".join(f"+{line}\n" for line in added)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1 +1,{len(added) + 1} @@\n"
        " context\n"
        + body
    )


def _new_file(path: str, added: list[str]) -> str:
    body = "".join(f"+{line}\n" for line in added)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(added)} @@\n"
        + body
    )


def _migration_ids(subjects) -> list[str]:
    return [item["id"] for item in DuplicateMigrationCheck().run(subjects) if item["message"] == "duplicate migration id"]


def _finding_ids(body) -> list[str]:
    return [item["id"] for item in body["findings"] if item.get("message") == "duplicate migration id"]


def test_checkout_remote_ignores_credential_userinfo():
    url = "https://x-access-token:secret@github.com/acme/app.git"
    match = _ORIGIN.match(url)
    assert match is not None
    assert f"{match.group(1)}/{match.group(2)}" == "acme/app"
    assert "secret" not in f"{match.group(1)}/{match.group(2)}"


def test_migration_ids_do_not_cross_repos():
    left = Subject(key="pr:acme/app#1", kind="pr", repo="acme/app", pr=1, diff=V7_A)
    other = Subject(key="pr:other/lib#2", kind="pr", repo="other/lib", pr=2, diff=V7_B)
    assert _migration_ids([left, other]) == []
    same = Subject(key="pr:acme/app#3", kind="pr", repo="acme/app", pr=3, diff=V7_B)
    assert _migration_ids([left, same]) == ["v7"]


def test_comments_and_docs_are_not_migration_ids():
    real = Subject(key="branch:real", kind="branch", branch="real", diff=V7_A)
    noises = [
        _patch("AppDatabase.swift", ['// registerMigration("v7")']),
        _patch(
            "AppDatabase.swift",
            ["/*", 'registerMigration("v7")', "*/"],
        ),
        (
            "diff --git a/AppDatabase.swift b/AppDatabase.swift\n"
            "--- a/AppDatabase.swift\n"
            "+++ b/AppDatabase.swift\n"
            "@@ -1,3 +1,4 @@\n"
            " func migrate() {\n"
            "     /*\n"
            '+    registerMigration("v7")\n'
            "     */\n"
            " }\n"
        ),
        _patch("migrations/scratch.sql", ['-- registerMigration("v7")', '# registerMigration("v7")']),
        _patch("docs/note.md", ['registerMigration("v7")', "See migrations/V7__color.sql"]),
        _new_file("docs/V7__note.sql.md", ["select 1;"]),
    ]
    for diff in noises:
        assert _migration_ids([real, Subject(key="branch:noise", kind="branch", branch="noise", diff=diff)]) == []
    commented = _patch("AppDatabase.swift", ['migrator.registerMigration("v7") // ship it'])
    assert _migration_ids(
        [real, Subject(key="branch:code", kind="branch", branch="code", diff=commented)]
    ) == ["v7"]


def test_v7_filename_and_register_migration_are_the_same_id():
    flyway = Subject(
        key="branch:sql",
        kind="branch",
        branch="sql",
        diff=_new_file("migrations/V7__color.sql", ["select 1;"]),
    )
    grdb = Subject(key="branch:swift", kind="branch", branch="swift", diff=V7_A)
    numbered = Subject(
        key="branch:num",
        kind="branch",
        branch="num",
        diff=_new_file("migrations/0007_name.sql", ["select 1;"]),
    )
    assert _migration_ids([flyway, grdb]) == ["v7"]
    assert _migration_ids([numbered, grdb]) == []


def test_tuple_switch_and_method_case_are_not_warnings():
    added = Subject(key="branch:add", kind="branch", branch="add", diff=ENUM)
    tuples = (
        SWITCH.replace("case .light:", "case (.color, _):", 1),
        SWITCH.replace("case .dark:", "case let (.color, x):", 1),
    )
    for diff in tuples:
        report = analyze([added, Subject(key="branch:sw", kind="branch", branch="sw", diff=diff)])
        assert report["findings"] == []
    method = (
        "diff --git a/Sources/Theme.swift b/Sources/Theme.swift\n"
        "--- a/Sources/Theme.swift\n"
        "+++ b/Sources/Theme.swift\n"
        "@@ -1,4 +1,8 @@\n"
        " enum Appearance {\n"
        "     case light\n"
        "+    func title() -> String {\n"
        "+        case color\n"
        '+        return "light"\n'
        "+    }\n"
        " }\n"
    )
    header = (
        "diff --git a/Sources/Theme.swift b/Sources/Theme.swift\n"
        "--- a/Sources/Theme.swift\n"
        "+++ b/Sources/Theme.swift\n"
        "@@ -1,3 +1,4 @@ func title() -> String {\n"
        '     return "light"\n'
        "+    case color\n"
        " }\n"
    )
    for diff in (method, header):
        report = analyze([Subject(key="branch:add", kind="branch", branch="add", diff=diff), Subject(key="branch:sw", kind="branch", branch="sw", diff=SWITCH)])
        assert report["findings"] == []
    bare = (
        "diff --git a/Sources/Theme.swift b/Sources/Theme.swift\n"
        "--- a/Sources/Theme.swift\n"
        "+++ b/Sources/Theme.swift\n"
        "@@ -1,4 +1,5 @@\n"
        " enum Appearance\n"
        " {\n"
        "     case light\n"
        "+    case color\n"
        " }\n"
    )
    report = analyze([Subject(key="branch:add", kind="branch", branch="add", diff=bare), Subject(key="branch:sw", kind="branch", branch="sw", diff=SWITCH)])
    assert report["findings"][0]["case"] == "color"


def _with_origin(path: Path, url: str) -> None:
    _git(path, "remote", "add", "origin", url)


def test_other_repo_pr_is_not_diffed_from_this_checkout(env, tmp_path):
    left, _right = _cards(env, ("AppDatabase.swift",), ("Other.swift",))
    other = env.ledger.add_task(env.actors["eng-lead"], "library sheet", repo="other/lib")
    env.ledger.link_pr(env.actors["eng-lead"], other["id"], "other/lib", 12, node_id="PR_node_other_lib_12")
    env.ledger.apply_github_snapshot(
        PrSnapshot(
            repo="other/lib",
            number=12,
            node_id="PR_node_other_lib_12",
            branch="feat/v7",
            base_branch="main",
            head_sha=SHA_A,
            state="open",
            review_state=None,
            ci_state="pending",
            files=("AppDatabase.swift",),
        )
    )
    repo = tmp_path / "repo"
    _repo(repo)
    _with_origin(repo, "https://github.com/acme/app.git")
    _branch(repo, "feat/v7", _v7_text())
    body = env.ledger.check_overlap(
        env.actors["eng-lead"],
        left["id"],
        repo_path=str(repo),
        diffs={("pr", "acme/app", 124): V7_A},
    )
    assert _finding_ids(body) == []
    assert any(
        note == "Skipped diff for other/lib#12. The checkout remote does not match that repo."
        for note in body["notes"]
    )
    assert all("acme/app#124" not in note for note in body["notes"])


def test_pr_diff_prefers_head_sha_over_the_branch_tip(env, tmp_path):
    repo = tmp_path / "repo"
    _repo(repo)
    _with_origin(repo, "git@github.com:acme/app.git")
    _branch(
        repo,
        "feat/124",
        "enum Mig {\n    func up() {\n        migrator.registerMigration(\"v6\") { _ in }\n        let note = 1\n    }\n}\n",
    )
    _git(repo, "checkout", "feat/124")
    (repo / "AppDatabase.swift").write_text(_v7_text(), encoding="utf-8")
    _git(repo, "add", "AppDatabase.swift")
    _git(repo, "commit", "-m", "add v7")
    v7_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "reset", "--hard", "HEAD~1")
    tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    left, _right = _cards(env, ("AppDatabase.swift",), ("AppDatabase.swift",))
    env.ledger.apply_github_snapshot(
        snap(number=124, node="PR_node_124", files=("AppDatabase.swift",), branch="feat/124", head=tip)
    )
    quiet = env.ledger.check_overlap(
        env.actors["eng-lead"],
        left["id"],
        repo_path=str(repo),
        diffs={("pr", "acme/app", 127): V7_B},
    )
    assert _finding_ids(quiet) == []
    env.ledger.apply_github_snapshot(
        snap(number=124, node="PR_node_124", files=("AppDatabase.swift",), branch="feat/124", head=v7_sha)
    )
    flagged = env.ledger.check_overlap(
        env.actors["eng-lead"],
        left["id"],
        repo_path=str(repo),
        diffs={("pr", "acme/app", 127): V7_B},
    )
    assert _finding_ids(flagged) == ["v7"]


def test_server_overlap_uses_the_checkout_setting(env, tmp_path, monkeypatch):
    left, _right = _cards(env, ("AppDatabase.swift",), ("AppDatabase.swift",))
    repo = tmp_path / "repo"
    _repo(repo)
    _with_origin(repo, "git@github.com:acme/app.git")
    _branch(repo, "feat/124", _v7_text())
    _branch(repo, "feat/127", _v7_text())
    monkeypatch.chdir(repo)

    blind = env.ledger.check_overlap(env.actors["eng-lead"], left["id"], allow_cwd=False)
    assert _finding_ids(blind) == []
    status, raw, _ctype = Api(env.db).handle(
        "GET",
        f"/v1/tasks/{left['id']}/overlap",
        {"Authorization": f"Bearer {env.tokens['eng-lead']}"},
        b"",
    )
    assert status == 200
    assert _finding_ids(json.loads(raw)) == []
    text, is_error = call_tool(
        "check_overlap",
        {"task_id": left["id"]},
        db_path=str(env.db),
        token=env.tokens["eng-lead"],
    )
    assert is_error is False
    assert _finding_ids(json.loads(text)) == []

    local = env.ledger.check_overlap(env.actors["eng-lead"], left["id"], allow_cwd=True)
    assert _finding_ids(local) == ["v7"]

    env.ledger.set_setting("overlap.checkout", str(tmp_path / "missing"))
    bad = env.ledger.check_overlap(env.actors["eng-lead"], left["id"], allow_cwd=False)
    assert _finding_ids(bad) == []
    assert any("not a git checkout" in note for note in bad["notes"])
    with pytest.raises(LedgerError) as raised:
        env.ledger.check_overlap(env.actors["eng-lead"], branches=["feat/124"], allow_cwd=False)
    assert raised.value.code == "not_a_repo"

    env.ledger.set_setting("overlap.checkout", str(repo))
    configured = env.ledger.check_overlap(env.actors["eng-lead"], left["id"], allow_cwd=False)
    assert _finding_ids(configured) == ["v7"]


def test_oversized_diff_is_truncated_and_noted(env):
    left, _right = _cards(env, ("AppDatabase.swift",), ("AppDatabase.swift",))
    line = "+" + ("x" * 100) + "\n"
    huge = V7_A + line * ((_DIFF_CAP // len(line)) + 2)
    assert len(huge) > _DIFF_CAP
    body = env.ledger.check_overlap(
        env.actors["eng-lead"],
        left["id"],
        diffs={
            ("pr", "acme/app", 124): huge,
            ("pr", "acme/app", 127): V7_B,
        },
    )
    assert _finding_ids(body) == ["v7"]
    assert any(
        note == "Diff for acme/app#124 is larger than 5 MB and was truncated." for note in body["notes"]
    )
