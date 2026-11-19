"""Persistência da biblioteca em SQLite."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from .config import DB_PATH

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row

_conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS documents (
        id            TEXT PRIMARY KEY,
        title         TEXT NOT NULL,
        filename      TEXT NOT NULL,
        voice         TEXT NOT NULL,
        status        TEXT NOT NULL,   -- queued | extracting | synthesizing | finalizing | ready | error
        pages         INTEGER DEFAULT 0,
        pages_done    INTEGER DEFAULT 0,
        chunks_total  INTEGER DEFAULT 0,
        chunks_done   INTEGER DEFAULT 0,
        characters    INTEGER DEFAULT 0,
        duration      REAL DEFAULT 0,
        file_size     INTEGER DEFAULT 0,
        error         TEXT,
        page_marks    TEXT DEFAULT '[]',
        created_at    REAL NOT NULL,
        updated_at    REAL NOT NULL
    );
    """
)
_conn.commit()

ACTIVE_STATUSES = ("queued", "extracting", "synthesizing", "finalizing")


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    d["page_marks"] = json.loads(d.get("page_marks") or "[]")
    return d


def create(doc_id: str, title: str, filename: str, voice: str) -> dict:
    now = time.time()
    with _lock:
        _conn.execute(
            "INSERT INTO documents (id, title, filename, voice, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'queued', ?, ?)",
            (doc_id, title, filename, voice, now, now),
        )
        _conn.commit()
    return get(doc_id)


def update(doc_id: str, **fields: Any) -> None:
    if not fields:
        return
    if "page_marks" in fields:
        fields["page_marks"] = json.dumps(fields["page_marks"])
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _lock:
        _conn.execute(f"UPDATE documents SET {cols} WHERE id = ?", (*fields.values(), doc_id))
        _conn.commit()


def get(doc_id: str) -> dict | None:
    with _lock:
        r = _conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return _row(r)


def list_all() -> list[dict]:
    with _lock:
        rows = _conn.execute("SELECT * FROM documents ORDER BY created_at DESC").fetchall()
    return [_row(r) for r in rows]


def list_active() -> list[dict]:
    q = f"SELECT * FROM documents WHERE status IN ({','.join('?' * len(ACTIVE_STATUSES))}) ORDER BY created_at"
    with _lock:
        rows = _conn.execute(q, ACTIVE_STATUSES).fetchall()
    return [_row(r) for r in rows]


def delete(doc_id: str) -> None:
    with _lock:
        _conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        _conn.commit()
