"""SQLite storage for spans and evidence. One file, no server."""

from __future__ import annotations

import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS spans (
  trace_id TEXT NOT NULL,
  span_id TEXT NOT NULL,
  parent_span_id TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL,
  start_ns INTEGER NOT NULL,
  end_ns INTEGER NOT NULL,
  status_code INTEGER NOT NULL DEFAULT 0,
  status_message TEXT NOT NULL DEFAULT '',
  attributes TEXT NOT NULL DEFAULT '{}',
  events TEXT NOT NULL DEFAULT '[]',
  resource TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (trace_id, span_id)
);
CREATE INDEX IF NOT EXISTS spans_start ON spans(start_ns);
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  trace_id TEXT NOT NULL,
  adapter TEXT NOT NULL,
  status TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  details TEXT NOT NULL DEFAULT '{}',
  checked_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS evidence_trace ON evidence(trace_id, id);
"""


class Store:
    def __init__(self, path: str = "runactuals.db"):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def add_spans(self, spans: list[dict]) -> int:
        rows = [(
            s["trace_id"], s["span_id"], s.get("parent_span_id", ""), s.get("name", ""),
            int(s.get("start_ns", 0)), int(s.get("end_ns", 0)), int(s.get("status_code", 0)),
            s.get("status_message", ""), json.dumps(s.get("attributes", {})),
            json.dumps(s.get("events", [])), json.dumps(s.get("resource", {})),
        ) for s in spans]
        with self._lock, self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO spans VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        return len(rows)

    def spans(self, trace_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM spans"
        args: tuple = ()
        if trace_id:
            q += " WHERE trace_id = ?"
            args = (trace_id,)
        q += " ORDER BY start_ns, span_id"
        with self._lock:
            rows = self._db.execute(q, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("attributes", "events", "resource"):
                d[k] = json.loads(d[k])
            out.append(d)
        return out

    def add_evidence(self, trace_id: str, adapter: str, status: str, summary: str = "",
                     url: str = "", details: dict | None = None) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO evidence (trace_id, adapter, status, summary, url, details, checked_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (trace_id, adapter, status, summary, url, json.dumps(details or {}), time.time()))

    def latest_evidence(self) -> dict[str, dict]:
        """Latest evidence row per (trace_id, adapter), grouped by trace_id."""
        with self._lock:
            rows = self._db.execute(
                "SELECT e.* FROM evidence e JOIN (SELECT trace_id, adapter, MAX(id) AS mid "
                "FROM evidence GROUP BY trace_id, adapter) m ON e.id = m.mid").fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            d = dict(r)
            d["details"] = json.loads(d["details"])
            out.setdefault(d["trace_id"], {})[d["adapter"]] = d
        return out
