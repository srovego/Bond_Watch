from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


MIGRATIONS = [
    """
    CREATE TABLE issuers (
      id INTEGER PRIMARY KEY, name TEXT NOT NULL, inn TEXT, moex_id TEXT,
      category TEXT NOT NULL DEFAULT 'corporate', status TEXT NOT NULL DEFAULT 'verified',
      UNIQUE(inn), UNIQUE(moex_id)
    );
    CREATE TABLE bonds (
      isin TEXT PRIMARY KEY, secid TEXT, issuer_id INTEGER REFERENCES issuers(id),
      shortname TEXT, issue_name TEXT, issue_number TEXT, bond_type TEXT,
      card_url TEXT, nominal REAL, status TEXT NOT NULL DEFAULT 'unresolved',
      last_resolved_at TEXT, resolution_error TEXT
    );
    CREATE TABLE sources (
      id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL,
      status TEXT NOT NULL, last_success_at TEXT, last_error TEXT
    );
    CREATE TABLE news (
      id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
      source_item_id TEXT NOT NULL, url TEXT NOT NULL, source_name TEXT NOT NULL,
      published_at TEXT, discovered_at TEXT NOT NULL, title TEXT NOT NULL,
      body TEXT, category TEXT NOT NULL, severity TEXT NOT NULL,
      analysis_status TEXT NOT NULL DEFAULT 'pending', notification_status TEXT NOT NULL DEFAULT 'pending',
      fingerprint TEXT NOT NULL, UNIQUE(source_id, source_item_id)
    );
    CREATE TABLE news_issuers (
      news_id INTEGER NOT NULL REFERENCES news(id) ON DELETE CASCADE,
      issuer_id INTEGER NOT NULL REFERENCES issuers(id),
      PRIMARY KEY(news_id, issuer_id)
    );
    CREATE TABLE analysis (
      news_id INTEGER PRIMARY KEY REFERENCES news(id) ON DELETE CASCADE,
      summary TEXT, facts_json TEXT, impact TEXT, confidence REAL,
      model TEXT, analyzed_at TEXT, error TEXT
    );
    CREATE TABLE notifications (
      id INTEGER PRIMARY KEY, news_id INTEGER REFERENCES news(id),
      kind TEXT NOT NULL, chat_id TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE,
      status TEXT NOT NULL, created_at TEXT NOT NULL, sent_at TEXT, error TEXT
    );
    CREATE TABLE collector_runs (
      id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
      started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
      items_seen INTEGER NOT NULL DEFAULT 0, error TEXT
    );
    CREATE TABLE price_observations (
      isin TEXT NOT NULL REFERENCES bonds(isin), trading_date TEXT NOT NULL,
      clean_price REAL NOT NULL, nominal REAL NOT NULL, observed_at TEXT NOT NULL,
      PRIMARY KEY(isin, trading_date)
    );
    CREATE TABLE bot_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE INDEX idx_bonds_issuer ON bonds(issuer_id);
    CREATE INDEX idx_news_fingerprint ON news(fingerprint);
    CREATE INDEX idx_news_discovered ON news(discovered_at);
    CREATE INDEX idx_notifications_status ON notifications(status);
    CREATE INDEX idx_runs_source_started ON collector_runs(source_id, started_at);
    """,
    """
    ALTER TABLE bonds ADD COLUMN rating TEXT;
    ALTER TABLE bonds ADD COLUMN rating_agency TEXT;
    ALTER TABLE bonds ADD COLUMN rating_url TEXT;
    CREATE TABLE news_bonds (
      news_id INTEGER NOT NULL REFERENCES news(id) ON DELETE CASCADE,
      isin TEXT NOT NULL REFERENCES bonds(isin) ON DELETE CASCADE,
      PRIMARY KEY(news_id, isin)
    );
    """,
    """
    ALTER TABLE bonds ADD COLUMN rating_date TEXT;
    ALTER TABLE bonds ADD COLUMN rating_status TEXT;
    """
]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for index in range(version, len(MIGRATIONS)):
        conn.executescript(f"BEGIN IMMEDIATE;\n{MIGRATIONS[index]}\nPRAGMA user_version={index + 1};\nCOMMIT;")
    return conn


def backup(conn: sqlite3.Connection, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(destination) as target:
        conn.backup(target)





