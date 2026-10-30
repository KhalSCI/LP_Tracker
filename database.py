import json

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
                discord_name TEXT,
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
        # Ranked games of tracked players, one row per (match, tracked player)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                match_id TEXT NOT NULL,
                puuid TEXT NOT NULL,
                queue_id INTEGER NOT NULL,
                team_id INTEGER,
                champion_name TEXT,
                win BOOLEAN,
                remake BOOLEAN DEFAULT 0,
                kills INTEGER,
                deaths INTEGER,
                assists INTEGER,
                game_start INTEGER,
                game_duration INTEGER,
                PRIMARY KEY (match_id, puuid)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_matches_puuid_start ON matches(puuid, game_start)"
        )
        # When each player's match history was last fetched
        await db.execute("""
            CREATE TABLE IF NOT EXISTS match_sync (
                puuid TEXT PRIMARY KEY,
                synced_at INTEGER NOT NULL
            )
        """)
        # Rank snapshots, stored only when a player's rank changes
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rank_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                puuid TEXT NOT NULL,
                tier TEXT,
                rank TEXT,
                lp INTEGER,
                wins INTEGER,
                losses INTEGER,
                recorded_at TIMESTAMP NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_rank_history_puuid ON rank_history(puuid, recorded_at)"
        )
        # Team games (scrims and officials) added to the sheet, one row per game
        await db.execute("""
            CREATE TABLE IF NOT EXISTS team_games (
                match_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                match_label TEXT,
                start_ms INTEGER,
                data TEXT NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Match reminders already posted, so each is sent once
        await db.execute("""
            CREATE TABLE IF NOT EXISTS match_reminders (
                reminder_key TEXT PRIMARY KEY,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

        # Migration: Add Discord nickname column to players table if it doesn't exist
        try:
            await db.execute(
                "ALTER TABLE players ADD COLUMN discord_name TEXT"
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


async def add_player(leaderboard_id: int, riot_id: str, puuid: str, discord_name: str = None) -> int:
    """Add a player to a leaderboard. Returns the player ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO players (leaderboard_id, riot_id, discord_name, puuid, last_updated)
               VALUES (?, ?, ?, ?, ?)""",
            (leaderboard_id, riot_id, discord_name, puuid, datetime.utcnow())
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
    """Update a player's rank information and record it in the rank history."""
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

        cursor = await db.execute("SELECT puuid FROM players WHERE id = ?", (player_id,))
        row = await cursor.fetchone()
        if row and row[0]:
            await _record_rank_snapshot(db, row[0], (tier, rank, lp, wins, losses))
        await db.commit()


async def _record_rank_snapshot(db: aiosqlite.Connection, puuid: str, rank_data: tuple) -> None:
    """Add a rank_history row unless the player's last snapshot is the same rank."""
    cursor = await db.execute(
        """SELECT tier, rank, lp, wins, losses FROM rank_history
           WHERE puuid = ? ORDER BY id DESC LIMIT 1""",
        (puuid,)
    )
    last = await cursor.fetchone()
    if last is not None and tuple(last) == tuple(rank_data):
        return
    await db.execute(
        """INSERT INTO rank_history (puuid, tier, rank, lp, wins, losses, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (puuid, *rank_data, datetime.utcnow())
    )


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


# ============ Team Sheet Functions ============

async def update_player_identity(player_id: int, riot_id: str, discord_name: str) -> None:
    """Update a player's Riot ID and Discord nick (e.g., after an account rename)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE players SET riot_id = ?, discord_name = ? WHERE id = ?",
            (riot_id, discord_name, player_id)
        )
        await db.commit()


async def get_stored_match_ids(puuid: str, match_ids: list[str]) -> set[str]:
    """Return which of the given match IDs are already stored for this player."""
    if not match_ids:
        return set()
    async with aiosqlite.connect(DB_PATH) as db:
        placeholders = ",".join("?" * len(match_ids))
        cursor = await db.execute(
            f"SELECT match_id FROM matches WHERE puuid = ? AND match_id IN ({placeholders})",
            (puuid, *match_ids)
        )
        rows = await cursor.fetchall()
        return {row[0] for row in rows}


async def save_matches(rows: list[dict]) -> None:
    """Store match rows (one per match and tracked player), skipping ones already stored."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(
            """INSERT OR IGNORE INTO matches
               (match_id, puuid, queue_id, team_id, champion_name, win, remake,
                kills, deaths, assists, game_start, game_duration)
               VALUES (:match_id, :puuid, :queue_id, :team_id, :champion_name, :win, :remake,
                       :kills, :deaths, :assists, :game_start, :game_duration)""",
            rows
        )
        await db.commit()


async def get_match_sync_time(puuid: str) -> int | None:
    """Get when this player's match history was last fetched (Unix seconds)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT synced_at FROM match_sync WHERE puuid = ?", (puuid,))
        row = await cursor.fetchone()
        return row[0] if row else None


async def set_match_sync_time(puuid: str, synced_at: int) -> None:
    """Record when this player's match history was last fetched (Unix seconds)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO match_sync (puuid, synced_at) VALUES (?, ?)
               ON CONFLICT(puuid) DO UPDATE SET synced_at = excluded.synced_at""",
            (puuid, synced_at)
        )
        await db.commit()


async def get_matches_since(puuids: list[str], since_ms: int) -> list[dict]:
    """Get stored matches of the given players that started at or after since_ms."""
    if not puuids:
        return []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" * len(puuids))
        cursor = await db.execute(
            f"""SELECT * FROM matches
                WHERE puuid IN ({placeholders}) AND game_start >= ?
                ORDER BY game_start DESC""",
            (*puuids, since_ms)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_rank_baseline(puuid: str, at: datetime) -> dict | None:
    """
    Get the player's rank as it was at the given UTC time: the last snapshot taken
    at or before it, or the first one after it if history starts later.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM rank_history WHERE puuid = ? AND recorded_at <= ?
               ORDER BY recorded_at DESC LIMIT 1""",
            (puuid, at)
        )
        row = await cursor.fetchone()
        if row is None:
            cursor = await db.execute(
                "SELECT * FROM rank_history WHERE puuid = ? ORDER BY recorded_at LIMIT 1",
                (puuid,)
            )
            row = await cursor.fetchone()
        return dict(row) if row else None


async def is_reminder_sent(reminder_key: str) -> bool:
    """Check if a match reminder has already been posted."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT 1 FROM match_reminders WHERE reminder_key = ?",
            (reminder_key,)
        )
        return await cursor.fetchone() is not None


async def mark_reminder_sent(reminder_key: str) -> None:
    """Record that a match reminder was posted."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO match_reminders (reminder_key) VALUES (?)",
            (reminder_key,)
        )
        await db.commit()


async def get_team_game_ids() -> set[str]:
    """Get the match IDs of all team games already added."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT match_id FROM team_games")
        rows = await cursor.fetchall()
        return {row[0] for row in rows}


async def save_team_game(game: dict, match_label: str) -> bool:
    """Store a team game (normalized, as JSON) unless it is already stored. Returns True if stored."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT OR IGNORE INTO team_games (match_id, source, match_label, start_ms, data)
               VALUES (?, ?, ?, ?, ?)""",
            (game["match_id"], game["source"], match_label, game["start_ms"], json.dumps(game))
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_team_games() -> list[dict]:
    """Get all stored team games, each with its "match_label"."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT match_label, data FROM team_games ORDER BY start_ms")
        rows = await cursor.fetchall()
        return [dict(json.loads(data), match_label=label) for label, data in rows]
