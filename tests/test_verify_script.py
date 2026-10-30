import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def identity():
    return {
        "head_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "python": subprocess.check_output(
            [sys.executable, "--version"], text=True
        ).splitlines()[0],
    }


def start(out, pytest_cmd="echo '2 passed in 0.01s'; echo ''; exit 0",
          hooks_cmd="echo 'githooks: 4 passed, 0 failed'; exit 0"):
    return subprocess.Popen(
        [str(ROOT / "scripts/verify.sh")],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHON": sys.executable,
            "VERIFY_OUT": str(out),
            "VERIFY_PYTEST_CMD": pytest_cmd,
            "VERIFY_HOOKS_CMD": hooks_cmd,
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def summary(process, out, exit_code):
    stdout, stderr = process.communicate(timeout=15)
    assert process.returncode == exit_code, stderr
    assert stdout == f"{out / 'verify.json'}\n"
    return json.loads((out / "verify.json").read_text())


def test_both_pass(tmp_path, identity):
    out = tmp_path / "new output"
    assert summary(start(out), out, 0) == {
        **identity,
        "pytest": {"passed": 2, "failed": 0, "exit": 0},
        "githooks": {"passed": 4, "failed": 0, "exit": 0},
        "ok": True,
    }


def test_hooks_fail(tmp_path, identity):
    process = start(
        tmp_path, hooks_cmd="echo 'githooks: 4 passed, 1 failed'; exit 1"
    )
    assert summary(process, tmp_path, 1) == {
        **identity,
        "pytest": {"passed": 2, "failed": 0, "exit": 0},
        "githooks": {"passed": 4, "failed": 1, "exit": 1},
        "ok": False,
    }


def test_pytest_counts_missing(tmp_path, identity):
    process = start(tmp_path, pytest_cmd="echo 'nothing useful'; exit 0")
    assert summary(process, tmp_path, 1) == {
        **identity,
        "pytest": {"passed": None, "failed": None, "exit": 0},
        "githooks": {"passed": 4, "failed": 0, "exit": 0},
        "ok": False,
    }


def test_concurrent_runs(tmp_path, identity):
    first_out, second_out = tmp_path / "first", tmp_path / "second"
    first = start(
        first_out,
        pytest_cmd="echo \"$TMPDIR\"; echo '2 passed'; exit 0",
        hooks_cmd="echo \"$TMPDIR\"; echo 'githooks: 4 passed, 0 failed'; exit 0",
    )
    second = start(
        second_out,
        pytest_cmd="echo \"$TMPDIR\"; echo '1 failed, 7 passed'; exit 1",
        hooks_cmd="echo \"$TMPDIR\"; echo 'githooks: 9 passed, 0 failed'; exit 0",
    )
    assert summary(first, first_out, 0) == {
        **identity,
        "pytest": {"passed": 2, "failed": 0, "exit": 0},
        "githooks": {"passed": 4, "failed": 0, "exit": 0},
        "ok": True,
    }
    assert summary(second, second_out, 1) == {
        **identity,
        "pytest": {"passed": 7, "failed": 1, "exit": 1},
        "githooks": {"passed": 9, "failed": 0, "exit": 0},
        "ok": False,
    }
    for out in (first_out, second_out):
        pytest_tmp = Path((out / "pytest.log").read_text().splitlines()[0])
        hooks_tmp = Path((out / "githooks.log").read_text().splitlines()[0])
        assert pytest_tmp.is_dir() and pytest_tmp.parent == out
        assert hooks_tmp.is_dir() and hooks_tmp.parent == out
        assert pytest_tmp != hooks_tmp
