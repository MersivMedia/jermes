"""Decision cache and decision log (one SQLite file).

The cache is what makes the harness deterministic over a near-deterministic
model: a decision keyed by (model, canonical state, canonical questions) is
answered once and replayed forever after, so run-to-run sampling noise can
never flip the action taken for identical inputs.

The log records every decision point evaluation with enough detail to replay
it against a new policy: full answers, confidence, policy version, the action
Jermes would take, whether it was applied, latency and token usage.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

# Keys whose values change every call without changing meaning. Dropped from
# state before hashing so equivalent requests share a cache entry.
VOLATILE_KEYS = frozenset(
    {"timestamp", "ts", "request_id", "tool_call_id", "api_request_id", "turn_id", "started_at"}
)


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def data_dir() -> Path:
    path = Path(os.environ.get("JERMES_HOME") or hermes_home() / "jermes")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _strip_volatile(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _strip_volatile(v) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, (list, tuple)):
        return [_strip_volatile(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(_strip_volatile(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def cache_key(model: str, state: Any, wire_questions: Mapping[str, Any]) -> str:
    h = hashlib.sha256()
    h.update(model.encode())
    h.update(b"\x00")
    h.update(canonical_json(state).encode())
    h.update(b"\x00")
    h.update(canonical_json(wire_questions).encode())
    return h.hexdigest()


def state_hash(state: Any) -> str:
    return hashlib.sha256(canonical_json(state).encode()).hexdigest()[:16]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session_id TEXT,
    point TEXT NOT NULL,
    mode TEXT NOT NULL,
    spec_version TEXT,
    policy_version TEXT,
    model TEXT,
    state_hash TEXT,
    answers_json TEXT,
    action TEXT,
    applied INTEGER NOT NULL DEFAULT 0,
    cached INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL,
    input_tokens INTEGER,
    error TEXT,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_point_ts ON decisions(point, ts);
"""


class Store:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else data_dir() / "decisions.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- cache ------------------------------------------------------------

    def cache_get(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT response_json FROM cache WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def cache_put(self, key: str, model: str, response: Mapping[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache(key, model, response_json, created_at) VALUES (?,?,?,?)",
                (key, model, json.dumps(response, sort_keys=True), time.time()),
            )
            self._conn.commit()

    # -- log --------------------------------------------------------------

    def log(self, **row: Any) -> int:
        cols = [
            "session_id", "point", "mode", "spec_version", "policy_version", "model",
            "state_hash", "answers_json", "action", "applied", "cached", "latency_ms",
            "input_tokens", "error", "detail_json",
        ]
        values = [row.get(c) for c in cols]
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO decisions(ts, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
                [time.time(), *values],
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def mark_applied(self, row_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE decisions SET applied=1 WHERE id=?", (row_id,))
            self._conn.commit()

    def recent(self, limit: int = 20, point: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM decisions"
        args: List[Any] = []
        if point:
            sql += " WHERE point=?"
            args.append(point)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            cur = self._conn.execute(sql, args)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]

    def stats(self) -> List[Dict[str, Any]]:
        sql = """
            SELECT point, mode, COUNT(*) AS n,
                   SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors,
                   SUM(cached) AS cache_hits,
                   SUM(applied) AS applied,
                   ROUND(AVG(CASE WHEN cached=0 AND error IS NULL THEN latency_ms END), 1) AS avg_latency_ms,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens
            FROM decisions GROUP BY point, mode ORDER BY point, mode
        """
        with self._lock:
            cur = self._conn.execute(sql)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]

    def action_counts(self, point: str) -> Dict[str, int]:
        with self._lock:
            rows: Iterable = self._conn.execute(
                "SELECT action, COUNT(*) FROM decisions WHERE point=? GROUP BY action", (point,)
            ).fetchall()
        return {str(a): int(n) for a, n in rows}
