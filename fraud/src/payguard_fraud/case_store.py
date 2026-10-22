"""Analyst case store over captured authorization events.

Two backends share one schema and one set of SQL statements:

* SQLite (default, ``PAYGUARD_CASE_DB``): a single-machine development store.
* PostgreSQL (``PAYGUARD_CASE_DSN``): a durable store several API and worker
  processes can share. A review is one conditional ``UPDATE`` plus history and
  audit inserts in one transaction, so two analysts cannot both review a case.

Every case read, draft and review is attributed to an ``Actor`` and written to
an append-only audit table. Reviews are also kept in an append-only history.
A recorded decision never executes a card action.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROLES = {"analyst": frozenset({"view", "draft"}),
         "lead": frozenset({"view", "draft", "review", "audit"})}
DECISIONS = {"APPROVED", "REJECTED"}
# Placeholder policy for this demo, not a regulatory determination: audit rows
# younger than this are never purged.
MIN_AUDIT_RETENTION_DAYS = 365 * 7
SYSTEM = None  # Marker for ingestion by the stream worker.


class Forbidden(PermissionError):
    """The actor's role does not permit this action."""


@dataclass(frozen=True)
class Actor:
    name: str
    role: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("actor name is required")
        if self.role not in ROLES:
            raise ValueError(f"unknown role: {self.role}")

    def require(self, permission: str) -> None:
        if permission not in ROLES[self.role]:
            raise Forbidden(f"role {self.role} may not {permission}")


def _schema(dialect: str) -> list[str]:
    identity = ("INTEGER PRIMARY KEY AUTOINCREMENT" if dialect == "sqlite"
                else "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY")
    return [
        """CREATE TABLE IF NOT EXISTS events (
            auth_id TEXT PRIMARY KEY, card_id TEXT NOT NULL, merchant_id TEXT NOT NULL,
            device_id TEXT NOT NULL, event_at TEXT NOT NULL, payload TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_events_card ON events(card_id,event_at)",
        "CREATE INDEX IF NOT EXISTS idx_events_merchant ON events(merchant_id,event_at)",
        """CREATE TABLE IF NOT EXISTS cases (
            case_id TEXT PRIMARY KEY, auth_id TEXT UNIQUE NOT NULL,
            card_id TEXT NOT NULL, merchant_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN',
            draft TEXT, analyst TEXT, analyst_note TEXT,
            created_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_cases_merchant ON cases(merchant_id)",
        f"""CREATE TABLE IF NOT EXISTS case_reviews (
            review_id {identity}, case_id TEXT NOT NULL REFERENCES cases(case_id),
            decision TEXT NOT NULL, analyst TEXT NOT NULL, role TEXT NOT NULL,
            note TEXT NOT NULL, reviewed_at TEXT NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS case_audit (
            event_id {identity}, at TEXT NOT NULL, actor TEXT NOT NULL, role TEXT NOT NULL,
            action TEXT NOT NULL, case_id TEXT, detail TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_case_audit_at ON case_audit(at)",
    ]


# Columns added after the first release; existing SQLite files gain them on open.
ADDED_CASE_COLUMNS = ("drafted_by", "drafted_at", "reviewed_at")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class CaseStore:
    def __init__(self, path: Path | None = None, dsn: str | None = None):
        if (path is None) == (dsn is None):
            raise ValueError("pass exactly one of a SQLite path or a PostgreSQL DSN")
        self.path, self.dsn = (Path(path) if path else None), dsn
        self.dialect = "postgres" if dsn else "sqlite"
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._tx() as db:
            for statement in _schema(self.dialect):
                db.execute(statement)
            if self.dialect == "sqlite":
                have = {row["name"] for row in db.execute("PRAGMA table_info(cases)")}
                for column in ADDED_CASE_COLUMNS:
                    if column not in have:
                        db.execute(f"ALTER TABLE cases ADD COLUMN {column} TEXT")
            else:
                for column in ADDED_CASE_COLUMNS:
                    db.execute(f"ALTER TABLE cases ADD COLUMN IF NOT EXISTS {column} TEXT")

    @classmethod
    def from_env(cls) -> CaseStore:
        dsn = os.getenv("PAYGUARD_CASE_DSN")
        if dsn:
            return cls(dsn=dsn)
        return cls(Path(os.getenv("PAYGUARD_CASE_DB", "data/cases.sqlite")))

    @contextmanager
    def _tx(self) -> Iterator[_Session]:
        """One transaction: commit on success, roll back on any exception."""
        if self.dialect == "sqlite":
            connection = sqlite3.connect(self.path, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
        else:
            import psycopg
            from psycopg.rows import dict_row

            connection = psycopg.connect(self.dsn, row_factory=dict_row)
        try:
            yield _Session(connection, self.dialect)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _audit(self, db: _Session, actor: Actor | None, action: str,
               case_id: str | None, detail: dict | None = None) -> None:
        db.execute("""INSERT INTO case_audit (at,actor,role,action,case_id,detail)
            VALUES (?,?,?,?,?,?)""", (
            _now(), actor.name if actor else "system:case-stream",
            actor.role if actor else "system", action, case_id,
            json.dumps(detail or {}, separators=(",", ":"), sort_keys=True)))

    def observe(self, payload: dict[str, Any], threshold: float = 0.1) -> str | None:
        request, result = payload["request"], payload["result"]
        auth_id = request["authId"]
        case_id = f"case_{auth_id}"
        score = result.get("modelScore")
        opens = isinstance(score, (float, int)) and score >= threshold
        with self._tx() as db:
            db.execute("""INSERT INTO events VALUES (?,?,?,?,?,?)
                ON CONFLICT (auth_id) DO NOTHING""", (
                auth_id, request["cardId"], request["merchantId"],
                request.get("deviceId", "device_demo"), payload["timestamp"],
                json.dumps(payload, separators=(",", ":")),
            ))
            if opens:
                created = db.execute("""INSERT INTO cases
                    (case_id,auth_id,card_id,merchant_id,created_at) VALUES (?,?,?,?,?)
                    ON CONFLICT (case_id) DO NOTHING""", (
                    case_id, auth_id, request["cardId"], request["merchantId"],
                    payload["timestamp"],
                )).rowcount
                if created == 1:
                    self._audit(db, SYSTEM, "case_opened", case_id, {"modelScore": score})
        return case_id if opens else None

    def list_cases(self, actor: Actor | None = None) -> list[dict[str, Any]]:
        if actor:
            actor.require("view")
        with self._tx() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM cases ORDER BY created_at DESC LIMIT 100")]
            if actor:
                self._audit(db, actor, "list_cases", None, {"returned": len(rows)})
        return rows

    def case(self, case_id: str, actor: Actor | None = None) -> dict[str, Any]:
        if actor:
            actor.require("view")
        with self._tx() as db:
            row = db.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
            if row is None:
                raise KeyError(case_id)
            if actor:
                self._audit(db, actor, "view_case", case_id)
        return dict(row)

    def reviews(self, case_id: str) -> list[dict[str, Any]]:
        with self._tx() as db:
            return [dict(row) for row in db.execute(
                """SELECT decision, analyst, role, note, reviewed_at FROM case_reviews
                WHERE case_id=? ORDER BY review_id""", (case_id,))]

    def _event(self, auth_id: str) -> dict[str, Any]:
        with self._tx() as db:
            row = db.execute("SELECT payload FROM events WHERE auth_id=?", (auth_id,)).fetchone()
            if row is None:
                raise KeyError(auth_id)
            return json.loads(row["payload"])

    def get_card_history(self, case_id: str) -> list[dict[str, Any]]:
        case = self.case(case_id)
        with self._tx() as db:
            rows = db.execute(
                """SELECT payload FROM events WHERE card_id=? AND event_at<=?
                ORDER BY event_at DESC LIMIT 20""",
                (case["card_id"], case["created_at"]),
            ).fetchall()
        return [{
            "transactionId": event["request"]["authId"],
            "cardId": event["request"]["cardId"],
            "merchantId": event["request"]["merchantId"],
            "amountMinor": event["request"]["amountMinor"],
            "eventAt": event["timestamp"],
            "deviceId": event["request"].get("deviceId", "device_demo"),
            "geo": "provided_location",
            "status": event["result"]["status"],
        } for row in reversed(rows) for event in [json.loads(row["payload"])]]

    def get_merchant_profile(self, case_id: str) -> dict[str, Any]:
        case = self.case(case_id)
        event = self._event(case["auth_id"])
        with self._tx() as db:
            count = db.execute("SELECT count(*) AS n FROM events WHERE merchant_id=?",
                               (case["merchant_id"],)).fetchone()["n"]
        return {"merchantId": case["merchant_id"],
                "risk": event["request"].get("merchantRisk", 0.1), "observedAuths": count}

    def get_linked_cards(self, case_id: str) -> list[str]:
        case = self.case(case_id)
        event = self._event(case["auth_id"])
        device = event["request"].get("deviceId", "device_demo")
        with self._tx() as db:
            rows = db.execute("""SELECT DISTINCT card_id FROM events
                WHERE (device_id=? OR merchant_id=?) AND card_id<>? ORDER BY card_id LIMIT 20""",
                (device, case["merchant_id"], case["card_id"])).fetchall()
        return [row["card_id"] for row in rows]

    def get_reason_codes(self, case_id: str) -> list[str]:
        case = self.case(case_id)
        return self._event(case["auth_id"])["result"].get("modelReasonCodes", [])

    def get_similar_cases(self, case_id: str) -> list[str]:
        case = self.case(case_id)
        with self._tx() as db:
            rows = db.execute("""SELECT case_id FROM cases
                WHERE merchant_id=? AND case_id<>? ORDER BY case_id LIMIT 10""",
                (case["merchant_id"], case_id)).fetchall()
        return [row["case_id"] for row in rows]

    def save_draft(self, case_id: str, draft: dict[str, Any], actor: Actor) -> None:
        actor.require("draft")
        with self._tx() as db:
            changed = db.execute("""UPDATE cases SET draft=?, drafted_by=?, drafted_at=?
                WHERE case_id=? AND status='OPEN'""",
                (json.dumps(draft), actor.name, _now(), case_id)).rowcount
            if changed != 1:
                raise ValueError("case missing or already reviewed")
            self._audit(db, actor, "draft_saved", case_id,
                        {"recommendedAction": draft.get("recommendedAction"),
                         "draftSource": draft.get("draftSource")})

    def review(self, case_id: str, decision: str, actor: Actor, note: str) -> None:
        """Record a lead's decision once; it never executes a card action."""
        actor.require("review")
        if decision not in DECISIONS:
            raise ValueError("decision must be APPROVED or REJECTED")
        at = _now()
        with self._tx() as db:
            changed = db.execute("""UPDATE cases SET status=?, analyst=?, analyst_note=?,
                reviewed_at=? WHERE case_id=? AND status='OPEN' AND draft IS NOT NULL""",
                (decision, actor.name, note, at, case_id)).rowcount
            if changed != 1:
                raise ValueError("case missing, already reviewed, or draft absent")
            db.execute("""INSERT INTO case_reviews
                (case_id,decision,analyst,role,note,reviewed_at) VALUES (?,?,?,?,?,?)""",
                (case_id, decision, actor.name, actor.role, note, at))
            self._audit(db, actor, "review_recorded", case_id, {"decision": decision})

    def audit_log(self, actor: Actor, case_id: str | None = None,
                  limit: int = 200) -> list[dict[str, Any]]:
        actor.require("audit")
        limit = max(1, min(int(limit), 1000))
        with self._tx() as db:
            if case_id:
                rows = db.execute("""SELECT * FROM case_audit WHERE case_id=?
                    ORDER BY event_id DESC LIMIT ?""", (case_id, limit)).fetchall()
            else:
                rows = db.execute("SELECT * FROM case_audit ORDER BY event_id DESC LIMIT ?",
                                  (limit,)).fetchall()
            self._audit(db, actor, "read_audit", case_id, {"returned": len(rows)})
        return [dict(row) for row in rows]

    def purge_audit(self, actor: Actor, retention_days: int,
                    now: datetime | None = None) -> int:
        """Delete audit rows older than the retention window, never younger ones."""
        actor.require("audit")
        if retention_days < MIN_AUDIT_RETENTION_DAYS:
            raise ValueError(f"retention must be at least {MIN_AUDIT_RETENTION_DAYS} days")
        cutoff = ((now or datetime.now(UTC)) - timedelta(days=retention_days)).isoformat(
            timespec="microseconds")
        with self._tx() as db:
            removed = db.execute("DELETE FROM case_audit WHERE at < ?", (cutoff,)).rowcount
            self._audit(db, actor, "purge_audit", None,
                        {"retentionDays": retention_days, "removed": removed})
        return removed


class _Session:
    """Run the shared ``?``-placeholder SQL on either backend."""

    def __init__(self, connection, dialect: str):
        self.connection, self.dialect = connection, dialect

    def execute(self, sql: str, params: tuple = ()):
        if self.dialect == "postgres":
            sql = sql.replace("?", "%s")
            return self.connection.execute(sql, params)
        return self.connection.execute(sql, params)
