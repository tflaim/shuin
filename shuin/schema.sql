-- Shuin ledger schema. Weekend MVP.
-- GitHub stays the source of truth. These rows store ids and cached fields.
-- Free text lives only in updates, tried_failed, result_note, and task_runs.summary.
-- Confirmed facts are cached PR columns plus ledger checks (stale evidence, queue, overlap).

CREATE TABLE schema_migrations(
  version INTEGER PRIMARY KEY,
  applied_at INTEGER NOT NULL
);

CREATE TABLE agents(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN('cursor','claude_code','codex','hermes','lead','verifier','human')),
  role TEXT NOT NULL CHECK(role IN('owner','lead','worker','verifier','system')),
  token_hash TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  disabled_at INTEGER,
  model TEXT
);
CREATE UNIQUE INDEX agents_token ON agents(token_hash);

CREATE TABLE tasks(
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
  owner_agent TEXT REFERENCES agents(id),
  status TEXT NOT NULL CHECK(status IN('todo','active','blocked','in_review','verifying','done','dropped')),
  blocked_reason TEXT CHECK(blocked_reason IN('needs_human','needs_review','waiting_verifier','waiting_dep','unclear_spec','other')),
  depends_on TEXT REFERENCES tasks(id),
  repo TEXT,
  idempotency_key TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  closed_by TEXT CHECK(closed_by IS NULL OR closed_by IN('merge','sealed_merge','manual')),
  closed_reason TEXT CHECK(closed_reason IS NULL OR length(closed_reason) BETWEEN 1 AND 300)
);
CREATE UNIQUE INDEX tasks_idem ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL;

-- A card waits on another task, or on a PR merge. depends_on stays the stack parent.
CREATE TABLE task_deps(
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
);
CREATE UNIQUE INDEX task_deps_task ON task_deps(task_id, dep_task_id) WHERE dep_task_id IS NOT NULL;
CREATE UNIQUE INDEX task_deps_pr ON task_deps(task_id, dep_repo, dep_number) WHERE dep_number IS NOT NULL;
CREATE INDEX task_deps_by_task ON task_deps(dep_task_id) WHERE dep_task_id IS NOT NULL;
CREATE INDEX task_deps_by_pr ON task_deps(dep_repo, dep_number) WHERE dep_number IS NOT NULL;

CREATE TABLE prs(
  id INTEGER PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  repo TEXT NOT NULL,
  number INTEGER NOT NULL,
  github_node_id TEXT,
  branch TEXT,
  base_branch TEXT,
  head_sha TEXT,
  pending_head TEXT,
  head_epoch INTEGER NOT NULL DEFAULT 0,
  head_seen_at INTEGER,
  patch_id TEXT,
  tree_sha TEXT,
  state TEXT,
  review_state TEXT,
  ci_state TEXT,
  files_json TEXT,
  last_synced_at INTEGER,
  UNIQUE(repo, number)
);
-- First PR bound to a card sticks. Stacked PRs are child tasks, not a second PR.
CREATE UNIQUE INDEX prs_one_per_task ON prs(task_id);
CREATE UNIQUE INDEX prs_node ON prs(github_node_id) WHERE github_node_id IS NOT NULL;

CREATE TABLE trust_events(
  id INTEGER PRIMARY KEY,
  agent_id TEXT NOT NULL REFERENCES agents(id),
  repo TEXT NOT NULL,
  task_id TEXT REFERENCES tasks(id),
  pr_id INTEGER REFERENCES prs(id),
  kind TEXT NOT NULL CHECK(kind IN('merged_clean','merged_edited','reverted','reworked')),
  head_sha TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX trust_by_agent_repo ON trust_events(agent_id, repo, id);

CREATE TABLE process_runs(
  id INTEGER PRIMARY KEY,
  job_id TEXT NOT NULL,
  step TEXT NOT NULL CHECK(step IN('waybook-warm-start','verify','shuin-record')),
  status TEXT NOT NULL CHECK(status IN('ok','missing','error','not-run','passed','failed','timeout','none')),
  created_at INTEGER NOT NULL
);
CREATE INDEX process_runs_by_time ON process_runs(created_at);

CREATE TABLE updates(
  id INTEGER PRIMARY KEY,
  task_id TEXT REFERENCES tasks(id),
  agent_id TEXT NOT NULL REFERENCES agents(id),
  kind TEXT NOT NULL CHECK(kind IN('progress','question','handoff','note','done','tried-failed')),
  body TEXT NOT NULL CHECK(length(body)<=2000),
  label TEXT NOT NULL DEFAULT 'agent_claim' CHECK(label='agent_claim'),
  on_behalf_of TEXT,
  reported_via TEXT CHECK(reported_via IS NULL OR reported_via='lead'),
  created_at INTEGER NOT NULL
);

CREATE TABLE evidence(
  id INTEGER PRIMARY KEY,
  pr_id INTEGER NOT NULL REFERENCES prs(id),
  agent_id TEXT NOT NULL REFERENCES agents(id),
  kind TEXT NOT NULL CHECK(kind IN('screenshot','film','test_log','build_log')),
  uri TEXT NOT NULL CHECK(length(uri) BETWEEN 1 AND 2000),
  sha256 TEXT,
  commit_sha TEXT NOT NULL,
  result TEXT CHECK(result IN('pass','fail','info')),
  on_behalf_of TEXT,
  reported_via TEXT CHECK(reported_via IS NULL OR reported_via='lead'),
  created_at INTEGER NOT NULL
);
-- stale when the evidence commit is not the PR head. Case and space do not count.
CREATE VIEW evidence_v AS
  SELECT e.*,
    CASE
      WHEN p.head_sha IS NULL OR trim(p.head_sha)='' THEN 0
      WHEN trim(e.commit_sha)='' THEN 1
      WHEN lower(trim(e.commit_sha))=lower(trim(p.head_sha)) THEN 0
      ELSE 1
    END AS stale
  FROM evidence e JOIN prs p ON p.id=e.pr_id;

CREATE TABLE verification_queue(
  id INTEGER PRIMARY KEY,
  pr_id INTEGER NOT NULL REFERENCES prs(id),
  commit_sha TEXT NOT NULL,
  requested_by TEXT NOT NULL REFERENCES agents(id),
  state TEXT NOT NULL CHECK(state IN('queued','running','passed','failed','expired','cancelled','superseded')),
  enqueued_at INTEGER NOT NULL,
  claimed_by TEXT,
  claimed_at INTEGER,
  lease_expires_at INTEGER,
  finished_at INTEGER,
  result_note TEXT CHECK(result_note IS NULL OR length(result_note) BETWEEN 1 AND 300),
  on_behalf_of TEXT,
  reported_via TEXT CHECK(reported_via IS NULL OR reported_via='lead'),
  run_kind TEXT NOT NULL DEFAULT 'full' CHECK(run_kind IN('full','carried_over','patch_identity')),
  opened_sha TEXT,
  opened_epoch INTEGER,
  pass_kind TEXT NOT NULL DEFAULT 'fresh' CHECK(pass_kind IN('fresh','tree_match','patch_carried')),
  require_run INTEGER NOT NULL DEFAULT 0,
  patch_id TEXT,
  tree_sha TEXT
);
CREATE INDEX vq_fifo ON verification_queue(state, enqueued_at);

CREATE TABLE tried_failed(
  id INTEGER PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  pr_id INTEGER REFERENCES prs(id),
  agent_id TEXT NOT NULL REFERENCES agents(id),
  entry TEXT NOT NULL CHECK(length(trim(entry)) BETWEEN 1 AND 300 AND entry = trim(entry)),
  on_behalf_of TEXT,
  reported_via TEXT CHECK(reported_via IS NULL OR reported_via='lead'),
  created_at INTEGER NOT NULL
);

CREATE TABLE review_requests(
  id INTEGER PRIMARY KEY,
  pr_id INTEGER NOT NULL REFERENCES prs(id),
  requested_by TEXT NOT NULL REFERENCES agents(id),
  tried_failed_id INTEGER NOT NULL REFERENCES tried_failed(id),
  state TEXT NOT NULL DEFAULT 'open',
  on_behalf_of TEXT,
  reported_via TEXT CHECK(reported_via IS NULL OR reported_via='lead'),
  created_at INTEGER NOT NULL
);

-- One row per attempt. Summary holds the tried-and-failed line for that attempt.
CREATE TABLE task_runs(
  id INTEGER PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  agent_id TEXT NOT NULL REFERENCES agents(id),
  model TEXT,
  attempt INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN('started','review_requested','completed','failed','blocked')),
  outcome TEXT CHECK(outcome IS NULL OR outcome IN('pass','fail','blocked','info')),
  summary TEXT CHECK(summary IS NULL OR length(summary)<=300),
  metadata_json TEXT,
  error TEXT CHECK(error IS NULL OR length(error)<=300),
  started_at INTEGER NOT NULL,
  ended_at INTEGER,
  UNIQUE(task_id, attempt)
);

-- Fail-closed PR acceptance receipt. Written in the same transaction as the decision.
CREATE TABLE pr_acceptance(
  id INTEGER PRIMARY KEY,
  pr_id INTEGER NOT NULL REFERENCES prs(id),
  task_id TEXT NOT NULL REFERENCES tasks(id),
  head_sha TEXT,
  head_sha_reread TEXT,
  classification TEXT NOT NULL CHECK(classification IN('success','pending','failure','stale','missing','auth','infra')),
  required_checks_json TEXT NOT NULL,
  verifier_state TEXT CHECK(verifier_state IS NULL OR verifier_state IN('pass','fail','missing')),
  receipt_json TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE subscriptions(
  id INTEGER PRIMARY KEY,
  agent_id TEXT NOT NULL REFERENCES agents(id),
  event_type TEXT NOT NULL CHECK(event_type IN('pr.updated','review.requested','verification.completed','agent.blocked','verification_slot.freed','verification.queued','task.closed','task.advanced')),
  filter_json TEXT,
  channel_type TEXT NOT NULL CHECK(channel_type IN('webhook','cursor_followup','poll')),
  channel_target TEXT,
  secret TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  expires_at INTEGER,
  created_at INTEGER NOT NULL
);

-- events.id is the cursor (Hermes task_events shape). Payloads are ids and enums only.
CREATE TABLE events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,
  task_id TEXT,
  pr_id INTEGER,
  queue_id INTEGER,
  actor TEXT,
  enum_state TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE deliveries(
  id INTEGER PRIMARY KEY,
  event_id INTEGER REFERENCES events(id),
  subscription_id INTEGER REFERENCES subscriptions(id),
  status TEXT NOT NULL CHECK(status IN('pending','sent','failed','dead')),
  attempts INTEGER DEFAULT 0,
  next_try_at INTEGER
);

-- Lead outbox. The lead relays these. Shuin does not call an external agent API.
CREATE TABLE lead_outbox(
  id INTEGER PRIMARY KEY,
  delivery_id INTEGER NOT NULL UNIQUE REFERENCES deliveries(id),
  event_id INTEGER NOT NULL,
  target TEXT,
  payload_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  acked_at INTEGER
);

CREATE TABLE inbox_cursor(
  agent_id TEXT PRIMARY KEY,
  last_event_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE gh_webhook_log(
  delivery_id TEXT PRIMARY KEY,
  event TEXT,
  received_at INTEGER
);

CREATE TABLE metrics_daily(
  day TEXT,
  metric TEXT,
  value INTEGER,
  PRIMARY KEY(day, metric)
);

CREATE TABLE settings(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
