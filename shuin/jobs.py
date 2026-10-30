"""Detach a local agent command in its own git worktree.

A job started with nohup stays in the launching session. On macOS that
session can take the process down when a bridge reconnects, even after
the parent has become init. This launcher double-forks and calls setsid
so the command is in a new session, then records the pid and process
group. On exit, timeout, or ``shuin job stop`` it signals that whole
group, so helpers the command spawned do not stay behind.

Stop signals a running command group only when that process is still
the one recorded at launch. The supervisor's stop handler sends
SIGTERM, then SIGKILL after the grace period. If the supervisor has
died and the command is still alive, stop signals the recorded command
group after its start time matches. A stop during start sets a flag
the supervisor checks before and after it launches the command. Liveness
is ``os.kill(pid, 0)``. The start token is only the process start time,
tagged ``proc:`` or ``ps:``. It is never the command line: argv can hold
``--token``, and it changes when the command execs. An older record may
store the same start time with no tag. Stop still recognizes that. If
the old value cannot be compared, stop refuses and leaves the job running.

The job record is created exclusively before the worktree, so a second
run with the same id cannot remove a live checkout. Cleanup removes a
worktree or branch only when this launch created it. A start timeout
removes the worktree only after the supervisor has recorded that it
exited; otherwise the worktree stays and the job is marked failed.

Commits need writable roots. The sandbox has to be able to write the
worktree, the directory that contains it, the main repo's
``.git/worktrees/<name>`` (index.lock and FETCH_HEAD live there), the
objects directory, and the refs directory. ``shuin job run`` checks
those before it starts the command.

After a job has started, the worktree stays until ``shuin job rm``.
``shuin job rm`` refuses while any process is still in the recorded
command group.

Job rows live in a sidecar next to the ledger (``jobs/<id>.json``). The
gate, the queue, and the ledger schema are unchanged. Windows is refused.
"""

import json
import os
import re
import secrets
import select
import shutil
import signal
import subprocess
import time
from pathlib import Path

from shuin.errors import LedgerError

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    _fcntl = None

try:
    import resource
except ImportError:  # pragma: no cover - Windows has no resource
    resource = None

_JOB_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_TERMINAL = frozenset({"exited", "stopped", "failed", "timeout"})
_ROOT_LABELS = (
    ("worktree", "worktree"),
    ("base", "worktree base"),
    ("gitdir", ".git/worktrees/<name>"),
    ("objects", "objects"),
    ("refs", "refs"),
)
_GIT_ENV_BLOCK = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX")
_DEFAULT_GRACE = 5.0
_READY_TIMEOUT = 10.0
# ctime / strftime %c. Trailing argv is matched only so it can be discarded.
_PS_LSTART = re.compile(
    r"^(?P<start>[A-Za-z]{3}\s+[A-Za-z]{3}\s+\d{1,2}\s+\d{1,2}:\d{2}:\d{2}\s+\d{4})"
    r"(?:\s+.*)?$"
)


class _StartFailed(Exception):
    def __init__(self, message: str, supervisor: dict | None = None, *, fatal: bool = True):
        super().__init__(message)
        self.message = message
        self.supervisor = supervisor
        self.fatal = fatal


def require_posix() -> None:
    if os.name == "nt" or not hasattr(os, "fork") or not hasattr(os, "setsid") or not hasattr(os, "killpg"):
        raise LedgerError(
            400,
            "job_unsupported",
            "shuin job needs a POSIX system. Windows is not supported.",
        )


def run_job(
    *,
    db: Path,
    token: str | None,
    repo: Path,
    branch: str,
    command: list[str],
    agent_id: str | None = None,
    kind: str = "codex",
    role: str = "worker",
    timeout: int | None = None,
    job_id: str | None = None,
    base: str | None = None,
    grace: float | None = None,
) -> dict:
    """Mint or reuse a token, add a worktree, and detach the command."""
    require_posix()
    command = _clean_command(command)
    branch = _clean_branch(branch)
    if base is not None:
        base = _clean_branch(base)
    if timeout is not None and (not isinstance(timeout, int) or timeout <= 0):
        raise LedgerError(400, "bad_timeout", "Timeout must be a positive number of seconds.")
    grace = _clean_grace(grace)
    job_id = _clean_job_id(job_id) if job_id else "job-" + secrets.token_hex(4)
    repo = _main_checkout(repo)
    record = _record_path(db, job_id)

    agent_id, agent_token, _minted = _resolve_agent(db, token, agent_id, kind, role)
    worktree = _worktree_path(repo, job_id)
    log_path = _jobs_dir(db) / f"{job_id}.log"
    payload = {
        "id": job_id,
        "agent_id": agent_id,
        "kind": kind,
        "role": role,
        "branch": branch,
        "repo": str(repo),
        "worktree": str(worktree),
        "log": str(log_path),
        "pid": None,
        "pgid": None,
        "command_start": None,
        "supervisor": None,
        "supervisor_start": None,
        "supervisor_pgid": None,
        "status": "starting",
        "stop_requested": False,
        "timeout": timeout,
        "grace": grace,
        "exit_code": None,
        "started_at": int(time.time()),
        "ended_at": None,
        "roots": {},
    }
    # Reserve the id before creating a checkout. A duplicate run stops here
    # and must not touch the worktree the first run already owns.
    _create_record(record, payload)
    created_worktree = False
    created_branch = False
    detached = False
    try:
        created_branch = _add_worktree(repo, worktree, branch, base)
        created_worktree = True
        roots = sandbox_roots(worktree)
        require_writable(roots)
        os.close(os.open(log_path, os.O_CREAT | os.O_WRONLY, 0o600))
        _patch(record, roots={key: str(path) for key, path in roots.items()})
        env = os.environ.copy()
        for key in _GIT_ENV_BLOCK:
            env.pop(key, None)
        env["SHUIN_TOKEN"] = agent_token
        env["SHUIN_DB"] = str(Path(db).resolve())
        env["SHUIN_JOB"] = job_id
        detached = True
        _detach(record, command, worktree, env, log_path, timeout, grace)
    except Exception:
        if not detached:
            _discard_launch(repo, worktree, branch, created_worktree, created_branch)
            _remove_sidecar(record, log_path)
        else:
            if _supervisor_confirmed_exit(record):
                _discard_launch(repo, worktree, branch, created_worktree, created_branch)
            _mark_failed_if_open(record)
        raise
    return _load(record)


def job_status(db: Path, job_id: str | None = None) -> dict:
    directory = _jobs_dir(db, create=False)
    if job_id is None:
        rows = []
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                rows.append(_refresh(_load(path)))
        return {"jobs": rows}
    path = directory / f"{_clean_job_id(job_id)}.json"
    if not path.is_file():
        raise LedgerError(404, "not_found", "Job not found.")
    return _refresh(_load(path))


def stop_job(db: Path, job_id: str, grace: float | None = None) -> dict:
    """Stop a starting job, or signal a command group that is still the one we launched."""
    require_posix()
    path = _jobs_dir(db, create=False) / f"{_clean_job_id(job_id)}.json"
    if not path.is_file():
        raise LedgerError(404, "not_found", "Job not found.")
    record = _load(path)
    if record.get("status") in _TERMINAL:
        return record
    blocked = _legacy_stop_error(record)
    if blocked:
        raise LedgerError(409, "legacy_record", blocked)
    grace = _clean_grace(grace if grace is not None else record.get("grace"))
    _patch(path, stop_requested=True)
    signaled = False
    deadline = time.monotonic() + grace + 2
    while time.monotonic() < deadline:
        current = _load(path)
        if current.get("status") in _TERMINAL:
            return current
        ours = _supervisor_is_ours(path, current)
        command_ours = _command_is_ours(current)
        if not signaled:
            if command_ours:
                _kill_group(current.get("pgid"), grace)
            if ours:
                _signal_supervisor(path, current, grace)
            if command_ours or ours:
                signaled = True
            elif current.get("supervisor") and _alive(current.get("supervisor")):
                # Supervisor pid was reused and the command is not ours.
                return _load(path)
        current = _load(path)
        if current.get("status") in _TERMINAL:
            return current
        supervisor_alive = bool(current.get("supervisor")) and _alive(current.get("supervisor"))
        if not _supervisor_is_ours(path, current) and not _command_is_ours(current) and not supervisor_alive:
            break
        time.sleep(0.05)
    current = _load(path)
    if current.get("status") in _TERMINAL:
        return current
    # A stopped row means the command group is gone. rm refuses a live group.
    if _recorded_group_alive(current):
        return current
    _mark_stopped_if_open(path)
    return _load(path)


def remove_job(db: Path, job_id: str) -> dict:
    """Remove a finished job's worktree. The branch is kept."""
    path = _jobs_dir(db, create=False) / f"{_clean_job_id(job_id)}.json"
    if not path.is_file():
        raise LedgerError(404, "not_found", "Job not found.")
    record = _load(path)
    if record.get("status") in ("running", "starting") or _recorded_group_alive(record):
        raise LedgerError(409, "job_live", "Stop the job before removing its worktree.")
    repo = Path(record["repo"]) if record.get("repo") else None
    worktree = Path(record["worktree"]) if record.get("worktree") else None
    if repo is not None and worktree is not None:
        _remove_worktree(repo, worktree)
    log = Path(record["log"]) if record.get("log") else None
    path.unlink(missing_ok=True)
    path.with_suffix(".lock").unlink(missing_ok=True)
    if log is not None:
        log.unlink(missing_ok=True)
    return {"id": record["id"], "removed": True, "worktree": str(worktree) if worktree else None}


def sandbox_roots(worktree: Path) -> dict[str, Path]:
    """Directories a sandbox must be able to write, including the worktree base."""
    worktree = Path(worktree).resolve()
    gitdir = _git_path(worktree, "--absolute-git-dir")
    if gitdir.parent.name != "worktrees":
        raise LedgerError(400, "worktree_required", "The job checkout is not a linked worktree.")
    return {
        "worktree": worktree,
        "base": worktree.parent,
        "gitdir": gitdir,
        "objects": _git_path(worktree, "--git-path", "objects"),
        "refs": _git_path(worktree, "--git-path", "refs"),
    }


def require_writable(roots: dict[str, Path]) -> None:
    bad = []
    for key, label in _ROOT_LABELS:
        path = Path(roots[key])
        if not _can_write_dir(path):
            bad.append(f"{label} ({path})")
    if bad:
        raise LedgerError(
            400,
            "sandbox_unwritable",
            "Preflight failed. The sandbox cannot write "
            + ", ".join(bad)
            + ". Writable roots: the worktree, the worktree base, .git/worktrees/<name>, objects, and refs.",
        )


def _resolve_agent(db, caller_token, agent_id, kind, role):
    from shuin.ledger import open_ledger
    from shuin.sanitize import clean_agent_id

    ledger = open_ledger(db)
    try:
        actor = ledger.authenticate(caller_token) if caller_token else None
        if agent_id is None:
            if actor is not None and actor["role"] != "owner":
                return actor["id"], caller_token, False
            agent_id = "job-" + secrets.token_hex(4)
        else:
            agent_id = clean_agent_id(agent_id)
        row = ledger.conn.execute(
            "SELECT id, disabled_at FROM agents WHERE id=?",
            (agent_id,),
        ).fetchone()
        if row is not None:
            if row["disabled_at"] is not None:
                raise LedgerError(409, "disabled", "Agent is disabled.")
            reused = _reusable_token(ledger, db, agent_id, caller_token, actor)
            if reused is None:
                raise LedgerError(
                    409,
                    "token_unavailable",
                    f"Agent {agent_id} exists and no token is on file.",
                )
            return agent_id, reused, False
        if actor is None:
            raise LedgerError(401, "token_required", "Token required.")
        minted = ledger.mint_agent(actor, agent_id, kind, role)
        _store_token(db, agent_id, minted)
        return agent_id, minted, True
    finally:
        ledger.close()


def _reusable_token(ledger, db, agent_id, caller_token, actor):
    """Reuse a stored minted token, or the caller's own token. Never store the caller's."""
    if actor is not None and actor["id"] == agent_id and caller_token:
        return caller_token
    stored = _read_token(db, agent_id)
    if not stored:
        return None
    try:
        who = ledger.authenticate(stored)
    except LedgerError:
        return None
    if who["id"] != agent_id:
        return None
    return stored


def _store_token(db, agent_id, token: str) -> None:
    path = _jobs_dir(db) / "tokens" / agent_id
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (token + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _read_token(db, agent_id) -> str | None:
    path = _jobs_dir(db) / "tokens" / agent_id
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip() or None


def _detach(record: Path, command, worktree: Path, env, log_path: Path, timeout, grace: float) -> None:
    read_fd, write_fd = os.pipe()
    first = os.fork()
    if first == 0:
        os.close(read_fd)
        try:
            os.setsid()
        except OSError as exc:
            _notify(write_fd, f"ERR setsid failed: {exc}")
            os._exit(1)
        second = os.fork()
        if second > 0:
            os.close(write_fd)
            os._exit(0)
        try:
            _supervise(write_fd, record, command, worktree, env, log_path, timeout, grace)
        except Exception as exc:
            _notify(write_fd, f"ERR {exc}")
            os._exit(1)
        os._exit(0)
    os.close(write_fd)
    try:
        os.waitpid(first, 0)
    except ChildProcessError:
        pass
    try:
        try:
            _read_ready(read_fd, _ready_timeout(), record)
        except _StartFailed as exc:
            _patch(record, stop_requested=True)
            supervisor = exc.supervisor or {}
            pid = supervisor.get("supervisor")
            # Kill the supervisor only when it is still the process we launched.
            # The first child's process group is not a safe fallback: that pid
            # can be reused once the supervisor is unknown or already dead.
            if supervisor and _supervisor_is_ours(record, supervisor) and isinstance(pid, int) and pid > 1:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            if exc.fatal:
                _wait_supervisor_confirm(record, max(grace, 2.0))
                if (
                    isinstance(pid, int)
                    and pid > 1
                    and _alive(pid)
                    and _starts_match(_process_start(pid), supervisor.get("supervisor_start"))
                ):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
                    _wait_supervisor_confirm(record, 1.0)
                raise LedgerError(400, "job_failed", exc.message) from None
    finally:
        os.close(read_fd)


def _supervise(notify_fd, record, command, worktree, env, log_path, timeout, grace: float) -> None:
    """Run in the detached grandchild. The command gets its own session."""
    _close_extra_fds(notify_fd)
    _redirect_stdio()
    os.umask(0o077)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    lock_fd = _hold_supervisor_lock(record)
    command_pgid = {"value": None}

    def _on_stop(_signum, _frame):
        pgid = command_pgid["value"]
        if pgid:
            _kill_group(pgid, grace)
        try:
            _mark_stopped_if_open(record)
        except (OSError, json.JSONDecodeError):
            pass
        os._exit(0)

    signal.signal(signal.SIGTERM, _on_stop)
    start = _process_start(os.getpid()) or ""
    identity = {
        "supervisor": os.getpid(),
        "supervisor_start": start,
        "supervisor_pgid": os.getpgrp(),
    }
    _patch(record, **identity)
    _notify(notify_fd, f"SUP {os.getpid()} {os.getpgrp()} {start}")

    def _give_up():
        if command_pgid["value"]:
            _kill_group(command_pgid["value"], grace)
        _mark_stopped_if_open(record)
        _notify(notify_fd, "ERR stopped")

    if _stop_requested(record):
        _give_up()
        os.close(lock_fd)
        return
    delay = _start_delay()
    deadline = time.monotonic() + delay
    while time.monotonic() < deadline:
        if _stop_requested(record):
            _give_up()
            os.close(lock_fd)
            return
        time.sleep(0.05)
    if _stop_requested(record):
        _give_up()
        os.close(lock_fd)
        return

    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(worktree),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
            close_fds=True,
            preexec_fn=_reset_sighup,
        )
    except Exception as exc:
        os.close(log_fd)
        try:
            _mark_failed_if_open(record)
        except (OSError, json.JSONDecodeError):
            pass
        _notify(notify_fd, f"ERR {exc}")
        os.close(lock_fd)
        return
    os.close(log_fd)
    try:
        os.chdir("/")
    except OSError:
        pass
    command_pgid["value"] = proc.pid
    command_start = _process_start(proc.pid) or ""
    if _stop_requested(record):
        _give_up()
        os.close(lock_fd)
        return
    _patch(record, pid=proc.pid, pgid=proc.pid, command_start=command_start, status="running")
    fresh = _load(record)
    if fresh.get("status") != "running" or fresh.get("stop_requested"):
        _give_up()
        os.close(lock_fd)
        return
    _notify(notify_fd, f"OK {proc.pid} {proc.pid} {os.getpid()}")
    os.close(notify_fd)
    try:
        status, code = _wait_command(proc, proc.pid, timeout, grace)
        _kill_group(proc.pid, grace)
        if _stop_requested(record):
            status = "stopped"
        _patch(record, status=status, exit_code=code, ended_at=int(time.time()))
    except Exception:
        _kill_group(proc.pid, grace)
        try:
            _patch(record, status="failed", ended_at=int(time.time()))
        except (OSError, json.JSONDecodeError):
            pass
    os.close(lock_fd)


def _wait_command(proc, pgid: int, timeout, grace: float):
    deadline = None if not timeout else time.monotonic() + timeout
    while True:
        slice_ = 0.2
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                _kill_group(pgid, grace)
                try:
                    code = proc.wait(timeout=grace + 1)
                except subprocess.TimeoutExpired:
                    code = None
                return "timeout", code
            slice_ = min(0.2, left)
        try:
            return "exited", proc.wait(timeout=slice_)
        except subprocess.TimeoutExpired:
            continue


def _kill_group(pgid: int | None, grace: float) -> None:
    """SIGTERM the group, then SIGKILL after grace if anything remains. Never our own group."""
    if not isinstance(pgid, int) or pgid <= 1:
        return
    try:
        if pgid == os.getpgrp():
            return
    except OSError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + max(0.0, grace)
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def _signal_supervisor(record_path: Path, record: dict, grace: float) -> None:
    if not _supervisor_is_ours(record_path, record):
        return
    pgid = record.get("supervisor_pgid")
    if isinstance(pgid, int) and pgid > 1:
        _kill_group(pgid, grace)
        return
    pid = record.get("supervisor")
    if not isinstance(pid, int) or pid <= 1 or pid == os.getpid():
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + max(0.0, grace)
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _supervisor_is_ours(record_path: Path, record: dict) -> bool:
    """True when this pid is still the supervisor that took the job lock."""
    pid = record.get("supervisor")
    expected = record.get("supervisor_start")
    if not isinstance(pid, int) or pid <= 1 or not expected:
        return False
    if not _starts_match(_process_start(pid), expected):
        return False
    return _lock_is_held(record_path.with_suffix(".lock"))


def _command_is_ours(record: dict) -> bool:
    """True when the recorded command group is still the process we launched.

    A missing start time is not proof. ``pid == pgid`` is true of every
    session leader, including a recycled one, so it is not used on its own.
    """
    expected = record.get("command_start")
    pgid = record.get("pgid")
    pid = record.get("pid")
    if not expected or not isinstance(pgid, int) or pgid <= 1:
        return False
    if not isinstance(pid, int) or pid != pgid:
        return False
    if not _group_alive(pgid):
        return False
    if not _starts_match(_process_start(pgid), expected):
        return False
    try:
        return os.getpgid(pid) == pgid
    except OSError:
        return False


def _recorded_group_alive(record: dict) -> bool:
    pgid = record.get("pgid")
    if not isinstance(pgid, int) or pgid <= 1:
        return False
    return _group_alive(pgid)


def _supervisor_confirmed_exit(path: Path) -> bool:
    """True when the supervisor has recorded a terminal status and is gone."""
    try:
        current = _load(path)
    except (OSError, json.JSONDecodeError):
        return False
    if current.get("status") not in _TERMINAL:
        return False
    pid = current.get("supervisor")
    if not isinstance(pid, int) or pid <= 1:
        return True
    # A start-token mismatch is not enough. hidepid can make a proc token
    # unreadable so the next read is a ps token for the same live process.
    return not _alive(pid)


def _wait_supervisor_confirm(path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if _supervisor_confirmed_exit(path):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _remove_sidecar(record: Path, log_path: Path | None) -> None:
    record.unlink(missing_ok=True)
    record.with_suffix(".lock").unlink(missing_ok=True)
    if log_path is not None:
        log_path.unlink(missing_ok=True)


def _lock_is_held(path: Path) -> bool:
    if _fcntl is None or not path.exists():
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        _fcntl.flock(fd, _fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _hold_supervisor_lock(record: Path) -> int:
    path = record.with_suffix(".lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
    return fd


def _procfs() -> Path | None:
    """Linux procfs when this host has it. macOS does not.

    ``SHUIN_JOB_NO_PROC=1`` hides procfs so tests exercise the ps path.
    """
    if os.environ.get("SHUIN_JOB_NO_PROC") == "1":
        return None
    path = Path("/proc")
    try:
        if path.is_dir():
            return path
    except OSError:
        return None
    return None


def _format_start(kind: str, value: str) -> str:
    return f"{kind}:{value}"


def _split_start(token) -> tuple[str, str] | None:
    if not isinstance(token, str) or ":" not in token:
        return None
    kind, value = token.split(":", 1)
    if kind not in ("proc", "ps") or not value:
        return None
    return kind, value


def _legacy_message(pgid) -> str:
    shown = str(pgid) if isinstance(pgid, int) and pgid > 1 else "<pgid>"
    return (
        "This job was recorded by an older shuin; "
        f"stop it with kill -TERM -{shown}, then shuin job rm"
    )


def _legacy_lstart(token: str) -> str | None:
    """lstart from an untagged ps token. A trailing command is ignored."""
    if token.startswith("proc:") or token.startswith("ps:"):
        return None
    return _parse_ps_lstart(token)


def _legacy_unreadable(expected, actual) -> bool:
    """True when an old untagged token cannot be compared to a fresh read.

    Plain digits compare with ``proc:``. An old ps value compares on its
    lstart, command and all, with ``ps:``. Any other shape, or the other
    clock, is not a reliable match.
    """
    if not isinstance(expected, str) or not expected or _split_start(expected) is not None:
        return False
    fresh = _split_start(actual)
    if expected.isdigit():
        return fresh is None or fresh[0] != "proc"
    if _legacy_lstart(expected) is not None:
        return fresh is None or fresh[0] != "ps"
    return True


def _legacy_stop_error(record: dict) -> str | None:
    """Refuse to guess when a live process still carries an unreadable old token."""
    message = _legacy_message(record.get("pgid"))
    supervisor = record.get("supervisor")
    if isinstance(supervisor, int) and supervisor > 1 and _alive(supervisor):
        if _legacy_unreadable(record.get("supervisor_start"), _process_start(supervisor)):
            return message
    pgid = record.get("pgid")
    if isinstance(pgid, int) and pgid > 1 and _group_alive(pgid):
        if _legacy_unreadable(record.get("command_start"), _process_start(pgid)):
            return message
    return None


def _starts_match(actual, expected) -> bool:
    """True when both tokens are the same start time on the same clock.

    A proc tick and a ps lstart are different clocks. Untagged legacy values
    compare with the tagged form of that same clock: digits with ``proc:``,
    and an lstart prefix (the old ps token may append the command) with ``ps:``.
    """
    left = _split_start(actual)
    right = _split_start(expected)
    if left is not None and right is not None:
        return left == right
    if left is None or not isinstance(expected, str) or not expected or right is not None:
        return False
    kind, value = left
    if expected.isdigit():
        return kind == "proc" and expected == value
    lstart = _legacy_lstart(expected)
    if lstart is not None:
        return kind == "ps" and lstart == value
    return False


def _process_start(pid: int) -> str | None:
    """Start token for pid. ``proc:<tick>`` or ``ps:<lstart>``. Never argv."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    proc = _procfs()
    if proc is not None:
        ticks = _start_from_proc(proc, pid)
        if ticks is not None:
            return _format_start("proc", ticks)
        try:
            gone = not (proc / str(pid) / "stat").exists()
        except OSError:
            gone = False
        if gone:
            return None
    stamp = _lstart_from_ps(pid)
    if not stamp:
        return None
    return _format_start("ps", stamp)


def _start_from_proc(proc: Path, pid: int) -> str | None:
    try:
        text = (proc / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    rest = text[text.rfind(")") + 2 :].split()
    if len(rest) > 19:
        return rest[19]
    return None


def _ps_env() -> dict[str, str]:
    env = os.environ.copy()
    # Pin lstart's month and day names. The command column is not requested.
    env["LC_ALL"] = "C"
    return env


def _parse_ps_lstart(text: str) -> str | None:
    """Return the lstart field. Anything after it, including argv, is dropped."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.upper().startswith("STARTED"):
            continue
        match = _PS_LSTART.match(line)
        if match:
            return match.group("start")
    return None


def _lstart_from_ps(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_ps_env(),
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return _parse_ps_lstart(result.stdout)


def _group_alive(pgid: int) -> bool:
    """True when a process is still in this group. Procfs ignores zombies."""
    if not isinstance(pgid, int) or pgid <= 1:
        return False
    proc = _procfs()
    if proc is not None:
        return _group_alive_proc(proc, pgid)
    return _group_alive_signal(pgid)


def _group_alive_proc(proc: Path, pgid: int) -> bool:
    try:
        entries = list(proc.iterdir())
    except OSError:
        return _group_alive_signal(pgid)
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            text = (entry / "stat").read_text(encoding="utf-8")
        except OSError:
            continue
        rest = text[text.rfind(")") + 2 :].split()
        if len(rest) > 2 and int(rest[2]) == pgid and rest[0] != "Z":
            return True
    return False


def _group_alive_signal(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _notify(fd: int, line: str) -> None:
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    except OSError:
        pass


def _read_ready(fd: int, timeout: float, record: Path) -> None:
    deadline = time.monotonic() + timeout
    buf = b""
    supervisor = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _StartFailed("Job did not start.", supervisor)
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            raise _StartFailed("Job did not start.", supervisor)
        try:
            piece = os.read(fd, 1024)
        except OSError as exc:
            raise _StartFailed("Job did not start.", supervisor) from exc
        if not piece:
            current = _load(record)
            if current.get("stop_requested") or current.get("status") in _TERMINAL:
                return
            raise _StartFailed("Job did not start.", supervisor)
        buf += piece
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            if text.startswith("SUP "):
                supervisor = _parse_sup(text)
                continue
            if text.startswith("OK "):
                return
            if text.startswith("ERR "):
                current = _load(record)
                if current.get("stop_requested") or current.get("status") == "stopped":
                    return
                raise _StartFailed(text[4:], supervisor)


def _parse_sup(text: str) -> dict | None:
    parts = text.split(" ", 3)
    if len(parts) != 4:
        return None
    try:
        pid = int(parts[1])
        pgid = int(parts[2])
    except ValueError:
        return None
    return {"supervisor": pid, "supervisor_pgid": pgid, "supervisor_start": parts[3]}


def _reset_sighup() -> None:
    signal.signal(signal.SIGHUP, signal.SIG_DFL)


def _close_extra_fds(keep: int) -> None:
    limit = 1024
    if resource is not None:
        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft == resource.RLIM_INFINITY:
            limit = 1024 * 1024
        else:
            limit = max(int(soft), 3)
    if keep > 2:
        os.closerange(3, min(keep, limit))
        os.closerange(keep + 1, limit)
    else:
        os.closerange(3, limit)


def _redirect_stdio() -> None:
    fd = os.open(os.devnull, os.O_RDWR)
    os.dup2(fd, 0)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    if fd > 2:
        os.close(fd)


def _refresh(record: dict) -> dict:
    """A running row whose supervisor and command are both gone has exited."""
    if record.get("status") != "running":
        return record
    if _alive(record.get("pid")) or _alive(record.get("supervisor")):
        return record
    path = None
    if record.get("id") and record.get("log"):
        path = Path(record["log"]).parent / f"{record['id']}.json"
    if path is not None and path.is_file():
        _patch(path, status="exited", ended_at=record.get("ended_at") or int(time.time()))
        return _load(path)
    updated = dict(record)
    updated["status"] = "exited"
    return updated


def _can_write_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    probe = path / f".shuin-probe-{os.getpid()}-{secrets.token_hex(4)}"
    fd = None
    try:
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        fd = None
        os.unlink(probe)
        return True
    except OSError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if probe.exists():
                probe.unlink()
        except OSError:
            pass
        return False


def _main_checkout(start: Path) -> Path:
    """Main worktree, even when start is itself a linked worktree."""
    common = _git_common_dir(start)
    if common.name == ".git":
        return common.parent.resolve()
    return common.resolve()


def _git_common_dir(start: Path) -> Path:
    result = _git(start, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    if result.returncode != 0:
        result = _git(start, "rev-parse", "--git-common-dir")
    return _resolve_git(start, result.stdout.strip())


def _toplevel(path: Path) -> Path:
    result = _git(path, "rev-parse", "--show-toplevel", check=False)
    if result.returncode != 0:
        detail = (result.stderr or "Not a git repository.").strip().splitlines()
        raise LedgerError(400, "git_failed", detail[-1] if detail else "Not a git repository.")
    return Path(result.stdout.strip()).resolve()


def _add_worktree(repo: Path, worktree: Path, branch: str, base: str | None) -> bool:
    """Return True when this call created the branch."""
    worktree.parent.mkdir(parents=True, exist_ok=True)
    exists = _git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    if exists.returncode == 0:
        _git(repo, "worktree", "add", str(worktree), branch)
        return False
    _git(repo, "worktree", "add", "-b", branch, str(worktree), base or "HEAD")
    return True


def _discard_launch(
    repo: Path,
    worktree: Path,
    branch: str,
    created_worktree: bool,
    created_branch: bool,
) -> None:
    """Remove only the checkout and branch this launch created."""
    if created_worktree:
        _remove_worktree(repo, worktree)
    if created_branch:
        subprocess.run(
            ["git", "-C", str(repo), "branch", "-D", branch],
            capture_output=True,
            text=True,
        )


def _remove_worktree(repo: Path, worktree: Path) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True, text=True)
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)


def _worktree_path(repo: Path, job_id: str) -> Path:
    return (repo.parent / ".shuin-worktrees" / repo.name / job_id).resolve()


def _git_path(cwd: Path, *args: str) -> Path:
    if args == ("--absolute-git-dir",):
        result = _git(cwd, "rev-parse", "--path-format=absolute", "--absolute-git-dir", check=False)
        if result.returncode != 0:
            result = _git(cwd, "rev-parse", "--absolute-git-dir", check=False)
        if result.returncode != 0:
            result = _git(cwd, "rev-parse", "--git-dir")
    else:
        result = _git(cwd, "rev-parse", "--path-format=absolute", *args, check=False)
        if result.returncode != 0:
            result = _git(cwd, "rev-parse", *args)
    return _resolve_git(cwd, result.stdout.strip())


def _resolve_git(cwd: Path, raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = Path(cwd) / path
    return path.resolve()


def _git(cwd, *args, check=True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "-c", "core.fsmonitor=false", "-c", "core.untrackedcache=false", *args],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise LedgerError(400, "git_failed", "git is not on PATH.") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "git failed").strip().splitlines()
        message = detail[-1] if detail else "git failed"
        raise LedgerError(400, "git_failed", message[:300])
    return result


def _clean_command(command: list[str]) -> list[str]:
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise LedgerError(400, "command_required", "Pass the command after --.")
    return list(command)


def _clean_branch(name: str) -> str:
    if not isinstance(name, str) or not name or name.startswith("-") or any(ch.isspace() for ch in name):
        raise LedgerError(400, "bad_branch", "Branch name is not usable.")
    result = subprocess.run(
        ["git", "check-ref-format", f"refs/heads/{name}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise LedgerError(400, "bad_branch", "Branch name is not usable.")
    return name


def _clean_grace(grace: float | None) -> float:
    if grace is None:
        return _DEFAULT_GRACE
    try:
        value = float(grace)
    except (TypeError, ValueError) as exc:
        raise LedgerError(400, "bad_grace", "Grace must be zero or more seconds.") from exc
    if value < 0:
        raise LedgerError(400, "bad_grace", "Grace must be zero or more seconds.")
    return value


def _clean_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
        raise LedgerError(400, "bad_job", "Job id is not usable.")
    return job_id


def _ready_timeout() -> float:
    """Seconds ``job run`` waits for the supervisor to report the command started.

    Test hook ``SHUIN_JOB_READY_TIMEOUT`` overrides the default of 10 seconds.
    The value must be a positive number. When it expires, the launch fails.
    """
    raw = os.environ.get("SHUIN_JOB_READY_TIMEOUT")
    if not raw:
        return _READY_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return _READY_TIMEOUT
    return value if value > 0 else _READY_TIMEOUT


def _start_delay() -> float:
    """Seconds the supervisor waits after it is ready and before it execs.

    Test hook ``SHUIN_JOB_START_DELAY`` (default 0). During the wait the
    supervisor checks the stop-requested flag, so a stop or a ready timeout
    can cancel the command before it starts.
    """
    raw = os.environ.get("SHUIN_JOB_START_DELAY")
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


def _stop_requested(path: Path) -> bool:
    try:
        return bool(_load(path).get("stop_requested"))
    except (OSError, json.JSONDecodeError):
        return False


def _jobs_dir(db: Path, *, create: bool = True) -> Path:
    path = Path(db).resolve().parent / "jobs"
    if create:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    return path


def _record_path(db: Path, job_id: str) -> Path:
    return _jobs_dir(db) / f"{job_id}.json"


def _alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_SH)
        return json.loads(handle.read())


def _create_record(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(data, indent=2) + "\n").encode("utf-8")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise LedgerError(409, "exists", "Job already exists.") from exc
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _mark_failed_if_open(path: Path) -> None:
    """Mark a launch that never ran as failed. Leave a real exit code alone."""

    def apply(current):
        status = current.get("status")
        if status in _TERMINAL:
            never_ran = status == "stopped" and current.get("pid") is None and current.get("exit_code") is None
            if not never_ran:
                return
        current["status"] = "failed"
        current["ended_at"] = int(time.time())

    try:
        _mutate(path, apply)
    except (OSError, json.JSONDecodeError):
        pass


def _mark_stopped_if_open(path: Path) -> None:
    def apply(current):
        if current.get("status") in _TERMINAL:
            return
        current["status"] = "stopped"
        current["stop_requested"] = True
        current["ended_at"] = int(time.time())

    _mutate(path, apply)


def _patch(path: Path, **fields) -> None:
    def apply(current):
        if current.get("status") in _TERMINAL:
            for key, value in fields.items():
                if key in ("status", "exit_code", "ended_at"):
                    continue
                current[key] = value
            return
        incoming = dict(fields)
        if incoming.get("status") == "running" and current.get("stop_requested"):
            incoming["status"] = "stopped"
            incoming.setdefault("ended_at", int(time.time()))
        if (
            incoming.get("status") == "stopped"
            and incoming.get("exit_code") is None
            and current.get("exit_code") is not None
        ):
            incoming.pop("exit_code", None)
        current.update(incoming)

    _mutate(path, apply)


def _mutate(path: Path, fn) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blocked = hasattr(signal, "pthread_sigmask")
    if blocked:
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if _fcntl is not None:
                _fcntl.flock(fd, _fcntl.LOCK_EX)
            raw = b""
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                raw += chunk
            data = json.loads(raw.decode("utf-8")) if raw.strip() else {}
            fn(data)
            text = json.dumps(data, indent=2) + "\n"
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, text.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if blocked:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
