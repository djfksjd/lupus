-- Protected files, schema version 3.
-- The files a verifier relies on (tests, runner configuration) are frozen per acceptance
-- revision before any worker of the goal runs. A worker that edits, deletes or adds to them
-- cannot make verification easier: the frozen state is put back before verifying.
CREATE TABLE protected_file (
  goal_id             TEXT NOT NULL REFERENCES goal(goal_id),
  acceptance_revision INTEGER NOT NULL,
  path                TEXT NOT NULL,          -- file path relative to the project root
  sha256              TEXT NOT NULL,
  content             BLOB,                   -- NULL when too large or credential-like: detect only
  frozen_at           INTEGER NOT NULL,
  PRIMARY KEY (goal_id, acceptance_revision, path)
) STRICT;

-- Directories declared as protected: any file appearing in them later was not there at freeze.
CREATE TABLE protected_dir (
  goal_id             TEXT NOT NULL REFERENCES goal(goal_id),
  acceptance_revision INTEGER NOT NULL,
  path                TEXT NOT NULL,
  PRIMARY KEY (goal_id, acceptance_revision, path)
) STRICT;
