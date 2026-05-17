"""Activity selection logic.

Picks a random activity from the pool, respecting the configured
blackout window so the same activity isn't re-suggested too soon.
"""

import logging
import random
import tomllib
from pathlib import Path

from alone_bot.db import get_conn


logger = logging.getLogger(__name__)


def _load_blackout_days() -> int:
    """Read the blackout window from config.toml."""
    with Path("/app/config.toml").open("rb") as f:
        config = tomllib.load(f)
    return config["behavior"]["recent_repeat_blackout_days"]


def pick_activity(exclude_ids: list[int] | None = None) -> dict | None:
    """Pick a random active activity, respecting the blackout window.

    Args:
        exclude_ids: Additional activity IDs to exclude from this pick.
            Used by the 'Another' button to avoid re-picking what was
            just rejected in the current session.

    Returns:
        A dict with 'id' and 'text' keys, or None if no eligible
        activities exist (everything is on cooldown).
    """
    blackout_days = _load_blackout_days()
    exclude_ids = exclude_ids or []

    # Build the exclusion clause only if there are IDs to exclude.
    # SQL's `NOT IN (NULL)` returns NULL (not TRUE) for every row,
    # which silently filters everything out — must omit the clause entirely.
    if exclude_ids:
        placeholders = ",".join("?" * len(exclude_ids))
        exclude_clause = f"AND id NOT IN ({placeholders})"
        params = exclude_ids
    else:
        exclude_clause = ""
        params = []

    query = f"""
        SELECT id, text FROM activities
        WHERE active = 1
          AND id NOT IN (
              SELECT activity_id FROM suggestions
              WHERE suggested_at > datetime('now', '-{blackout_days} days')
                AND activity_id IS NOT NULL
          )
          {exclude_clause}
    """

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()

    if not rows:
        logger.warning(
            f"No eligible activities (blackout={blackout_days}d, "
            f"excluded={len(exclude_ids)} in session)"
        )
        return None

    chosen = random.choice(rows)
    return {"id": chosen["id"], "text": chosen["text"]}