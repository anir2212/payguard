"""SQLite card lifecycle, holds, double-entry capture/refund and rule-based review."""
import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

TRANSITIONS = {"CREATED": {"ACTIVE", "CANCELLED"}, "ACTIVE": {"FROZEN", "LOST", "CANCELLED"}, "FROZEN": {"ACTIVE", "CANCELLED"}, "LOST": {"CANCELLED"}, "CANCELLED": set()}


def connect(path="payguard.sqlite"):
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS cards(id TEXT PRIMARY KEY,holder TEXT NOT NULL,state TEXT NOT NULL,limit_cents INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS auths(id TEXT PRIMARY KEY,card_id TEXT NOT NULL,merchant TEXT NOT NULL,amount_cents INTEGER NOT NULL,captured_cents INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS journals(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,ref TEXT NOT NULL,currency TEXT NOT NULL,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS postings(journal_id INTEGER NOT NULL,account TEXT NOT NULL,amount_cents INTEGER NOT NULL,FOREIGN KEY(journal_id) REFERENCES journals(id));
    CREATE TABLE IF NOT EXISTS idempotency(key TEXT PRIMARY KEY,kind TEXT NOT NULL,result TEXT NOT NULL,request_hash TEXT);
    """)
    if "request_hash" not in [row[1] for row in db.execute("PRAGMA table_info(idempotency)")]:
        db.execute("ALTER TABLE idempotency ADD COLUMN request_hash TEXT")
    return db


def now():
    return datetime.now(timezone.utc).isoformat()


def _journal(db, kind, ref, postings, currency="SGD"):
    if sum(amount for _, amount in postings) != 0 or len(postings) < 2:
        raise ValueError("unbalanced journal")
    cur = db.execute("INSERT INTO journals(kind,ref,currency,created_at) VALUES(?,?,?,?)", (kind, ref, currency, now()))
    db.executemany("INSERT INTO postings VALUES(?,?,?)", [(cur.lastrowid, account, amount) for account, amount in postings])
    return cur.lastrowid


def issue(db, card_id, holder, limit_cents=100000):
    if limit_cents <= 0:
        raise ValueError("positive limit required")
    with db:
        db.execute("INSERT INTO cards VALUES(?,?,?,?)", (card_id, holder, "CREATED", limit_cents))
        _journal(db, "fund", card_id, [(f"funding:{card_id}", limit_cents), ("bank", -limit_cents)])


def change_state(db, card_id, next_state):
    row = db.execute("SELECT state FROM cards WHERE id=?", (card_id,)).fetchone()
    if not row or next_state not in TRANSITIONS[row[0]]:
        raise ValueError("illegal card transition")
    with db:
        db.execute("UPDATE cards SET state=? WHERE id=?", (next_state, card_id))


def balance(db, account):
    return db.execute("SELECT coalesce(sum(amount_cents),0) FROM postings WHERE account=?", (account,)).fetchone()[0]


def available(db, card_id):
    holds = db.execute("SELECT coalesce(sum(amount_cents-captured_cents),0) FROM auths WHERE card_id=? AND status='OPEN'", (card_id,)).fetchone()[0]
    return balance(db, f"funding:{card_id}") - holds


def fraud_flags(db, card_id, amount_cents):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    recent = db.execute("SELECT count(*) FROM auths WHERE card_id=? AND created_at>=?", (card_id, cutoff)).fetchone()[0]
    return (["high_amount"] if amount_cents > 50000 else []) + (["velocity"] if recent >= 5 else [])


def request_hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def previous(db, key, kind, payload):
    row = db.execute("SELECT kind,result,request_hash FROM idempotency WHERE key=?", (key,)).fetchone()
    if row and row[0] != kind:
        raise ValueError("idempotency key reused for another operation")
    if row and row[2] != request_hash(payload):
        raise ValueError("idempotency key reused with changed request")
    return json.loads(row[1]) if row else None


def authorize(db, auth_id, card_id, merchant, amount_cents, key):
    if amount_cents <= 0:
        raise ValueError("positive amount required")
    payload = [auth_id, card_id, merchant, amount_cents]
    existing = previous(db, key, "authorize", payload)
    if existing:
        return existing
    row = db.execute("SELECT state,limit_cents FROM cards WHERE id=?", (card_id,)).fetchone()
    if not row or row[0] != "ACTIVE":
        raise ValueError("card inactive")
    flags = fraud_flags(db, card_id, amount_cents)
    if flags or amount_cents > min(row[1], available(db, card_id)):
        result = {"status": "DECLINED", "flags": flags or ["limit"]}
    else:
        result = {"status": "APPROVED", "auth_id": auth_id, "flags": []}
    with db:
        if result["status"] == "APPROVED":
            db.execute("INSERT INTO auths(id,card_id,merchant,amount_cents,status,created_at) VALUES(?,?,?,?,?,?)", (auth_id, card_id, merchant, amount_cents, "OPEN", now()))
        db.execute("INSERT INTO idempotency VALUES(?,?,?,?)", (key, "authorize", json.dumps(result), request_hash(payload)))
    return result


def capture(db, auth_id, amount_cents, key):
    payload = [auth_id, amount_cents]
    existing = previous(db, key, "capture", payload)
    if existing:
        return existing
    row = db.execute("SELECT card_id,merchant,amount_cents,captured_cents,status FROM auths WHERE id=?", (auth_id,)).fetchone()
    if not row or row[4] != "OPEN" or not 0 < amount_cents <= row[2]-row[3]:
        raise ValueError("invalid capture")
    card, merchant, original, captured, _ = row
    result = {"status": "CAPTURED", "amount_cents": amount_cents}
    with db:
        _journal(db, "capture", auth_id, [(f"funding:{card}", -amount_cents), (f"merchant:{merchant}", amount_cents)])
        db.execute("UPDATE auths SET captured_cents=?,status=? WHERE id=?", (captured+amount_cents, "CLOSED" if captured+amount_cents == original else "OPEN", auth_id))
        db.execute("INSERT INTO idempotency VALUES(?,?,?,?)", (key, "capture", json.dumps(result), request_hash(payload)))
    return result


def refund(db, auth_id, amount_cents, key):
    payload = [auth_id, amount_cents]
    existing = previous(db, key, "refund", payload)
    if existing:
        return existing
    row = db.execute("SELECT card_id,merchant,captured_cents FROM auths WHERE id=?", (auth_id,)).fetchone()
    refunded = db.execute("SELECT coalesce(sum(-amount_cents),0) FROM postings p JOIN journals j ON p.journal_id=j.id WHERE j.kind='refund' AND j.ref=? AND p.account LIKE 'merchant:%'", (auth_id,)).fetchone()[0]
    if not row or not 0 < amount_cents <= row[2]-refunded:
        raise ValueError("invalid refund")
    result = {"status": "REFUNDED", "amount_cents": amount_cents}
    with db:
        _journal(db, "refund", auth_id, [(f"funding:{row[0]}", amount_cents), (f"merchant:{row[1]}", -amount_cents)])
        db.execute("INSERT INTO idempotency VALUES(?,?,?,?)", (key, "refund", json.dumps(result), request_hash(payload)))
    return result


def investigate(db, auth_id):
    row = db.execute("SELECT id,card_id,merchant,amount_cents,status FROM auths WHERE id=?", (auth_id,)).fetchone()
    if not row:
        raise ValueError("unknown authorization")
    return {"summary": f"Authorization {row[0]} for {row[3]} SGD cents at {row[2]} is {row[4]}.", "sources": [f"auth:{row[0]}"]}


def main():
    db = connect()
    if not db.execute("SELECT 1 FROM cards WHERE id='card-demo'").fetchone():
        issue(db, "card-demo", "demo-user")
        change_state(db, "card-demo", "ACTIVE")
    result = authorize(db, "auth-demo", "card-demo", "shop", 1250, "auth-demo-key")
    if result["status"] == "APPROVED":
        capture(db, "auth-demo", 1250, "capture-demo-key")
    print(json.dumps({"auth": result, "balance_cents": available(db, "card-demo"), "investigation": investigate(db, "auth-demo")}, indent=2))


if __name__ == "__main__":
    main()
