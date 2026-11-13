import os
import sqlite3
from contextlib import contextmanager

SCHEMA_VERSION = 1
SCHEMA_SQL = """
CREATE TABLE sender_accounts (
  id INTEGER PRIMARY KEY,
  x_user_id TEXT NOT NULL UNIQUE,
  username TEXT NOT NULL,
  daily_send_cap INTEGER NOT NULL DEFAULT 20 CHECK (daily_send_cap >= 0),
  token_ciphertext BLOB,
  created_at TEXT NOT NULL
);

CREATE TABLE campaigns (
  id INTEGER PRIMARY KEY,
  sender_account_id INTEGER NOT NULL REFERENCES sender_accounts(id),
  name TEXT NOT NULL,
  brief TEXT NOT NULL DEFAULT '',
  budget_usd REAL NOT NULL DEFAULT 5.0,
  created_at TEXT NOT NULL
);

CREATE TABLE x_users (
  x_user_id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  name TEXT,
  bio TEXT,
  receives_your_dm INTEGER,
  connection_status TEXT,
  fetched_at TEXT NOT NULL
);

CREATE TABLE x_posts (
  post_id TEXT PRIMARY KEY,
  author_id TEXT NOT NULL REFERENCES x_users(x_user_id),
  text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  url TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);

CREATE TABLE prospects (
  id INTEGER PRIMARY KEY,
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  x_user_id TEXT NOT NULL REFERENCES x_users(x_user_id),
  state TEXT NOT NULL CHECK (state IN
    ('discovered','scored','filtered_out','no_evidence','drafted','rejected','queued')),
  state_reason TEXT,
  score INTEGER,
  fit_reasons TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (campaign_id, x_user_id)
);

CREATE TABLE evidence (
  id INTEGER PRIMARY KEY,
  prospect_id INTEGER NOT NULL REFERENCES prospects(id),
  source TEXT NOT NULL CHECK (source IN ('post','bio')),
  post_id TEXT REFERENCES x_posts(post_id),
  quote TEXT NOT NULL,
  url TEXT NOT NULL
);

CREATE TABLE drafts (
  id INTEGER PRIMARY KEY,
  prospect_id INTEGER NOT NULL REFERENCES prospects(id),
  version INTEGER NOT NULL,
  text TEXT NOT NULL,
  claims TEXT NOT NULL DEFAULT '[]',
  validator_flags TEXT NOT NULL DEFAULT '[]',
  edited_by_human INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE (prospect_id, version)
);

CREATE TABLE outreach (
  id INTEGER PRIMARY KEY,
  sender_account_id INTEGER NOT NULL REFERENCES sender_accounts(id),
  recipient_x_user_id TEXT NOT NULL REFERENCES x_users(x_user_id),
  prospect_id INTEGER NOT NULL REFERENCES prospects(id),
  state TEXT NOT NULL CHECK (state IN
    ('approved','sending','sent','failed','unknown','blocked','cancelled','replied')),
  state_reason TEXT,
  final_text TEXT NOT NULL,
  x_dm_event_id TEXT,
  x_dm_conversation_id TEXT,
  approved_at TEXT NOT NULL,
  attempt_started_at TEXT,
  sent_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  UNIQUE (sender_account_id, recipient_x_user_id)
);

CREATE TABLE replies (
  id INTEGER PRIMARY KEY,
  outreach_id INTEGER NOT NULL REFERENCES outreach(id),
  x_event_id TEXT UNIQUE,
  text TEXT,
  classification TEXT,
  handled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE suppression (
  x_user_id TEXT PRIMARY KEY,
  reason TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('opt_out','manual')),
  created_at TEXT NOT NULL
);

CREATE TABLE ledger (
  id INTEGER PRIMARY KEY,
  campaign_id INTEGER REFERENCES campaigns(id),
  endpoint TEXT NOT NULL,
  resource_count INTEGER NOT NULL,
  unit_cost_usd REAL NOT NULL,
  cost_usd REAL NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY,
  entity TEXT NOT NULL CHECK (entity IN ('prospect','outreach')),
  entity_id INTEGER NOT NULL,
  from_state TEXT,
  to_state TEXT NOT NULL,
  actor TEXT NOT NULL CHECK (actor IN ('system','human')),
  reason TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE send_locks (
  sender_account_id INTEGER PRIMARY KEY REFERENCES sender_accounts(id),
  owner TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE INDEX idx_outreach_sender_state ON outreach (sender_account_id, state);
CREATE INDEX idx_prospects_campaign_state ON prospects (campaign_id, state);
CREATE INDEX idx_audit_entity ON audit_log (entity, entity_id);
"""


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def init_db(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA user_version")
    ver = cur.fetchone()[0]
    if ver == 0:
        conn.executescript(SCHEMA_SQL)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif ver == 1:
        return
    else:
        raise RuntimeError(
            f"Database version {ver} is higher than supported schema version {SCHEMA_VERSION}"
        )
