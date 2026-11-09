-- Controller-only candidate provenance. Suppression survives a forgotten node.
CREATE TABLE auto_learning_source (
  project_id TEXT NOT NULL REFERENCES project(project_id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK(kind IN ('correction','file')),
  source_key TEXT NOT NULL,
  node_id TEXT REFERENCES node(node_id) ON DELETE SET NULL,
  snapshot TEXT,
  fingerprint TEXT NOT NULL,
  suppressed INTEGER NOT NULL DEFAULT 0 CHECK(suppressed IN (0,1)),
  provenance TEXT NOT NULL,
  PRIMARY KEY(project_id, kind, source_key)
);
CREATE INDEX auto_learning_node ON auto_learning_source(node_id);
CREATE TABLE auto_file_observation (
  project_id TEXT NOT NULL REFERENCES project(project_id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  root_identity TEXT NOT NULL,
  revocation_epoch INTEGER NOT NULL,
  recovery_epoch INTEGER NOT NULL,
  activity TEXT NOT NULL,
  file_hash TEXT NOT NULL,
  line_hashes TEXT NOT NULL,
  pending INTEGER NOT NULL DEFAULT 0 CHECK(pending IN (0,1)),
  PRIMARY KEY(project_id,path)
);
