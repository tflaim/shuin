"""Detached worktree jobs. The command is a dummy script, the repo is temporary."""

import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from shuin.cli import main
from shuin.errors import LedgerError
from shuin.jobs import _process_start, job_status, require_writable, sandbox_roots, stop_job
from shuin.ledger import open_ledger

WORKER = """
import os, sys, time, pathlib, subprocess, signal
mode, pidfile, childfile = sys.argv[1:4]
os.makedirs(os.path.dirname(pidfile), exist_ok=True)
with open(pidfile, "w", encoding="utf-8") as handle:
    handle.write(str(os.getpid()))
if mode == "spawn":
    child = os.fork()
    if child == 0:
        with open(childfile, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        fd = os.open(childfile + ".ready", os.O_CREAT | os.O_WRONLY, 0o644)
        os.close(fd)
        time.sleep(120)
        os._exit(0)
    while not os.path.exists(childfile + ".ready"):
        time.sleep(0.01)
    time.sleep(120)
elif mode == "orphan":
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        with open(childfile, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        os.write(write_fd, b"x")
        os.close(write_fd)
        time.sleep(120)
        os._exit(0)
    os.close(write_fd)
    os.read(read_fd, 1)
    os.close(read_fd)
elif mode == "commit":
    pathlib.Path("note.txt").write_text("from the job\\n", encoding="utf-8")
    subprocess.check_call(["git", "add", "note.txt"])
    subprocess.check_call(["git", "-c", "commit.gpgsign=false", "commit", "-m", "job note"])
elif mode == "token":
    pathlib.Path(childfile).write_text(os.environ.get("SHUIN_TOKEN", ""), encoding="utf-8")
elif mode == "ignore":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = os.fork()
    if child == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with open(childfile, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        time.sleep(60)
        os._exit(0)
    while not os.path.exists(childfile) or os.path.getsize(childfile) == 0:
        time.sleep(0.01)
    time.sleep(60)
else:
    time.sleep(120)
"""


def _init_repo(path: Path) -> Path:
    repo = path / "repo"
    repo.mkdir()
    subprocess.check_call(["git", "init", "-q", "-b", "main", str(repo)])
    subprocess.check_call(["git", "-C", str(repo), "config", "user.email", "test@example.com"])
    subprocess.check_call(["git", "-C", str(repo), "config", "user.name", "Test"])
    subprocess.check_call(["git", "-C", str(repo), "config", "commit.gpgsign", "false"])
    subprocess.check_call(["git", "-C", str(repo), "config", "core.fsmonitor", "false"])
    subprocess.check_call(["git", "-C", str(repo), "config", "core.untrackedcache", "false"])
    (repo / "README").write_text("seed\n", encoding="utf-8")
    subprocess.check_call(["git", "-C", str(repo), "add", "README"])
    subprocess.check_call(["git", "-C", str(repo), "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"])
    return repo


def _init_ledger(path: Path) -> tuple[Path, str]:
    db = path / "ledger.db"
    assert main(["--db", str(db), "init"]) == 0
    token = (path / "owner.token").read_text(encoding="utf-8").strip()
    return db, token


def _script(path: Path) -> Path:
    script = path / "worker.py"
    script.write_text(WORKER, encoding="utf-8")
    return script


def _env() -> dict:
    env = os.environ.copy()
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.pop("SHUIN_DB", None)
    env.pop("SHUIN_TOKEN", None)
    env.pop("LEDGER_TOKEN", None)
    return env


def _popen(db, token, repo, args, command, env_extra=None) -> subprocess.Popen:
    argv = [
        sys.executable,
        "-m",
        "shuin",
        "--db",
        str(db),
        "--token",
        token,
        "--json",
        "job",
        "run",
        "--repo",
        str(repo),
        *args,
        "--",
        *command,
    ]
    env = _env()
    if env_extra:
        env.update(env_extra)
    return subprocess.Popen(
        argv,
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _launch(db, token, repo, args, command, env_extra=None) -> tuple[int, dict]:
    proc = _popen(db, token, repo, args, command, env_extra)
    out, err = proc.communicate(timeout=30)
    if proc.returncode != 0:
        raise AssertionError(f"launcher exited {proc.returncode}\n{out}\n{err}")
    return proc.pid, json.loads(out)


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _ps_int(pid: int, field: str) -> int:
    result = subprocess.run(
        ["ps", "-o", f"{field}=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in reversed(result.stdout.splitlines()):
        text = line.strip()
        if text.isdigit():
            return int(text)
    raise AssertionError(f"ps {field} for {pid}: {result.stdout!r} {result.stderr!r}")


def _proc(pid: int) -> dict:
    """ppid, process group, and session. No /proc."""
    return {"ppid": _ps_int(pid, "ppid"), "pgrp": os.getpgid(pid), "sid": os.getsid(pid)}


def _group(pgid: int) -> list[int]:
    """Pids whose process group is pgid. ``ps`` so this works without /proc."""
    result = subprocess.run(
        ["ps", "-A", "-o", "pid=,pgid="],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    found = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid, group = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if group == pgid:
            found.append(pid)
    return found


def _wait_file(path: Path, timeout: float = 10) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.is_file() and path.stat().st_size:
            return path.read_text(encoding="utf-8").strip()
        time.sleep(0.05)
    raise AssertionError(f"missing {path}")


def _wait_dead(pid: int, timeout: float = 10) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(pid):
            return
        time.sleep(0.05)
    raise AssertionError(f"pid {pid} still alive")


def _wait_status(db, job_id, states, timeout: float = 15) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = job_status(db, job_id)
        if last["status"] in states:
            return last
        time.sleep(0.05)
    raise AssertionError(last)


def _stop(db, job_id) -> None:
    try:
        stop_job(db, job_id)
    except LedgerError:
        pass


def test_job_preserves_global_options_after_separator(tmp_path):
    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    _launcher, record = _launch(
        db,
        token,
        repo,
        ["--branch", "feat/argv", "--id", "job-argv"],
        ["sh", "-c", 'printf "%s\\n" "$@"', "x", "--json", "--db", "a", "--token=b", "--no-color"],
    )
    try:
        finished = _wait_status(db, "job-argv", {"exited", "failed"})
        assert finished["exit_code"] == 0
        assert Path(record["log"]).read_text(encoding="utf-8") == "--json\n--db\na\n--token=b\n--no-color\n"
    finally:
        _stop(db, "job-argv")


def test_job_global_options_before_job(tmp_path):
    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    _launcher, record = _launch(
        db, token, repo, ["--branch", "feat/globals", "--id", "job-globals"], ["true"]
    )
    try:
        assert record["id"] == "job-globals"
        assert _wait_status(db, "job-globals", {"exited", "failed"})["exit_code"] == 0
    finally:
        _stop(db, "job-globals")


def test_job_global_options_before_separator(tmp_path, capsys):
    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    capsys.readouterr()
    assert main([
        "--db", str(db), "--token", token, "job", "run", "--repo", str(repo),
        "--branch", "feat/json", "--id", "job-json", "--json", "--", "true",
    ]) == 0
    record = json.loads(capsys.readouterr().out)
    try:
        assert record["id"] == "job-json"
        assert _wait_status(db, "job-json", {"exited", "failed"})["exit_code"] == 0
    finally:
        _stop(db, "job-json")


def test_job_survives_launcher_and_stop_kills_the_group(tmp_path):
    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    script = _script(tmp_path)
    pidfile = tmp_path / "pids" / "parent"
    childfile = tmp_path / "pids" / "child"
    launcher, record = _launch(
        db,
        token,
        repo,
        ["--branch", "feat/survive", "--agent", "worker-1", "--id", "job-survive"],
        [sys.executable, str(script), "spawn", str(pidfile), str(childfile)],
    )
    try:
        assert not _alive(launcher)
        parent = int(_wait_file(pidfile))
        child = int(_wait_file(childfile))
        assert _alive(parent)
        assert _alive(child)
        assert record["pid"] == parent
        assert record["pgid"] == parent
        assert record["worktree"]
        assert record["log"]
        assert record["status"] == "running"
        assert "token" not in record
        assert record["grace"] == 5
        on_disk = json.loads(Path(record["log"]).with_suffix(".json").read_text(encoding="utf-8"))
        assert "token" not in on_disk
        assert on_disk["pid"] == parent
        assert on_disk["pgid"] == parent
        assert on_disk["worktree"] == record["worktree"]
        assert on_disk["log"] == record["log"]
        ident = _proc(parent)
        assert ident["ppid"] != launcher
        assert ident["sid"] != os.getsid(0)
        assert ident["pgrp"] == record["pgid"]
        assert _proc(child)["pgrp"] == record["pgid"]
        assert _alive(record["supervisor"])
        stopped = stop_job(db, "job-survive")
        assert stopped["status"] == "stopped"
        _wait_dead(parent)
        _wait_dead(child)
        _wait_dead(record["supervisor"])
        assert _group(record["pgid"]) == []
    finally:
        _stop(db, "job-survive")


def test_exit_kills_orphaned_children(tmp_path):
    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    script = _script(tmp_path)
    pidfile = tmp_path / "pids" / "parent"
    childfile = tmp_path / "pids" / "child"
    _launcher, record = _launch(
        db,
        token,
        repo,
        ["--branch", "feat/orphan", "--agent", "worker-1", "--id", "job-orphan"],
        [sys.executable, str(script), "orphan", str(pidfile), str(childfile)],
    )
    try:
        child = int(_wait_file(childfile))
        finished = _wait_status(db, "job-orphan", {"exited"})
        assert finished["exit_code"] == 0
        _wait_dead(child)
        _wait_dead(record["supervisor"])
        assert _group(record["pgid"]) == []
    finally:
        _stop(db, "job-orphan")


def test_timeout_kills_the_group(tmp_path):
    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    script = _script(tmp_path)
    pidfile = tmp_path / "pids" / "parent"
    childfile = tmp_path / "pids" / "child"
    _launcher, record = _launch(
        db,
        token,
        repo,
        ["--branch", "feat/timeout", "--agent", "worker-1", "--id", "job-timeout", "--timeout", "1"],
        [sys.executable, str(script), "spawn", str(pidfile), str(childfile)],
    )
    try:
        parent = int(_wait_file(pidfile))
        child = int(_wait_file(childfile))
        finished = _wait_status(db, "job-timeout", {"timeout"}, timeout=15)
        assert finished["status"] == "timeout"
        _wait_dead(parent)
        _wait_dead(child)
        assert _group(record["pgid"]) == []
    finally:
        _stop(db, "job-timeout")


def test_job_can_commit_in_its_worktree(tmp_path):
    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    script = _script(tmp_path)
    pidfile = tmp_path / "pids" / "parent"
    _launcher, record = _launch(
        db,
        token,
        repo,
        ["--branch", "feat/commit", "--agent", "worker-1", "--id", "job-commit"],
        [sys.executable, str(script), "commit", str(pidfile), str(tmp_path / "unused")],
    )
    try:
        finished = _wait_status(db, "job-commit", {"exited", "failed"})
        log = Path(record["log"]).read_text(encoding="utf-8")
        assert finished["status"] == "exited", log
        assert finished["exit_code"] == 0, log
        worktree = record["worktree"]
        sha = subprocess.check_output(["git", "-C", worktree, "rev-parse", "HEAD"], text=True).strip()
        branch = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "feat/commit"], text=True).strip()
        assert sha == branch
        names = subprocess.check_output(
            ["git", "-C", worktree, "show", "--name-only", "--pretty=format:", "HEAD"],
            text=True,
        )
        assert "note.txt" in names.split()
        assert not (repo / "note.txt").exists()
        note = subprocess.check_output(["git", "-C", worktree, "show", "HEAD:note.txt"], text=True)
        assert note == "from the job\n"
    finally:
        _stop(db, "job-commit")


def test_second_run_reuses_the_agent_token(tmp_path):
    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    script = _script(tmp_path)
    first_out = tmp_path / "token-1"
    _launcher, first = _launch(
        db,
        token,
        repo,
        ["--branch", "feat/one", "--agent", "worker-1", "--id", "job-one"],
        [sys.executable, str(script), "token", str(tmp_path / "pid-1"), str(first_out)],
    )
    try:
        finished = _wait_status(db, "job-one", {"exited", "failed"})
        assert finished["status"] == "exited"
        minted = _wait_file(first_out)
        assert "token" not in first
        token_file = tmp_path / "jobs" / "tokens" / "worker-1"
        assert token_file.read_text(encoding="utf-8").strip() == minted
        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
        second_out = tmp_path / "token-2"
        _launcher, second = _launch(
            db,
            token,
            repo,
            ["--branch", "feat/two", "--agent", "worker-1", "--id", "job-two"],
            [sys.executable, str(script), "token", str(tmp_path / "pid-2"), str(second_out)],
        )
        assert "token" not in second
        assert _wait_file(second_out) == minted
        _wait_status(db, "job-two", {"exited"})
        ledger = open_ledger(db)
        try:
            count = ledger.conn.execute("SELECT COUNT(*) AS n FROM agents WHERE id='worker-1'").fetchone()["n"]
        finally:
            ledger.close()
        assert count == 1
    finally:
        _stop(db, "job-one")
        _stop(db, "job-two")


def test_preflight_names_unwritable_refs(tmp_path):
    repo = _init_repo(tmp_path)
    worktree = tmp_path / "wt"
    subprocess.check_call(["git", "-C", str(repo), "worktree", "add", "-b", "feat/probe", str(worktree), "HEAD"])
    roots = sandbox_roots(worktree)
    assert roots["gitdir"].parent.name == "worktrees"
    assert roots["gitdir"].is_relative_to(repo / ".git")
    assert roots["base"] == worktree.parent
    os.chmod(roots["refs"], 0o555)
    try:
        with pytest.raises(LedgerError) as raised:
            require_writable(roots)
    finally:
        os.chmod(roots["refs"], 0o755)
    assert raised.value.code == "sandbox_unwritable"
    message = raised.value.message
    assert str(roots["refs"]) in message
    assert "objects" in message
    assert ".git/worktrees/<name>" in message
    assert "worktree" in message
    assert "worktree base" in message


def test_launcher_stops_when_refs_are_not_writable(tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    capsys.readouterr()
    import shuin.jobs as jobs

    real = jobs.require_writable

    def wrapped(roots):
        os.chmod(roots["refs"], 0o555)
        try:
            return real(roots)
        finally:
            os.chmod(roots["refs"], 0o755)

    monkeypatch.setattr(jobs, "require_writable", wrapped)
    code = main(
        [
            "--db",
            str(db),
            "--token",
            token,
            "--json",
            "job",
            "run",
            "--repo",
            str(repo),
            "--branch",
            "feat/denied",
            "--agent",
            "worker-1",
            "--id",
            "job-denied",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ]
    )
    captured = capsys.readouterr()
    assert code == 1
    payload = json.loads(captured.out or captured.err)
    assert payload["error"] == "sandbox_unwritable"
    assert "refs" in payload["message"]
    assert ".git/worktrees" in payload["message"]
    listed = subprocess.check_output(["git", "-C", str(repo), "worktree", "list"], text=True)
    assert "feat/denied" not in listed
    branches = subprocess.check_output(["git", "-C", str(repo), "branch", "--list", "feat/denied"], text=True)
    assert branches.strip() == ""
    assert list((tmp_path / "jobs").glob("*.json")) == []


def test_windows_is_refused(tmp_path, monkeypatch, capsys):
    import shuin.jobs as jobs

    monkeypatch.setattr(jobs.os, "name", "nt")
    code = main(
        [
            "--db",
            str(tmp_path / "ledger.db"),
            "--json",
            "job",
            "run",
            "--branch",
            "feat/win",
            "--",
            "true",
        ]
    )
    captured = capsys.readouterr()
    assert code == 1
    payload = json.loads(captured.out or captured.err)
    assert payload["error"] == "job_unsupported"
    assert "Windows" in payload["message"]


def test_run_help_mentions_writable_roots(capsys):
    code = main(["job", "run", "--help"])
    assert code == 0
    text = capsys.readouterr().out
    assert ".git/worktrees/<name>" in text
    assert "worktree base" in text
    assert "objects" in text
    assert "refs" in text
    assert "--grace" in text


def test_command_is_required(capsys):
    code = main(["--json", "job", "run", "--branch", "feat/empty"])
    captured = capsys.readouterr()
    assert code == 1
    payload = json.loads(captured.out or captured.err)
    assert payload["error"] == "command_required"


def test_stop_finished_job_keeps_status_and_spares_a_reused_pid(tmp_path):
    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    _launcher, _record = _launch(
        db,
        token,
        repo,
        ["--branch", "feat/done", "--agent", "worker-1", "--id", "job-done"],
        [sys.executable, "-c", "import sys; sys.exit(7)"],
    )
    sleeper = None
    try:
        finished = _wait_status(db, "job-done", {"exited", "failed"})
        assert finished["status"] == "exited"
        assert finished["exit_code"] == 7
        stopped = stop_job(db, "job-done")
        assert stopped["status"] == "exited"
        assert stopped["exit_code"] == 7
        from shuin.jobs import _patch

        _patch(
            tmp_path / "jobs" / "job-done.json",
            status="stopped",
            exit_code=None,
            ended_at=1,
        )
        kept = job_status(db, "job-done")
        assert kept["status"] == "exited"
        assert kept["exit_code"] == 7

        sleeper = subprocess.Popen(["sleep", "120"], start_new_session=True)
        stale = {
            "id": "job-stale",
            "status": "running",
            "pid": sleeper.pid,
            "pgid": os.getpgid(sleeper.pid),
            "supervisor": sleeper.pid,
            "supervisor_start": "not-ours",
            "supervisor_pgid": os.getpgid(sleeper.pid),
            "exit_code": None,
            "stop_requested": False,
        }
        (tmp_path / "jobs" / "job-stale.json").write_text(json.dumps(stale), encoding="utf-8")
        with pytest.raises(LedgerError) as raised:
            stop_job(db, "job-stale", grace=0.2)
        assert raised.value.code == "legacy_record"
        assert f"kill -TERM -{os.getpgid(sleeper.pid)}" in raised.value.message
        again = job_status(db, "job-stale")
        assert again["status"] == "running"
        assert again["exit_code"] is None
        assert _alive(sleeper.pid)
    finally:
        if sleeper is not None and sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)
        _stop(db, "job-done")


def test_stop_while_starting_does_not_launch(tmp_path):
    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    script = _script(tmp_path)
    pidfile = tmp_path / "pids" / "parent"
    proc = _popen(
        db,
        token,
        repo,
        ["--branch", "feat/starting", "--agent", "worker-1", "--id", "job-starting", "--grace", "0"],
        [sys.executable, str(script), "spawn", str(pidfile), str(tmp_path / "pids" / "child")],
        env_extra={"SHUIN_JOB_START_DELAY": "30", "SHUIN_JOB_READY_TIMEOUT": "60"},
    )
    supervisor = None
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                current = job_status(db, "job-starting")
            except LedgerError:
                time.sleep(0.05)
                continue
            if current.get("status") == "starting" and current.get("supervisor"):
                supervisor = current["supervisor"]
                break
            time.sleep(0.05)
        else:
            out, err = proc.communicate(timeout=5)
            raise AssertionError(f"supervisor never registered\n{out}\n{err}")
        stop_job(db, "job-starting", grace=1)
        out, err = proc.communicate(timeout=20)
        assert proc.returncode == 0, f"{out}\n{err}"
        final = job_status(db, "job-starting")
        assert final["status"] == "stopped"
        assert final.get("pid") is None
        assert not pidfile.exists()
        _wait_dead(supervisor)
        assert Path(final["worktree"]).is_dir()
        branch = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "--verify", "feat/starting"], text=True)
        assert branch.strip()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
        _stop(db, "job-starting")


def test_start_timeout_does_not_launch_and_discards_the_worktree(tmp_path):
    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    script = _script(tmp_path)
    pidfile = tmp_path / "pids" / "parent"
    proc = _popen(
        db,
        token,
        repo,
        ["--branch", "feat/late", "--agent", "worker-1", "--id", "job-late", "--grace", "0"],
        [sys.executable, str(script), "spawn", str(pidfile), str(tmp_path / "pids" / "child")],
        env_extra={"SHUIN_JOB_START_DELAY": "30", "SHUIN_JOB_READY_TIMEOUT": "1"},
    )
    try:
        out, err = proc.communicate(timeout=20)
        assert proc.returncode == 1, out
        payload = json.loads(out or err)
        assert payload["error"] == "job_failed"
        assert not pidfile.exists()
        record = json.loads((tmp_path / "jobs" / "job-late.json").read_text(encoding="utf-8"))
        assert record["status"] == "failed"
        assert record["pid"] is None
        assert not Path(record["worktree"]).exists()
        branches = subprocess.check_output(["git", "-C", str(repo), "branch", "--list", "feat/late"], text=True)
        assert branches.strip() == ""
        if record.get("supervisor"):
            _wait_dead(int(record["supervisor"]))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)


def test_failed_launch_removes_worktree_and_created_branch(tmp_path):
    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    proc = _popen(
        db,
        token,
        repo,
        ["--branch", "feat/boom", "--agent", "worker-1", "--id", "job-boom"],
        ["/no/such/shuin-command"],
    )
    out, err = proc.communicate(timeout=20)
    assert proc.returncode == 1, out
    payload = json.loads(out or err)
    assert payload["error"] == "job_failed"
    listed = subprocess.check_output(["git", "-C", str(repo), "worktree", "list"], text=True)
    assert "feat/boom" not in listed
    branches = subprocess.check_output(["git", "-C", str(repo), "branch", "--list", "feat/boom"], text=True)
    assert branches.strip() == ""
    record = json.loads((tmp_path / "jobs" / "job-boom.json").read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert not Path(record["worktree"]).exists()


def test_job_rm_removes_the_worktree_and_keeps_the_branch(tmp_path, capsys):
    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    capsys.readouterr()
    _launcher, record = _launch(
        db,
        token,
        repo,
        ["--branch", "feat/keep", "--agent", "worker-1", "--id", "job-rm"],
        [sys.executable, "-c", "import sys; sys.exit(0)"],
    )
    try:
        finished = _wait_status(db, "job-rm", {"exited", "failed"})
        assert finished["status"] == "exited"
        worktree = Path(record["worktree"])
        assert worktree.is_dir()
        code = main(["--db", str(db), "--json", "job", "rm", "--id", "job-rm"])
        captured = capsys.readouterr()
        assert code == 0, captured.out
        assert not worktree.exists()
        assert not (tmp_path / "jobs" / "job-rm.json").exists()
        sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "feat/keep"], text=True).strip()
        assert sha
        missing = main(["--db", str(db), "--json", "job", "status", "--id", "job-rm"])
        assert missing == 1
        assert json.loads(capsys.readouterr().out)["error"] == "not_found"
    finally:
        _stop(db, "job-rm")

    proc = _popen(
        db,
        token,
        repo,
        ["--branch", "feat/live", "--agent", "worker-1", "--id", "job-live"],
        [sys.executable, "-c", "import time; time.sleep(120)"],
    )
    try:
        deadline = time.time() + 15
        current = None
        while time.time() < deadline:
            try:
                current = job_status(db, "job-live")
            except LedgerError:
                time.sleep(0.05)
                continue
            if current.get("status") == "running":
                break
            time.sleep(0.05)
        else:
            out, err = proc.communicate(timeout=5)
            raise AssertionError(f"job never ran\n{out}\n{err}\n{current}")
        code = main(["--db", str(db), "--json", "job", "rm", "--id", "job-live"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 1
        assert payload["error"] == "job_live"
        assert Path(current["worktree"]).is_dir()
        out, err = proc.communicate(timeout=20)
        assert proc.returncode == 0, f"{out}\n{err}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
        _stop(db, "job-live")


def test_caller_token_is_not_stored_or_printed(tmp_path, capsys):
    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    capsys.readouterr()
    code = main(
        [
            "--db",
            str(db),
            "--token",
            token,
            "--json",
            "agent",
            "mint",
            "--id",
            "worker-9",
            "--kind",
            "codex",
            "--role",
            "worker",
        ]
    )
    assert code == 0
    worker = json.loads(capsys.readouterr().out)["token"]
    script = _script(tmp_path)
    seen = tmp_path / "seen-token"
    _launcher, record = _launch(
        db,
        worker,
        repo,
        ["--branch", "feat/own", "--id", "job-own"],
        [sys.executable, str(script), "token", str(tmp_path / "pid-own"), str(seen)],
    )
    try:
        assert "token" not in record
        assert worker not in json.dumps(record)
        assert _wait_file(seen) == worker
        assert not (tmp_path / "jobs" / "tokens" / "worker-9").exists()
        _wait_status(db, "job-own", {"exited"})
    finally:
        _stop(db, "job-own")


def test_linked_worktree_uses_the_main_repo(tmp_path):
    repo = _init_repo(tmp_path)
    linked = tmp_path / "linked"
    subprocess.check_call(["git", "-C", str(repo), "worktree", "add", "-b", "feat/link", str(linked), "HEAD"])
    from shuin.jobs import _main_checkout

    assert _main_checkout(linked) == repo.resolve()
    db, token = _init_ledger(tmp_path)
    script = _script(tmp_path)
    _launcher, record = _launch(
        db,
        token,
        linked,
        ["--branch", "feat/from-link", "--agent", "worker-1", "--id", "job-link"],
        [sys.executable, str(script), "token", str(tmp_path / "pid-link"), str(tmp_path / "tok-link")],
    )
    try:
        assert Path(record["repo"]) == repo.resolve()
        assert Path(record["roots"]["base"]) == Path(record["worktree"]).parent
        gitdir = Path(record["roots"]["gitdir"])
        assert gitdir.parent.name == "worktrees"
        assert gitdir.is_relative_to((repo / ".git").resolve())
        _wait_status(db, "job-link", {"exited"})
    finally:
        _stop(db, "job-link")


def _wait_running(db, job_id, proc, timeout=15) -> dict:
    deadline = time.time() + timeout
    current = None
    while time.time() < deadline:
        try:
            current = job_status(db, job_id)
        except LedgerError:
            time.sleep(0.05)
            continue
        if current.get("status") == "running" and current.get("pid"):
            return current
        time.sleep(0.05)
    out, err = proc.communicate(timeout=5)
    raise AssertionError(f"{job_id} never ran\n{out}\n{err}\n{current}")


def test_second_run_keeps_the_live_worktree(tmp_path):
    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    proc = _popen(
        db,
        token,
        repo,
        ["--branch", "feat/dup", "--agent", "worker-1", "--id", "dup1"],
        ["sleep", "30"],
    )
    try:
        current = _wait_running(db, "dup1", proc)
        out, err = proc.communicate(timeout=15)
        assert proc.returncode == 0, f"{out}\n{err}"
        worktree = Path(current["worktree"])
        assert worktree.is_dir()
        pid = current["pid"]
        assert _alive(pid)
        second = _popen(
            db,
            token,
            repo,
            ["--branch", "feat/dup-again", "--agent", "worker-1", "--id", "dup1"],
            ["sleep", "30"],
        )
        out, err = second.communicate(timeout=20)
        assert second.returncode != 0, out
        payload = json.loads(out or err)
        assert payload["error"] == "exists"
        assert worktree.is_dir()
        again = job_status(db, "dup1")
        assert again["status"] == "running"
        assert again["worktree"] == str(worktree)
        assert _alive(pid)
        branches = subprocess.check_output(["git", "-C", str(repo), "branch", "--list", "feat/dup-again"], text=True)
        assert branches.strip() == ""
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
        _stop(db, "dup1")


def test_supervisor_term_kills_an_ignoring_command_after_grace(tmp_path):
    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    script = _script(tmp_path)
    pidfile = tmp_path / "pids" / "parent"
    childfile = tmp_path / "pids" / "child"
    proc = _popen(
        db,
        token,
        repo,
        ["--branch", "feat/ignore", "--agent", "worker-1", "--id", "job-ignore", "--grace", "0.4"],
        [sys.executable, str(script), "ignore", str(pidfile), str(childfile)],
    )
    try:
        current = _wait_running(db, "job-ignore", proc)
        out, err = proc.communicate(timeout=15)
        assert proc.returncode == 0, f"{out}\n{err}"
        parent = int(_wait_file(pidfile))
        child = int(_wait_file(childfile))
        assert current["pgid"] == parent
        assert _proc(child)["pgrp"] == parent
        os.kill(current["supervisor"], signal.SIGTERM)
        _wait_dead(parent, timeout=3)
        _wait_dead(child, timeout=3)
        _wait_dead(current["supervisor"])
        finished = _wait_status(db, "job-ignore", {"stopped", "exited", "failed"})
        assert finished["status"] == "stopped"
        assert _group(parent) == []
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
        _stop(db, "job-ignore")


def test_stop_signals_command_group_after_supervisor_dies(tmp_path):
    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    proc = _popen(
        db,
        token,
        repo,
        ["--branch", "feat/headless", "--agent", "worker-1", "--id", "job-headless"],
        ["sleep", "30"],
    )
    try:
        current = _wait_running(db, "job-headless", proc)
        out, err = proc.communicate(timeout=15)
        assert proc.returncode == 0, f"{out}\n{err}"
        command = current["pid"]
        supervisor = current["supervisor"]
        assert current["command_start"]
        assert current["command_start"] == _process_start(command)
        os.kill(supervisor, signal.SIGKILL)
        _wait_dead(supervisor)
        assert _alive(command)
        stopped = stop_job(db, "job-headless", grace=0.2)
        assert stopped["status"] == "stopped"
        _wait_dead(command)
        assert _group(current["pgid"]) == []

        sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            stale = {
                "id": "job-reused-cmd",
                "status": "running",
                "pid": sleeper.pid,
                "pgid": os.getpgid(sleeper.pid),
                "command_start": "not-ours",
                "supervisor": None,
                "supervisor_start": None,
                "exit_code": None,
                "stop_requested": False,
            }
            (tmp_path / "jobs" / "job-reused-cmd.json").write_text(json.dumps(stale), encoding="utf-8")
            with pytest.raises(LedgerError) as raised:
                stop_job(db, "job-reused-cmd", grace=0.2)
            assert raised.value.code == "legacy_record"
            assert _alive(sleeper.pid)
            left = job_status(db, "job-reused-cmd")
            assert left["status"] == "running"
            assert left["pid"] == sleeper.pid
        finally:
            if sleeper.poll() is None:
                sleeper.kill()
                sleeper.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
        _stop(db, "job-headless")


def test_rm_refuses_while_the_command_group_is_alive(tmp_path, capsys):
    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    capsys.readouterr()
    script = _script(tmp_path)
    pidfile = tmp_path / "pids" / "parent"
    childfile = tmp_path / "pids" / "child"
    proc = _popen(
        db,
        token,
        repo,
        ["--branch", "feat/group", "--agent", "worker-1", "--id", "job-group"],
        [sys.executable, str(script), "spawn", str(pidfile), str(childfile)],
    )
    try:
        current = _wait_running(db, "job-group", proc)
        out, err = proc.communicate(timeout=15)
        assert proc.returncode == 0, f"{out}\n{err}"
        parent = int(_wait_file(pidfile))
        child = int(_wait_file(childfile))
        from shuin.jobs import _patch

        _patch(
            tmp_path / "jobs" / "job-group.json",
            status="exited",
            exit_code=0,
            ended_at=1,
        )
        code = main(["--db", str(db), "--json", "job", "rm", "--id", "job-group"])
        payload = json.loads(capsys.readouterr().out)
        assert code == 1
        assert payload["error"] == "job_live"
        assert Path(current["worktree"]).is_dir()
        assert _alive(parent)
        assert _alive(child)
        assert _group(current["pgid"])
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
        _stop(db, "job-group")


def _comm(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-o", "comm=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    rows = [line.strip() for line in result.stdout.splitlines() if line.strip() and not line.strip().isupper()]
    return rows[-1] if rows else ""


def test_ps_lstart_parse_drops_argv():
    from shuin.jobs import _parse_ps_lstart

    assert _parse_ps_lstart("") is None
    assert _parse_ps_lstart("   \nSTARTED COMMAND\n") is None
    assert _parse_ps_lstart("Tue Oct 13 05:36:42 2026 sleep 30\n") == "Tue Oct 13 05:36:42 2026"
    padded = "\nSTARTED\nTue Oct  3  5:36:42 2026 /bin/sleep 30\n"
    assert _parse_ps_lstart(padded) == "Tue Oct  3  5:36:42 2026"
    leaked = _parse_ps_lstart("Tue Oct 13 05:36:42 2026 python --token secretvalue\n")
    assert leaked == "Tue Oct 13 05:36:42 2026"
    assert "secretvalue" not in leaked
    assert "--token" not in leaked


def test_start_tokens_compare_only_within_a_kind():
    from shuin.jobs import _starts_match

    assert _starts_match("proc:100", "proc:100")
    assert not _starts_match("proc:100", "proc:101")
    assert _starts_match("ps:Tue Oct 13 05:36:42 2026", "ps:Tue Oct 13 05:36:42 2026")
    assert not _starts_match("proc:100", "ps:Tue Oct 13 05:36:42 2026")
    assert not _starts_match("ps:Tue Oct 13 05:36:42 2026", None)
    assert not _starts_match("not-ours", "ps:Tue Oct 13 05:36:42 2026")
    assert _starts_match("proc:100", "100")
    assert not _starts_match("proc:100", "101")
    assert not _starts_match("ps:Tue Oct 13 05:36:42 2026", "100")
    old_ps = "Tue Oct 13 05:36:42 2026 python --token secretvalue"
    assert _starts_match("ps:Tue Oct 13 05:36:42 2026", old_ps)
    assert "secretvalue" not in "ps:Tue Oct 13 05:36:42 2026"
    assert not _starts_match("proc:100", old_ps)


def test_identity_without_procfs_uses_ps(monkeypatch):
    """The non-/proc path, including on Linux where procfs is present."""
    import shuin.jobs as jobs

    sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
    pgid = os.getpgid(sleeper.pid)
    try:
        if os.environ.get("SHUIN_JOB_NO_PROC") != "1" and Path("/proc").is_dir():
            via_proc = jobs._process_start(sleeper.pid)
            assert via_proc is not None and via_proc.startswith("proc:")
            assert via_proc.split(":", 1)[1].isdigit()
        monkeypatch.setattr(jobs, "_procfs", lambda: None)
        assert jobs._procfs() is None
        assert jobs._alive(sleeper.pid)
        token = jobs._process_start(sleeper.pid)
        assert token is not None and token.startswith("ps:")
        assert "sleep" not in token
        assert jobs._process_start(sleeper.pid) == token
        assert jobs._group_alive(pgid)
        record = {"command_start": token, "pgid": pgid, "pid": sleeper.pid}
        assert jobs._command_is_ours(record)
        assert not jobs._command_is_ours({**record, "command_start": "ps:Tue Jan  1 00:00:00 1970"})
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
        sleeper.wait(timeout=5)
    assert not jobs._alive(sleeper.pid)
    assert jobs._process_start(sleeper.pid) is None
    assert not jobs._group_alive(pgid)


def test_ps_identity_survives_exec(monkeypatch):
    import shuin.jobs as jobs

    monkeypatch.setattr(jobs, "_procfs", lambda: None)
    proc = subprocess.Popen(["sh", "-c", "sleep 0.4; exec sleep 30"], start_new_session=True)
    pgid = os.getpgid(proc.pid)
    try:
        before = jobs._process_start(proc.pid)
        assert before is not None and before.startswith("ps:")
        assert "sh" not in before and "sleep" not in before
        deadline = time.time() + 5
        comm = ""
        while time.time() < deadline:
            comm = _comm(proc.pid)
            if comm == "sleep" or comm.endswith("/sleep"):
                break
            time.sleep(0.05)
        assert comm == "sleep" or comm.endswith("/sleep")
        assert jobs._process_start(proc.pid) == before
        record = {"command_start": before, "pgid": pgid, "pid": proc.pid}
        assert jobs._command_is_ours(record)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_command_identity_fails_closed_without_a_start_time(monkeypatch):
    import shuin.jobs as jobs

    sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        pgid = os.getpgid(sleeper.pid)
        assert pgid == sleeper.pid
        monkeypatch.setattr(jobs, "_process_start", lambda _pid: None)
        record = {
            "command_start": "ps:Tue Oct 13 05:36:42 2026",
            "pgid": pgid,
            "pid": sleeper.pid,
        }
        assert jobs._group_alive(pgid)
        assert not jobs._command_is_ours(record)
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
        sleeper.wait(timeout=5)


def test_supervisor_exit_requires_the_process_to_be_gone(tmp_path):
    import shuin.jobs as jobs

    sleeper = subprocess.Popen(["sleep", "30"])
    path = tmp_path / "job.json"
    path.write_text(
        json.dumps(
            {
                "status": "exited",
                "supervisor": sleeper.pid,
                "supervisor_start": "proc:1",
            }
        ),
        encoding="utf-8",
    )
    try:
        assert jobs._alive(sleeper.pid)
        assert not jobs._starts_match(jobs._process_start(sleeper.pid), "proc:1")
        assert not jobs._supervisor_confirmed_exit(path)
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
        sleeper.wait(timeout=5)
    assert jobs._supervisor_confirmed_exit(path)


def test_ps_path_does_not_store_the_caller_token(tmp_path, capsys, monkeypatch):
    import shuin.jobs as jobs

    monkeypatch.setenv("SHUIN_JOB_NO_PROC", "1")
    assert jobs._procfs() is None
    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    capsys.readouterr()
    code = main(
        [
            "--db",
            str(db),
            "--token",
            token,
            "--json",
            "agent",
            "mint",
            "--id",
            "worker-9",
            "--kind",
            "codex",
            "--role",
            "worker",
        ]
    )
    assert code == 0
    worker = json.loads(capsys.readouterr().out)["token"]
    script = _script(tmp_path)
    seen = tmp_path / "seen-token"
    proc = _popen(
        db,
        worker,
        repo,
        ["--branch", "feat/own-ps", "--id", "job-own-ps"],
        [sys.executable, str(script), "token", str(tmp_path / "pid-own-ps"), str(seen)],
    )
    try:
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, f"{out}\n{err}"
        record = json.loads(out)
        blob = json.dumps(record)
        sidecar = Path(record["log"]).with_suffix(".json").read_text(encoding="utf-8")
        assert worker not in blob
        assert worker not in sidecar
        assert worker not in out
        assert worker not in err
        assert "--token" not in blob
        assert record["supervisor_start"].startswith("ps:")
        assert record["command_start"].startswith("ps:")
        assert "token" not in record
        assert _wait_file(seen) == worker
        assert not (tmp_path / "jobs" / "tokens" / "worker-9").exists()
        _wait_status(db, "job-own-ps", {"exited"})
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
        _stop(db, "job-own-ps")


def test_stop_after_exec_on_the_ps_path(tmp_path, monkeypatch):
    import shuin.jobs as jobs

    monkeypatch.setenv("SHUIN_JOB_NO_PROC", "1")
    assert jobs._procfs() is None
    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    proc = _popen(
        db,
        token,
        repo,
        ["--branch", "feat/exec", "--agent", "worker-1", "--id", "job-exec"],
        ["sh", "-c", "sleep 0.4; exec sleep 60"],
    )
    try:
        current = _wait_running(db, "job-exec", proc)
        out, err = proc.communicate(timeout=15)
        assert proc.returncode == 0, f"{out}\n{err}"
        assert token not in out and token not in err
        assert token not in json.dumps(current)
        command = current["pid"]
        assert current["command_start"].startswith("ps:")
        assert "sh" not in current["command_start"]
        assert "sleep" not in current["command_start"]
        deadline = time.time() + 5
        comm = ""
        while time.time() < deadline:
            comm = _comm(command)
            if comm == "sleep" or comm.endswith("/sleep"):
                break
            time.sleep(0.05)
        assert comm == "sleep" or comm.endswith("/sleep")
        assert jobs._process_start(command) == current["command_start"]
        os.kill(current["supervisor"], signal.SIGKILL)
        _wait_dead(current["supervisor"])
        assert _alive(command)
        stopped = stop_job(db, "job-exec", grace=0.2)
        assert stopped["status"] == "stopped"
        _wait_dead(command)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
        _stop(db, "job-exec")


def _untag(token: str, form: str) -> str:
    kind, value = token.split(":", 1)
    if form == "proc":
        assert kind == "proc"
        return value
    assert kind == "ps"
    return f"{value} python --token secretvalue"


@pytest.mark.parametrize("form", ["proc", "ps"])
@pytest.mark.parametrize("keep_supervisor", [True, False])
def test_legacy_untagged_token_can_be_stopped(tmp_path, monkeypatch, form, keep_supervisor):
    if form == "ps":
        monkeypatch.setenv("SHUIN_JOB_NO_PROC", "1")
    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    job_id = f"job-leg-{form}-{int(keep_supervisor)}"
    proc = _popen(
        db,
        token,
        repo,
        ["--branch", f"feat/{job_id}", "--agent", "worker-1", "--id", job_id],
        ["sleep", "30"],
    )
    try:
        current = _wait_running(db, job_id, proc)
        out, err = proc.communicate(timeout=15)
        assert proc.returncode == 0, f"{out}\n{err}"
        if form == "proc" and not str(current["command_start"]).startswith("proc:"):
            pytest.skip("this host records ps start tokens")
        if form == "ps":
            assert str(current["command_start"]).startswith("ps:")
            assert str(current["supervisor_start"]).startswith("ps:")
        from shuin.jobs import _patch

        legacy_cmd = _untag(current["command_start"], form)
        legacy_sup = _untag(current["supervisor_start"], form)
        if form == "ps":
            assert "secretvalue" in legacy_cmd
            assert not legacy_cmd.startswith("ps:")
        else:
            assert legacy_cmd.isdigit()
        if not keep_supervisor:
            os.kill(current["supervisor"], signal.SIGKILL)
            _wait_dead(current["supervisor"])
            assert _alive(current["pid"])
        _patch(
            tmp_path / "jobs" / f"{job_id}.json",
            supervisor_start=legacy_sup,
            command_start=legacy_cmd,
        )
        stopped = stop_job(db, job_id, grace=0.2)
        assert stopped["status"] == "stopped"
        _wait_dead(current["pid"])
        if keep_supervisor:
            _wait_dead(current["supervisor"])
        assert _group(current["pgid"]) == []
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
        _stop(db, job_id)


def test_stop_does_not_mark_stopped_while_the_group_is_alive(tmp_path, monkeypatch):
    import shuin.jobs as jobs

    repo = _init_repo(tmp_path)
    db, token = _init_ledger(tmp_path)
    proc = _popen(
        db,
        token,
        repo,
        ["--branch", "feat/live-group", "--agent", "worker-1", "--id", "job-live-group"],
        ["sleep", "30"],
    )
    try:
        current = _wait_running(db, "job-live-group", proc)
        out, err = proc.communicate(timeout=15)
        assert proc.returncode == 0, f"{out}\n{err}"
        os.kill(current["supervisor"], signal.SIGKILL)
        _wait_dead(current["supervisor"])
        assert _alive(current["pid"])
        monkeypatch.setattr(jobs, "_kill_group", lambda *_args, **_kwargs: None)
        left = stop_job(db, "job-live-group", grace=0.2)
        assert left["status"] == "running"
        assert _alive(current["pid"])
        assert _group(current["pgid"])
    finally:
        monkeypatch.undo()
        if proc.poll() is None:
            proc.kill()
            proc.communicate(timeout=5)
        _stop(db, "job-live-group")


def test_legacy_ps_token_is_refused_against_proc(tmp_path):
    if not Path("/proc").is_dir():
        pytest.skip("procfs is required to show the clocks differ")
    db, _token = _init_ledger(tmp_path)
    sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
    pgid = os.getpgid(sleeper.pid)
    try:
        (tmp_path / "jobs").mkdir(exist_ok=True)
        record = {
            "id": "job-legacy-ps",
            "status": "running",
            "pid": sleeper.pid,
            "pgid": pgid,
            "command_start": "Tue Oct 13 05:36:42 2026 python --token secretvalue",
            "supervisor": None,
            "supervisor_start": None,
            "exit_code": None,
            "stop_requested": False,
        }
        (tmp_path / "jobs" / "job-legacy-ps.json").write_text(json.dumps(record), encoding="utf-8")
        with pytest.raises(LedgerError) as raised:
            stop_job(db, "job-legacy-ps", grace=0.2)
        assert raised.value.code == "legacy_record"
        assert f"kill -TERM -{pgid}" in raised.value.message
        assert "shuin job rm" in raised.value.message
        assert _alive(sleeper.pid)
        assert job_status(db, "job-legacy-ps")["status"] == "running"
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)


def test_legacy_proc_token_is_refused_against_ps(tmp_path, monkeypatch):
    import shuin.jobs as jobs

    monkeypatch.setattr(jobs, "_procfs", lambda: None)
    db, _token = _init_ledger(tmp_path)
    sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
    pgid = os.getpgid(sleeper.pid)
    try:
        (tmp_path / "jobs").mkdir(exist_ok=True)
        record = {
            "id": "job-legacy-proc",
            "status": "running",
            "pid": sleeper.pid,
            "pgid": pgid,
            "command_start": "123456789",
            "supervisor": None,
            "supervisor_start": None,
            "exit_code": None,
            "stop_requested": False,
        }
        (tmp_path / "jobs" / "job-legacy-proc.json").write_text(json.dumps(record), encoding="utf-8")
        with pytest.raises(LedgerError) as raised:
            stop_job(db, "job-legacy-proc", grace=0.2)
        assert raised.value.code == "legacy_record"
        assert f"kill -TERM -{pgid}" in raised.value.message
        assert _alive(sleeper.pid)
        left = job_status(db, "job-legacy-proc")
        assert left["status"] == "running"
        assert left["command_start"] == "123456789"
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)
