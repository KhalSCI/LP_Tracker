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
