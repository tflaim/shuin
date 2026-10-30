import contextvars
import os
import sqlite3
import time
from pathlib import Path

from shuin.errors import LedgerError

_ledger_path: contextvars.ContextVar[str | None] = contextvars.ContextVar("shuin_ledger_path", default=None)


def default_db_path() -> Path:
    env = os.environ.get("SHUIN_DB")
    if env:
        return Path(env)
    home = Path(os.environ.get("SHUIN_HOME", Path.home() / ".shuin"))
    return home / "ledger.db"


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# schema.sql is the latest shape. These steps bring an already-created ledger forward.
LATEST_VERSION = 11

_EVIDENCE_VIEW = """CREATE VIEW evidence_v AS
  SELECT e.*,
    CASE
      WHEN p.head_sha IS NULL OR trim(p.head_sha)='' THEN 0
      WHEN trim(e.commit_sha)='' THEN 1
      WHEN lower(trim(e.commit_sha))=lower(trim(p.head_sha)) THEN 0
      ELSE 1
    END AS stale
  FROM evidence e JOIN prs p ON p.id=e.pr_id"""

_V1_TABLES = frozenset({
    "schema_migrations",
    "agents",
    "tasks",
    "prs",
    "updates",
    "evidence",
    "verification_queue",
    "tried_failed",
    "review_requests",
    "task_runs",
    "pr_acceptance",
    "subscriptions",
    "events",
    "deliveries",
    "lead_outbox",
    "inbox_cursor",
    "gh_webhook_log",
    "metrics_daily",
    "settings",
})


def migrate(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "schema_migrations"):
        _install_schema(conn)
        return
    current = _lock_version(conn)
    _refuse_newer(current)
    if current == LATEST_VERSION:
        return
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Another connection may have finished this upgrade while we waited for the lock.
            current = _schema_version(conn)
            _refuse_newer(current)
            if current < 2:
                _migrate_verifier_state(conn)
                _record_version(conn, 2)
            if current < 3:
                _migrate_update_kinds(conn)
                _record_version(conn, 3)
            if current < 4:
                _migrate_blocked_reason(conn)
                _record_version(conn, 4)
            if current < 5:
                _migrate_fresh_run(conn)
                _record_version(conn, 5)
            if current < 6:
                _migrate_close_and_pass_kind(conn)
                _record_version(conn, 6)
            if current < 7:
                _migrate_closed_reason(conn)
                _record_version(conn, 7)
            if current < 8:
                _migrate_pending_head_and_model(conn)
                _record_version(conn, 8)
            if current < 9:
                _migrate_task_deps(conn)
                _record_version(conn, 9)
            if current < 10:
                _migrate_evidence_stale(conn)
                _record_version(conn, 10)
            if current < 11:
                _migrate_trust_events(conn)
                _record_version(conn, 11)
            bad = conn.execute("PRAGMA foreign_key_check").fetchall()
            if bad:
                raise sqlite3.IntegrityError("foreign key check failed")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _refuse_newer(current: int) -> None:
    if current > LATEST_VERSION:
        raise LedgerError(
            400,
            "schema_newer",
            f"ledger schema {current}, code expects {LATEST_VERSION}",
        )


def _schema_statements() -> list[str]:
    """schema.sql, one statement at a time. executescript commits, so it cannot run inside a lock."""
    lines = []
    for line in Path(__file__).with_name("schema.sql").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("--"):
            continue
        lines.append(line)
    return [part.strip() for part in "\n".join(lines).split(";") if part.strip()]


def _install_schema(conn: sqlite3.Connection) -> None:
    sql = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    conn.executescript(sql)
    _record_version(conn, LATEST_VERSION)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _has_ledger_tables(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        """SELECT 1 FROM sqlite_master
           WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='schema_migrations'"""
    ).fetchone()
    return row is not None


def _lock_version(conn: sqlite3.Connection) -> int:
    """Read or infer the schema version, and install a blank file, under the write lock."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = _schema_version(conn)
        if current == 0 and not _has_ledger_tables(conn):
            conn.execute("DROP TABLE schema_migrations")
            for statement in _schema_statements():
                conn.execute(statement)
            _record_version(conn, LATEST_VERSION)
            current = _schema_version(conn)
        elif current == 0:
            current = _infer_version(conn)
            _record_version(conn, current)
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return current


def _schema_unknown() -> None:
    raise LedgerError(
        400,
        "schema_unknown",
        "Ledger schema is not a known version. Restore schema_migrations from a backup.",
    )


def _schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    version = row["v"]
    if version is None:
        return 0
    return int(version)


def _table_sql(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    if row is None or not row["sql"]:
        return ""
    return row["sql"]


def _user_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in conn.execute(
            """SELECT name FROM sqlite_master
               WHERE type='table' AND name NOT LIKE 'sqlite_%'"""
        )
    }


def _object_sql(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name=? AND type IN ('table', 'view')",
        (name,),
    ).fetchone()
    if row is None or not row["sql"]:
        return ""
    return row["sql"]


def _has_column(conn: sqlite3.Connection, table: str, name: str) -> bool:
    return _table_exists(conn, table) and name in _column_names(conn, table)


def _version_markers(conn: sqlite3.Connection, version: int) -> list[bool]:
    """Pieces this migration adds. All of these, and every removal, or it is not this version."""
    if version == 2:
        return [_has_column(conn, "pr_acceptance", "verifier_state")]
    if version == 3:
        sql = _object_sql(conn, "updates")
        return ["tried-failed" in sql, "'done'" in sql]
    if version == 4:
        return ["waiting_verifier" in _object_sql(conn, "tasks")]
    if version == 5:
        subs = _object_sql(conn, "subscriptions")
        return [
            _has_column(conn, "prs", "head_epoch"),
            _has_column(conn, "prs", "head_seen_at"),
            _has_column(conn, "prs", "patch_id"),
            _has_column(conn, "verification_queue", "run_kind"),
            _has_column(conn, "verification_queue", "opened_sha"),
            _has_column(conn, "verification_queue", "opened_epoch"),
            "verification.queued" in subs,
            "task.closed" in subs,
            "task.advanced" in subs,
        ]
    if version == 6:
        return [
            _has_column(conn, "prs", "tree_sha"),
            _has_column(conn, "tasks", "closed_by"),
            _has_column(conn, "verification_queue", "pass_kind"),
            _has_column(conn, "verification_queue", "require_run"),
            _has_column(conn, "verification_queue", "patch_id"),
            _has_column(conn, "verification_queue", "tree_sha"),
        ]
    if version == 7:
        return [_has_column(conn, "tasks", "closed_reason")]
    if version == 8:
        return [
            _has_column(conn, "prs", "pending_head"),
            _has_column(conn, "agents", "model"),
            _has_column(conn, "task_runs", "model"),
        ]
    if version == 9:
        return [_table_exists(conn, "task_deps")]
    if version == 10:
        return ["lower(trim(" in _object_sql(conn, "evidence_v")]
    if version == 11:
        return [_table_exists(conn, "trust_events")]
    raise AssertionError(version)


def _version_removals(conn: sqlite3.Connection, version: int) -> list[bool]:
    """Old pieces this version deletes. A missing one is not a later piece."""
    if version == 2:
        return [not _has_column(conn, "pr_acceptance", "mac_state")]
    if version == 4:
        return ["waiting_mac" not in _object_sql(conn, "tasks")]
    return []


def _infer_version(conn: sqlite3.Connection) -> int:
    """The newest complete schema this ledger already has.

    Walk versions from 1 upward. A later piece without every earlier piece,
    a half-applied version, or a table this code does not know is not a version.
    A column this version removes does not count as a later piece.
    """
    tables = _user_tables(conn)
    if not _V1_TABLES <= tables:
        _schema_unknown()
    version = 1
    for n in range(2, LATEST_VERSION + 1):
        added = _version_markers(conn, n)
        removed = _version_removals(conn, n)
        if all(added) and all(removed):
            version = n
            continue
        later = any(
            any(_version_markers(conn, later))
            for later in range(n + 1, LATEST_VERSION + 1)
        )
        if any(added) or any(removed) or later:
            _schema_unknown()
        break
    allowed = set(_V1_TABLES)
    if version >= 9:
        allowed.add("task_deps")
    if version >= 11:
        allowed.add("trust_events")
    if tables != allowed:
        _schema_unknown()
    return version


def _migrate_trust_events(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS trust_events(
             id INTEGER PRIMARY KEY,
             agent_id TEXT NOT NULL REFERENCES agents(id),
             repo TEXT NOT NULL,
             task_id TEXT REFERENCES tasks(id),
             pr_id INTEGER REFERENCES prs(id),
             kind TEXT NOT NULL CHECK(kind IN('merged_clean','merged_edited','reverted','reworked')),
             head_sha TEXT,
             created_at INTEGER NOT NULL
           )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS trust_by_agent_repo ON trust_events(agent_id, repo, id)")


def _migrate_evidence_stale(conn: sqlite3.Connection) -> None:
    """The evidence view compares commits the same way a SHA compare does."""
    conn.execute("DROP VIEW IF EXISTS evidence_v")
    conn.execute(_EVIDENCE_VIEW)


def _record_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
        (version, int(time.time())),
    )


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_update_kinds(conn: sqlite3.Connection) -> None:
    """updates.kind is a CHECK constraint, so existing ledgers need a table rebuild."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='updates'").fetchone()
    if row is None or "tried-failed" in (row["sql"] or ""):
        return
    conn.execute(
        """CREATE TABLE updates_new(
             id INTEGER PRIMARY KEY,
             task_id TEXT REFERENCES tasks(id),
             agent_id TEXT NOT NULL REFERENCES agents(id),
             kind TEXT NOT NULL CHECK(kind IN('progress','question','handoff','note','done','tried-failed')),
             body TEXT NOT NULL CHECK(length(body)<=2000),
             label TEXT NOT NULL DEFAULT 'agent_claim' CHECK(label='agent_claim'),
             on_behalf_of TEXT,
             reported_via TEXT CHECK(reported_via IS NULL OR reported_via='lead'),
             created_at INTEGER NOT NULL
           )"""
    )
    conn.execute(
        """INSERT INTO updates_new(
             id, task_id, agent_id, kind, body, label, on_behalf_of, reported_via, created_at
           )
           SELECT id, task_id, agent_id, kind, body, label, on_behalf_of, reported_via, created_at
           FROM updates"""
    )
    conn.execute("DROP TABLE updates")
    conn.execute("ALTER TABLE updates_new RENAME TO updates")


def _migrate_blocked_reason(conn: sqlite3.Connection) -> None:
    """Old ledgers allow waiting_mac. The code writes waiting_verifier."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone()
    if row is None or "waiting_mac" not in (row["sql"] or ""):
        return
    conn.execute(
        """CREATE TABLE tasks_new(
             id TEXT PRIMARY KEY,
             title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
             owner_agent TEXT REFERENCES agents(id),
             status TEXT NOT NULL CHECK(status IN('todo','active','blocked','in_review','verifying','done','dropped')),
             blocked_reason TEXT CHECK(blocked_reason IN('needs_human','needs_review','waiting_verifier','waiting_dep','unclear_spec','other')),
             depends_on TEXT REFERENCES tasks(id),
             repo TEXT,
             idempotency_key TEXT,
             created_at INTEGER NOT NULL,
             updated_at INTEGER NOT NULL
           )"""
    )
    conn.execute(
        """INSERT INTO tasks_new(
             id, title, owner_agent, status, blocked_reason, depends_on, repo, idempotency_key, created_at, updated_at
           )
           SELECT id, title, owner_agent, status,
                  CASE blocked_reason WHEN 'waiting_mac' THEN 'waiting_verifier' ELSE blocked_reason END,
                  depends_on, repo, idempotency_key, created_at, updated_at
           FROM tasks"""
    )
    conn.execute("DROP TABLE tasks")
    conn.execute("ALTER TABLE tasks_new RENAME TO tasks")
    conn.execute(
        "CREATE UNIQUE INDEX tasks_idem ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL"
    )


def _add_column(conn: sqlite3.Connection, table: str, name: str, ddl: str) -> None:
    if name in _column_names(conn, table):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _migrate_fresh_run(conn: sqlite3.Connection) -> None:
    """A pass needs the head epoch and the run that was opened on that head."""
    _add_column(conn, "prs", "head_epoch", "head_epoch INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "prs", "head_seen_at", "head_seen_at INTEGER")
    _add_column(conn, "prs", "patch_id", "patch_id TEXT")
    _add_column(
        conn,
        "verification_queue",
        "run_kind",
        "run_kind TEXT NOT NULL DEFAULT 'full' CHECK(run_kind IN('full','carried_over','patch_identity'))",
    )
    _add_column(conn, "verification_queue", "opened_sha", "opened_sha TEXT")
    _add_column(conn, "verification_queue", "opened_epoch", "opened_epoch INTEGER")
    conn.execute(
        """UPDATE prs SET head_epoch=1
           WHERE head_sha IS NOT NULL AND (head_epoch IS NULL OR head_epoch=0)"""
    )
    conn.execute(
        """UPDATE prs SET head_seen_at=last_synced_at
           WHERE head_seen_at IS NULL AND last_synced_at IS NOT NULL"""
    )
    conn.execute(
        """UPDATE verification_queue
           SET opened_sha=commit_sha,
               opened_epoch=(SELECT head_epoch FROM prs WHERE prs.id=verification_queue.pr_id)
           WHERE opened_sha IS NULL AND claimed_at IS NOT NULL
             AND commit_sha=(SELECT head_sha FROM prs WHERE prs.id=verification_queue.pr_id)"""
    )
    _migrate_subscription_events(conn)


def _migrate_close_and_pass_kind(conn: sqlite3.Connection) -> None:
    """Record how a card was closed, and which kind of pass a queue row is."""
    _add_column(conn, "prs", "tree_sha", "tree_sha TEXT")
    _add_column(
        conn,
        "tasks",
        "closed_by",
        "closed_by TEXT CHECK(closed_by IS NULL OR closed_by IN('merge','sealed_merge','manual'))",
    )
    _add_column(
        conn,
        "verification_queue",
        "pass_kind",
        "pass_kind TEXT NOT NULL DEFAULT 'fresh' CHECK(pass_kind IN('fresh','tree_match','patch_carried'))",
    )
    _add_column(conn, "verification_queue", "require_run", "require_run INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "verification_queue", "patch_id", "patch_id TEXT")
    _add_column(conn, "verification_queue", "tree_sha", "tree_sha TEXT")


def _migrate_task_deps(conn: sqlite3.Connection) -> None:
    """A card can wait on task ids or on a PR merge. Copy the old parent pointer."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS task_deps(
             id INTEGER PRIMARY KEY,
             task_id TEXT NOT NULL REFERENCES tasks(id),
             dep_task_id TEXT REFERENCES tasks(id),
             dep_repo TEXT,
             dep_number INTEGER,
             dep_state TEXT CHECK(dep_state IS NULL OR dep_state IN('open','merged','closed')),
             created_at INTEGER NOT NULL,
             CHECK (
               (dep_task_id IS NOT NULL AND dep_repo IS NULL AND dep_number IS NULL)
               OR
               (dep_task_id IS NULL AND dep_repo IS NOT NULL AND dep_number IS NOT NULL)
             )
           )"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS task_deps_task
           ON task_deps(task_id, dep_task_id) WHERE dep_task_id IS NOT NULL"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS task_deps_pr
           ON task_deps(task_id, dep_repo, dep_number) WHERE dep_number IS NOT NULL"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS task_deps_by_task
           ON task_deps(dep_task_id) WHERE dep_task_id IS NOT NULL"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS task_deps_by_pr
           ON task_deps(dep_repo, dep_number) WHERE dep_number IS NOT NULL"""
    )
    conn.execute(
        """INSERT INTO task_deps(task_id, dep_task_id, created_at)
           SELECT t.id, t.depends_on, t.updated_at
           FROM tasks t
           WHERE t.depends_on IS NOT NULL
             AND NOT EXISTS (
               SELECT 1 FROM task_deps d
               WHERE d.task_id=t.id AND d.dep_task_id=t.depends_on
             )"""
    )


def _migrate_pending_head_and_model(conn: sqlite3.Connection) -> None:
    """A claimed commit is a pending head until poll confirms it. Runs remember a model."""
    _add_column(conn, "prs", "pending_head", "pending_head TEXT")
    _add_column(conn, "agents", "model", "model TEXT")
    _add_column(conn, "task_runs", "model", "model TEXT")


def _migrate_closed_reason(conn: sqlite3.Connection) -> None:
    """A lead records why a card was closed when the merge happened outside the ledger."""
    _add_column(
        conn,
        "tasks",
        "closed_reason",
        "closed_reason TEXT CHECK(closed_reason IS NULL OR length(closed_reason) BETWEEN 1 AND 300)",
    )


def _migrate_subscription_events(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='subscriptions'"
    ).fetchone()
    if row is None or "verification.queued" in (row["sql"] or ""):
        return
    conn.execute(
        """CREATE TABLE subscriptions_new(
             id INTEGER PRIMARY KEY,
             agent_id TEXT NOT NULL REFERENCES agents(id),
             event_type TEXT NOT NULL CHECK(event_type IN(
               'pr.updated','review.requested','verification.completed','agent.blocked',
               'verification_slot.freed','verification.queued','task.closed','task.advanced'
             )),
             filter_json TEXT,
             channel_type TEXT NOT NULL CHECK(channel_type IN('webhook','cursor_followup','poll')),
             channel_target TEXT,
             secret TEXT,
             active INTEGER NOT NULL DEFAULT 1,
             expires_at INTEGER,
             created_at INTEGER NOT NULL
           )"""
    )
    conn.execute(
        """INSERT INTO subscriptions_new(
             id, agent_id, event_type, filter_json, channel_type, channel_target,
             secret, active, expires_at, created_at
           )
           SELECT id, agent_id, event_type, filter_json, channel_type, channel_target,
                  secret, active, expires_at, created_at
           FROM subscriptions"""
    )
    conn.execute("DROP TABLE subscriptions")
    conn.execute("ALTER TABLE subscriptions_new RENAME TO subscriptions")


def _migrate_verifier_state(conn: sqlite3.Connection) -> None:
    """Seal inserts verifier_state. Ledgers created before the rename still have mac_state."""
    columns = _column_names(conn, "pr_acceptance")
    if "verifier_state" in columns:
        return
    if "mac_state" in columns:
        try:
            conn.execute("ALTER TABLE pr_acceptance RENAME COLUMN mac_state TO verifier_state")
        except sqlite3.OperationalError:
            conn.execute(
                """ALTER TABLE pr_acceptance ADD COLUMN verifier_state TEXT
                   CHECK(verifier_state IS NULL OR verifier_state IN('pass','fail','missing'))"""
            )
            conn.execute("UPDATE pr_acceptance SET verifier_state=mac_state")
        return
    conn.execute(
        """ALTER TABLE pr_acceptance ADD COLUMN verifier_state TEXT
           CHECK(verifier_state IS NULL OR verifier_state IN('pass','fail','missing'))"""
    )


def open_db(path: Path | str | None = None) -> sqlite3.Connection:
    target = Path(path) if path else default_db_path()
    conn = connect(target)
    try:
        migrate(conn)
        _repair_verifier_state(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _repair_verifier_state(conn: sqlite3.Connection) -> None:
    """Heal a ledger whose version stamp is current but the column is missing.

    `shuin seal` inserts verifier_state. A database that was stamped at the
    latest version and still has mac_state, or neither column, used to raise
    OperationalError on that insert.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pr_acceptance'"
    ).fetchone()
    if exists is None:
        return
    _migrate_verifier_state(conn)


def bind_ledger_path(path: str | None) -> None:
    if path:
        _ledger_path.set(str(path))


def token_required_message(db_path: str | Path | None = None) -> str:
    """Name the env var, the flag, and any token files sitting next to the ledger."""
    raw = db_path or _ledger_path.get() or default_db_path()
    folder = Path(raw).parent
    found: list[str] = []
    try:
        if folder.is_dir():
            found = sorted(item.name for item in folder.glob("*.token") if item.is_file())
    except OSError:
        found = []
    files = ", ".join(found) if found else "*.token"
    return f"Token required. Set SHUIN_TOKEN, pass --token, or use {files} next to SHUIN_DB."


def _role_phrase(roles: tuple[str, ...]) -> str:
    names: list[str] = []
    for role in ("owner", *roles):
        if role not in names:
            names.append(role)
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} or {names[1]}"
    return ", ".join(names[:-1]) + f", or {names[-1]}"


def role_mismatch_message(*roles: str, action: str | None = None) -> str:
    phrase = _role_phrase(roles)
    if action:
        return f"Role mismatch. {action} requires {phrase}."
    return f"Role mismatch. This action requires {phrase}."


def require(actor: dict | None, *roles: str, action: str | None = None) -> None:
    if actor is None:
        raise LedgerError(401, "token_required", token_required_message())
    if actor["role"] == "owner":
        return
    if actor["role"] in roles:
        return
    raise LedgerError(403, "role_mismatch", role_mismatch_message(*roles, action=action))
