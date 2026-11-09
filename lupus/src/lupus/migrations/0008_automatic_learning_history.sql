-- Exact automatic source history permits safe same-source reversions and user suppression
-- of historical versions. No user knowledge is adopted as automatic provenance.
CREATE TABLE auto_learning_entry (
  node_id TEXT PRIMARY KEY REFERENCES node(node_id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES project(project_id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK(kind IN ('correction','file')),
  source_key TEXT NOT NULL,
  snapshot TEXT NOT NULL
);
CREATE INDEX auto_learning_entry_source ON auto_learning_entry(project_id,kind,source_key);
INSERT INTO auto_learning_entry(node_id,project_id,kind,source_key,snapshot)
SELECT node_id,project_id,kind,source_key,snapshot FROM auto_learning_source
WHERE node_id IS NOT NULL AND snapshot IS NOT NULL;
