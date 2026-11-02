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
    "true; pip3 install .",
])
def test_bare_python_rejected(tmp_path, cmd):
    assert checks(brief(tmp_path, SPEC.replace(NAMED, cmd))) == ["bare-python"]


@pytest.mark.parametrize("cmd", [
    "V=$(mktemp -d)/venv && /opt/homebrew/bin/python3.14 -m venv $V && $V/bin/pip install -q pytest . && $V/bin/python -m pytest -q",
    "$V/bin/python -m pip install -q .",
    "sh githooks/test.sh",
])
def test_named_interpreter_passes(tmp_path, cmd):
    assert checks(brief(tmp_path, SPEC.replace(NAMED, cmd))) == []


@pytest.mark.parametrize("line", [
    "Make one commit with the change.",
    "Then git commit -m fix.",
    "Commit your work when tests pass.",
    "Push to the branch.",
    "No AI attribution anywhere. One commit, authored and committed by Thomas Flaim, no trailers.",
])
def test_commit_instruction_rejected(tmp_path, line):
    assert checks(brief(tmp_path, prompt=PROMPT + line + "\n")) == ["commit-instruction"]


@pytest.mark.parametrize("line", [
    "Do not commit or push; the bridge commits.",
    "Never run git commit.",
    "Leave the working tree uncommitted.",
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
    assert checks(brief(tmp_path, SPEC + "BRANCH=tflaim/other\n")) == ["branch-key"]


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
