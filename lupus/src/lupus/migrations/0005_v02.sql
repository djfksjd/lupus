-- Schema version 5: everything added for v0.2.

-- A frozen file keeps its permission bits (a test script must stay executable when put back).
ALTER TABLE protected_file ADD COLUMN mode INTEGER NOT NULL DEFAULT 420;

-- Model calls that are not project writers: a judge reading a document, a learning pass over
-- recorded failures. They run in an empty directory, hold a budget reservation like any other
-- call, and what they report is an opinion or a proposal, never completion evidence by itself.
CREATE TABLE service_call (
  call_id      TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL REFERENCES project(project_id),
  goal_id      TEXT REFERENCES goal(goal_id),
  budget_id    TEXT NOT NULL REFERENCES budget(budget_id),
  purpose      TEXT NOT NULL CHECK (purpose IN ('judge','rubric','learn')),
  driver       TEXT NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('RUNNING','DONE','FAILED')),
  usage        TEXT,                          -- JSON as reported by the host; NULL = not observed
  error_class  TEXT,
  started_at   INTEGER NOT NULL,
  ended_at     INTEGER
) STRICT;
CREATE INDEX service_call_by_goal ON service_call(goal_id);

-- Alpha: one persistent identity per project plus one for the whole runtime. It is bookkeeping
-- for the supervisor (shared budget, ordering), not a running model.
CREATE TABLE alpha (
  alpha_id    TEXT PRIMARY KEY,
  scope       TEXT NOT NULL CHECK (scope IN ('global','project')),
  project_id  TEXT UNIQUE REFERENCES project(project_id),
  budget_id   TEXT REFERENCES budget(budget_id),
  created_at  INTEGER NOT NULL,
  CHECK ((scope = 'global') = (project_id IS NULL))
) STRICT;
CREATE UNIQUE INDEX alpha_single_global ON alpha(scope) WHERE scope = 'global';

-- Larger number = earlier. Only the user sets it.
ALTER TABLE goal ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;

-- Supervisors started in the background. The row is written before the process may do anything.
CREATE TABLE job (
  job_id      TEXT PRIMARY KEY,
  command     TEXT NOT NULL,                  -- JSON argv after `lupus`
  pid         INTEGER,
  proc_start  TEXT,
  log_path    TEXT NOT NULL,
  status      TEXT NOT NULL CHECK (status IN ('STARTING','RUNNING','EXITED')),
  exit_code   INTEGER,
  started_at  INTEGER NOT NULL,
  ended_at    INTEGER
) STRICT;

-- An interactive session: the user works in the CLI's own screen while Lupus holds the writer
-- slot, keeps the frozen files frozen and verifies at the end.
CREATE TABLE session (
  goal_id     TEXT PRIMARY KEY REFERENCES goal(goal_id),
  cli         TEXT NOT NULL,
  stop_blocks INTEGER NOT NULL DEFAULT 0,     -- times the in-session check sent the model back to work
  created_at  INTEGER NOT NULL
) STRICT;
