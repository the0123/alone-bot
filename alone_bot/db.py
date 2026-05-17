"""SQLite access layer for alone-bot.

The database file lives at the path configured in config.toml
(default: /data/alone-bot.db inside the container, which maps to
/opt/alone-bot/data/alone-bot.db on the host).
"""

import sqlite3
import tomllib
from contextlib import contextmanager
from pathlib import Path

from alone_bot.seed_data import SEED_ACTIVITIES


def _load_db_path() -> str:
    """Read the database path from config.toml."""
    config_path = Path("/app/config.toml")
    with config_path.open("rb") as f:
        config = tomllib.load(f)
    return config["database"]["path"]


@contextmanager
def get_conn():
    """Yield a SQLite connection with foreign keys enabled.

    Used as a context manager so connections always close cleanly:
        with get_conn() as conn:
            conn.execute(...)
    """
    conn = sqlite3.connect(_load_db_path())
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row  # access columns by name
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if they don't exist. Safe to run on every startup."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS activities (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                text        TEXT NOT NULL UNIQUE,
                source      TEXT,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                active      BOOLEAN DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS suggestions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                activity_id     INTEGER REFERENCES activities(id),
                suggested_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                trigger         TEXT,
                response        TEXT,
                response_at     TIMESTAMP,
                completed       BOOLEAN,
                completed_at    TIMESTAMP,
                followup_sent_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS state (
                key    TEXT PRIMARY KEY,
                value  TEXT
            );
        """)


def seed_activities() -> int:
    """Insert seed activities only if the table is empty.

    Returns the number of rows inserted (0 if already seeded).
    """
    with get_conn() as conn:
        existing = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
        if existing > 0:
            return 0

        conn.executemany(
            "INSERT INTO activities (text, source) VALUES (?, ?)",
            [(text, "seed:personal") for text in SEED_ACTIVITIES],
        )
        return len(SEED_ACTIVITIES)
    

def set_state(key: str, value: str) -> None:
    """Upsert a key/value pair in the state table."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def get_state(key: str) -> str | None:
    """Return the value for a state key, or None if not set."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None
    
    
def log_suggestion(activity_id: int | None, trigger: str) -> int:
    """Insert a new suggestion row and return its ID.

    Args:
        activity_id: ID of the activity being suggested, or None for
            'not_alone' rows where no activity was picked.
        trigger: One of 'scheduled', 'on_demand', 'rerolled'.

    Returns:
        The ID of the newly inserted suggestion row.
    """
    with get_conn() as conn:
        cursor = conn.execute(
            "INSERT INTO suggestions (activity_id, trigger) VALUES (?, ?)",
            (activity_id, trigger),
        )
        return cursor.lastrowid
    
def update_suggestion_response(suggestion_id: int, response: str) -> dict | None:
    """Mark a suggestion with the user's response and return the row.

    Args:
        suggestion_id: The row to update.
        response: One of 'accepted', 'rejected', 'another', 'not_alone'.

    Returns:
        The updated row as a dict (id, activity_id, text, response, etc.),
        or None if the suggestion_id doesn't exist.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE suggestions "
            "SET response = ?, response_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (response, suggestion_id),
        )
        row = conn.execute(
            "SELECT s.*, a.text AS activity_text "
            "FROM suggestions s "
            "LEFT JOIN activities a ON s.activity_id = a.id "
            "WHERE s.id = ?",
            (suggestion_id,),
        ).fetchone()
        return dict(row) if row else None


def recent_session_activity_ids(minutes: int) -> list[int]:
    """Return activity IDs suggested within the last N minutes.

    Used to exclude recently-shown activities when the user taps 'Another'
    so they don't see the same option twice in a session.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT activity_id FROM suggestions "
            "WHERE suggested_at > datetime('now', ? || ' minutes') "
            "  AND activity_id IS NOT NULL",
            (f"-{minutes}",),
        ).fetchall()
        return [row["activity_id"] for row in rows]
    

def get_chat_id() -> int | None:
    """Return the bound chat_id as an int, or None if /start hasn't run yet."""
    value = get_state("chat_id")
    return int(value) if value else None


def get_pending_followups(within_hours: int = 24) -> list[dict]:
    """Return accepted suggestions that haven't had their followup sent yet."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, activity_id, response_at "
            "FROM suggestions "
            "WHERE response = 'accepted' "
            "  AND completed IS NULL "
            "  AND followup_sent_at IS NULL "
            "  AND response_at > datetime('now', ? || ' hours')",
            (f"-{within_hours}",),
        ).fetchall()
        return [dict(row) for row in rows]


def mark_followup_sent(suggestion_id: int) -> None:
    """Stamp followup_sent_at when the followup message goes out."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE suggestions SET followup_sent_at = CURRENT_TIMESTAMP WHERE id = ?",
            (suggestion_id,),
        )


def update_suggestion_completion(suggestion_id: int, completed: bool) -> dict | None:
    """Mark whether an accepted suggestion was actually completed.

    Returns the updated row (with joined activity_text) or None.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE suggestions "
            "SET completed = ?, completed_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (completed, suggestion_id),
        )
        row = conn.execute(
            "SELECT s.*, a.text AS activity_text "
            "FROM suggestions s "
            "LEFT JOIN activities a ON s.activity_id = a.id "
            "WHERE s.id = ?",
            (suggestion_id,),
        ).fetchone()
        return dict(row) if row else None
    
    
def sweep_stale_suggestions(timeout_hours: int) -> int:
    """Mark suggestions older than timeout_hours with response = 'no_response'
    if they haven't been answered yet.

    Returns the number of rows updated.
    """
    with get_conn() as conn:
        cursor = conn.execute(
            "UPDATE suggestions "
            "SET response = 'no_response', response_at = CURRENT_TIMESTAMP "
            "WHERE response IS NULL "
            "  AND suggested_at < datetime('now', ? || ' hours')",
            (f"-{timeout_hours}",),
        )
        return cursor.rowcount
    
def add_activity(text: str) -> tuple[bool, str]:
    """Insert a new activity. Returns (success, message_or_error)."""
    text = text.strip()
    if not text:
        return (False, "Activity text can't be empty.")

    try:
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO activities (text, source) VALUES (?, ?)",
                (text, "user"),
            )
        return (True, f"Added: {text}")
    except sqlite3.IntegrityError:
        return (False, f"Already in the list: {text}")


def list_activities() -> list[dict]:
    """Return all active activities, ordered by id."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, text FROM activities WHERE active = 1 ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]


def get_stats() -> dict:
    """Compute summary stats across the suggestions log."""
    with get_conn() as conn:
        # Total activities in the pool
        total_activities = conn.execute(
            "SELECT COUNT(*) FROM activities WHERE active = 1"
        ).fetchone()[0]

        # Total suggestions (excluding gate-only rows that never led
        # to an actual activity pick)
        total_suggestions = conn.execute(
            "SELECT COUNT(*) FROM suggestions WHERE activity_id IS NOT NULL"
        ).fetchone()[0]

        # Acceptance rate: accepted / (accepted + rejected)
        # Excludes 'another' (rerolls aren't rejections), 'no_response',
        # and gate-only rows.
        accept_reject = conn.execute(
            "SELECT response, COUNT(*) AS n FROM suggestions "
            "WHERE response IN ('accepted', 'rejected') "
            "GROUP BY response"
        ).fetchall()
        accepted = next((r["n"] for r in accept_reject if r["response"] == "accepted"), 0)
        rejected = next((r["n"] for r in accept_reject if r["response"] == "rejected"), 0)
        accept_total = accepted + rejected
        accept_rate = (accepted / accept_total * 100) if accept_total else None

        # Completion rate: completed=1 / (completed in (0, 1))
        completion = conn.execute(
            "SELECT completed, COUNT(*) AS n FROM suggestions "
            "WHERE completed IS NOT NULL "
            "GROUP BY completed"
        ).fetchall()
        completed_yes = next((r["n"] for r in completion if r["completed"] == 1), 0)
        completed_no = next((r["n"] for r in completion if r["completed"] == 0), 0)
        completion_total = completed_yes + completed_no
        completion_rate = (
            completed_yes / completion_total * 100 if completion_total else None
        )

        # Top 3 accepted (by activity)
        top_accepted = conn.execute(
            "SELECT a.text, COUNT(*) AS n FROM suggestions s "
            "JOIN activities a ON s.activity_id = a.id "
            "WHERE s.response = 'accepted' "
            "GROUP BY a.id ORDER BY n DESC LIMIT 3"
        ).fetchall()

        # Top 3 rejected (rejected + rerolled-past, by activity)
        top_rejected = conn.execute(
            "SELECT a.text, COUNT(*) AS n FROM suggestions s "
            "JOIN activities a ON s.activity_id = a.id "
            "WHERE s.response IN ('rejected', 'another') "
            "GROUP BY a.id ORDER BY n DESC LIMIT 3"
        ).fetchall()

        return {
            "total_activities": total_activities,
            "total_suggestions": total_suggestions,
            "accepted": accepted,
            "rejected": rejected,
            "accept_rate": accept_rate,
            "completed_yes": completed_yes,
            "completed_no": completed_no,
            "completion_rate": completion_rate,
            "top_accepted": [dict(r) for r in top_accepted],
            "top_rejected": [dict(r) for r in top_rejected],
        }