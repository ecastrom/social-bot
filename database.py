"""
database.py — Local SQLite storage for posts and metrics.
"""
import sqlite3
import json
from datetime import datetime
from pathlib import Path

DB_PATH = Path("bot_data.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS scheduled_posts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            platform    TEXT NOT NULL,           -- 'threads'
            content     TEXT NOT NULL,
            media_path  TEXT,
            scheduled_at TEXT NOT NULL,          -- ISO 8601
            status      TEXT DEFAULT 'pending',  -- pending | sent | failed
            created_at  TEXT DEFAULT (datetime('now')),
            response    TEXT                     -- JSON with API response
        );

        CREATE TABLE IF NOT EXISTS metrics (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            platform    TEXT NOT NULL,
            post_id     TEXT,
            likes       INTEGER DEFAULT 0,
            reposts     INTEGER DEFAULT 0,
            replies     INTEGER DEFAULT 0,
            impressions INTEGER DEFAULT 0,
            recorded_at TEXT DEFAULT (datetime('now'))
        );
        """)
    print("Database initialized.")

# ── Scheduled Posts ──────────────────────────────────────────────────────────

def add_scheduled_post(platform: str, content: str, scheduled_at: str, media_path: str = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO scheduled_posts (platform, content, media_path, scheduled_at) VALUES (?,?,?,?)",
            (platform, content, media_path, scheduled_at)
        )
        return cur.lastrowid

def get_pending_posts(before: str = None) -> list:
    """Obtiene posts pendientes opcionalmente hasta cierta hora."""
    before = before or datetime.utcnow().isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM scheduled_posts WHERE status='pending' AND scheduled_at <= ? ORDER BY scheduled_at",
            (before,)
        ).fetchall()
    return [dict(r) for r in rows]

def mark_post_sent(post_id: int, response: dict):
    with get_conn() as conn:
        conn.execute(
            "UPDATE scheduled_posts SET status='sent', response=? WHERE id=?",
            (json.dumps(response), post_id)
        )

def mark_post_failed(post_id: int, error: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE scheduled_posts SET status='failed', response=? WHERE id=?",
            (json.dumps({"error": error}), post_id)
        )

def list_scheduled_posts():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM scheduled_posts ORDER BY scheduled_at").fetchall()]

# ── Metrics ───────────────────────────────────────────────────────────────────

def save_metric(platform: str, post_id: str, likes: int, reposts: int, replies: int, impressions: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO metrics (platform, post_id, likes, reposts, replies, impressions) VALUES (?,?,?,?,?,?)",
            (platform, post_id, likes, reposts, replies, impressions)
        )

def get_metrics_summary(platform: str = None) -> dict:
    query = "SELECT platform, SUM(likes) likes, SUM(reposts) reposts, SUM(replies) replies, SUM(impressions) impressions FROM metrics"
    params = ()
    if platform:
        query += " WHERE platform=?"
        params = (platform,)
    query += " GROUP BY platform"
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return {r["platform"]: dict(r) for r in rows}

