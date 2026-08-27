"""
The decision ledger -- what makes a daily re-run safe.

Every proposal carries a fingerprint derived from what it would change. Once a
human approves or rejects that fingerprint, it is recorded here forever, and the
next run filters it out before a reviewer ever sees it. Rejections are as sticky
as approvals: a proposal a human said no to must not come back tomorrow asking
the same question.

Applied writes are recorded with the API's own response, so the ledger doubles
as an audit trail of everything the pipeline changed in the CRM.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "decisions.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    fingerprint   TEXT PRIMARY KEY,
    decision      TEXT NOT NULL,          -- approved | rejected
    proposal_type TEXT NOT NULL,
    account_id    TEXT,
    account_name  TEXT,
    changes       TEXT NOT NULL,          -- json
    decided_at    TEXT NOT NULL,
    decided_by    TEXT NOT NULL,
    apply_status  TEXT,                   -- applied | failed | skipped | NULL
    apply_result  TEXT,                   -- json: API response or error
    applied_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_decision ON decisions(decision);
CREATE INDEX IF NOT EXISTS idx_account  ON decisions(account_id);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def connect(path=DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def decided_fingerprints(conn):
    return {r["fingerprint"] for r in conn.execute("SELECT fingerprint FROM decisions")}


def get(conn, fingerprint):
    r = conn.execute("SELECT * FROM decisions WHERE fingerprint = ?",
                     (fingerprint,)).fetchone()
    return dict(r) if r else None


def record(conn, proposal, decision, by="reviewer"):
    """Write the decision down. Re-deciding the same fingerprint is refused --
    the ledger is append-only so the audit trail cannot be quietly rewritten."""
    if get(conn, proposal["fingerprint"]):
        raise ValueError(f"{proposal['fingerprint']} has already been decided")
    conn.execute(
        "INSERT INTO decisions (fingerprint, decision, proposal_type, account_id,"
        " account_name, changes, decided_at, decided_by)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (proposal["fingerprint"], decision, proposal["type"],
         proposal.get("account_id"), proposal.get("account_name"),
         json.dumps(proposal["changes"], ensure_ascii=False), now(), by))
    conn.commit()


def record_apply(conn, fingerprint, status, result):
    conn.execute(
        "UPDATE decisions SET apply_status = ?, apply_result = ?, applied_at = ?"
        " WHERE fingerprint = ?",
        (status, json.dumps(result, ensure_ascii=False, default=str), now(), fingerprint))
    conn.commit()


def pending(conn, proposals):
    """Proposals no human has ruled on yet."""
    seen = decided_fingerprints(conn)
    return [p for p in proposals if p["fingerprint"] not in seen]


def summary(conn):
    rows = conn.execute(
        "SELECT decision, apply_status, COUNT(*) n FROM decisions"
        " GROUP BY decision, apply_status").fetchall()
    return [dict(r) for r in rows]


def history(conn, limit=200):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM decisions ORDER BY decided_at DESC LIMIT ?", (limit,))]


if __name__ == "__main__":
    conn = connect()
    print(f"ledger: {DB_PATH}")
    print(f"decided: {len(decided_fingerprints(conn))}")
    for row in summary(conn):
        print(f"  {row['decision']:10} apply={row['apply_status'] or '-':8} {row['n']}")
