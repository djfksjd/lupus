-- Lupus memory graph, schema version 2.
-- Knowledge is a graph of small typed nodes with typed links, not a pile of notes:
--   * every node has a scope (one project, or global), an origin and a lifecycle status
--   * every node records where it came from (goal / task / attempt)
--   * every recall is recorded against the attempt that used it, and the attempt's verified
--     outcome feeds back into the node (helped / unhelped)
-- The Markdown vault and the graph viewer are projections of these tables.

CREATE TABLE node (
  node_id           TEXT PRIMARY KEY,
  project_id        TEXT REFERENCES project(project_id),   -- NULL = global knowledge
  kind              TEXT NOT NULL CHECK (kind IN ('lesson','decision','fact','procedure','preference')),
  title             TEXT NOT NULL,
  body              TEXT NOT NULL,
  origin            TEXT NOT NULL CHECK (origin IN ('user','supervisor','worker')),
  -- candidate  recorded, not yet shown to be useful
  -- confirmed  repeatedly recalled in attempts that then passed verification (correlation)
  -- verified   the user vouches for it
  -- retired    no longer recalled (superseded, shown useless, or retired by the user)
  status            TEXT NOT NULL CHECK (status IN ('candidate','confirmed','verified','retired')),
  retired_reason    TEXT,
  source_goal_id    TEXT REFERENCES goal(goal_id),
  source_task_id    TEXT REFERENCES task(task_id),
  source_attempt_id TEXT REFERENCES attempt(attempt_id),
  content_hash      TEXT NOT NULL,
  recalled          INTEGER NOT NULL DEFAULT 0,
  helped            INTEGER NOT NULL DEFAULT 0,
  unhelped          INTEGER NOT NULL DEFAULT 0,
  created_at        INTEGER NOT NULL,
  updated_at        INTEGER NOT NULL,
  CHECK (origin <> 'user' OR status IN ('verified','retired')),
  CHECK (status <> 'retired' OR retired_reason IS NOT NULL)
) STRICT;
-- The same statement is stored once per scope.
CREATE UNIQUE INDEX node_unique_content ON node(coalesce(project_id, ''), content_hash);
CREATE INDEX node_by_scope ON node(project_id, status);

-- src --type--> dst
--   supersedes    src replaces dst (dst is retired)
--   derived_from  src was learned from / copied from dst
--   contradicts   both are shown together so the conflict is visible
--   part_of       src is a detail of dst
--   relates       loose association
CREATE TABLE edge (
  src        TEXT NOT NULL REFERENCES node(node_id) ON DELETE CASCADE,
  dst        TEXT NOT NULL REFERENCES node(node_id) ON DELETE CASCADE,
  type       TEXT NOT NULL CHECK (type IN ('supersedes','derived_from','contradicts','part_of','relates')),
  created_by TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (src, dst, type),
  CHECK (src <> dst)
) STRICT;
CREATE INDEX edge_by_dst ON edge(dst);

-- Search index. Text is pre-tokenised by lupus.memory.tokenize (words for Latin text, character
-- bigrams for Korean/CJK) so Korean particles do not hide a match. rowid = node.rowid.
CREATE VIRTUAL TABLE node_fts USING fts5(title, body, tokenize = 'unicode61');

-- One row per node handed to a worker for one attempt.
CREATE TABLE recall (
  recall_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id    TEXT NOT NULL REFERENCES node(node_id) ON DELETE CASCADE,
  attempt_id TEXT NOT NULL REFERENCES attempt(attempt_id),
  goal_id    TEXT NOT NULL REFERENCES goal(goal_id),
  via        TEXT NOT NULL CHECK (via IN ('match','link')),
  tokens     INTEGER NOT NULL,               -- conservative estimate of prompt tokens added
  outcome    TEXT,                           -- the attempt's outcome, once known
  created_at INTEGER NOT NULL,
  UNIQUE (node_id, attempt_id)
) STRICT;
CREATE INDEX recall_by_attempt ON recall(attempt_id);

-- Knowledge the user deleted is not re-admitted, whoever proposes it again.
CREATE TABLE node_tombstone (
  scope        TEXT NOT NULL,                -- project_id or '' for global
  content_hash TEXT NOT NULL,
  reason       TEXT NOT NULL,
  created_at   INTEGER NOT NULL,
  PRIMARY KEY (scope, content_hash)
) STRICT;
