-- Independent test authors have their own service purpose. Preserve existing rows.
CREATE TABLE service_call_new (
  call_id      TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL REFERENCES project(project_id),
  goal_id      TEXT REFERENCES goal(goal_id),
  budget_id    TEXT NOT NULL REFERENCES budget(budget_id),
  purpose      TEXT NOT NULL CHECK (purpose IN ('judge','rubric','learn','crosscheck')),
  driver       TEXT NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('RUNNING','DONE','FAILED')),
  usage        TEXT,                          -- JSON as reported by the host; NULL = not observed
  error_class  TEXT,
  started_at   INTEGER NOT NULL,
  ended_at     INTEGER
) STRICT;
INSERT INTO service_call_new SELECT * FROM service_call;
DROP TABLE service_call;
ALTER TABLE service_call_new RENAME TO service_call;
CREATE INDEX service_call_by_goal ON service_call(goal_id);
