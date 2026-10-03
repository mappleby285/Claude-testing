"""SQLite persistence for cards, images, review decisions and processing events.

Each method opens a short-lived connection, so the store is safe to use from the
Streamlit script thread and the watch-folder thread at the same time (WAL mode).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .schema import Contact

# Card statuses
QUEUED = "queued"
PROCESSING = "processing"
NEEDS_REVIEW = "needs_review"
DUPLICATE_REVIEW = "duplicate_review"
APPROVED = "approved"
EXPORTED = "exported"
SKIPPED = "skipped"
FAILED = "failed"

REVIEW_STATUSES = (NEEDS_REVIEW, DUPLICATE_REVIEW)
ALL_STATUSES = (QUEUED, PROCESSING, NEEDS_REVIEW, DUPLICATE_REVIEW, APPROVED, EXPORTED, SKIPPED, FAILED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    hash TEXT PRIMARY KEY,
    stored_path TEXT NOT NULL,
    original_name TEXT NOT NULL,
    width INTEGER,
    height INTEGER,
    first_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    extraction_json TEXT,
    contact_json TEXT,
    issues_json TEXT NOT NULL DEFAULT '[]',
    duplicates_json TEXT NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT '',
    error_category TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL DEFAULT '',
    export_revision INTEGER NOT NULL DEFAULT 0,
    exported_revision INTEGER NOT NULL DEFAULT 0,
    exported_at TEXT NOT NULL DEFAULT '',
    vcf_path TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS card_images (
    card_id TEXT NOT NULL REFERENCES cards(id),
    image_hash TEXT NOT NULL REFERENCES images(hash),
    position INTEGER NOT NULL,
    PRIMARY KEY (card_id, position)
);
CREATE INDEX IF NOT EXISTS idx_card_images_hash ON card_images(image_hash);
CREATE INDEX IF NOT EXISTS idx_cards_status ON cards(status);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    card_id TEXT,
    kind TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class CardRow:
    id: str
    status: str
    created_at: str
    updated_at: str
    source: str
    contact: Contact | None
    extraction: dict[str, Any] | None
    issues: list[dict[str, Any]]
    duplicates: list[dict[str, Any]]
    error: str
    error_category: str
    attempts: int
    model: str
    decision: str
    export_revision: int
    exported_revision: int
    exported_at: str
    vcf_path: str
    image_hashes: list[str] = field(default_factory=list)


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ----------------------------------------------------------------- images
    def get_image(self, image_hash: str) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM images WHERE hash=?", (image_hash,)).fetchone()

    def add_image(self, image_hash: str, stored_path: str, original_name: str, width: int, height: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO images(hash, stored_path, original_name, width, height, first_seen) "
                "VALUES (?,?,?,?,?,?)",
                (image_hash, stored_path, original_name, width, height, now_iso()),
            )

    def cards_for_image(self, image_hash: str) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT card_id FROM card_images WHERE image_hash=?", (image_hash,)).fetchall()
        return [r["card_id"] for r in rows]

    def find_card_with_images(self, hashes: list[str]) -> str | None:
        """Return a card whose image set is exactly `hashes` (order-insensitive)."""
        if not hashes:
            return None
        target = sorted(hashes)
        for card_id in self.cards_for_image(hashes[0]):
            if sorted(self.card_image_hashes(card_id)) == target:
                return card_id
        return None

    def card_image_hashes(self, card_id: str) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT image_hash FROM card_images WHERE card_id=? ORDER BY position", (card_id,)
            ).fetchall()
        return [r["image_hash"] for r in rows]

    def image_paths(self, card_id: str) -> list[tuple[str, str]]:
        """[(stored_path, original_name)] in position order (front first)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT i.stored_path, i.original_name FROM card_images ci JOIN images i ON i.hash=ci.image_hash "
                "WHERE ci.card_id=? ORDER BY ci.position",
                (card_id,),
            ).fetchall()
        return [(r["stored_path"], r["original_name"]) for r in rows]

    # ------------------------------------------------------------------ cards
    def create_card(self, image_hashes: list[str], source: str) -> str:
        card_id = uuid.uuid4().hex
        ts = now_iso()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO cards(id, status, created_at, updated_at, source) VALUES (?,?,?,?,?)",
                (card_id, QUEUED, ts, ts, source),
            )
            for pos, h in enumerate(image_hashes):
                conn.execute(
                    "INSERT INTO card_images(card_id, image_hash, position) VALUES (?,?,?)", (card_id, h, pos)
                )
        return card_id

    def _row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> CardRow:
        contact = Contact.model_validate_json(row["contact_json"]) if row["contact_json"] else None
        hashes = [
            r["image_hash"]
            for r in conn.execute(
                "SELECT image_hash FROM card_images WHERE card_id=? ORDER BY position", (row["id"],)
            ).fetchall()
        ]
        return CardRow(
            id=row["id"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            source=row["source"],
            contact=contact,
            extraction=json.loads(row["extraction_json"]) if row["extraction_json"] else None,
            issues=json.loads(row["issues_json"] or "[]"),
            duplicates=json.loads(row["duplicates_json"] or "[]"),
            error=row["error"],
            error_category=row["error_category"],
            attempts=row["attempts"],
            model=row["model"],
            decision=row["decision"],
            export_revision=row["export_revision"],
            exported_revision=row["exported_revision"],
            exported_at=row["exported_at"],
            vcf_path=row["vcf_path"],
            image_hashes=hashes,
        )

    def get_card(self, card_id: str) -> CardRow | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
            return self._row(conn, row) if row else None

    def list_cards(self, statuses: tuple[str, ...] | list[str] | None = None) -> list[CardRow]:
        with self._conn() as conn:
            if statuses:
                marks = ",".join("?" * len(statuses))
                rows = conn.execute(
                    f"SELECT * FROM cards WHERE status IN ({marks}) ORDER BY created_at, rowid", tuple(statuses)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM cards ORDER BY created_at, rowid").fetchall()
            return [self._row(conn, r) for r in rows]

    def update_card(self, card_id: str, **fields: Any) -> None:
        allowed = {
            "status", "source", "error", "error_category", "attempts", "model", "decision",
            "export_revision", "exported_revision", "exported_at", "vcf_path",
        }
        cols: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "contact":
                cols["contact_json"] = value.model_dump_json() if value is not None else None
            elif key == "extraction":
                cols["extraction_json"] = json.dumps(value, ensure_ascii=False) if value is not None else None
            elif key == "issues":
                cols["issues_json"] = json.dumps(value, ensure_ascii=False)
            elif key == "duplicates":
                cols["duplicates_json"] = json.dumps(value, ensure_ascii=False)
            elif key in allowed:
                cols[key] = value
            else:
                raise KeyError(key)
        cols["updated_at"] = now_iso()
        assignments = ", ".join(f"{k}=?" for k in cols)
        with self._conn() as conn:
            conn.execute(f"UPDATE cards SET {assignments} WHERE id=?", (*cols.values(), card_id))

    def claim_card(self, card_id: str) -> bool:
        """Atomically move a queued card to processing. False if someone else took it."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE cards SET status=?, updated_at=? WHERE id=? AND status=?",
                (PROCESSING, now_iso(), card_id, QUEUED),
            )
            return cur.rowcount == 1

    def recover_interrupted(self) -> int:
        """Cards left in 'processing' by a crash or restart go back to the queue."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE cards SET status=?, updated_at=? WHERE status=?", (QUEUED, now_iso(), PROCESSING)
            )
            return cur.rowcount

    def status_counts(self) -> dict[str, int]:
        counts = {s: 0 for s in ALL_STATUSES}
        with self._conn() as conn:
            for row in conn.execute("SELECT status, COUNT(*) AS n FROM cards GROUP BY status"):
                counts[row["status"]] = row["n"]
        return counts

    # ----------------------------------------------------------------- events
    def log_event(self, kind: str, message: str, card_id: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO events(ts, card_id, kind, message) VALUES (?,?,?,?)", (now_iso(), card_id, kind, message)
            )

    def recent_events(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def count_events(self, kind: str) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM events WHERE kind=?", (kind,)).fetchone()[0]

    # --------------------------------------------------------------- settings
    def get_setting(self, key: str, default: str = "") -> str:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)", (key, value))
