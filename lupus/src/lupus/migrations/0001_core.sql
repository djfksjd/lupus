-- Lupus supervisor kernel schema, version 1 (core).
-- Applied migrations are immutable: change the schema by adding a new numbered file.
-- The operational DB is the single authority for goal/task/approval/budget/checkpoint state
-- (LUPUS-PLAN §3, §5). Vault notes and handoff packets are projections of this DB.
-- Invariants that can be expressed as constraints are enforced here, not only in code.

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;

-- Command idempotency: the same request_id returns the stored result; the same id with a
-- different payload is rejected.
CREATE TABLE request (
  request_id   TEXT PRIMARY KEY,
  op           TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  result       TEXT NOT NULL,
  created_at   INTEGER NOT NULL
) STRICT;

-- ---------------------------------------------------------------- projects
CREATE TABLE project (
  project_id         TEXT PRIMARY KEY,
  alpha_id           TEXT NOT NULL UNIQUE,
  name               TEXT NOT NULL,
  canonical_root     TEXT NOT NULL UNIQUE,   -- realpath at registration
  root_identity      TEXT NOT NULL,          -- "dev:ino" of the root, detects moved/replaced dirs
  approved_providers TEXT NOT NULL DEFAULT '[]',
  policy_version     INTEGER NOT NULL DEFAULT 1,
  revocation_epoch   INTEGER NOT NULL DEFAULT 1,
  created_at         INTEGER NOT NULL
) STRICT;

-- ---------------------------------------------------------------- budgets
CREATE TABLE budget (
  budget_id        TEXT PRIMARY KEY,
  parent_budget_id TEXT REFERENCES budget(budget_id),
  label            TEXT NOT NULL,
  created_at       INTEGER NOT NULL
) STRICT;

-- One row per (budget, dimension). `used` may exceed `cap` after an observed overrun; the kernel
-- then refuses every new reservation. Unknown usage is never refunded.
--   calls/attempts/active_ms are charged through reservations
--   tokens/storage_bytes are charged directly from observed usage / committed recovery objects
CREATE TABLE budget_line (
  budget_id       TEXT NOT NULL REFERENCES budget(budget_id),
  dimension       TEXT NOT NULL CHECK (dimension IN
                    ('calls','attempts','active_ms','tokens','storage_bytes')),
  cap             INTEGER NOT NULL CHECK (cap >= 0),
  used            INTEGER NOT NULL DEFAULT 0 CHECK (used >= 0),
  reserved        INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0),
  safety_reserved INTEGER NOT NULL DEFAULT 0 CHECK (safety_reserved >= 0),
  PRIMARY KEY (budget_id, dimension)
) STRICT;

CREATE TABLE reservation (
  reservation_id TEXT PRIMARY KEY,
  budget_id      TEXT NOT NULL REFERENCES budget(budget_id),
  kind           TEXT NOT NULL CHECK (kind IN ('work','safety')),
  purpose        TEXT NOT NULL,
  owner_run_id   TEXT,
  amounts        TEXT NOT NULL,              -- JSON {dimension: n}
  status         TEXT NOT NULL CHECK (status IN ('HELD','SETTLED')),
  actual         TEXT,                       -- JSON {dimension: n} once settled
  observation    TEXT CHECK (observation IN ('measured','estimated','unknown')),
  created_at     INTEGER NOT NULL,
  settled_at     INTEGER,
  CHECK (status <> 'SETTLED' OR (actual IS NOT NULL AND observation IS NOT NULL))
) STRICT;
CREATE INDEX reservation_by_budget ON reservation(budget_id, status);
CREATE INDEX reservation_by_run ON reservation(owner_run_id, status);

-- Every raise of a cap is a user decision and is recorded; there is no automatic extension.
CREATE TABLE budget_change (
  change_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  budget_id  TEXT NOT NULL REFERENCES budget(budget_id),
  dimension  TEXT NOT NULL,
  old_cap    INTEGER NOT NULL,
  new_cap    INTEGER NOT NULL,
  actor      TEXT NOT NULL CHECK (actor = 'user'),
  reason     TEXT NOT NULL,
  created_at INTEGER NOT NULL
) STRICT;

-- ---------------------------------------------------------------- goals
-- goal.status is derived from task states except for the sticky values
-- PAUSED / CANCELLED / FAILED / DONE (see docs/CONTRACT.md).
CREATE TABLE goal (
  goal_id             TEXT PRIMARY KEY,
  project_id          TEXT NOT NULL REFERENCES project(project_id),
  objective           TEXT NOT NULL,
  status              TEXT NOT NULL CHECK (status IN (
                        'ACTIVE','NEEDS_ANSWER','NEEDS_APPROVAL','BUDGET_EXHAUSTED',
                        'EXTERNAL_BLOCKED','EXECUTION_UNKNOWN','RECONCILING','NO_PROGRESS',
                        'PAUSED','CANCELLED','FAILED','DONE')),
  acceptance_revision INTEGER NOT NULL DEFAULT 1,
  budget_id           TEXT NOT NULL REFERENCES budget(budget_id),
  revision            INTEGER NOT NULL DEFAULT 1,   -- CAS counter for goal-level mutations
  created_at          INTEGER NOT NULL,
  updated_at          INTEGER NOT NULL
) STRICT;

-- One row per acceptance revision. Criteria are immutable once written (§5, ACCEPTANCE-01).
CREATE TABLE acceptance (
  goal_id     TEXT NOT NULL REFERENCES goal(goal_id),
  revision    INTEGER NOT NULL,
  criteria    TEXT NOT NULL,                 -- JSON [{id, text, verifier:{kind,...}}]
  changed_by  TEXT NOT NULL,
  reason      TEXT NOT NULL,
  narrows     INTEGER NOT NULL DEFAULT 0 CHECK (narrows IN (0,1)),
  created_at  INTEGER NOT NULL,
  PRIMARY KEY (goal_id, revision),
  CHECK (narrows = 0 OR changed_by = 'user')
) STRICT;

CREATE TRIGGER acceptance_immutable_update BEFORE UPDATE ON acceptance
BEGIN SELECT RAISE(ABORT, 'acceptance rows are immutable'); END;
CREATE TRIGGER acceptance_immutable_delete BEFORE DELETE ON acceptance
BEGIN SELECT RAISE(ABORT, 'acceptance rows are immutable'); END;

-- ---------------------------------------------------------------- tasks
CREATE TABLE task (
  task_id            TEXT PRIMARY KEY,
  goal_id            TEXT NOT NULL REFERENCES goal(goal_id),
  title              TEXT NOT NULL,
  spec               TEXT NOT NULL DEFAULT '{}',   -- JSON {prompt, criteria:[criterion ids]}
  status             TEXT NOT NULL CHECK (status IN (
                       'PENDING','RUNNING','NEEDS_ANSWER','NEEDS_APPROVAL','BUDGET_EXHAUSTED',
                       'EXTERNAL_BLOCKED','EXECUTION_UNKNOWN','RECONCILING','NO_PROGRESS',
                       'CANCELLED','FAILED','DONE')),
  wait_reason        TEXT,
  attempt_count      INTEGER NOT NULL DEFAULT 0,   -- never reset (LOOP-02, BUDGET-01)
  no_progress_streak INTEGER NOT NULL DEFAULT 0,
  created_at         INTEGER NOT NULL,
  updated_at         INTEGER NOT NULL
) STRICT;
CREATE INDEX task_by_goal ON task(goal_id, status);

CREATE TABLE task_dep (
  task_id    TEXT NOT NULL REFERENCES task(task_id),
  depends_on TEXT NOT NULL REFERENCES task(task_id),
  PRIMARY KEY (task_id, depends_on),
  CHECK (task_id <> depends_on)
) STRICT;

-- ---------------------------------------------------------------- runs / leases
-- A run is one execution_driver holding one task lease (§3, DRIVER-01).
--   ACTIVE   lease holder; its results may be integrated
--   STOPPING new work refused; the writer has NOT been confirmed stopped
--   STOPPED  writer exit confirmed with recorded evidence
-- A lease that merely expired stays ACTIVE/STOPPING: expiry is not evidence that a live process
-- stopped writing files (§6.7 step 2), so no new writer is admitted until STOPPED.
CREATE TABLE run (
  run_id            TEXT PRIMARY KEY,
  project_id        TEXT NOT NULL REFERENCES project(project_id),
  goal_id           TEXT NOT NULL REFERENCES goal(goal_id),
  task_id           TEXT NOT NULL REFERENCES task(task_id),
  execution_driver  TEXT NOT NULL,
  auth_mode         TEXT NOT NULL,
  fencing_token     INTEGER NOT NULL UNIQUE, -- monotonic, from meta.next_fencing_token
  status            TEXT NOT NULL CHECK (status IN ('ACTIVE','STOPPING','STOPPED')),
  lease_expires_at  INTEGER NOT NULL,
  pid               INTEGER,                 -- process-group leader of the worker
  proc_start        TEXT,                    -- start time of that pid, guards against pid reuse
  revocation_epoch  INTEGER NOT NULL,
  recovery_epoch    INTEGER NOT NULL,
  policy_version    INTEGER NOT NULL,
  stop_evidence     TEXT,
  closed_seq        INTEGER,                 -- order counter shared with evidence.seq
  result            TEXT,
  started_at        INTEGER NOT NULL,
  ended_at          INTEGER,
  CHECK (status <> 'STOPPED' OR stop_evidence IS NOT NULL)
) STRICT;

-- Single writer per registered original project (§3.2, §5). Enforced by the database, and
-- acquired in the same transaction as the task lease.
CREATE UNIQUE INDEX run_single_writer ON run(project_id) WHERE status IN ('ACTIVE','STOPPING');
CREATE INDEX run_by_task ON run(task_id);

-- Process groups other than a run's worker that execute inside a project (command verifiers).
-- Recorded BEFORE they are allowed to exec, removed only once the group is confirmed empty.
-- While a row's group is alive the project's writer slot counts as taken.
CREATE TABLE aux_process (
  pid         INTEGER PRIMARY KEY,           -- process-group leader
  proc_start  TEXT NOT NULL,
  project_id  TEXT NOT NULL REFERENCES project(project_id),
  purpose     TEXT NOT NULL,
  created_at  INTEGER NOT NULL
) STRICT;

-- ---------------------------------------------------------------- attempts / evidence
CREATE TABLE attempt (
  attempt_id            TEXT PRIMARY KEY,
  goal_id               TEXT NOT NULL REFERENCES goal(goal_id),
  task_id               TEXT NOT NULL REFERENCES task(task_id),
  run_id                TEXT NOT NULL REFERENCES run(run_id),
  hypothesis_id         TEXT NOT NULL,
  acceptance_revision   INTEGER NOT NULL,
  fingerprint           TEXT NOT NULL,   -- H(baseline, change scope, verifier, environment, hypothesis)
  new_evidence          TEXT NOT NULL,   -- what differs from every earlier attempt on this task
  retry_reason          TEXT,            -- pre-declared flaky/transient resampling only
  retry_cap             INTEGER,
  work_reservation_id   TEXT NOT NULL REFERENCES reservation(reservation_id),
  safety_reservation_id TEXT NOT NULL REFERENCES reservation(reservation_id),
  outcome               TEXT CHECK (outcome IN
                          ('PROGRESS','NEW_INFO','NO_PROGRESS','ENV_BLOCKED','ABANDONED')),
  outcome_note          TEXT,
  started_at            INTEGER NOT NULL,
  ended_at              INTEGER
) STRICT;
CREATE INDEX attempt_by_task ON attempt(task_id, hypothesis_id);
-- At most one open attempt per task.
CREATE UNIQUE INDEX attempt_single_open ON attempt(task_id) WHERE outcome IS NULL;

CREATE TABLE evidence (
  evidence_id         TEXT PRIMARY KEY,
  goal_id             TEXT NOT NULL REFERENCES goal(goal_id),
  task_id             TEXT REFERENCES task(task_id),
  attempt_id          TEXT REFERENCES attempt(attempt_id),
  criterion_id        TEXT NOT NULL,
  acceptance_revision INTEGER NOT NULL,
  verifier            TEXT NOT NULL,         -- canonical JSON of the verifier that was run
  verifier_version    TEXT NOT NULL,
  artifact_hash       TEXT NOT NULL,
  result              TEXT NOT NULL CHECK (result IN ('PASS','FAIL')),
  detail              TEXT NOT NULL DEFAULT '',
  relinked_from       TEXT REFERENCES evidence(evidence_id),
  checked_at          INTEGER NOT NULL,
  seq                 INTEGER NOT NULL       -- global order counter (shared with run.closed_seq)
) STRICT;
CREATE INDEX evidence_by_goal ON evidence(goal_id, acceptance_revision, criterion_id, seq);

-- ---------------------------------------------------------------- approvals / external actions
CREATE TABLE approval (
  approval_id         TEXT PRIMARY KEY,
  goal_id             TEXT NOT NULL REFERENCES goal(goal_id),
  acceptance_revision INTEGER NOT NULL,
  action_digest       TEXT NOT NULL,
  scope               TEXT NOT NULL,
  issued_by           TEXT NOT NULL CHECK (issued_by = 'user'),
  issued_at           INTEGER NOT NULL,
  expires_at          INTEGER NOT NULL,
  revocation_epoch    INTEGER NOT NULL,
  recovery_epoch      INTEGER NOT NULL,
  consumed_at         INTEGER,
  consumed_by_intent  TEXT UNIQUE,           -- an approval is bound to exactly one intent
  revoked_at          INTEGER
) STRICT;

-- Approval consumption and intent creation happen in ONE transaction, before the call leaves
-- the machine.
--   DISPATCHED durable intent; a crash here means the outcome is unknown
--   UNKNOWN    outcome not established; blind retry is forbidden (ACTION-01)
--   CONFIRMED / NOT_DONE are only reachable with a recorded receipt
-- Continuing after NOT_DONE re-dispatches the SAME intent (same idempotency key); it never
-- re-uses the approval for a different intent.
CREATE TABLE action_intent (
  intent_id       TEXT PRIMARY KEY,
  goal_id         TEXT NOT NULL REFERENCES goal(goal_id),
  task_id         TEXT NOT NULL REFERENCES task(task_id),
  run_id          TEXT NOT NULL REFERENCES run(run_id),
  approval_id     TEXT REFERENCES approval(approval_id),
  idempotency_key TEXT NOT NULL UNIQUE,
  action_digest   TEXT NOT NULL,
  status          TEXT NOT NULL CHECK (status IN ('DISPATCHED','UNKNOWN','CONFIRMED','NOT_DONE')),
  receipt         TEXT,
  dispatch_count  INTEGER NOT NULL DEFAULT 1,
  created_at      INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL,
  CHECK (status NOT IN ('CONFIRMED','NOT_DONE') OR receipt IS NOT NULL)
) STRICT;
CREATE INDEX intent_by_goal ON action_intent(goal_id, status);

-- ---------------------------------------------------------------- checkpoints / recovery objects
CREATE TABLE checkpoint (
  checkpoint_id    TEXT PRIMARY KEY,
  project_id       TEXT NOT NULL REFERENCES project(project_id),
  goal_id          TEXT NOT NULL REFERENCES goal(goal_id),
  task_id          TEXT REFERENCES task(task_id),
  run_id           TEXT REFERENCES run(run_id),
  revision         INTEGER NOT NULL,         -- per goal, strictly +1
  state            TEXT NOT NULL CHECK (state IN ('COMMITTED','BLOCKED')),
  blocked_reason   TEXT,
  payload          TEXT NOT NULL,            -- canonical JSON
  payload_hash     TEXT NOT NULL,
  manifest         TEXT NOT NULL,            -- canonical JSON [{path, sha256, size}]
  policy_version   INTEGER NOT NULL,
  revocation_epoch INTEGER NOT NULL,
  recovery_epoch   INTEGER NOT NULL,
  created_at       INTEGER NOT NULL,
  UNIQUE (goal_id, revision),
  CHECK (state <> 'BLOCKED' OR blocked_reason IS NOT NULL)
) STRICT;

-- An unreleased row here IS the pin (RECOVERY-PIN-01).
CREATE TABLE checkpoint_object (
  checkpoint_id TEXT NOT NULL REFERENCES checkpoint(checkpoint_id),
  sha256        TEXT NOT NULL,
  size          INTEGER NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('patch','snapshot','artifact')),
  label         TEXT NOT NULL,
  released_at   INTEGER,                     -- set on goal end / supersede / revocation
  PRIMARY KEY (checkpoint_id, label)
) STRICT;
CREATE INDEX checkpoint_object_by_sha ON checkpoint_object(sha256);

-- Deletion/revocation outranks pins (§6.6). A tombstoned hash can never be re-admitted.
CREATE TABLE object_tombstone (
  sha256     TEXT PRIMARY KEY,
  reason     TEXT NOT NULL,
  created_at INTEGER NOT NULL
) STRICT;

-- ---------------------------------------------------------------- handoff
-- The packet stored here is the only authoritative copy. A presented packet is accepted only
-- if its canonical hash equals packet_hash AND current state still matches (single accept).
CREATE TABLE handoff (
  handoff_id          TEXT PRIMARY KEY,
  goal_id             TEXT NOT NULL REFERENCES goal(goal_id),
  checkpoint_id       TEXT NOT NULL REFERENCES checkpoint(checkpoint_id),
  checkpoint_revision INTEGER NOT NULL,
  source_adapter      TEXT NOT NULL,
  target_adapter      TEXT NOT NULL,
  packet              TEXT NOT NULL,
  packet_hash         TEXT NOT NULL,
  status              TEXT NOT NULL CHECK (status IN ('PREPARED','ACCEPTED','REJECTED')),
  reject_reason       TEXT,
  accepted_by_run     TEXT REFERENCES run(run_id),
  expires_at          INTEGER NOT NULL,
  created_at          INTEGER NOT NULL,
  resolved_at         INTEGER
) STRICT;
-- One open handoff per goal.
CREATE UNIQUE INDEX handoff_single_open ON handoff(goal_id) WHERE status = 'PREPARED';

-- ---------------------------------------------------------------- usage
CREATE TABLE usage_event (
  event_id            TEXT PRIMARY KEY,      -- producer-assigned; replays are no-ops
  run_id              TEXT NOT NULL REFERENCES run(run_id),
  goal_id             TEXT NOT NULL REFERENCES goal(goal_id),
  provider_session_id TEXT NOT NULL,
  mode                TEXT NOT NULL CHECK (mode IN ('delta','cumulative')),
  sequence            INTEGER NOT NULL,
  reported            TEXT NOT NULL,         -- JSON as reported by the source
  applied             TEXT NOT NULL,         -- JSON delta actually charged
  observation         TEXT NOT NULL CHECK (observation IN ('measured','estimated','unknown')),
  source              TEXT NOT NULL,
  anomaly             TEXT,
  created_at          INTEGER NOT NULL
) STRICT;
CREATE INDEX usage_by_goal ON usage_event(goal_id);

-- Per provider session: the highest sequence seen and the provider's running counter as far
-- as it has been charged (cumulative reports set it, delta reports add to it).
CREATE TABLE usage_watermark (
  provider_session_id TEXT PRIMARY KEY,
  last_sequence       INTEGER NOT NULL,
  cumulative_sequence INTEGER NOT NULL,      -- highest sequence covered by a cumulative report
  counter             TEXT NOT NULL
) STRICT;

-- ---------------------------------------------------------------- adapter capabilities (P0.5 output)
-- A capability is a measurement, not a boolean promise: it is bound to a CLI version, an
-- execution mode and the exact probe that produced it.
CREATE TABLE capability (
  adapter     TEXT NOT NULL,
  name        TEXT NOT NULL,
  mode        TEXT NOT NULL,                 -- e.g. default_auth_stripped | isolated_profile
  status      TEXT NOT NULL CHECK (status IN ('verified','unsupported','unverified')),
  cli_version TEXT NOT NULL,
  scope       TEXT NOT NULL,                 -- what exactly was observed, and what was not
  test_id     TEXT NOT NULL,
  measured_at INTEGER NOT NULL,
  PRIMARY KEY (adapter, name, mode)
) STRICT;

-- ---------------------------------------------------------------- audit log
CREATE TABLE event (
  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           INTEGER NOT NULL,
  actor        TEXT NOT NULL,
  type         TEXT NOT NULL,
  aggregate    TEXT NOT NULL,
  aggregate_id TEXT NOT NULL,
  payload      TEXT NOT NULL DEFAULT '{}'    -- references and measurements, never raw content
) STRICT;
CREATE INDEX event_by_aggregate ON event(aggregate, aggregate_id, seq);

CREATE TRIGGER event_append_only_update BEFORE UPDATE ON event
BEGIN SELECT RAISE(ABORT, 'event log is append-only'); END;
CREATE TRIGGER event_append_only_delete BEFORE DELETE ON event
BEGIN SELECT RAISE(ABORT, 'event log is append-only'); END;
