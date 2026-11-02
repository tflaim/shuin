import pytest

from tests.test_brief_lint import PROMPT, SPEC, brief, checks

NAMED = "/opt/homebrew/bin/python3.14 -m pytest"


@pytest.mark.parametrize("cmd", [
    "python -m pytest",
    "python3 -m pytest",
    "python3.12 -m pytest",
    "pip install -q pytest && /opt/homebrew/bin/python3.14 -m pytest",
    "cd web && python3 -m pytest",
    "PYTHONDONTWRITEBYTECODE=1 python -m pytest",
    "true; pip3 install pytest",
])
def test_bare_python_rejected(tmp_path, cmd):
    assert checks(brief(tmp_path, SPEC.replace(NAMED, cmd))) == ["bare-python"]


@pytest.mark.parametrize("cmd", [
    "V=$(mktemp -d)/venv && /opt/homebrew/bin/python3.14 -m venv $V && $V/bin/pip install -q pytest $d/src && $V/bin/python -m pytest -q",
    "$V/bin/python -m pip install -q $d/src",
    "sh githooks/test.sh",
])
def test_named_interpreter_passes(tmp_path, cmd):
    # Single quotes so `. spec.env` does not expand $(...) or $V.
    spec = SPEC.replace(f'"{NAMED}"', f"'{cmd}'")
    assert checks(brief(tmp_path, spec)) == []


@pytest.mark.parametrize("line", [
    "Make one commit with the change.",
    "Then git commit -m fix.",
    "Commit your work when tests pass.",
    "Push to the branch.",
    "Commit your changes.",
    "When tests pass, run git commit with the title.",
    "Make one commit.",
    "Push the branch.",
    "1. Then commit it.",
    "- You should push to origin.",
    "No AI attribution anywhere. One commit, authored and committed by Thomas Flaim, no trailers.",
])
def test_commit_instruction_rejected(tmp_path, line):
    assert checks(brief(tmp_path, prompt=PROMPT + line + "\n")) == ["commit-instruction"]


@pytest.mark.parametrize("line", [
    "Do not commit or push; the bridge commits.",
    "Never run git commit.",
    "Leave the working tree uncommitted.",
    "- fail_revert: a commit on the default branch whose message reverts another.",
    "The bridge commits; the result must be authored by Thomas Flaim with no trailers.",
    "Read the commit message and the push event payload.",
    "Do not commit, and do not push the branch.",
    "A new commit appears on main after merge.",
    "Never run git commit, git push or any branch command.",
])
def test_commit_prohibition_passes(tmp_path, line):
    assert checks(brief(tmp_path, prompt=PROMPT + line + "\n")) == []


def test_allowed_paths_must_be_inline(tmp_path):
    from scripts.brief_lint import lint

    prompt = PROMPT.replace("Add tests/test_brief_lint.py", "Add a test file")
    assert lint(brief(tmp_path, prompt=prompt)) == [{
        "check": "paths-not-inline",
        "message": "Prompt must list every ALLOWED_PATHS entry: tests/test_brief_lint.py",
    }]


def test_branch_key_rejected(tmp_path):
    # The bridge parser also rejects BRANCH as an unknown key.
    assert sorted(checks(brief(tmp_path, SPEC + "BRANCH=tflaim/other\n"))) == ["branch-key", "spec-parse"]


VENDOR_SPEC = SPEC.replace("tests/test_brief_lint.py\"", "tests/test_brief_lint.py web/vendor/rough.js\"")
VENDOR_PROMPT = PROMPT + "Copy web/vendor/rough.js from the staged folder.\n"


def test_vendor_without_manifest_test_rejected(tmp_path):
    assert checks(brief(tmp_path, VENDOR_SPEC, VENDOR_PROMPT)) == ["vendor-manifest"]


def test_vendor_with_manifest_test_passes(tmp_path):
    prompt = VENDOR_PROMPT + (
        "Out of scope, read only: web/vendor/manifest.json.\n"
        "Add a test in tests/test_brief_lint.py that every manifest entry is a file whose sha256 matches.\n"
    )
    assert checks(brief(tmp_path, VENDOR_SPEC, prompt)) == []


def test_no_vendor_no_manifest_rule(tmp_path):
    assert "vendor-manifest" not in checks(brief(tmp_path))


SWIFT_SPEC = SPEC.replace("tests/test_brief_lint.py\"", "tests/test_brief_lint.py Tests/ViewTests.swift\"")
SWIFT_PROMPT = PROMPT + "Tests: add Tests/ViewTests.swift.\n"


def test_swift_writes_get_only_environment(tmp_path):
    prompt = SWIFT_PROMPT + "Render the view with .environment(\\.colorSchemeContrast, .increased); inject nothing else.\n"
    assert checks(brief(tmp_path, SWIFT_SPEC, prompt)) == ["swift-readonly-env"]


def test_swift_names_get_only_key_without_injection(tmp_path):
    prompt = SWIFT_PROMPT + "Cover the colorSchemeContrast increased case.\n"
    assert checks(brief(tmp_path, SWIFT_SPEC, prompt)) == ["swift-readonly-env"]


def test_swift_injected_input_passes(tmp_path):
    prompt = SWIFT_PROMPT + (
        "The view reads colorSchemeContrast from @Environment and passes it to an init parameter;"
        " tests inject .increased through that init.\n"
    )
    assert checks(brief(tmp_path, SWIFT_SPEC, prompt)) == []


def test_swift_settable_key_passes(tmp_path):
    prompt = SWIFT_PROMPT + "Render with .environment(\\.colorScheme, .dark).\n"
    assert checks(brief(tmp_path, SWIFT_SPEC, prompt)) == []


def test_swift_quoting_the_failure_passes(tmp_path):
    prompt = SWIFT_PROMPT + (
        "The failing test sets .environment(\\.colorSchemeContrast, contrast), but that key is get-only."
        " Inject contrast through the init instead.\n"
    )
    assert checks(brief(tmp_path, SWIFT_SPEC, prompt)) == []

ESC = "'\\''"
GOOD_PARSE = [
    "TITLE=Fail briefs that do not parse",
    "TEST_CMD=V=$(mktemp -d)/venv && /opt/homebrew/bin/python3.14 -m venv $V && $V/bin/python -m pytest",
    "TEST_CMD=node -e 'console.log(1)'",
    "TITLE='Fail briefs that do not parse'",
    'TITLE="Fail briefs that do not parse"',
]
BAD_PARSE = [
    ("TEST_CMD='node -e " + ESC + "console.log(1)" + ESC + "'", "literal"),
    ("TITLE='unclosed", "unmatched"),
    ('TITLE=unclosed"', "unmatched"),
    ("NAME=x", "unknown key"),
    ("export TITLE=x", "unknown key"),
    ("BODY_FILE=", "empty"),
]


def _with_line(line):
    key = line.split("=", 1)[0]
    kept = [old for old in SPEC.splitlines() if not old.startswith(key + "=")]
    return "\n".join(kept + [line]) + "\n"


def _parse_messages(tmp_path, spec):
    from scripts.brief_lint import lint

    return [f["message"] for f in lint(brief(tmp_path, spec)) if f["check"] == "spec-parse"]


@pytest.mark.parametrize("line", GOOD_PARSE)
def test_spec_parse_clean(tmp_path, line):
    assert "spec-parse" not in checks(brief(tmp_path, _with_line(line)))


@pytest.mark.parametrize("line,words", BAD_PARSE)
def test_spec_parse_rejected(tmp_path, line, words):
    assert any(words in message for message in _parse_messages(tmp_path, _with_line(line)))


def test_spec_parse_duplicate_key(tmp_path):
    messages = _parse_messages(tmp_path, SPEC + "TITLE=Again\n")
    assert any("duplicate key TITLE" in message for message in messages)


def test_spec_parse_runs_nothing(tmp_path):
    marker = tmp_path / "ran"
    spec = _with_line(f"TEST_CMD=$(touch {marker}); `touch {marker}`; touch {marker}")
    assert "spec-parse" not in checks(brief(tmp_path, spec))
    assert marker.exists() is False


def test_spec_parse_missing_bridge_lib_is_an_error(tmp_path, monkeypatch):
    import scripts.brief_lint as brief_lint

    monkeypatch.setattr(brief_lint, "BRIDGE_LIB", tmp_path / "absent.sh")
    assert any("bridge-lib.sh not found" in message for message in _parse_messages(tmp_path, SPEC))

def test_commit_inside_a_path_is_not_a_commit_line(tmp_path):
    prompt = PROMPT + "The diff is staged at /Users/taco/codex-bridge/briefs/brief-lint-commit-narrow/staged/x.patch.\n"
    assert "attribution" not in checks(brief(tmp_path, prompt=prompt))


TEMP_COPY = (
    "d=$(mktemp -d) && cp -R . $d/src && rm -rf $d/src/.git && /opt/homebrew/bin/python3.14 -m venv $d/v"
    " && $d/v/bin/pip install -q pytest $d/src && env PYTHONDONTWRITEBYTECODE=1 $d/v/bin/python -m pytest -q"
    " -p no:cacheprovider"
)


@pytest.mark.parametrize("cmd", [
    "V=$(mktemp -d)/venv && /opt/homebrew/bin/python3.14 -m venv $V && $V/bin/pip install -q pytest . && $V/bin/python -m pytest -q",
    "$V/bin/pip install -e . && $V/bin/python -m pytest",
    "$V/bin/pip install ./ && $V/bin/python -m pytest",
    "$V/bin/python -m pip install -q .[dev] && $V/bin/python -m pytest",
    "$V/bin/pip install . && $V/bin/python -m pytest; rm -rf build",
])
def test_pip_install_tree_rejected(tmp_path, cmd):
    assert "pip-install-tree" in checks(brief(tmp_path, _with_line("TEST_CMD=" + cmd)))


@pytest.mark.parametrize("cmd", [
    TEMP_COPY,
    "$V/bin/pip install . && $V/bin/python -m pytest; s=$?; rm -rf build *.egg-info; exit $s",
    "$V/bin/pip install -q -r requirements.txt && $V/bin/python -m pytest",
    "$V/bin/pip install -q pytest && $V/bin/python -m pytest ./tests",
])
def test_pip_install_tree_passes(tmp_path, cmd):
    assert "pip-install-tree" not in checks(brief(tmp_path, _with_line("TEST_CMD=" + cmd)))
