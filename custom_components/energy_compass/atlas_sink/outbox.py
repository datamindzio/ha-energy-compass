"""Durable SQLite outbox: ordered queue, dead table, 30-day retention."""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

KINDS = ("attributes", "telemetry", "hourly", "solves")
RETENTION = timedelta(days=30)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    item_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dead (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    item_time TEXT NOT NULL,
    status INTEGER NOT NULL,
    reason TEXT NOT NULL
);
"""

# Delivery order: attributes, then telemetry, then solves; FIFO inside a kind.
_KIND_RANK = "CASE kind WHEN 'attributes' THEN 0 WHEN 'telemetry' THEN 1 WHEN 'hourly' THEN 2 ELSE 3 END"


@dataclass(frozen=True)
class Item:
    id: int
    kind: str
    payload: dict[str, Any]
    item_time: datetime


@dataclass(frozen=True)
class DeadItem:
    id: int
    kind: str
    payload: dict[str, Any]
    item_time: datetime
    status: int
    reason: str


def _is_backfill(it: "Item") -> bool:
    return it.kind == "hourly" or it.payload.get("origin") == "backfill"


def _ts(t: datetime) -> str:
    return t.astimezone(UTC).isoformat()


class Outbox:
    def __init__(self, path: Path):
        self._db = sqlite3.connect(str(path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def add(self, kind: str, payload: dict, item_time: datetime) -> int:
        if kind not in KINDS:
            raise ValueError(f"unknown outbox kind: {kind!r}")
        if item_time.tzinfo is None:
            raise ValueError("item_time must be timezone-aware")
        with self._db:
            cur = self._db.execute(
                "INSERT INTO outbox (kind, payload, item_time) VALUES (?, ?, ?)",
                (kind, json.dumps(payload), _ts(item_time)),
            )
        return int(cur.lastrowid)

    def pending(self, kind: str | None = None, limit: int | None = None) -> list[Item]:
        limit_sql = " LIMIT ?" if limit is not None else ""
        if kind is None:
            sql = f"SELECT id, kind, payload, item_time FROM outbox ORDER BY {_KIND_RANK}, id{limit_sql}"
            params: tuple = () if limit is None else (limit,)
        else:
            sql = f"SELECT id, kind, payload, item_time FROM outbox WHERE kind = ? ORDER BY id{limit_sql}"
            params = (kind,) if limit is None else (kind, limit)
        rows = self._db.execute(sql, params)
        return [self._item(r) for r in rows.fetchall()]

    def pending_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def remove(self, item_id: int) -> None:
        with self._db:
            self._db.execute("DELETE FROM outbox WHERE id = ?", (item_id,))

    def remove_many(self, ids: list[int]) -> None:
        """Delete a whole acked batch in one transaction (one fsync)."""
        if not ids:
            return
        with self._db:
            for i in range(0, len(ids), 500):
                chunk = ids[i : i + 500]
                marks = ",".join("?" * len(chunk))
                self._db.execute(f"DELETE FROM outbox WHERE id IN ({marks})", chunk)

    def mark_dead(self, item_id: int, status: int, reason: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO dead (id, kind, payload, item_time, status, reason) "
                "SELECT id, kind, payload, item_time, ?, ? FROM outbox WHERE id = ?",
                (status, reason, item_id),
            )
            self._db.execute("DELETE FROM outbox WHERE id = ?", (item_id,))

    def dead(self) -> list[DeadItem]:
        rows = self._db.execute(
            "SELECT id, kind, payload, item_time, status, reason FROM dead ORDER BY id"
        ).fetchall()
        return [
            DeadItem(
                r[0], r[1], json.loads(r[2]), datetime.fromisoformat(r[3]), r[4], r[5]
            )
            for r in rows
        ]

    def enforce_retention(self, now: datetime, on_drop: Callable[[Item], None]) -> int:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        cutoff = now - RETENTION
        rows = self._db.execute(
            "SELECT id, kind, payload, item_time FROM outbox WHERE item_time < ? "
            "ORDER BY item_time, id",
            (_ts(cutoff),),
        ).fetchall()
        old = (self._item(r) for r in rows)
        dropped = 0
        for it in old:
            if _is_backfill(it):
                continue  # historic by design (ADR-0009): retention covers live items only
            on_drop(it)
            self.remove(it.id)
            dropped += 1
        return dropped

    def close(self) -> None:
        self._db.close()

    @staticmethod
    def _item(r: tuple) -> Item:
        return Item(r[0], r[1], json.loads(r[2]), datetime.fromisoformat(r[3]))
