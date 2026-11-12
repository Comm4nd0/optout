"""SQLite state: brokers (synced from YAML), requests (person x broker), events."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import yaml

SCHEMA = """
CREATE TABLE IF NOT EXISTS brokers (
  slug TEXT PRIMARY KEY, name TEXT, domain TEXT, jurisdiction TEXT,
  method TEXT, contact TEXT, alt_contact TEXT, needs_id INTEGER,
  recheck_months INTEGER, priority INTEGER, enabled INTEGER,
  category TEXT, notes TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person TEXT NOT NULL, broker TEXT NOT NULL REFERENCES brokers(slug),
  round INTEGER NOT NULL DEFAULT 1,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  sent_at TEXT, due_at TEXT, closed_at TEXT, recheck_at TEXT,
  message_ids TEXT NOT NULL DEFAULT '',
  chases INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS requests_open ON requests(person, broker, closed_at);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id INTEGER REFERENCES requests(id),
  ts TEXT NOT NULL, type TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_req ON events(request_id);
CREATE TABLE IF NOT EXISTS seen_mail (msgid TEXT PRIMARY KEY, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS heartbeat (name TEXT PRIMARY KEY, ts TEXT NOT NULL, ok INTEGER, detail TEXT);
"""

# open states: the request is still in flight
OPEN = ("queued", "sent", "acknowledged", "needs_verification", "needs_id",
        "needs_review", "chased", "overdue", "manual_todo", "form_queued", "refused")
# terminal for this round; recheck_at schedules the next round
CLOSED = ("confirmed", "no_data", "skipped")
# states that need a human to do something
NEEDS_YOU = ("needs_id", "needs_review", "overdue", "manual_todo", "refused", "needs_verification")


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con


def sync_brokers(con: sqlite3.Connection, brokers_dir: Path) -> int:
    """Upsert every brokers/*.yaml; brokers whose file vanished are disabled, not deleted."""
    slugs = []
    for f in sorted(brokers_dir.glob("*.yaml")):
        b = yaml.safe_load(f.read_text())
        slug = f.stem
        slugs.append(slug)
        con.execute(
            """INSERT INTO brokers VALUES (:slug,:name,:domain,:jurisdiction,:method,:contact,
                 :alt_contact,:needs_id,:recheck_months,:priority,:enabled,:category,:notes,:source)
               ON CONFLICT(slug) DO UPDATE SET name=excluded.name, domain=excluded.domain,
                 jurisdiction=excluded.jurisdiction, method=excluded.method, contact=excluded.contact,
                 alt_contact=excluded.alt_contact, needs_id=excluded.needs_id,
                 recheck_months=excluded.recheck_months, priority=excluded.priority,
                 enabled=excluded.enabled, category=excluded.category, notes=excluded.notes,
                 source=excluded.source""",
            {
                "slug": slug, "name": b["name"], "domain": b.get("domain", ""),
                "jurisdiction": b.get("jurisdiction", "us"), "method": b.get("method", "manual"),
                "contact": b.get("contact", ""), "alt_contact": b.get("alt_contact", ""),
                "needs_id": int(bool(b.get("needs_id"))), "recheck_months": b.get("recheck_months", 6),
                "priority": b.get("priority", 3), "enabled": int(b.get("enabled", True)),
                "category": b.get("category", ""), "notes": b.get("notes", ""),
                "source": b.get("source", ""),
            },
        )
    if slugs:
        q = ",".join("?" * len(slugs))
        con.execute(f"UPDATE brokers SET enabled=0 WHERE slug NOT IN ({q})", slugs)
    con.commit()
    return len(slugs)


def log(con, request_id, type_, detail=""):
    con.execute("INSERT INTO events(request_id, ts, type, detail) VALUES (?,?,?,?)",
                (request_id, now(), type_, detail[:20000]))


def set_state(con, request_id, state, detail="", **fields):
    fields["state"] = state
    fields["updated_at"] = now()
    if state in CLOSED:
        fields.setdefault("closed_at", now())
    cols = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE requests SET {cols} WHERE id=?", (*fields.values(), request_id))
    log(con, request_id, f"state:{state}", detail)


def beat(con, name, ok=True, detail=""):
    con.execute("INSERT OR REPLACE INTO heartbeat VALUES (?,?,?,?)", (name, now(), int(ok), detail))
    con.commit()
