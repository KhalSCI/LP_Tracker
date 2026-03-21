import aiosqlite
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "lp_tracker.db"


async def init_db():
    """Initialize the database with required tables."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS leaderboards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                channel_id INTEGER,
                message_id INTEGER,
                notification_channel_id INTEGER,
                notifications_enabled BOOLEAN DEFAULT 0,
                notifications_enabled_at TIMESTAMP,
                UNIQUE(guild_id, name)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                leaderboard_id INTEGER NOT NULL,
                riot_id TEXT NOT NULL,
                puuid TEXT,
                summoner_id TEXT,
                tier TEXT,
                rank TEXT,
                lp INTEGER DEFAULT 0,
                prev_lp INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                last_updated TIMESTAMP,
                FOREIGN KEY (leaderboard_id) REFERENCES leaderboards(id) ON DELETE CASCADE
            )
        """)
        # Guild settings for notifications
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                notification_channel_id INTEGER,
                notifications_enabled BOOLEAN DEFAULT 1,
                notifications_enabled_at TIMESTAMP
            )
        """)
        # Track notified matches to avoid duplicates (per leaderboard)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS notified_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                leaderboard_id INTEGER NOT NULL,
                match_id TEXT NOT NULL,
                puuid TEXT NOT NULL,
                notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(leaderboard_id, match_id, puuid),
                FOREIGN KEY (leaderboard_id) REFERENCES leaderboards(id) ON DELETE CASCADE
            )
        """)
        # Migration: Add notifications_enabled_at column if it doesn't exist
        try:
            await db.execute(
                "ALTER TABLE guild_settings ADD COLUMN notifications_enabled_at TIMESTAMP"
            )
        except Exception:
            pass  # Column already exists

        # Migration: Add notification columns to leaderboards table if they don't exist
        try:
            await db.execute(
                "ALTER TABLE leaderboards ADD COLUMN notification_channel_id INTEGER"
            )
        except Exception:
            pass  # Column already exists

        try:
            await db.execute(
                "ALTER TABLE leaderboards ADD COLUMN notifications_enabled BOOLEAN DEFAULT 0"
            )
        except Exception:
            pass  # Column already exists

        try:
            await db.execute(
                "ALTER TABLE leaderboards ADD COLUMN notifications_enabled_at TIMESTAMP"
            )
        except Exception:
            pass  # Column already exists

        # Migration: Migrate notified_matches from guild_id to leaderboard_id
        cursor = await db.execute("PRAGMA table_info(notified_matches)")
        columns = await cursor.fetchall()
        column_names = [col[1] for col in columns]

        if 'guild_id' in column_names and 'leaderboard_id' not in column_names:
            # Create new table with correct schema
            await db.execute("""
                CREATE TABLE IF NOT EXISTS notified_matches_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    leaderboard_id INTEGER NOT NULL,
                    match_id TEXT NOT NULL,
                    puuid TEXT NOT NULL,
                    notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(leaderboard_id, match_id, puuid),
                    FOREIGN KEY (leaderboard_id) REFERENCES leaderboards(id) ON DELETE CASCADE
                )
            """)
            # Drop old table and rename new one
            await db.execute("DROP TABLE IF EXISTS notified_matches")
            await db.execute("ALTER TABLE notified_matches_new RENAME TO notified_matches")

        await db.commit()


async def create_leaderboard(guild_id: int, name: str, channel_id: int = None) -> int:
    """Create a new leaderboard. Returns the leaderboard ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO leaderboards (guild_id, name, channel_id) VALUES (?, ?, ?)",
            (guild_id, name.lower(), channel_id)
        )
        await db.commit()
        return cursor.lastrowid


async def delete_leaderboard(guild_id: int, name: str) -> bool:
    """Delete a leaderboard and all its players. Returns True if deleted."""
    async with aiosqlite.connect(DB_PATH) as db:
        # First get the leaderboard id
        cursor = await db.execute(
            "SELECT id FROM leaderboards WHERE guild_id = ? AND name = ?",
            (guild_id, name.lower())
        )
        row = await cursor.fetchone()
        if not row:
            return False

        leaderboard_id = row[0]

        # Delete players first (cascade should handle this, but being explicit)
        await db.execute(
            "DELETE FROM players WHERE leaderboard_id = ?",
            (leaderboard_id,)
        )

        # Delete leaderboard
        await db.execute(
            "DELETE FROM leaderboards WHERE id = ?",
            (leaderboard_id,)
        )
        await db.commit()
        return True


async def get_leaderboard(guild_id: int, name: str) -> dict | None:
    """Get a leaderboard by guild and name."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM leaderboards WHERE guild_id = ? AND name = ?",
            (guild_id, name.lower())
        )
        row = await cursor.fetchone()
        if row:
            return dict(row)
        return None


async def get_guild_leaderboards(guild_id: int) -> list[dict]:
    """Get all leaderboards for a guild."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM leaderboards WHERE guild_id = ?",
            (guild_id,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def set_leaderboard_channel(guild_id: int, name: str, channel_id: int) -> bool:
    """Set the auto-update channel for a leaderboard."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE leaderboards SET channel_id = ?, message_id = NULL WHERE guild_id = ? AND name = ?",
            (channel_id, guild_id, name.lower())
        )
        await db.commit()
        return cursor.rowcount > 0


async def set_leaderboard_message(leaderboard_id: int, message_id: int) -> None:
    """Set the message ID for a leaderboard's auto-update message."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE leaderboards SET message_id = ? WHERE id = ?",
            (message_id, leaderboard_id)
        )
        await db.commit()


async def add_player(leaderboard_id: int, riot_id: str, puuid: str) -> int:
    """Add a player to a leaderboard. Returns the player ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO players (leaderboard_id, riot_id, puuid, last_updated)
               VALUES (?, ?, ?, ?)""",
            (leaderboard_id, riot_id, puuid, datetime.utcnow())
        )
        await db.commit()
        return cursor.lastrowid


async def remove_player(leaderboard_id: int, riot_id: str) -> bool:
    """Remove a player from a leaderboard. Returns True if removed."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM players WHERE leaderboard_id = ? AND LOWER(riot_id) = LOWER(?)",
            (leaderboard_id, riot_id)
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_player(leaderboard_id: int, riot_id: str) -> dict | None:
    """Get a player by leaderboard and riot_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM players WHERE leaderboard_id = ? AND LOWER(riot_id) = LOWER(?)",
            (leaderboard_id, riot_id)
        )
        row = await cursor.fetchone()
        if row:
            return dict(row)
        return None


async def get_leaderboard_players(leaderboard_id: int) -> list[dict]:
    """Get all players for a leaderboard."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM players WHERE leaderboard_id = ?",
            (leaderboard_id,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def update_player_rank(
    player_id: int,
    tier: str,
    rank: str,
    lp: int,
    wins: int,
    losses: int,
    prev_lp: int = None
) -> None:
    """Update a player's rank information."""
    async with aiosqlite.connect(DB_PATH) as db:
        if prev_lp is not None:
            await db.execute(
                """UPDATE players
                   SET tier = ?, rank = ?, lp = ?, prev_lp = ?, wins = ?, losses = ?, last_updated = ?
                   WHERE id = ?""",
                (tier, rank, lp, prev_lp, wins, losses, datetime.utcnow(), player_id)
            )
        else:
            await db.execute(
                """UPDATE players
                   SET tier = ?, rank = ?, lp = ?, wins = ?, losses = ?, last_updated = ?
                   WHERE id = ?""",
                (tier, rank, lp, wins, losses, datetime.utcnow(), player_id)
            )
        await db.commit()


async def get_all_leaderboards_with_channels() -> list[dict]:
    """Get all leaderboards that have an auto-update channel set."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM leaderboards WHERE channel_id IS NOT NULL"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


# ============ Notification Functions (Per-Leaderboard) ============

async def set_leaderboard_notification_channel(
    guild_id: int,
    leaderboard_name: str,
    channel_id: int
) -> bool:
    """Set or update the notification channel for a specific leaderboard."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """UPDATE leaderboards
               SET notification_channel_id = ?,
                   notifications_enabled = 1,
                   notifications_enabled_at = ?
               WHERE guild_id = ? AND name = ?""",
            (channel_id, datetime.utcnow(), guild_id, leaderboard_name.lower())
        )
        await db.commit()
        return cursor.rowcount > 0


async def disable_leaderboard_notifications(guild_id: int, leaderboard_name: str) -> bool:
    """Disable notifications for a specific leaderboard."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """UPDATE leaderboards
               SET notifications_enabled = 0
               WHERE guild_id = ? AND name = ?""",
            (guild_id, leaderboard_name.lower())
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_leaderboards_with_notifications() -> list[dict]:
    """Get all leaderboards that have notifications enabled."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM leaderboards
               WHERE notification_channel_id IS NOT NULL
               AND notifications_enabled = 1"""
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_players_for_leaderboard_notifications(leaderboard_id: int) -> list[dict]:
    """Get all players for a specific leaderboard (for notifications)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT puuid, riot_id FROM players
               WHERE leaderboard_id = ? AND puuid IS NOT NULL""",
            (leaderboard_id,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def is_match_notified(leaderboard_id: int, match_id: str, puuid: str) -> bool:
    """Check if a match has already been notified for this player in this leaderboard."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM notified_matches WHERE leaderboard_id = ? AND match_id = ? AND puuid = ?",
            (leaderboard_id, match_id, puuid)
        )
        return await cursor.fetchone() is not None


async def mark_match_notified(leaderboard_id: int, match_id: str, puuid: str) -> None:
    """Mark a match as notified for a specific leaderboard."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO notified_matches (leaderboard_id, match_id, puuid) VALUES (?, ?, ?)",
            (leaderboard_id, match_id, puuid)
        )
        await db.commit()


# ============ Legacy Guild-Based Functions (for reference) ============

async def set_notification_channel(guild_id: int, channel_id: int) -> None:
    """Set or update the notification channel for a guild. DEPRECATED: Use set_leaderboard_notification_channel instead."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO guild_settings (guild_id, notification_channel_id, notifications_enabled, notifications_enabled_at)
               VALUES (?, ?, 1, ?)
               ON CONFLICT(guild_id) DO UPDATE SET notification_channel_id = ?, notifications_enabled = 1, notifications_enabled_at = ?""",
            (guild_id, channel_id, datetime.utcnow(), channel_id, datetime.utcnow())
        )
        await db.commit()


async def get_notification_channel(guild_id: int) -> int | None:
    """Get the notification channel for a guild. DEPRECATED: Use leaderboard-based notifications instead."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT notification_channel_id FROM guild_settings WHERE guild_id = ? AND notifications_enabled = 1",
            (guild_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def disable_notifications(guild_id: int) -> bool:
    """Disable notifications for a guild. DEPRECATED: Use disable_leaderboard_notifications instead."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE guild_settings SET notifications_enabled = 0 WHERE guild_id = ?",
            (guild_id,)
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_guilds_with_notifications() -> list[dict]:
    """Get all guilds that have notification channels set and enabled. DEPRECATED: Use get_leaderboards_with_notifications instead."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM guild_settings WHERE notification_channel_id IS NOT NULL AND notifications_enabled = 1"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def cleanup_old_notified_matches(days: int = 7) -> int:
    """Remove notified match records older than N days. Returns count of deleted rows."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM notified_matches WHERE notified_at < datetime('now', ?)",
            (f"-{days} days",)
        )
        await db.commit()
        return cursor.rowcount


async def get_unique_players_for_guild(guild_id: int) -> list[dict]:
    """Get unique players (by puuid) across all leaderboards in a guild."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT DISTINCT p.puuid, p.riot_id
               FROM players p
               JOIN leaderboards lb ON p.leaderboard_id = lb.id
               WHERE lb.guild_id = ? AND p.puuid IS NOT NULL""",
            (guild_id,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_guild_settings(guild_id: int) -> dict | None:
    """Get all settings for a guild."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM guild_settings WHERE guild_id = ?",
            (guild_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
