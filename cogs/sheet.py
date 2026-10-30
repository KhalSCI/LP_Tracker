import asyncio
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

import database as db
from availability import find_missing_availability, format_day
from riot_api import riot_api, RiotAPIError
from sheets import team_sheet
from config import (
    RANK_ORDER, DIVISION_ORDER, TIMEZONE, RIOT_EUW_PLATFORM,
    GOOGLE_SHEET_ID, SHEET_GUILD_ID, SHEET_LEADERBOARD,
)

AUTO_DELETE_SECONDS = 60

SOLOQ_QUEUE = 420
FLEX_QUEUE = 440
QUEUE_NAMES = {SOLOQ_QUEUE: "SoloQ (duo)", FLEX_QUEUE: "Flex"}

DAY_SECONDS = 24 * 60 * 60
# Games are fetched this far back; top champions are counted over this window
MATCH_HISTORY_DAYS = 30
# Window for games played, LP change and games played together
ACTIVITY_DAYS = 7
# Each fetch re-checks this far before the previous one, so games in progress then aren't missed
MATCH_SYNC_OVERLAP_SECONDS = 3 * 60 * 60
MATCH_IDS_PAGE = 100
# SoloQ games stored before lane opponents were kept get them filled in, this many per sync
LANE_OPPONENT_BACKFILL = 150

# Layout of the SoloQ tab
TEAM_HEADERS = [
    "Nick", "Rola", "Riot ID EUW", "Ranga", "LP", "W", "L", "WR",
    "Gry 7 dni", "W-L 7 dni", "Zmiana LP 7 dni", "Top 3 championy (SoloQ 30 dni)",
    "Ostatnia gra", "Aktualizacja",
]
TEAM_FIRST_ROW = 5
TEAM_MAX_ROWS = 12  # Skład rows 8-19
DUO_HEADERS = ["Gracze", "Kolejka", "Gry", "W", "L", "WR", "Ostatnio"]
DUO_TITLE_ROW = 19
DUO_FIRST_ROW = 21
DUO_MAX_ROWS = 25
SHEET_ROWS = DUO_FIRST_ROW + DUO_MAX_ROWS - 1
SHEET_COLS = len(TEAM_HEADERS)

APEX_TIERS = ("MASTER", "GRANDMASTER", "CHALLENGER")
# match-v5 champion keys that differ from the in-game name beyond spacing
CHAMPION_NAMES = {"MonkeyKing": "Wukong"}
# Day zero of spreadsheet date serial numbers
SHEET_EPOCH = datetime(1899, 12, 30)

_sync_lock = asyncio.Lock()


class RosterError(Exception):
    """A roster row whose Riot ID can't be tracked. The message is shown in the sheet."""


def ladder_score(tier: str, rank: str, lp: int) -> int | None:
    """
    Position on the ranked ladder in LP, comparable across tiers: 100 LP per division,
    with Master, Grandmaster and Challenger sharing one LP scale above Diamond I.
    """
    if not tier:
        return None
    tier = tier.upper()
    if tier in APEX_TIERS:
        return RANK_ORDER["MASTER"] * 400 + lp
    return RANK_ORDER[tier] * 400 + DIVISION_ORDER.get((rank or "").upper(), 0) * 100 + lp


def format_tier(tier: str, rank: str) -> str:
    """Format the rank without LP, e.g. "Diamond II" or "Master"."""
    if not tier:
        return "Unranked"
    if tier.upper() in APEX_TIERS:
        return tier.title()
    return f"{tier.title()} {rank}"


def champion_display_name(name: str) -> str:
    """Turn match-v5 champion keys like "MissFortune" into display names."""
    return CHAMPION_NAMES.get(name, re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name))


def to_sheet_datetime(dt: datetime) -> float:
    """Convert a datetime (naive means UTC) to a spreadsheet date serial number in local time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(TIMEZONE).replace(tzinfo=None)
    return round((local - SHEET_EPOCH).total_seconds() / DAY_SECONDS, 6)


def from_timestamp_ms(timestamp_ms: int) -> datetime:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc)


def win_rate(wins: int, games: int) -> float | str:
    """Winrate as a fraction for a percent-formatted cell, blank without games."""
    return round(wins / games, 4) if games else ""


def top_champions(games: list[dict], count: int = 3) -> str:
    """Most played champions with games and winrate, e.g. "Ahri 14 (64%) · Syndra 9 (56%)"."""
    played = defaultdict(lambda: [0, 0])
    for game in games:
        played[game["champion_name"]][0] += 1
        played[game["champion_name"]][1] += bool(game["win"])

    ordered = sorted(
        played.items(),
        key=lambda item: (-item[1][0], -item[1][1] / item[1][0], item[0])
    )
    return " · ".join(
        f"{champion_display_name(name)} {total} ({round(wins / total * 100)}%)"
        for name, (total, wins) in ordered[:count]
    )


def match_rows(match_id: str, match_data: dict, tracked_puuids: set[str]) -> list[dict]:
    """Build matches-table rows for every tracked player in the match."""
    info = match_data.get("info", {})
    rows = []
    for participant in info.get("participants", []):
        puuid = participant.get("puuid")
        if puuid not in tracked_puuids:
            continue
        stats = riot_api.extract_player_match_stats(match_data, puuid)
        rows.append({
            "match_id": match_id,
            "puuid": puuid,
            "queue_id": stats["queue_id"],
            "team_id": stats["team_id"],
            "champion_name": stats["champion_name"],
            "win": stats["win"],
            "remake": stats["remake"],
            "kills": stats["kills"],
            "deaths": stats["deaths"],
            "assists": stats["assists"],
            "game_start": stats["game_start_timestamp"],
            "game_duration": info.get("gameDuration", 0),
            "lane_opponent": stats["lane_opponent"] or "",
        })
    return rows


def build_team_rows(entries: list[dict], matches: list[dict], baselines: dict, now: datetime) -> list[list]:
    """Rows of the SoloQ table: best rank first, then unranked, then roster rows that failed."""
    week_ago_ms = int((now - timedelta(days=ACTIVITY_DAYS)).timestamp() * 1000)
    soloq_games = defaultdict(list)  # newest first, as matches are
    for match in matches:
        if match["queue_id"] == SOLOQ_QUEUE and not match["remake"]:
            soloq_games[match["puuid"]].append(match)

    rows = []
    for entry in entries:
        player = entry["player"]
        # e.g. "Top (Sub)" when the roster gives a status
        role = f"{entry['role']} ({entry['status']})" if entry.get("status") else entry["role"]
        if player is None:
            row = [entry["nick"], role, entry["riot_id"], f"⚠ {entry['error']}"]
            rows.append(((2, 0), row))
            continue

        games = soloq_games[player["puuid"]]
        week = [game for game in games if game["game_start"] >= week_ago_ms]
        week_wins = sum(bool(game["win"]) for game in week)

        ranked = bool(player["tier"])
        score = ladder_score(player["tier"], player["rank"], player["lp"])
        baseline = baselines.get(player["puuid"])
        base_score = ladder_score(baseline["tier"], baseline["rank"], baseline["lp"]) if baseline else None
        lp_change = score - base_score if score is not None and base_score is not None else ""

        rows.append(((0, -score) if ranked else (1, 0), [
            entry["nick"],
            role,
            player["riot_id"],
            format_tier(player["tier"], player["rank"]),
            player["lp"] if ranked else "",
            player["wins"] if ranked else "",
            player["losses"] if ranked else "",
            win_rate(player["wins"], player["wins"] + player["losses"]) if ranked else "",
            len(week),
            f"{week_wins}-{len(week) - week_wins}" if week else "",
            lp_change,
            top_champions(games),
            to_sheet_datetime(from_timestamp_ms(games[0]["game_start"])) if games else "",
            to_sheet_datetime(datetime.fromisoformat(player["last_updated"])) if player["last_updated"] else "",
        ]))

    rows.sort(key=lambda item: item[0])
    return [row for _, row in rows]


def build_duo_rows(matches: list[dict], names: dict[str, str], now: datetime) -> list[list]:
    """
    Rows of the games-together table: tracked players on the same team in the last
    week, grouped by queue and set of players, most games first.
    """
    week_ago_ms = int((now - timedelta(days=ACTIVITY_DAYS)).timestamp() * 1000)
    order = {puuid: i for i, puuid in enumerate(names)}

    teams = defaultdict(list)
    for match in matches:
        if match["game_start"] >= week_ago_ms and not match["remake"]:
            teams[(match["match_id"], match["team_id"])].append(match)

    groups = {}
    for team in teams.values():
        if len(team) < 2:
            continue
        players = tuple(sorted({game["puuid"] for game in team}, key=order.get))
        group = groups.setdefault((team[0]["queue_id"], players), {"games": 0, "wins": 0, "last": 0})
        group["games"] += 1
        group["wins"] += bool(team[0]["win"])
        group["last"] = max(group["last"], team[0]["game_start"])

    ordered = sorted(groups.items(), key=lambda item: (-item[1]["games"], -item[1]["last"]))
    return [
        [
            " + ".join(names[puuid] for puuid in players),
            QUEUE_NAMES.get(queue, str(queue)),
            group["games"],
            group["wins"],
            group["games"] - group["wins"],
            win_rate(group["wins"], group["games"]),
            to_sheet_datetime(from_timestamp_ms(group["last"])),
        ]
        for (queue, players), group in ordered
    ]


def sync_status(baselines: dict, now: datetime) -> str:
    """Status line for the SoloQ tab, noting when LP history is shorter than a week."""
    status = f"Ostatnia synchronizacja: {now.astimezone(TIMEZONE):%d.%m.%Y %H:%M}"
    week_ago = now.astimezone(timezone.utc).replace(tzinfo=None) - timedelta(days=ACTIVITY_DAYS)
    starts = [datetime.fromisoformat(b["recorded_at"]) for b in baselines.values() if b]
    recent_starts = [start for start in starts if start > week_ago]
    if recent_starts:
        since = max(recent_starts).replace(tzinfo=timezone.utc).astimezone(TIMEZONE)
        status += f" · historia LP zapisywana od {since:%d.%m.%Y}, do tego czasu zmiana LP liczona od tej daty"
    return status


def pad_rows(rows: list[list], count: int, width: int) -> list[list]:
    """Cut or pad rows to exactly count x width, blanking leftovers from the previous sync."""
    rows = [row + [""] * (width - len(row)) for row in rows[:count]]
    return rows + [[""] * width for _ in range(count - len(rows))]


def column_letter(index: int) -> str:
    """1 -> "A", 14 -> "N"."""
    return chr(ord("A") + index - 1)


def build_sheet_updates(entries: list[dict], matches: list[dict], baselines: dict, now: datetime) -> list[dict]:
    """All value ranges written to the SoloQ tab in one sync."""
    names = {
        entry["player"]["puuid"]: entry["nick"] or entry["player"]["riot_id"]
        for entry in entries if entry["player"]
    }
    team_rows = build_team_rows(entries, matches, baselines, now)
    duo_rows = build_duo_rows(matches, names, now) or [["Brak wspólnych gier w ostatnich 7 dniach"]]

    last_col = column_letter(SHEET_COLS)
    duo_last_col = column_letter(len(DUO_HEADERS))
    team_last_row = TEAM_FIRST_ROW + TEAM_MAX_ROWS - 1
    return [
        {"range": "A3", "values": [[sync_status(baselines, now)]]},
        {"range": f"A{TEAM_FIRST_ROW - 1}:{last_col}{TEAM_FIRST_ROW - 1}", "values": [TEAM_HEADERS]},
        {
            "range": f"A{TEAM_FIRST_ROW}:{last_col}{team_last_row}",
            "values": pad_rows(team_rows, TEAM_MAX_ROWS, SHEET_COLS),
        },
        {"range": f"A{DUO_TITLE_ROW}", "values": [["Wspólne gry (7 dni): gracze z drużyny w tej samej drużynie w meczu"]]},
        {"range": f"A{DUO_FIRST_ROW - 1}:{duo_last_col}{DUO_FIRST_ROW - 1}", "values": [DUO_HEADERS]},
        {
            "range": f"A{DUO_FIRST_ROW}:{duo_last_col}{SHEET_ROWS}",
            "values": pad_rows(duo_rows, DUO_MAX_ROWS, len(DUO_HEADERS)),
        },
    ]


async def _add_roster_player(leaderboard_id: int, row: dict, by_puuid: dict) -> int:
    """
    Look up a roster Riot ID that isn't on the leaderboard: add the account, or rename
    the player if it is an already tracked account under a new name. Returns the player ID.
    """
    if "#" not in row["riot_id"]:
        raise RosterError("Zły format Riot ID, powinno być Nazwa#TAG")
    game_name, tag_line = row["riot_id"].rsplit("#", 1)

    try:
        player_info = await riot_api.get_player_full_info(game_name, tag_line)
    except RiotAPIError as e:
        if e.status == 404:
            raise RosterError("Nie znaleziono konta o tym Riot ID")
        raise

    if player_info["region"] != RIOT_EUW_PLATFORM:
        raise RosterError(f"Konto nie jest z EUW (serwer: {player_info['region'].upper() or '?'})")

    riot_id = f"{player_info['game_name']}#{player_info['tag_line']}"
    existing = by_puuid.get(player_info["puuid"])
    if existing:
        await db.update_player_identity(existing["id"], riot_id, row["nick"] or None)
        return existing["id"]

    player_id = await db.add_player(leaderboard_id, riot_id, player_info["puuid"], discord_name=row["nick"] or None)
    ranked = player_info.get("ranked")
    if ranked:
        await db.update_player_rank(
            player_id,
            ranked["tier"],
            ranked["rank"],
            ranked["lp"],
            ranked["wins"],
            ranked["losses"]
        )
    print(f"Added {riot_id} to the team leaderboard from the roster sheet")
    return player_id


async def sync_roster(leaderboard_id: int, roster: list[dict]) -> list[dict]:
    """
    Bring the roster sheet's players onto the leaderboard: add new Riot IDs and update
    nicks and renamed accounts. Players no longer listed stay on the leaderboard
    (e.g. ex-teammates still followed there) until removed with /player remove.
    Returns the roster rows, each with its "player_id" (None if it failed) and "error".
    """
    players = await db.get_leaderboard_players(leaderboard_id)
    by_riot_id = {player["riot_id"].lower(): player for player in players}
    by_puuid = {player["puuid"]: player for player in players}

    entries = []
    for row in roster:
        entry = dict(row, player_id=None, error=None)
        entries.append(entry)

        player = by_riot_id.get(row["riot_id"].lower())
        if player:
            if (player["discord_name"] or "") != row["nick"]:
                await db.update_player_identity(player["id"], player["riot_id"], row["nick"] or None)
            entry["player_id"] = player["id"]
            continue

        try:
            entry["player_id"] = await _add_roster_player(leaderboard_id, row, by_puuid)
        except RosterError as e:
            entry["error"] = str(e)
        except RiotAPIError as e:
            entry["error"] = f"Błąd Riot API ({e.status}), ponowię przy następnej synchronizacji"
        await asyncio.sleep(0.5)  # Rate limit

    return entries


async def _fetch_match_ids(puuid: str, queue: int, start_time: int) -> list[str]:
    """Get all of the player's match IDs in a queue since start_time, paging through results."""
    match_ids = []
    while True:
        page = await riot_api.get_match_ids_by_puuid(
            puuid,
            queue=queue,
            count=MATCH_IDS_PAGE,
            start_time=start_time,
            start=len(match_ids)
        )
        await asyncio.sleep(0.5)  # Rate limit
        match_ids += page
        if len(page) < MATCH_IDS_PAGE:
            return match_ids


async def sync_player_matches(puuid: str, tracked_puuids: set[str], now: int) -> int:
    """Store the player's SoloQ and Flex games not stored yet. Returns how many were added."""
    start_time = now - MATCH_HISTORY_DAYS * DAY_SECONDS
    synced_at = await db.get_match_sync_time(puuid)
    if synced_at is not None:
        start_time = max(start_time, synced_at - MATCH_SYNC_OVERLAP_SECONDS)

    added = 0
    for queue in (SOLOQ_QUEUE, FLEX_QUEUE):
        match_ids = await _fetch_match_ids(puuid, queue, start_time)
        stored = await db.get_stored_match_ids(puuid, match_ids)
        for match_id in match_ids:
            if match_id in stored:
                continue
            match_data = await riot_api.get_match_details(match_id)
            # Saved for every tracked player in the game, so a duo game is fetched once
            await db.save_matches(match_rows(match_id, match_data, tracked_puuids))
            added += 1
            await asyncio.sleep(0.5)  # Rate limit

    await db.set_match_sync_time(puuid, now)
    return added


async def backfill_lane_opponents(puuids: set[str], since_ms: int, limit: int) -> int:
    """Fill in the lane opponent of stored SoloQ games without one, fetching each game once. Returns how many."""
    match_ids = await db.get_matches_without_lane_opponent(list(puuids), since_ms, limit)
    for match_id in match_ids:
        try:
            match_data = await riot_api.get_match_details(match_id)
        except RiotAPIError as e:
            print(f"No lane opponents for {match_id}: {e}")
            continue
        updates = []
        for puuid in puuids:
            stats = riot_api.extract_player_match_stats(match_data, puuid)
            if stats:
                updates.append((stats["lane_opponent"] or "", match_id, puuid))
        await db.set_lane_opponents(updates)
        await asyncio.sleep(0.5)  # Rate limit
    return len(match_ids)


async def sync_team_sheet() -> dict:
    """
    Sync the team sheet: roster from "Skład" into the team leaderboard, new games from
    the Riot API, then stats into the "SoloQ" tab. Ranks come from the leaderboard updates.
    Returns a summary with the number of players, roster errors and new games.
    """
    async with _sync_lock:
        lb = await db.get_leaderboard(SHEET_GUILD_ID, SHEET_LEADERBOARD)
        if not lb:
            raise ValueError(f"leaderboard '{SHEET_LEADERBOARD}' not found in guild {SHEET_GUILD_ID}")

        roster = await team_sheet.read_roster()
        entries = await sync_roster(lb["id"], roster)

        players = {player["id"]: player for player in await db.get_leaderboard_players(lb["id"])}
        for entry in entries:
            entry["player"] = players.get(entry["player_id"])
        tracked = {entry["player"]["puuid"]: entry["player"] for entry in entries if entry["player"]}

        now = datetime.now(TIMEZONE)
        now_ts = int(now.timestamp())
        new_games = 0
        for puuid, player in tracked.items():
            try:
                new_games += await sync_player_matches(puuid, set(tracked), now_ts)
            except RiotAPIError as e:
                print(f"Error fetching matches for {player['riot_id']}: {e}")

        try:
            await backfill_lane_opponents(set(tracked), (now_ts - MATCH_HISTORY_DAYS * DAY_SECONDS) * 1000, LANE_OPPONENT_BACKFILL)
        except RiotAPIError as e:
            print(f"Error filling in lane opponents: {e}")

        matches = await db.get_matches_since(list(tracked), (now_ts - MATCH_HISTORY_DAYS * DAY_SECONDS) * 1000)
        week_ago = now.astimezone(timezone.utc).replace(tzinfo=None) - timedelta(days=ACTIVITY_DAYS)
        baselines = {puuid: await db.get_rank_baseline(puuid, week_ago) for puuid in tracked}

        updates = build_sheet_updates(entries, matches, baselines, now)
        await team_sheet.write_soloq(updates, SHEET_ROWS, SHEET_COLS)

        return {
            "players": len(tracked),
            "errors": sum(1 for entry in entries if entry["error"]),
            "new_games": new_games,
        }


class SheetCog(commands.Cog):
    """Cog for the team Google Sheet sync."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    sheet_group = app_commands.Group(name="sheet", description="Team Google Sheet sync")

    @sheet_group.command(name="sync", description="Update the team sheet now")
    async def sheet_sync(self, interaction: discord.Interaction):
        await interaction.response.defer()

        if not GOOGLE_SHEET_ID:
            text = "❌ Sheet sync is not configured (GOOGLE_SHEET_ID is not set)."
        elif _sync_lock.locked():
            text = "⏳ A sheet sync is already running, try again in a few minutes."
        else:
            try:
                summary = await sync_team_sheet()
                text = f"✅ Synced the team sheet: {summary['players']} players, {summary['new_games']} new games"
                if summary["errors"]:
                    text += f" ({summary['errors']} roster errors, see the SoloQ tab)"
            except Exception as e:
                text = f"❌ Sheet sync failed: {e}"

        msg = await interaction.followup.send(text, wait=True)
        await msg.delete(delay=AUTO_DELETE_SECONDS)

    @sheet_group.command(name="availability", description="Show who would be pinged about missing availability (no pings)")
    async def sheet_availability(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if not GOOGLE_SHEET_ID:
            await interaction.followup.send("❌ Sheet sync is not configured (GOOGLE_SHEET_ID is not set).")
            return
        try:
            missing = await find_missing_availability(datetime.now(TIMEZONE).date())
        except Exception as e:
            await interaction.followup.send(f"❌ Nie udało się sprawdzić dostępności: {e}")
            return
        if not missing:
            text = "✅ Wszyscy z głównego składu mają wpisaną dostępność na najbliższe dni."
        else:
            text = "Przypomnienie dostałby:\n" + "\n".join(
                f"• **{discord.utils.escape_markdown(nick)}**: {', '.join(format_day(day) for day in days)}"
                for nick, days in missing.items()
            )
        await interaction.followup.send(text)


async def setup(bot: commands.Bot):
    await bot.add_cog(SheetCog(bot))
