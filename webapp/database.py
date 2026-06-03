import sqlite3
from contextlib import contextmanager
from config import DB_PATH

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT NOT NULL UNIQUE,
    email      TEXT NOT NULL DEFAULT '',
    hashed_pw  TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    is_active  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS clients (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_name  TEXT NOT NULL UNIQUE,
    folder_path  TEXT NOT NULL,
    is_standby   INTEGER NOT NULL DEFAULT 0,
    last_scanned TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id    INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    category     TEXT,
    subclient    TEXT,
    filename     TEXT NOT NULL,
    rel_path     TEXT NOT NULL UNIQUE,
    abs_path     TEXT NOT NULL,
    file_size    INTEGER,
    file_mtime   TEXT,
    file_hash    TEXT,
    mime_type    TEXT,
    indexed_at   TEXT,
    page_count   INTEGER DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    doc_id     UNINDEXED,
    client_id  UNINDEXED,
    category   UNINDEXED,
    filename,
    body,
    tokenize = 'unicode61 remove_diacritics 1'
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id  INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    sources    TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS organizer_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    log_text     TEXT DEFAULT '',
    client_scope TEXT
);

CREATE TABLE IF NOT EXISTS pending_clients (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_name   TEXT NOT NULL UNIQUE,
    folder_path   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'waiting',
    file_count    INTEGER NOT NULL DEFAULT 0,
    detected_name TEXT,
    conflict_with TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_docs_client    ON documents(client_id);
CREATE INDEX IF NOT EXISTS idx_docs_category  ON documents(client_id, category);
CREATE INDEX IF NOT EXISTS idx_chat_user      ON chat_messages(user_id, client_id);

INSERT OR IGNORE INTO settings(key,value) VALUES ('anthropic_api_key','');
INSERT OR IGNORE INTO settings(key,value) VALUES ('watcher_enabled','1');
INSERT OR IGNORE INTO settings(key,value) VALUES ('index_on_startup','1');
"""

def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)
        # Migrations for existing databases
        try:
            conn.execute("ALTER TABLE documents ADD COLUMN subclient TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
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

def setting(key: str) -> str:
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else ""

def set_setting(key: str, value: str):
    with get_db() as db:
        db.execute("INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES(?,?,datetime('now'))", (key, value))
