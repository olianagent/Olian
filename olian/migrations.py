"""Versioned schema migrations. v1 is the original schema (idempotent), later versions are append-only."""
from __future__ import annotations

import sqlite3
import time

V2 = """
ALTER TABLE leads ADD COLUMN consent INTEGER NOT NULL DEFAULT 0;

CREATE TABLE tg_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  chat_ref TEXT NOT NULL, kind TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', username TEXT NOT NULL DEFAULT '',
  ingest TEXT NOT NULL DEFAULT 'bot',
  status TEXT NOT NULL DEFAULT 'active', status_reason TEXT NOT NULL DEFAULT '',
  last_ok_at REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '', fail_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL DEFAULT 0, checked_at REAL NOT NULL DEFAULT 0,
  msg_count INTEGER NOT NULL DEFAULT 0, error_count INTEGER NOT NULL DEFAULT 0, dup_count INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, UNIQUE (project_id, chat_ref));

CREATE TABLE tg_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'general',
  phrases TEXT NOT NULL DEFAULT '[]', all_of TEXT NOT NULL DEFAULT '[]', exclude TEXT NOT NULL DEFAULT '[]',
  min_hits INTEGER NOT NULL DEFAULT 1, weight REAL NOT NULL DEFAULT 1.0, enabled INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);

CREATE TABLE tg_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source_id INTEGER NOT NULL REFERENCES tg_sources(id) ON DELETE CASCADE,
  tg_message_id INTEGER NOT NULL, message_date REAL NOT NULL, text TEXT NOT NULL,
  norm_hash TEXT NOT NULL, simhash TEXT NOT NULL, author_ref TEXT,
  matched INTEGER NOT NULL DEFAULT 0, rule_hits TEXT NOT NULL DEFAULT '[]',
  state TEXT NOT NULL DEFAULT 'received', attempts INTEGER NOT NULL DEFAULT 0, verdict TEXT NOT NULL DEFAULT '{}',
  lead_id INTEGER, dup_of INTEGER, created_at REAL NOT NULL,
  UNIQUE (source_id, tg_message_id));
CREATE INDEX idx_tgm_state ON tg_messages(project_id, state);
CREATE INDEX idx_tgm_hash ON tg_messages(project_id, norm_hash);
CREATE INDEX idx_tgm_match ON tg_messages(project_id, matched, created_at);
CREATE INDEX idx_tgm_author ON tg_messages(project_id, author_ref);

CREATE TABLE tg_leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source_id INTEGER REFERENCES tg_sources(id) ON DELETE SET NULL,
  message_row_id INTEGER REFERENCES tg_messages(id) ON DELETE SET NULL,
  tg_message_id INTEGER, message_date REAL NOT NULL DEFAULT 0, message_text TEXT NOT NULL DEFAULT '',
  message_url TEXT NOT NULL DEFAULT '', author_ref TEXT,
  category TEXT NOT NULL DEFAULT '', service TEXT NOT NULL DEFAULT '',
  relevance_score REAL NOT NULL DEFAULT 0, confidence REAL NOT NULL DEFAULT 0,
  qualification_reason TEXT NOT NULL DEFAULT '', qualifier TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'new', hit_count INTEGER NOT NULL DEFAULT 1, last_seen_at REAL NOT NULL DEFAULT 0,
  operator_note TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL DEFAULT '',
  reviewed_by TEXT NOT NULL DEFAULT '', reviewed_at REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE INDEX idx_tgl_p ON tg_leads(project_id, status);
CREATE INDEX idx_tgl_author ON tg_leads(project_id, author_ref, category);

CREATE TABLE tg_lead_hits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id INTEGER NOT NULL REFERENCES tg_leads(id) ON DELETE CASCADE,
  message_row_id INTEGER REFERENCES tg_messages(id) ON DELETE SET NULL,
  kind TEXT NOT NULL, created_at REAL NOT NULL);

CREATE TABLE tg_lead_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id INTEGER NOT NULL REFERENCES tg_leads(id) ON DELETE CASCADE,
  actor TEXT NOT NULL, from_status TEXT NOT NULL DEFAULT '', to_status TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);

CREATE TABLE tg_state (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  key TEXT NOT NULL, value TEXT NOT NULL DEFAULT '', PRIMARY KEY (project_id, key));
"""

V3 = """
ALTER TABLE tg_rules ADD COLUMN require_any TEXT NOT NULL DEFAULT '[]';
ALTER TABLE senders ADD COLUMN status_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE senders ADD COLUMN fail_streak INTEGER NOT NULL DEFAULT 0;
CREATE INDEX idx_leads_addr ON leads(project_id, address);
"""

# (version, sql). v1 is supplied by db.py (the original schema; every statement is IF NOT EXISTS).
MIGRATIONS = [(2, V2), (3, V3)]


def apply(conn: sqlite3.Connection, v1_sql: str) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
    have = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    if 1 not in have:
        conn.executescript(v1_sql)
        conn.execute("INSERT INTO schema_version VALUES (1, ?)", (time.time(),))
    for version, sql in MIGRATIONS:
        if version in have:
            continue
        conn.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
        conn.execute("INSERT INTO schema_version VALUES (?, ?)", (version, time.time()))
    return max(r[0] for r in conn.execute("SELECT version FROM schema_version"))
