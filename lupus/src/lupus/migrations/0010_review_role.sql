-- Separately selected reviewers have their own charged service purpose; preserve row identities.
CREATE TABLE service_call_new (
  call_id      TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL REFERENCES project(project_id),
  goal_id      TEXT REFERENCES goal(goal_id),
  budget_id    TEXT NOT NULL REFERENCES budget(budget_id),
  purpose      TEXT NOT NULL CHECK (purpose IN ('judge','rubric','learn','crosscheck','supervisor','review')),
  driver       TEXT NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('RUNNING','DONE','FAILED')),
  usage        TEXT,                          -- JSON as reported by the host; NULL = not observed
  error_class  TEXT,
  started_at   INTEGER NOT NULL,
  ended_at     INTEGER
) STRICT;
INSERT INTO service_call_new(rowid,call_id,project_id,goal_id,budget_id,purpose,driver,status,usage,error_class,started_at,ended_at)
SELECT rowid,call_id,project_id,goal_id,budget_id,purpose,driver,status,usage,error_class,started_at,ended_at FROM service_call;
DROP TABLE service_call;
ALTER TABLE service_call_new RENAME TO service_call;
CREATE INDEX service_call_by_goal ON service_call(goal_id);
