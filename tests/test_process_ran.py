import json
import socket
import sqlite3
import time

import pytest

from shuin.cli import main
from shuin.ledger import open_ledger

JOB = "tflaim/x-20261030T231500Z"
SECOND_JOB = "tflaim/y-20261030T231500Z"
STEPS = ("shuin-record", "verify", "waybook-warm-start")


@pytest.fixture
def db(tmp_path, clock, monkeypatch):
    monkeypatch.delenv("SHUIN_TOKEN", raising=False)
    monkeypatch.delenv("LEDGER_TOKEN", raising=False)
    monkeypatch.setattr("shuin.cli.open_ledger", lambda path: open_ledger(path, clock=lambda: clock["t"]))

    def offline(*args, **kwargs):
        pytest.fail("process commands must not use the network")

    monkeypatch.setattr(socket, "create_connection", offline)
    return tmp_path / "ledger.db"


def run(db, *args):
    return main(["--db", str(db), *args])


def record(db, job=JOB, steps=STEPS):
    for step in steps:
        status = "passed" if step == "verify" else "ok"
        assert run(db, "process-ran", "--job", job, "--step", step, "--status", status) == 0


def rows(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT job_id, step, status, created_at FROM process_runs ORDER BY id").fetchall()


def test_three_steps_leave_three_rows_and_pass(db, clock, capsys):
    record(db, steps=("waybook-warm-start", "verify", "shuin-record"))
    assert rows(db) == [
        (JOB, "waybook-warm-start", "ok", clock["t"]),
        (JOB, "verify", "passed", clock["t"]),
        (JOB, "shuin-record", "ok", clock["t"]),
    ]
    assert capsys.readouterr() == ("", "")
    assert run(db, "process-check", "--since", "7d", "--json") == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "since": "7d", "jobs": [JOB], "missing": [], "absent": [], "no_jobs": False,
    }
    assert run(db, "process-check", "--since", "7d") == 0
    assert capsys.readouterr() == ("process-check: 1 jobs, 0 missing, 0 absent\n", "")


def test_second_job_names_its_missing_step(db, capsys):
    record(db)
    record(db, SECOND_JOB, ("verify", "shuin-record"))
    assert run(db, "process-check", "--since", "7d", "--json") == 1
    assert json.loads(capsys.readouterr().out) == {
        "since": "7d", "jobs": [JOB, SECOND_JOB],
        "missing": [{"job": SECOND_JOB, "step": "waybook-warm-start"}],
        "absent": [], "no_jobs": False,
    }
    assert run(db, "process-check", "--since", "7d") == 1
    assert capsys.readouterr() == (
        f"missing: {SECOND_JOB} waybook-warm-start\nprocess-check: 2 jobs, 1 missing, 0 absent\n", "",
    )


def test_silent_drop_is_absent_and_findings_are_sorted(db, capsys):
    record(db, SECOND_JOB, ("verify",))
    record(db, JOB, ("verify",))
    assert run(db, "process-check", "--since", "7d", "--json") == 1
    assert json.loads(capsys.readouterr().out) == {
        "since": "7d", "jobs": [JOB, SECOND_JOB],
        "missing": [
            {"job": JOB, "step": "shuin-record"},
            {"job": JOB, "step": "waybook-warm-start"},
            {"job": SECOND_JOB, "step": "shuin-record"},
            {"job": SECOND_JOB, "step": "waybook-warm-start"},
        ],
        "absent": ["shuin-record", "waybook-warm-start"], "no_jobs": False,
    }
    assert run(db, "process-check", "--since", "7d") == 1
    assert capsys.readouterr() == (
        f"missing: {JOB} shuin-record\nmissing: {JOB} waybook-warm-start\n"
        f"missing: {SECOND_JOB} shuin-record\nmissing: {SECOND_JOB} waybook-warm-start\n"
        "absent: shuin-record\nabsent: waybook-warm-start\n"
        "process-check: 2 jobs, 4 missing, 2 absent\n", "",
    )


def test_warm_start_absent_across_jobs(db, capsys):
    for job in (JOB, SECOND_JOB):
        record(db, job, ("verify", "shuin-record"))
    assert run(db, "process-check", "--since", "7d", "--json") == 1
    assert json.loads(capsys.readouterr().out)["absent"] == ["waybook-warm-start"]


@pytest.mark.parametrize("old_rows", [False, True])
def test_empty_window_reports_no_jobs(db, clock, capsys, old_rows):
    if old_rows:
        record(db)
        clock["t"] += 7 * 86400 + 1
    assert run(db, "process-check", "--since", "7d", "--json") == 1
    assert json.loads(capsys.readouterr().out) == {
        "since": "7d", "jobs": [], "missing": [],
        "absent": ["shuin-record", "verify", "waybook-warm-start"], "no_jobs": True,
    }
    assert run(db, "process-check", "--since", "7d") == 1
    assert capsys.readouterr() == (
        "absent: shuin-record\nabsent: verify\nabsent: waybook-warm-start\n"
        "no jobs in the last 7d\nprocess-check: 0 jobs, 0 missing, 3 absent\n", "",
    )


@pytest.mark.parametrize("status", ["ok", "missing", "error", "not-run", "passed", "failed", "timeout", "none"])
def test_every_status_counts_and_repeated_signals_append(db, capsys, status):
    record(db)
    before = rows(db)
    assert run(db, "process-ran", "--job", JOB, "--step", "verify", "--status", status) == 0
    assert rows(db)[:3] == before
    assert len(rows(db)) == 4
    assert rows(db)[3][0:3] == (JOB, "verify", status)
    assert run(db, "process-check", "--since", "7d", "--json") == 0
    assert json.loads(capsys.readouterr().out) == {
        "since": "7d", "jobs": [JOB], "missing": [], "absent": [], "no_jobs": False,
    }


@pytest.mark.parametrize("job,step,status,error", [
    ("", "verify", "ok", "Process job must be 1-200 printable characters with no whitespace."),
    ("x" * 201, "verify", "ok", "Process job must be 1-200 printable characters with no whitespace."),
    ("has space", "verify", "ok", "Process job must be 1-200 printable characters with no whitespace."),
    ("has\ttab", "verify", "ok", "Process job must be 1-200 printable characters with no whitespace."),
    ("has\x00null", "verify", "ok", "Process job must be 1-200 printable characters with no whitespace."),
    (JOB, "waybook", "ok", "Process step is not usable."),
    (JOB, "verify", "skipped", "Process status is not usable."),
])
def test_bad_signal_exits_one_on_stderr_without_a_row(db, capsys, job, step, status, error):
    assert run(db, "process-ran", "--job", job, "--step", step, "--status", status) == 1
    assert capsys.readouterr() == ("", error + "\n")
    assert rows(db) == []


@pytest.mark.parametrize("job", ["x", "x" * 200, "opaque/☃:id"])
def test_job_is_stored_verbatim_without_a_token(db, job):
    assert run(db, "process-ran", "--job", job, "--step", "verify", "--status", "ok") == 0
    assert rows(db)[0][0] == job


@pytest.mark.parametrize("since,seconds", [("1d", 86400), ("365d", 365 * 86400), ("1h", 3600), ("8760h", 8760 * 3600)])
def test_window_boundary_is_inclusive(db, clock, capsys, since, seconds):
    record(db)
    clock["t"] += seconds
    assert run(db, "process-check", "--since", since, "--json") == 0
    assert json.loads(capsys.readouterr().out) == {
        "since": since, "jobs": [JOB], "missing": [], "absent": [], "no_jobs": False,
    }
    clock["t"] += 1
    assert run(db, "process-check", "--since", since, "--json") == 1
    assert json.loads(capsys.readouterr().out)["no_jobs"] is True


@pytest.mark.parametrize("since", ["0d", "366d", "0h", "8761h", "7w", "1.5d", "-1d", "7d\n", ""])
def test_bad_window_exits_one(db, capsys, since):
    assert run(db, "process-check", "--since", since) == 1
    assert capsys.readouterr() == ("", "Since must be 1-365d or 1-8760h.\n")


def test_recent_job_keeps_older_steps_but_absence_uses_the_window(db, clock, capsys):
    record(db)
    clock["t"] += 7 * 86400 + 1
    record(db, steps=("verify",))
    assert run(db, "process-check", "--since", "7d", "--json") == 1
    assert json.loads(capsys.readouterr().out) == {
        "since": "7d", "jobs": [JOB], "missing": [],
        "absent": ["shuin-record", "waybook-warm-start"], "no_jobs": False,
    }


def test_ten_thousand_rows_finish_in_under_two_seconds(db, clock, capsys, monkeypatch):
    ledger = open_ledger(db, clock=lambda: clock["t"])
    try:
        with ledger.tx():
            for n in range(10000):
                ledger.record_process_run(f"job-{n // 3:04d}", STEPS[n % 3], "ok")
    finally:
        ledger.close()
    monkeypatch.setenv("SHUIN_DB", str(db))
    started = time.perf_counter()
    assert main(["process-check", "--since", "7d", "--json"]) == 1
    elapsed = time.perf_counter() - started
    assert json.loads(capsys.readouterr().out) == {
        "since": "7d", "jobs": [f"job-{n:04d}" for n in range(3334)],
        "missing": [
            {"job": "job-3333", "step": "verify"},
            {"job": "job-3333", "step": "waybook-warm-start"},
        ],
        "absent": [], "no_jobs": False,
    }
    assert elapsed < 2
