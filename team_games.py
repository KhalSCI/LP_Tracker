"""Team games (scrims sent from the LoL client, officials from the Riot API) into the team sheet."""
import asyncio
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import database as db
from champions import champions
from config import TIMEZONE, SHEET_GUILD_ID, SHEET_LEADERBOARD
from game_stats import store_new_game, add_client_timeline
from reminders import from_sheet_serial
from riot_api import riot_api, RiotAPIError
from sheets import team_sheet, MATCHES_TAB, MAPS_TAB

ROLES = ["Top", "Jungle", "Mid", "ADC", "Support"]
# At least this many of our players on one team make it our game
MIN_OUR_PLAYERS = 3
SUMMONERS_RIFT = 11
SMITE = 11
# Upload files from the Windows script say this in their "format" field
UPLOAD_FORMAT = "lp-tracker-scrims"
SCRIM_TYPE = "Scrim"
# Officials are looked up this far back, so a match added to "Mecze" a bit late still gets its maps
OFFICIALS_LOOKBACK_DAYS = 14
# A game may start this long before the time written in "Mecze" and still belong to that match
EARLY_START = timedelta(minutes=30)
API_POSITIONS = {"TOP": "Top", "JUNGLE": "Jungle", "MIDDLE": "Mid", "BOTTOM": "ADC", "UTILITY": "Support"}

_ingest_lock = asyncio.Lock()
# Tournament games already checked and not ours, so they aren't fetched again
_not_team_games: set[str] = set()


class TeamRoster:
    """Our players, matched to game participants by PUUID (Riot API) or Riot ID (LoL client)."""

    def __init__(self, members: list[dict]):
        self.members = members
        self.by_puuid = {m["puuid"]: m for m in members if m.get("puuid")}
        self.by_riot_id = {m["riot_id"].lower(): m for m in members if m.get("riot_id")}

    def find(self, participant: dict) -> dict | None:
        return (
            self.by_puuid.get(participant.get("puuid"))
            or self.by_riot_id.get((participant.get("riot_id") or "").lower())
        )

    @property
    def puuids(self) -> list[str]:
        return [m["puuid"] for m in self.members if m.get("puuid")]


async def load_team_roster() -> TeamRoster:
    """Players from the roster sheet plus everyone on the team leaderboard (e.g. ex-members)."""
    lb = await db.get_leaderboard(SHEET_GUILD_ID, SHEET_LEADERBOARD)
    players = await db.get_leaderboard_players(lb["id"]) if lb else []
    members = {
        p["riot_id"].lower(): {"nick": p["discord_name"] or p["riot_id"], "role": None, "riot_id": p["riot_id"], "puuid": p["puuid"]}
        for p in players
    }
    for row in await team_sheet.read_roster():
        member = members.setdefault(row["riot_id"].lower(), {"riot_id": row["riot_id"], "puuid": None})
        member["nick"] = row["nick"] or row["riot_id"]
        member["role"] = row["role"] if row["role"] in ROLES else None
    return TeamRoster(list(members.values()))


def assign_roles(players: list[dict], trust_position: bool, fill_rest: bool) -> None:
    """
    Set "role" on each player of one team. Roles come from the in-game "position" and the
    roster's "roster_role" (in-game position first when trust_position), each used only when
    exactly one player claims a free role; then Smite means jungle. With fill_rest, players
    still without a role get the free ones, otherwise their role stays None.
    """
    free = list(ROLES)
    for player in players:
        player["role"] = None

    def take(player: dict, role: str) -> None:
        player["role"] = role
        free.remove(role)

    for source in (["position", "roster_role"] if trust_position else ["roster_role", "position"]):
        claims = defaultdict(list)
        for player in players:
            if player["role"] is None and player.get(source) in free:
                claims[player[source]].append(player)
        for role, claimants in claims.items():
            if len(claimants) == 1:
                take(claimants[0], role)

    if "Jungle" in free:
        smiters = [p for p in players if p["role"] is None and p.get("smite")]
        if len(smiters) == 1:
            take(smiters[0], "Jungle")

    if fill_rest:
        for player in players:
            if player["role"] is None and free:
                take(player, free[0])


def build_team_game(match_id: str, source: str, start_ms: int, duration_s: int, tournament: bool,
                    participants: list[dict], bans: dict, roster: TeamRoster) -> dict | None:
    """
    Turn a 5v5 game's participants into our team game: who played what for us and against
    us. Returns None unless at least MIN_OUR_PLAYERS of ours were on one team.
    """
    if len(participants) != 10:
        return None
    counts = Counter(p["team_id"] for p in participants if roster.find(p))
    if not counts:
        return None
    our_team, count = counts.most_common(1)[0]
    if count < MIN_OUR_PLAYERS:
        return None

    ours = [p for p in participants if p["team_id"] == our_team]
    enemy = [p for p in participants if p["team_id"] != our_team]
    for player in ours:
        member = roster.find(player)
        player["player"] = member["nick"] if member else player["riot_id"]
        player["roster_role"] = member["role"] if member else None
    assign_roles(ours, trust_position=source == "api", fill_rest=True)
    assign_roles(enemy, trust_position=True, fill_rest=False)
    enemy_team = enemy[0]["team_id"]

    def role_order(player: dict) -> int:
        return ROLES.index(player["role"]) if player["role"] else len(ROLES)

    return {
        "match_id": match_id,
        "source": source,
        "start_ms": start_ms,
        "duration_s": duration_s,
        "tournament": tournament,
        "side": "Blue" if our_team == 100 else "Red",
        "win": any(p["win"] for p in ours),
        "ours": [
            {key: p[key] for key in ("role", "player", "riot_id", "champion", "kills", "deaths", "assists")}
            for p in sorted(ours, key=role_order)
        ],
        "enemy": [
            {"role": p["role"] or "", "riot_id": p["riot_id"], "champion": p["champion"]}
            for p in sorted(enemy, key=role_order)
        ],
        "our_bans": bans.get(our_team, []),
        "enemy_bans": bans.get(enemy_team, []),
    }


def client_match_id(game: dict) -> str:
    return f"{game.get('platformId') or 'EUW1'}_{game.get('gameId')}"


def client_position(timeline: dict | None) -> str | None:
    """Role from the LoL client's lane/role fields (often missing in custom games)."""
    timeline = timeline or {}
    lane, role = timeline.get("lane", ""), timeline.get("role", "")
    if lane == "TOP":
        return "Top"
    if lane == "JUNGLE":
        return "Jungle"
    if lane in ("MIDDLE", "MID"):
        return "Mid"
    if lane in ("BOTTOM", "BOT"):
        return {"DUO_CARRY": "ADC", "CARRY": "ADC", "DUO_SUPPORT": "Support", "SUPPORT": "Support"}.get(role)
    return None


def normalize_client_game(game: dict, roster: TeamRoster) -> dict | None:
    """Our team game from a LoL client match-history game (/lol-match-history/v1/games/{id})."""
    if game.get("mapId") != SUMMONERS_RIFT:
        return None
    identities = {i.get("participantId"): i.get("player") or {} for i in game.get("participantIdentities", [])}
    participants = []
    for p in game.get("participants", []):
        player = identities.get(p.get("participantId"), {})
        name, tag = player.get("gameName"), player.get("tagLine")
        stats = p.get("stats") or {}
        participants.append({
            "team_id": p.get("teamId"),
            "puuid": None,
            "riot_id": f"{name}#{tag}" if name and tag else player.get("summonerName", ""),
            "champion": champions.name(p.get("championId")),
            "kills": stats.get("kills", 0),
            "deaths": stats.get("deaths", 0),
            "assists": stats.get("assists", 0),
            "win": bool(stats.get("win")),
            "position": client_position(p.get("timeline")),
            "smite": SMITE in (p.get("spell1Id"), p.get("spell2Id")),
        })
    bans = {
        team.get("teamId"): [champions.name(b["championId"]) for b in team.get("bans") or [] if b.get("championId", -1) > 0]
        for team in game.get("teams", [])
    }
    return build_team_game(
        client_match_id(game), "client", game.get("gameCreation", 0), game.get("gameDuration", 0),
        False, participants, bans, roster,
    )


def normalize_api_game(match_data: dict, roster: TeamRoster) -> dict | None:
    """Our team game from a Riot API match-v5 payload."""
    info = match_data.get("info", {})
    if info.get("mapId") != SUMMONERS_RIFT:
        return None
    participants = [
        {
            "team_id": p.get("teamId"),
            "puuid": p.get("puuid"),
            "riot_id": f"{p.get('riotIdGameName')}#{p.get('riotIdTagline')}",
            "champion": champions.name(p.get("championId"), p.get("championName")),
            "kills": p.get("kills", 0),
            "deaths": p.get("deaths", 0),
            "assists": p.get("assists", 0),
            "win": bool(p.get("win")),
            "position": API_POSITIONS.get(p.get("teamPosition") or p.get("individualPosition")),
            "smite": SMITE in (p.get("summoner1Id"), p.get("summoner2Id")),
        }
        for p in info.get("participants", [])
    ]
    bans = {
        team.get("teamId"): [champions.name(b["championId"]) for b in team.get("bans") or [] if b.get("championId", -1) > 0]
        for team in info.get("teams", [])
    }
    return build_team_game(
        match_data.get("metadata", {}).get("matchId"), "api",
        info.get("gameStartTimestamp") or info.get("gameCreation", 0), info.get("gameDuration", 0),
        bool(info.get("tournamentCode")), participants, bans, roster,
    )


def game_start(game: dict) -> datetime:
    return datetime.fromtimestamp(game["start_ms"] / 1000, TIMEZONE)


def parse_match_entries(rows: list[list]) -> list[dict]:
    """Matches from raw "Mecze" rows (header row first): label, start, whether it has a time, type."""
    if not rows:
        return []
    header = [str(cell).strip() for cell in rows[0]]
    columns = {name: header.index(name) for name in ("Data", "Godz.", "Typ", "Mecz (do Map)") if name in header}

    def cell(row: list, name: str):
        i = columns.get(name)
        return row[i] if i is not None and i < len(row) else ""

    entries = []
    for row in rows[1:]:
        date, start_time, label = cell(row, "Data"), cell(row, "Godz."), cell(row, "Mecz (do Map)")
        if not isinstance(date, (int, float)) or not label:
            continue
        has_time = isinstance(start_time, (int, float))
        entries.append({
            "label": str(label),
            "start": from_sheet_serial(int(date) + (start_time % 1 if has_time else 0)),
            "has_time": has_time,
            "type": str(cell(row, "Typ")).strip(),
        })
    return entries


def pick_match(entries: list[dict], game: dict, enemies_by_label: dict[str, set]) -> str | None:
    """
    The "Mecze" match a game belongs to: same day, scrim or official as the game is, preferring
    the match already played against the same opponents, then the latest one that had started
    and has no games yet. Returns None for a scrim against new opponents (it gets its own match).
    """
    start = game_start(game)
    enemy_ids = {p["riot_id"].lower() for p in game["enemy"]}
    same_day = [e for e in entries if e["start"].date() == start.date()]
    kind = [e for e in same_day if (e["type"] == SCRIM_TYPE) != game["tournament"]] or same_day

    for entry in sorted(kind, key=lambda e: e["start"], reverse=True):
        if len(enemies_by_label.get(entry["label"], set()) & enemy_ids) >= MIN_OUR_PLAYERS:
            return entry["label"]

    started = [e for e in kind if not e["has_time"] or e["start"] - EARLY_START <= start]
    fresh = [e for e in started if not enemies_by_label.get(e["label"])]
    if fresh:
        return max(fresh, key=lambda e: e["start"])["label"]
    if game["tournament"] and started:
        return max(started, key=lambda e: e["start"])["label"]
    return None


def new_scrim_entry(game: dict) -> dict:
    """A "Mecze" match for a scrim that has none, labeled like the sheet's label formula."""
    start = game_start(game).replace(second=0, microsecond=0)
    return {"label": f"{start:%d.%m %H:%M} · {SCRIM_TYPE}", "start": start, "has_time": True, "type": SCRIM_TYPE, "game": game}


def match_row(entry: dict) -> dict:
    """The "Mecze" cells for a match the bot adds (entered like typed values)."""
    enemies = ", ".join(p["riot_id"] for p in entry["game"]["enemy"])
    return {
        "Data": f"{entry['start']:%d.%m.%Y}",
        "Godz.": f"{entry['start']:%H:%M}",
        "Typ": entry["type"],
        "Przypomnienie": False,
        "Notatki": f"Dodane automatycznie ze skryptu, uzupełnij przeciwnika. Gracze przeciwnika: {enemies}",
    }


def map_row(game: dict, label: str) -> dict:
    """The "Mapy" cells for one game; formula columns are left out."""
    row = {
        "Mecz": label,
        "Wynik": "W" if game["win"] else "L",
        "Strona": game["side"],
        "Picki przeciwnika": ", ".join(p["champion"] for p in game["enemy"]),
        "Game ID": game["match_id"],
        "Notatki / VOD": (
            f"Dodane automatycznie ({'klient LoL' if game['source'] == 'client' else 'Riot API'}), "
            f"{game['duration_s'] // 60} min"
        ),
    }
    for player in game["ours"]:
        row[player["role"]] = player["player"]
        row[f"{player['role']} champ"] = player["champion"]
    return row


async def read_map_game_ids() -> set[str]:
    """Game IDs already in the "Mapy" tab."""
    rows = await team_sheet.read_tab(MAPS_TAB)
    if not rows or "Game ID" not in rows[0]:
        return set()
    column = rows[0].index("Game ID")
    return {str(row[column]) for row in rows[1:] if column < len(row) and row[column]}


async def known_game_ids() -> set[str]:
    """Games already added: in the database or typed into the "Mapy" tab."""
    return await db.get_team_game_ids() | await read_map_game_ids()


async def ingest_games(games: list[dict], raws: dict[str, tuple] = None) -> dict:
    """
    Add team games to "Mapy" (and new scrims to "Mecze") and store them, skipping games
    already in the database or the sheet. raws maps match IDs to (source, raw JSON, timeline)
    kept as the games' full data. Returns what was added, skipped and left unlinked.
    """
    async with _ingest_lock:
        known = await known_game_ids()
        new = {g["match_id"]: g for g in games if g["match_id"] not in known}
        new = sorted(new.values(), key=lambda g: g["start_ms"])
        summary = {"added": [], "already_known": len(games) - len(new), "unlinked": [], "new_matches": []}
        if not new:
            return summary

        entries = parse_match_entries(await team_sheet.read_matches())
        enemies_by_label = defaultdict(set)
        for stored in await db.get_team_games():
            enemies_by_label[stored["match_label"]].update(p["riot_id"].lower() for p in stored["enemy"])

        for game in new:
            label = pick_match(entries, game, enemies_by_label)
            if label is None:
                if game["tournament"]:
                    summary["unlinked"].append(game)
                    continue
                entry = new_scrim_entry(game)
                entries.append(entry)
                summary["new_matches"].append(entry)
                label = entry["label"]
            enemies_by_label[label].update(p["riot_id"].lower() for p in game["enemy"])
            summary["added"].append((game, label))

        if summary["new_matches"]:
            await team_sheet.append_rows(MATCHES_TAB, "Data", [match_row(e) for e in summary["new_matches"]], "USER_ENTERED")
        if summary["added"]:
            await team_sheet.append_rows(MAPS_TAB, "Mecz", [map_row(g, label) for g, label in summary["added"]])
        for game, label in summary["added"]:
            await db.save_team_game(game, label)
            if raws and game["match_id"] in raws:
                await store_new_game(game, *raws[game["match_id"]])
        return summary


async def ingest_client_upload(payload: dict) -> dict:
    """
    Add the games from a Windows-script upload. Games the Riot API has (tournament codes)
    are read from it, the rest from the LoL client data. Games that aren't ours are skipped.
    """
    if payload.get("format") != UPLOAD_FORMAT:
        raise ValueError("to nie jest plik ze skryptu LP Tracker")
    roster = await load_team_roster()
    await champions.refresh()
    known = await known_game_ids()

    # Version 2 uploads also carry each game's timeline, keyed by game ID
    timelines = payload.get("timelines") or {}
    games, raws, already_known, not_ours, timelines_added = [], {}, 0, 0, 0
    for raw in payload.get("games", []):
        match_id = client_match_id(raw)
        timeline = timelines.get(str(raw.get("gameId")))
        if match_id in known:
            already_known += 1
            timelines_added += await add_client_timeline(match_id, raw, timeline)
            continue
        game = None
        try:
            match_data = await riot_api.get_match_details(match_id)
            game = normalize_api_game(match_data, roster)
            if game:
                raws[match_id] = ("api", match_data, None)
        except RiotAPIError as e:
            if e.status not in (403, 404):
                raise
        if game is None:
            game = normalize_client_game(raw, roster)
            if game:
                raws[match_id] = ("client", raw, timeline)
        if game is None:
            not_ours += 1
        else:
            games.append(game)

    summary = await ingest_games(games, raws)
    summary["already_known"] += already_known
    summary["not_ours"] = not_ours
    summary["timelines_added"] = timelines_added
    return summary


async def sync_official_games() -> dict:
    """Add our recent tournament-code games (officials) from the Riot API to the sheet."""
    roster = await load_team_roster()
    await champions.refresh()
    since = int(time.time()) - OFFICIALS_LOOKBACK_DAYS * 24 * 60 * 60

    match_ids = set()
    for puuid in roster.puuids:
        match_ids.update(await riot_api.get_tournament_match_ids(puuid, since))
        await asyncio.sleep(0.5)  # Rate limit

    games, raws = [], {}
    for match_id in sorted(match_ids - await db.get_team_game_ids() - _not_team_games):
        match_data = await riot_api.get_match_details(match_id)
        game = normalize_api_game(match_data, roster)
        await asyncio.sleep(0.5)  # Rate limit
        if game is None:
            _not_team_games.add(match_id)
        else:
            games.append(game)
            raws[match_id] = ("api", match_data, None)
    return await ingest_games(games, raws)


def format_summary(summary: dict) -> str:
    """Discord reply to an upload."""
    lines = []
    if summary["added"]:
        per_match = Counter(label for _, label in summary["added"])
        lines.append(f"✅ Dodane mapy: {len(summary['added'])} (" + ", ".join(f"{label}: {n}" for label, n in per_match.items()) + ")")
    else:
        lines.append("Brak nowych map.")
    if summary["new_matches"]:
        lines.append(
            f"📅 Nowe scrimy w zakładce Mecze: {len(summary['new_matches'])}. Uzupełnij tam przeciwnika."
        )
    if summary["unlinked"]:
        lines.append(f"⚠ Oficjale bez meczu w zakładce Mecze tego dnia: {len(summary['unlinked'])}. Dodaj mecz, a bot dopisze mapy.")
    if summary.get("timelines_added"):
        lines.append(f"📈 Dopisany przebieg gry do wcześniej dodanych map: {summary['timelines_added']}.")
    skipped = [f"{summary['already_known']} już były"]
    if summary.get("not_ours"):
        skipped.append(f"{summary['not_ours']} bez naszego składu")
    lines.append("Pominięte: " + ", ".join(skipped) + ".")
    return "\n".join(lines)
