import subprocess
import sys
from pathlib import Path

import pytest


SPEC = """SLUG=tflaim/brief-lint
REPO_URL=https://github.com/tflaim/shuin.git
BASE=main
MODEL=small-model
EFFORT=high
TIMEOUT_MIN=60
ALLOWED_PATHS="scripts/brief_lint.py tests/test_brief_lint.py"
TEST_CMD="python -m pytest"
PROMPT_FILE=prompt.md
TITLE="Check job briefs"
"""
PROMPT = """Goal. Check briefs before queueing.
Data shape: Finding has check and message fields.
Change. Add scripts/brief_lint.py.
Tests: Add tests/test_brief_lint.py and report exact counts.
"""
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "brief_lint.py"


def brief(tmp_path, spec=SPEC, prompt=PROMPT):
    (tmp_path / "spec.env").write_text(spec, encoding="utf-8")
    (tmp_path / "prompt.md").write_text(prompt, encoding="utf-8")
    return tmp_path


def checks(folder):
    from scripts.brief_lint import lint

    return [finding["check"] for finding in lint(folder)]


def test_clean(tmp_path):
    from scripts.brief_lint import lint

    assert lint(brief(tmp_path)) == []


@pytest.mark.parametrize("key", [
    "SLUG", "REPO_URL", "BASE", "MODEL", "EFFORT", "TIMEOUT_MIN",
    "ALLOWED_PATHS", "TEST_CMD", "PROMPT_FILE", "TITLE",
])
@pytest.mark.parametrize("value", [None, "", '"  "', "''"])
def test_missing_key(tmp_path, key, value):
    lines = [line for line in SPEC.splitlines() if not line.startswith(key + "=")]
    if value is not None:
        lines.append(f"{key}={value}")
    assert checks(brief(tmp_path, "\n".join(lines))) == ["missing-key"]


@pytest.mark.parametrize("slug", ["other/job", "tflaim/", "tflaim/a/b", "tflaim/a b", "tflaim/a$"])
def test_slug_prefix(tmp_path, slug):
    assert checks(brief(tmp_path, SPEC.replace("tflaim/brief-lint", slug))) == ["slug-prefix"]


@pytest.mark.parametrize("url", [
    "http://github.com/tflaim/shuin.git", "https://github.com/other/shuin.git",
    "https://github.com/tflaim/.git", "https://github.com/tflaim/shuin",
    "https://github.com/tflaim/a/b.git", "https://github.com/tflaim/shuin.git?x=1",
])
def test_repo_url(tmp_path, url):
    spec = SPEC.replace("https://github.com/tflaim/shuin.git", url)
    assert checks(brief(tmp_path, spec)) == ["repo-url"]


@pytest.mark.parametrize("path", [
    ".", "./", "./*", "*", "**", "**/*", "./**", "/tmp/file.py",
    "src/../file.py", "../src", "src/foo..bar", "githooks", "githooks/",
    "githooks/test.sh", "./githooks/**", "./**/*", "././*",
])
def test_broad_paths(tmp_path, path):
    spec = SPEC.replace("tests/test_brief_lint.py\"", f"tests/test_brief_lint.py {path}\"")
    assert checks(brief(tmp_path, spec)) == ["broad-paths"]


def test_prompt_outside_paths(tmp_path):
    from scripts.brief_lint import lint

    folder = brief(tmp_path, prompt=PROMPT + "Edit `shuin/cli.py`, then shuin/cli.py.\n")
    assert lint(folder) == [{
        "check": "prompt-outside-paths",
        "message": "shuin/cli.py: the diff gate would reject this file",
    }]


@pytest.mark.parametrize("title", ["x" * 73, "Check job briefs."])
def test_title(tmp_path, title):
    assert checks(brief(tmp_path, SPEC.replace("Check job briefs", title))) == ["title"]


@pytest.mark.parametrize("section", ["Goal", "Data shape", "Change", "Tests", "exact counts"])
def test_missing_section(tmp_path, section):
    assert checks(brief(tmp_path, prompt=PROMPT.replace(section, "Other"))) == ["missing-section"]


@pytest.mark.parametrize("text", [
    "Add " + "Co-authored" + "-by: Helper",
    "Add " + "Co-authored" + " by: Helper",
    "Add " + "Generated" + " with a helper",
    "Use noreply@" + "anth" + "ropic.com",
    "Use helper@users.noreply." + "git" + "hub.com",
    "Mention " + "Clau" + "de in commit messages",
    "Credit " + "Cur" + "sor in the commit message",
    "Put " + "Open" + "AI in the commit body",
    "Add " + "Co" + "dex to the commit message",
    "Mention " + "Chat" + "GPT in the commit subject",
    "Mention " + "GitHub" + "Co" + "pilot in the commit message",
    "Credit " + "Gem" + "ini in the commit",
])
@pytest.mark.parametrize("source", ["prompt", "title", "body"])
def test_attribution(tmp_path, text, source):
    spec, prompt = SPEC, PROMPT
    if source == "prompt":
        prompt += text + "\n"
    elif source == "title":
        spec = spec.replace("Check job briefs", text)
    else:
        spec += "BODY_FILE=body.md\n"
        (tmp_path / "body.md").write_text(text, encoding="utf-8")
    assert checks(brief(tmp_path, spec, prompt)) == ["attribution"]


@pytest.mark.parametrize("key,value", [
    ("EFFORT", "none"), ("EFFORT", "minimal"), ("EFFORT", "HIGH"),
    ("TIMEOUT_MIN", "0"), ("TIMEOUT_MIN", "-1"), ("TIMEOUT_MIN", "241"),
    ("TIMEOUT_MIN", "1.5"), ("TIMEOUT_MIN", "ten"),
    pytest.param("TIMEOUT_MIN", "9" * 5000, id="oversized-timeout"),
    ("BASE", "../main"), ("BASE", "-main"), ("BASE", "main.lock"),
    ("MODEL", "bad model"),
])
def test_invalid_field(tmp_path, key, value):
    lines = [line for line in SPEC.splitlines() if not line.startswith(key + "=")]
    assert checks(brief(tmp_path, "\n".join(lines) + f"\n{key}={value}\n")) == ["missing-key"]


@pytest.mark.parametrize("key,value,expected", [
    ("SLUG", "tflaim/job ", ["slug-prefix"]),
    ("REPO_URL", "https://github.com/tflaim/shuin.git ", ["repo-url"]),
    ("BASE", "main ", ["missing-key"]),
    ("MODEL", "small-model ", ["missing-key"]),
    ("EFFORT", " high", ["missing-key"]),
    ("TIMEOUT_MIN", "60 ", ["missing-key"]),
])
def test_quoted_values_keep_their_content(tmp_path, key, value, expected):
    lines = [line for line in SPEC.splitlines() if not line.startswith(key + "=")]
    assert checks(brief(tmp_path, "\n".join(lines) + f'\n{key}="{value}"\n')) == expected


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
@pytest.mark.parametrize("timeout", ["1", "240"])
def test_valid_field_boundaries(tmp_path, effort, timeout):
    spec = SPEC.replace("EFFORT=high", f"EFFORT={effort}").replace("TIMEOUT_MIN=60", f"TIMEOUT_MIN={timeout}")
    spec = spec.replace("tflaim/brief-lint", "tflaim/a.B_2-3")
    spec = spec.replace("Check job briefs", "x" * 72)
    assert checks(brief(tmp_path, spec)) == []


@pytest.mark.parametrize("quote", ["'", '"', ""])
def test_spec_is_data(tmp_path, quote):
    marker = tmp_path / "executed"
    spec = "\n# Comment\n  # Another comment\n\n" + "\n".join(
        "export " + line.replace('"', quote) for line in SPEC.splitlines()
    )
    spec = spec.replace("python -m pytest", f"$(touch {marker}) `touch {marker}`")
    assert checks(brief(tmp_path, spec)) == []
    assert marker.exists() is False


@pytest.mark.parametrize("line", ["invalid line", "export NO_EQUALS", "TITLE='unclosed"])
def test_malformed_spec(tmp_path, line):
    assert checks(brief(tmp_path, SPEC + line + "\n")) == ["missing-key"]


def test_missing_spec(tmp_path):
    assert checks(tmp_path) == ["missing-key"]


@pytest.mark.parametrize("key", ["PROMPT_FILE", "BODY_FILE"])
@pytest.mark.parametrize("filename", ["absent.md", "../outside.md", "/tmp/outside.md", "directory", "escape.md", ""])
def test_job_files_are_confined(tmp_path, key, filename):
    (tmp_path / "directory").mkdir()
    (tmp_path.parent / "outside.md").write_text(PROMPT, encoding="utf-8")
    (tmp_path / "escape.md").symlink_to(tmp_path.parent / "outside.md")
    spec = SPEC.replace("PROMPT_FILE=prompt.md", f"PROMPT_FILE={filename}") if key == "PROMPT_FILE" else SPEC + f"BODY_FILE={filename}\n"
    assert checks(brief(tmp_path, spec)) == ["missing-key"]


def test_nested_job_files(tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "prompt.md").write_text(PROMPT, encoding="utf-8")
    (tmp_path / "notes" / "body.md").write_text("Check briefs", encoding="utf-8")
    spec = SPEC.replace("PROMPT_FILE=prompt.md", "PROMPT_FILE=notes/prompt.md") + "BODY_FILE=notes/body.md\n"
    assert checks(brief(tmp_path, spec)) == []


@pytest.mark.parametrize("allowed,path,expected", [
    ("src/*.py", "src/main.py", []),
    ("src/*.py", "src/nested/main.py", ["prompt-outside-paths"]),
    ("src/**/*.py", "src/main.py", []),
    ("src/**/*.py", "src/nested/main.py", []),
    ("src/**", "src/a/b/main.py", []),
    ("src", "src/a/b/main.py", []),
    ("src/", "src/a/b/main.py", []),
    ("./src/", "./src/a/main.py", []),
    ("src/module*", "src/module/sub/file.py", []),
    ("src/m?in.py", "src/main.py", []),
    ("src", "src-other/main.py", ["prompt-outside-paths"]),
])
def test_globs(tmp_path, allowed, path, expected):
    spec = SPEC.replace("scripts/brief_lint.py tests/test_brief_lint.py", allowed)
    prompt = "Goal: Check files.\nData shape: Fields.\nChange: Edit " + path + ".\nTests: Report exact counts.\n"
    assert checks(brief(tmp_path, spec, prompt)) == expected


@pytest.mark.parametrize("path", [
    "stray.py", "stray.sh", "stray.md", "stray.swift", "stray.ts", "stray.json",
    "src/Makefile", "tests/fixture", "shuin/fonts", "scripts/tool",
    "../scripts/brief_lint.py", "/scripts/brief_lint.py",
    "scripts/../scripts/brief_lint.py", "stray.py:12", "src/Makefile:12",
])
def test_repo_path_tokens(tmp_path, path):
    assert checks(brief(tmp_path, prompt=PROMPT + f"Edit `{path}`.\n")) == ["prompt-outside-paths"]


def test_prompt_prose_urls_and_exclusions(tmp_path):
    prompt = PROMPT + """Support and/or TCP/IP; see https://example.com/outside.py and mailto:help@example.com.
Read www.example.com/outside.py and <https://example.com/another.py>.
Do not edit shuin/cli.py or stray.py.
Out of scope: shuin/db.py.
Keep scripts/brief_lint.py; DO NOT EDIT docs/readme.md.
"""
    assert checks(brief(tmp_path, prompt=prompt)) == []


def test_path_exclusion_ends_at_newline(tmp_path):
    prompt = PROMPT + "Do not edit stray.py.\nEdit other.py.\n"
    assert checks(brief(tmp_path, prompt=prompt)) == ["prompt-outside-paths"]


def test_path_before_exclusion_is_checked(tmp_path):
    prompt = PROMPT + "Edit stray.py; do not edit other.py.\n"
    assert checks(brief(tmp_path, prompt=prompt)) == ["prompt-outside-paths"]


@pytest.mark.parametrize("phrase", ["do not add", "Never include", "Do NOT use", "Do not mention"])
def test_attribution_prohibition(tmp_path, phrase):
    prompt = PROMPT + phrase + " Co-authored" + "-by or Generated" + " with or Clau" + "de in commit messages.\n"
    assert checks(brief(tmp_path, prompt=prompt)) == []


def test_attribution_prohibition_does_not_hide_request(tmp_path):
    prompt = PROMPT + "Do not add tests.\nAdd Co-authored" + " by: Helper\n"
    assert checks(brief(tmp_path, prompt=prompt)) == ["attribution"]


@pytest.mark.parametrize("text", [
    "Do not add tests; add Co-authored" + " by: Helper",
    "Do not add Co-authored" + "-by but mention Clau" + "de in the commit message",
    "Add Generated" + " with a helper; do not edit tests",
])
def test_attribution_clauses(tmp_path, text):
    assert checks(brief(tmp_path, prompt=PROMPT + text + "\n")) == ["attribution"]


def test_vendor_in_ordinary_prose(tmp_path):
    prompt = PROMPT + "Check the Cur" + "sor integration and Clau" + "de API.\n"
    assert checks(brief(tmp_path, prompt=prompt)) == []


def test_section_variants(tmp_path):
    prompt = PROMPT.replace("Goal.", "bUg:").replace("Data shape:", "DATA SHAPE.").replace("Change.", "change:").replace("Tests:", "tests.")
    assert checks(brief(tmp_path, prompt=prompt)) == []


@pytest.mark.parametrize("replacement", ["Goalkeeper.", "Goal ", "A Goal."])
def test_section_must_start_line_and_have_separator(tmp_path, replacement):
    assert checks(brief(tmp_path, prompt=PROMPT.replace("Goal.", replacement))) == ["missing-section"]


def test_cli_clean(tmp_path):
    folder = brief(tmp_path)
    result = subprocess.run([sys.executable, str(SCRIPT), str(folder)], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.splitlines() == []
    assert result.stderr == ""


def test_cli_dirty(tmp_path):
    folder = brief(tmp_path, SPEC.replace("tflaim/brief-lint", "other/job").replace("Check job briefs", "Check job briefs."))
    result = subprocess.run([sys.executable, str(SCRIPT), str(folder)], capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        "slug-prefix: SLUG must match tflaim/<letters, digits, dot, dash, underscore>",
        "title: TITLE must be at most 72 characters and must not end with a period",
    ]
    assert result.stderr == ""
