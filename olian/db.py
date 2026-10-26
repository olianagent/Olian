"""SQLite storage. Every row that belongs to a project carries project_id: this is the isolation boundary."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, goal TEXT NOT NULL DEFAULT '',
  api_port INTEGER NOT NULL UNIQUE, api_key TEXT NOT NULL, config TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'stopped', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS members (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  platform TEXT NOT NULL, user_id TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL, scopes TEXT NOT NULL DEFAULT '', PRIMARY KEY (project_id, platform, user_id));
CREATE TABLE IF NOT EXISTS bindings (
  platform TEXT NOT NULL, chat_id TEXT NOT NULL,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  PRIMARY KEY (platform, chat_id));
CREATE TABLE IF NOT EXISTS tokens (
  hash TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  label TEXT NOT NULL, role TEXT NOT NULL, scopes TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS goals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title TEXT NOT NULL, metric TEXT NOT NULL DEFAULT '', deadline REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  goal_id INTEGER, parent_id INTEGER, title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
  executor TEXT NOT NULL DEFAULT 'agent', assignee TEXT NOT NULL DEFAULT '',
  steps TEXT NOT NULL DEFAULT '[]', due REAL NOT NULL DEFAULT 0, blocked_reason TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'todo', result TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
  handled INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, args TEXT NOT NULL DEFAULT '{}', ok INTEGER NOT NULL, result TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS scrapers (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL, kind TEXT NOT NULL, spec TEXT NOT NULL DEFAULT '{}',
  interval_minutes INTEGER NOT NULL DEFAULT 0, last_run REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (project_id, name));
CREATE TABLE IF NOT EXISTS senders (
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  label TEXT NOT NULL, channel TEXT NOT NULL, config TEXT NOT NULL DEFAULT '{}',
  daily_cap INTEGER NOT NULL DEFAULT 20, sent_day TEXT NOT NULL DEFAULT '', sent_today INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active', PRIMARY KEY (project_id, label));
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  channel TEXT NOT NULL, address TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
  data TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'new',
  warmth INTEGER NOT NULL DEFAULT 0, last_contact REAL NOT NULL DEFAULT 0, next_touch REAL NOT NULL DEFAULT 0,
  detected_at REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL, UNIQUE (project_id, channel, address));
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE, direction TEXT NOT NULL,
  body TEXT NOT NULL, status TEXT NOT NULL, sender TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL, sent_at REAL NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_tasks_p ON tasks(project_id, status);
CREATE INDEX IF NOT EXISTS idx_msgs_p ON messages(project_id, status);
CREATE INDEX IF NOT EXISTS idx_events_p ON events(project_id, handled);
CREATE INDEX IF NOT EXISTS idx_audit_p ON audit(project_id, id);
"""


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        from .migrations import apply
        apply(self._conn, SCHEMA)

    def q(self, sql: str, args: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(args)).fetchall()]

    def one(self, sql: str, args: Iterable[Any] = ()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, tuple(args))
            return cur.lastrowid or 0

    # -- helpers shared by modules --
    def add_event(self, project_id: str, source: str, kind: str, payload: dict) -> int:
        return self.x(
            "INSERT INTO events(project_id,source,kind,payload,created_at) VALUES(?,?,?,?,?)",
            (project_id, source, kind, json.dumps(payload, ensure_ascii=False), time.time()),
        )
