"""Local, read-only investigation tools over captured authorization events."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class CaseStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    auth_id TEXT PRIMARY KEY, card_id TEXT NOT NULL, merchant_id TEXT NOT NULL,
                    device_id TEXT NOT NULL, event_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_card ON events(card_id,event_at);
                CREATE INDEX IF NOT EXISTS idx_events_merchant ON events(merchant_id,event_at);
                CREATE TABLE IF NOT EXISTS cases (
                    case_id TEXT PRIMARY KEY, auth_id TEXT UNIQUE NOT NULL,
                    card_id TEXT NOT NULL, merchant_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    draft TEXT, analyst TEXT, analyst_note TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cases_merchant ON cases(merchant_id);
            """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def observe(self, payload: dict[str, Any], threshold: float = 0.1) -> str | None:
        request, result = payload["request"], payload["result"]
        auth_id = request["authId"]
        case_id = f"case_{auth_id}"
        score = result.get("modelScore")
        opens = isinstance(score, (float, int)) and score >= threshold
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)", (
                auth_id, request["cardId"], request["merchantId"],
                request.get("deviceId", "device_demo"), payload["timestamp"],
                json.dumps(payload, separators=(",", ":")),
            ))
            if opens:
                db.execute("""INSERT OR IGNORE INTO cases
                    (case_id,auth_id,card_id,merchant_id,created_at) VALUES (?,?,?,?,?)""", (
                    case_id, auth_id, request["cardId"], request["merchantId"],
                    payload["timestamp"],
                ))
        return case_id if opens else None

    def list_cases(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM cases ORDER BY created_at DESC LIMIT 100"
            )]

    def case(self, case_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
            if row is None:
                raise KeyError(case_id)
            return dict(row)

    def _event(self, auth_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT payload FROM events WHERE auth_id=?", (auth_id,)).fetchone()
            if row is None:
                raise KeyError(auth_id)
            return json.loads(row["payload"])

    def get_card_history(self, case_id: str) -> list[dict[str, Any]]:
        case = self.case(case_id)
        with self._connect() as db:
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
        with self._connect() as db:
            count = db.execute("SELECT count(*) FROM events WHERE merchant_id=?",
                               (case["merchant_id"],)).fetchone()[0]
        return {"merchantId": case["merchant_id"],
                "risk": event["request"].get("merchantRisk", 0.1), "observedAuths": count}

    def get_linked_cards(self, case_id: str) -> list[str]:
        case = self.case(case_id)
        event = self._event(case["auth_id"])
        device = event["request"].get("deviceId", "device_demo")
        with self._connect() as db:
            rows = db.execute("""SELECT DISTINCT card_id FROM events
                WHERE (device_id=? OR merchant_id=?) AND card_id<>? LIMIT 20""",
                (device, case["merchant_id"], case["card_id"])).fetchall()
        return [row[0] for row in rows]

    def get_reason_codes(self, case_id: str) -> list[str]:
        case = self.case(case_id)
        return self._event(case["auth_id"])["result"].get("modelReasonCodes", [])

    def get_similar_cases(self, case_id: str) -> list[str]:
        case = self.case(case_id)
        with self._connect() as db:
            rows = db.execute("""SELECT case_id FROM cases
                WHERE merchant_id=? AND case_id<>? LIMIT 10""",
                (case["merchant_id"], case_id)).fetchall()
        return [row[0] for row in rows]

    def save_draft(self, case_id: str, draft: dict[str, Any]) -> None:
        self.case(case_id)
        with self._connect() as db:
            db.execute("UPDATE cases SET draft=? WHERE case_id=?",
                       (json.dumps(draft), case_id))

    def review(self, case_id: str, decision: str, analyst: str, note: str) -> None:
        if decision not in {"APPROVED", "REJECTED"} or not analyst.strip():
            raise ValueError("decision and analyst are required")
        with self._connect() as db:
            changed = db.execute("""UPDATE cases SET status=?, analyst=?, analyst_note=?
                WHERE case_id=? AND status='OPEN' AND draft IS NOT NULL""",
                (decision, analyst, note, case_id)).rowcount
            if changed != 1:
                raise ValueError("case missing, already reviewed, or draft absent")
