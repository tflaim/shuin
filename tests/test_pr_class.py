import pytest


@pytest.mark.parametrize("files", [
    ["README.md"],
    ["docs/a.md", "CHANGELOG.md"],
    ["docs/guide.md", "docs/a/b.md"],
    ["docs/example.py"],
    ["./x.md"],
    ["docs/"],
    ["tests/README.md"],
    ["docs/githooks-guide.md"],
    ["docs/licensing.md"],
    ["licensed.md"],
])
def test_docs(files):
    from shuin.pr_class import classify_pr

    assert classify_pr(files, 100, 100) == "docs"


@pytest.mark.parametrize("path", [
    "tests/test_x.py",
    "Tests/Example.swift",
    "pkg/test_x.py",
    "pkg/x_test.py",
    "pkg/ExampleTests.swift",
])
def test_test_only(path):
    from shuin.pr_class import classify_pr

    assert classify_pr([path], 100, 100) == "test_only"


@pytest.mark.parametrize("files, additions, deletions", [
    (["shuin/cli.py", "tests/test_cli.py"], 30, 5),
    (["shuin/cli.py", "tests/test_cli.py"], 0, 0),
    (["shuin/cli.py", "tests/test_cli.py", "README.md", "shuin/a.py", "shuin/b.py"], 70, 10),
])
def test_small_fix(files, additions, deletions):
    from shuin.pr_class import classify_pr

    assert classify_pr(files, additions, deletions) == "small_fix"


@pytest.mark.parametrize("files, additions, deletions", [
    (["shuin/cli.py", "tests/test_cli.py"], 70, 20),
    (["shuin/cli.py", "tests/test_cli.py"], 70, 11),
    (["shuin/cli.py", "tests/test_cli.py", "a.py", "b.py", "c.py", "d.py"], 30, 5),
    (["README.md", "shuin/cli.py"], 30, 5),
    (["README.md", "tests/test_cli.py"], 30, 5),
    (["tests/README.md", "shuin/cli.py"], 10, 1),
    (["docs/test_x.py", "shuin/cli.py"], 10, 1),
    (["shuin/cli.py"], 1, 0),
    ([], 0, 0),
])
def test_other(files, additions, deletions):
    from shuin.pr_class import classify_pr

    assert classify_pr(files, additions, deletions) == "other"


@pytest.mark.parametrize("path", [
    "githooks/commit-msg",
    ".githooks/x.md",
    ".github/x.md",
    ".husky/x.md",
    "skills/x/SKILL.md",
    ".cursor/x.md",
    "docs/githooks/x.md",
    "docs/.githooks/x.md",
    "sub/.github/x.yml",
    "docs/.husky/x.md",
    "docs/skills/x.md",
    "docs/.cursor/x.md",
    "AGENTS.md",
    "CLAUDE.md",
    "install.sh",
    "SECURITY.md",
    "LICENSE",
    "LICENSE.md",
    "docs/LICENSE.md",
    "License.md",
    "LICENSE.txt",
    "pyproject.toml",
    "docs/AGENTS.md",
    "packages/x/CLAUDE.md",
    "docs/install.sh",
    "a/b/SECURITY.md",
    "docs/LICENSE",
    "docs/pyproject.toml",
    "Agents.md",
    "CLAUDE.MD",
    ".GitHub/x.md",
    "Skills/x.md",
    "Security.md",
    "Githooks/x",
    "docs/AGeNTS.md",
    "docs/.GiTHuB/x.md",
    "./.github/x.md",
])
def test_guarded_paths(path):
    from shuin.pr_class import classify_pr

    assert classify_pr([path], 1, 0) == "other"
    assert classify_pr([path, "tests/test_x.py"], 1, 0) == "other"


@pytest.mark.parametrize("path", [
    "",
    "/README.md",
    "//docs/x.md",
    "../README.md",
    "docs/../x.md",
    "docs/a/../../x.md",
    "docs/..",
    r"docs\x.md",
    r"C:\docs\x.md",
    "C:.github/x.md",
    "docs/a:b.md",
])
def test_invalid_paths(path):
    from shuin.pr_class import classify_pr

    assert classify_pr([path], 1, 0) == "other"
    assert classify_pr([path, "tests/test_x.py"], 1, 0) == "other"


@pytest.mark.parametrize("path", [None, 5, b"README.md", ["README.md"]])
@pytest.mark.parametrize("files", [[], ["tests/test_x.py"]])
def test_non_string_paths(path, files):
    from shuin.pr_class import classify_pr

    assert classify_pr([path, *files], 1, 0) == "other"


@pytest.mark.parametrize("count", [-1, None, "5", 1.5, True, False])
@pytest.mark.parametrize("files", [
    ["README.md"],
    ["tests/test_x.py"],
    ["shuin/cli.py", "tests/test_cli.py"],
])
def test_invalid_counts(files, count):
    from shuin.pr_class import classify_pr

    assert classify_pr(files, count, 0) == "other"
    assert classify_pr(files, 0, count) == "other"
