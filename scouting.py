"""
Opponent scouting for draft prep: each account's champions in ranked and tournament-code
games of the last months (games, winrate, KDA), from the Riot API: SoloQ of EUW accounts
(on other servers like EUNE only from Master up) and tournament-code games, kept apart,
with pro-play priority from gol.gg, suggested bans and what they played against us.
Opponents' accounts come from the "Przeciwnicy" tab (multi op.gg links per team).
Posted to Discord and written to the bot-only "Scouting" tab.
"""
import asyncio
import re
import time
from collections import Counter, defaultdict
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlparse, parse_qs, unquote

import aiohttp
import discord
import gspread

import database as db
import llm
from champions import champions
from cogs.sheet import format_tier, APEX_TIERS, SOLOQ_QUEUE
from config import TIMEZONE, SCOUT_MONTHS, GOOGLE_SHEET_ID
from riot_api import riot_api, RiotAPIError
from roasts import plural, games_count
from sheets import team_sheet

SCOUTING_TAB = "Scouting"
# Filled in by hand: one row per team (name as in the "Przeciwnik" column of "Mecze"), headers in row 4
OPPONENTS_TAB = "Przeciwnicy"
OPPONENTS_HEADERS = ["Drużyna", "Multi EUW", "Multi EUNE", "Notatki"]
OPPONENTS_FIRST_ROW = 5
OPPONENTS_CACHE_SECONDS = 300
# Flex isn't scouted; this keeps Flex games stored earlier out of the tournament ones
RANKED_QUEUES = {420: "SoloQ", 440: "Flex"}
# SoloQ counts on this server; on others (EUNE) it's often a smurf's, so only from Master up
MAIN_PLATFORM = "euw1"
# Clash isn't scouted (match-v5's "tourney" type leaves it out anyway)
CLASH_QUEUE = 700
SUMMONERS_RIFT = 11
ROLES = ["Top", "Jungle", "Mid", "ADC", "Support"]
POSITIONS = {"TOP": "Top", "JUNGLE": "Jungle", "MIDDLE": "Mid", "BOTTOM": "ADC", "UTILITY": "Support"}
PLATFORM_NAMES = {"euw1": "EUW", "eun1": "EUNE"}
# match-v5 patch 16.x is season 2026 (gol.gg calls it S16)
SEASON_OFFSET = 2010
IDS_PAGE = 100
MONTH_SECONDS = 30 * 24 * 60 * 60
# Besides the whole period, the report shows the most recent months on their own
RECENT_MONTHS = 3
# Periods offered by /scout as (SoloQ months, tournament months); "quick" is fast to fetch
PERIODS = {"quick": (1, 12), "3": (3, 3), "6": (6, 6), "12": (12, 12)}
# Rough time to fetch one game with the Riot API rate limit (100 requests per 2 minutes)
SECONDS_PER_GAME = 1.3
GOLGG_URL = "https://gol.gg/champion/list/season-S{season}/split-ALL/tournament-ALL/"
PRO_META_TTL = 24 * 60 * 60
PRO_META_ROWS = 20
# Champions per player shown in the sheet, and in the embed (fewer when the embed gets too long)
SHEET_CHAMPIONS = 10
EMBED_CHAMPIONS = (4, 3, 2)
# Discord rejects embeds over 6000 characters
EMBED_MAX_LENGTH = 5900
BAN_SUGGESTIONS = 5
# Ranked games in the period (or tournament games) a champion needs to be suggested as a ban
MIN_BAN_GAMES = 5
MIN_BAN_TOURNEY_GAMES = 2
TOURNEY_WEIGHT = 3
# One champion with this share of a player's ranked games in the period (and enough games) is a one-trick
ONE_TRICK_SHARE = 0.4
ONE_TRICK_MIN_GAMES = 15
# gol.gg writes some champions differently than Data Dragon
CHAMPION_ALIASES = {"nunuwillump": "nunu"}
# Columns of the three champion blocks per player in the sheet (A, J, S) and their headers
BLOCK_COLUMNS = (0, 9, 18)
BLOCK_HEADERS = ["Champion", "Gry", "W", "L", "WR", "KDA", "Pro prio", "Pro WR"]
SHEET_COLS = 26

SUMMARY_PROMPT = """Jesteś analitykiem draftu w uczelnianej drużynie League of Legends.
Dostajesz dane o drużynie przeciwnika: pule championów graczy z rankedów z ostatnich miesięcy, gry turniejowe, sugerowane bany i priorytet championów w grze profesjonalnej.
Napisz po polsku 3-5 krótkich punktów do przygotowania draftu: co banować, na kim się skupić, comfort picki i one-tricki, na co uważać.
Wyraźnie rozróżniaj gry z SoloQ i turniejowe (kody turniejowe): turniejowe lepiej pokazują, co grają w meczach drużynowych.
Opieraj się wyłącznie na podanych danych, nie wymyślaj. Każdy punkt w osobnej linii zaczynającej się od "• ", razem najwyżej 700 znaków."""

_scout_lock = asyncio.Lock()
_pro_meta_cache: dict[int, tuple[float, dict]] = {}
_opponents_cache: tuple[float, dict] | None = None


def riot_id_from_slug(slug: str, allow_dash: bool = False) -> str | None:
    """A Riot ID from "Name#Tag" (or "Name-Tag" in op.gg profile URLs, with allow_dash)."""
    slug = unquote(slug).strip()
    separator = "#" if "#" in slug else "-" if allow_dash and "-" in slug else None
    if separator is None:
        return None
    name, tag = (part.strip() for part in slug.rsplit(separator, 1))
    return f"{name}#{tag}" if name and tag else None


def parse_players(text: str) -> list[str]:
    """
    Riot IDs from a multi op.gg link (or op.gg profile links) and/or a list separated by
    commas, semicolons or new lines.
    """
    riot_ids = []
    urls = re.findall(r"https?://\S+", text)
    for url in urls:
        parsed = urlparse(url)
        summoners = parse_qs(parsed.query).get("summoners")
        if summoners:
            riot_ids += [riot_id_from_slug(s) for s in summoners[0].split(",")]
        elif "/summoners/" in parsed.path:
            riot_ids.append(riot_id_from_slug(parsed.path.rstrip("/").split("/")[-1], allow_dash=True))
        text = text.replace(url, "\n")
    riot_ids += [riot_id_from_slug(part) for part in re.split(r"[,;\n]", text)]

    unique = {}
    for riot_id in riot_ids:
        if riot_id:
            unique.setdefault(riot_id.lower(), riot_id)
    return list(unique.values())


def season_of(game_version: str | None) -> int | None:
    """Season year of a game from its version: "16.21.712.4512" -> 2026."""
    try:
        major = int((game_version or "").split(".")[0])
    except ValueError:
        return None
    return SEASON_OFFSET + major


def maps_count(n: int) -> str:
    return plural(n, "mapa", "mapy", "map")


def pct(value: float | None) -> str:
    return f"{round(value * 100)}%" if value is not None else ""


def champion_key(name: str) -> str:
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    return CHAMPION_ALIASES.get(key, key)


class _ChampionTableParser(HTMLParser):
    """Rows (cell texts) of gol.gg's champion table, the one with class "table_list"."""

    def __init__(self):
        super().__init__()
        self.in_table = False
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table" and "table_list" in (dict(attrs).get("class") or ""):
            self.in_table = True
        elif self.in_table and tag == "tr":
            self._row = []
        elif self.in_table and tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if not self.in_table:
            return
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None
        elif tag == "table":
            self.in_table = False

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_percent(text: str) -> float | None:
    match = re.match(r"(-?\d+(?:\.\d+)?)%", text or "")
    return float(match.group(1)) / 100 if match else None


def parse_pro_meta(html: str) -> dict[str, dict]:
    """gol.gg champion stats by champion_key: picks, bans, priority score and winrate."""
    parser = _ChampionTableParser()
    parser.feed(html)
    if not parser.rows:
        return {}
    header = parser.rows[0]
    needed = ("Champion", "Picks", "Bans", "PrioScore", "Winrate")
    if any(name not in header for name in needed):
        print(f"gol.gg champion table has unexpected columns: {header}")
        return {}
    column = {name: header.index(name) for name in needed}

    meta = {}
    for row in parser.rows[1:]:
        if len(row) < len(header):
            continue
        name = row[column["Champion"]]
        meta[champion_key(name)] = {
            "champion": name,
            "picks": int(re.sub(r"\D", "", row[column["Picks"]]) or 0),
            "bans": int(re.sub(r"\D", "", row[column["Bans"]]) or 0),
            "prio": parse_percent(row[column["PrioScore"]]),
            "wr": parse_percent(row[column["Winrate"]]),
        }
    return meta


async def fetch_pro_meta(season: int) -> dict[str, dict]:
    """Pro-play champion stats of a season from gol.gg, cached for a day. Empty when unavailable."""
    major = season - SEASON_OFFSET
    cached = _pro_meta_cache.get(major)
    if cached and time.time() - cached[0] < PRO_META_TTL:
        return cached[1]
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60), headers={"User-Agent": "LP-Tracker (team Discord bot)"}
        ) as session:
            async with session.get(GOLGG_URL.format(season=major)) as response:
                if response.status != 200:
                    print(f"gol.gg returned {response.status}")
                    return cached[1] if cached else {}
                html = await response.text()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"Could not load pro meta from gol.gg: {e}")
        return cached[1] if cached else {}
    meta = parse_pro_meta(html)
    _pro_meta_cache[major] = (time.time(), meta)
    return meta


def scout_rows(match_id: str, match_data: dict, puuids: set[str]) -> list[dict]:
    """
    scout_games rows for the scouted players in a game. Remakes get win None and games
    off Summoner's Rift season None, so they're stored (not fetched again) but not counted.
    """
    info = match_data.get("info", {})
    on_rift = info.get("mapId") == SUMMONERS_RIFT
    season = season_of(info.get("gameVersion")) if on_rift else None
    rows = []
    for p in info.get("participants", []):
        if p.get("puuid") not in puuids:
            continue
        remake = p.get("gameEndedInEarlySurrender", False)
        rows.append({
            "puuid": p["puuid"],
            "match_id": match_id,
            "queue_id": info.get("queueId", 0),
            "game_start": info.get("gameStartTimestamp") or info.get("gameCreation", 0),
            "season": season,
            "champion": champions.name(p.get("championId"), p.get("championName")),
            "role": POSITIONS.get(p.get("teamPosition") or p.get("individualPosition"), ""),
            "win": None if remake else bool(p.get("win")),
            "kills": p.get("kills", 0),
            "deaths": p.get("deaths", 0),
            "assists": p.get("assists", 0),
        })
    return rows


def ranked_policy(platform: str, solo: dict | None) -> dict:
    """
    Which ranked games count for an account: {"queues", "label", "skipped"}. SoloQ on EUW;
    on other servers SoloQ only from Master up, otherwise none (skipped says why).
    """
    if platform == MAIN_PLATFORM or (solo and (solo.get("tier") or "").upper() in APEX_TIERS):
        return {"queues": {SOLOQ_QUEUE}, "label": "SoloQ", "skipped": None}
    server = PLATFORM_NAMES.get(platform, platform.upper())
    return {"queues": set(), "label": "SoloQ", "skipped": f"nie liczę, konto {server} poniżej Mastera"}


EUW_POLICY = ranked_policy(MAIN_PLATFORM, None)


async def fetch_match_ids(puuid: str, start_time: int, match_type: str = None, queue: int = None) -> list[str]:
    """All match IDs of one type or queue since start_time (Unix seconds), newest first."""
    ids = []
    while True:
        page = await riot_api.get_match_ids(
            puuid, match_type=match_type, start_time=start_time, start=len(ids), count=IDS_PAGE, queue=queue
        )
        ids += page
        if len(page) < IDS_PAGE:
            return ids


async def new_match_ids(puuid: str, since: int, ranked_queues: set[int], tourney_since: int = None) -> list[str]:
    """
    An account's games in ranked_queues since a Unix time and tournament-code games since
    tourney_since (default: the same time) that aren't stored yet.
    """
    known = await db.get_scout_match_ids(puuid)
    ids = []
    for queue in sorted(ranked_queues):
        ids += await fetch_match_ids(puuid, since, queue=queue)
    ids += await fetch_match_ids(puuid, tourney_since or since, match_type="tourney")
    return [match_id for match_id in dict.fromkeys(ids) if match_id not in known]


async def store_scout_games(match_ids: list[str], scouted: set[str]) -> None:
    """Fetch games and store them for every scouted account in them, so a shared game is fetched once."""
    for match_id in match_ids:
        try:
            match_data = await riot_api.get_match_details(match_id)
        except RiotAPIError as e:
            if e.status == 404:
                continue
            raise
        await db.save_scout_games(scout_rows(match_id, match_data, scouted))


def champion_table(games: list[dict], pro_meta: dict) -> list[dict]:
    """Champions played, most games first: games, wins, losses, winrate, KDA and pro stats."""
    played = defaultdict(lambda: {"games": 0, "wins": 0, "kills": 0, "deaths": 0, "assists": 0})
    for game in games:
        stats = played[game["champion"]]
        stats["games"] += 1
        stats["wins"] += bool(game["win"])
        for key in ("kills", "deaths", "assists"):
            stats[key] += game[key] or 0
    table = []
    for champion, s in played.items():
        pro = pro_meta.get(champion_key(champion), {})
        table.append({
            "champion": champion,
            "games": s["games"],
            "wins": s["wins"],
            "losses": s["games"] - s["wins"],
            "wr": s["wins"] / s["games"],
            "kda": round((s["kills"] + s["assists"]) / max(s["deaths"], 1), 2),
            "pro_prio": pro.get("prio"),
            "pro_wr": pro.get("wr"),
        })
    return sorted(table, key=lambda c: (-c["games"], -c["wins"], c["champion"]))


def recent_months(months: int) -> int | None:
    """
    The shorter SoloQ period shown next to the whole one: 3 months, 1 month for a 2-3 month
    period, none for a single month.
    """
    if months > RECENT_MONTHS:
        return RECENT_MONTHS
    return 1 if months > 1 else None


def summarize(games: list[dict], now: int, months: int, pro_meta: dict, policy: dict = EUW_POLICY,
              tourney_months: int = None) -> dict:
    """
    An account's champions in ranked (the queues its policy allows) over the last months and
    the most recent ones, in tournament-code games over tourney_months (default: months),
    its main role and a one-trick warning. now is Unix seconds.
    """
    tourney_months = tourney_months or months
    recent = recent_months(months)
    since_ms = (now - months * MONTH_SECONDS) * 1000
    tourney_since_ms = (now - tourney_months * MONTH_SECONDS) * 1000
    counted = [g for g in games if g["win"] is not None and g["season"]]
    ranked = [g for g in counted if g["queue_id"] in policy["queues"] and g["game_start"] >= since_ms]
    recent_games = [g for g in ranked if recent and g["game_start"] >= (now - recent * MONTH_SECONDS) * 1000]
    tournament = [
        g for g in counted
        if g["queue_id"] not in RANKED_QUEUES and g["queue_id"] != CLASH_QUEUE and g["game_start"] >= tourney_since_ms
    ]

    roles = Counter(g["role"] for g in ranked + tournament if g["role"])
    role, role_games = roles.most_common(1)[0] if roles else ("", 0)
    ranked_table = champion_table(ranked, pro_meta)
    one_trick = None
    if ranked_table and len(ranked) >= ONE_TRICK_MIN_GAMES and ranked_table[0]["games"] / len(ranked) >= ONE_TRICK_SHARE:
        one_trick = {"champion": ranked_table[0]["champion"], "share": ranked_table[0]["games"] / len(ranked)}
    return {
        "months": months,
        "recent_months": recent,
        "tourney_months": tourney_months,
        "ranked_label": policy["label"],
        "ranked_skipped": policy["skipped"],
        "ranked": ranked_table,
        "recent": champion_table(recent_games, pro_meta),
        "tournament": champion_table(tournament, pro_meta),
        "ranked_games": len(ranked),
        "recent_games": len(recent_games),
        "tournament_games": len(tournament),
        "role": role,
        "role_share": role_games / sum(roles.values()) if roles else 0,
        "one_trick": one_trick,
    }


def ranked_entries(entries: list[dict]) -> dict:
    """{"solo": entry, "flex": entry} from league-v4 entries."""
    queues = {"RANKED_SOLO_5x5": "solo", "RANKED_FLEX_SR": "flex"}
    return {queues[e["queueType"]]: e for e in entries if e.get("queueType") in queues}


def rank_text(entry: dict | None) -> str:
    if not entry:
        return "Unranked"
    games = entry.get("wins", 0) + entry.get("losses", 0)
    return (f"{format_tier(entry.get('tier'), entry.get('rank'))} {entry.get('leaguePoints', 0)} LP, "
            f"{entry.get('wins', 0)}-{entry.get('losses', 0)} ({pct(entry['wins'] / games) if games else '-'})")


def player_name(player: dict) -> str:
    return player["riot_id"].split("#")[0]


def ban_suggestions(players: list[dict]) -> list[dict]:
    """
    Champions worth banning: players' comfort picks in the period, weighted by games and a
    smoothed winrate, with tournament games counting TOURNEY_WEIGHT times.
    """
    candidates = []
    for player in players:
        summary = player.get("summary")
        if not summary:
            continue
        tourney = {c["champion"]: c for c in summary["tournament"]}
        ranked = {c["champion"]: c for c in summary["ranked"]}
        for champion in set(ranked) | set(tourney):
            r, t = ranked.get(champion), tourney.get(champion)
            if (r is None or r["games"] < MIN_BAN_GAMES) and (t is None or t["games"] < MIN_BAN_TOURNEY_GAMES):
                continue
            games = (r["games"] if r else 0) + TOURNEY_WEIGHT * (t["games"] if t else 0)
            wins = (r["wins"] if r else 0) + TOURNEY_WEIGHT * (t["wins"] if t else 0)
            reasons = []
            if r:
                reasons.append(f"{summary['ranked_label']}: {games_count(r['games'])}, {pct(r['wr'])} WR")
            if t:
                reasons.append(f"turnieje: {games_count(t['games'])}, {pct(t['wr'])} WR")
            candidates.append({
                "champion": champion,
                "player": player_name(player),
                "role": player["summary"]["role"],
                "score": games * (wins + 1) / (games + 2),
                "reason": " · ".join(reasons),
                "ranked_label": summary["ranked_label"],
                "ranked_games": r["games"] if r else 0,
                "ranked_wr": r["wr"] if r else None,
                "tourney_games": t["games"] if t else 0,
                "tourney_wr": t["wr"] if t else None,
                "pro_prio": (r or t)["pro_prio"],
                "pro_wr": (r or t)["pro_wr"],
            })
    suggestions, seen = [], set()
    for candidate in sorted(candidates, key=lambda c: -c["score"]):
        if candidate["champion"] not in seen:
            seen.add(candidate["champion"])
            suggestions.append(candidate)
    return suggestions[:BAN_SUGGESTIONS]


async def history_against_us(team_name: str) -> dict:
    """
    What the team did in our games against it (opponent name from "Mecze"): their Riot IDs
    by games played, champions picked, bans, and our record.
    """
    history = {"games": 0, "wins": 0, "players": Counter(), "picks": Counter(), "bans": Counter()}
    if not GOOGLE_SHEET_ID:
        return history
    try:
        rows = await team_sheet.read_matches()
    except Exception as e:
        print(f"Scouting: could not read Mecze: {e}")
        return history
    if not rows or "Przeciwnik" not in rows[0] or "Mecz (do Map)" not in rows[0]:
        return history
    opponent, label = rows[0].index("Przeciwnik"), rows[0].index("Mecz (do Map)")
    labels = {
        row[label] for row in rows[1:]
        if max(opponent, label) < len(row) and str(row[opponent]).strip().lower() == team_name.strip().lower()
    }
    for game in await db.get_team_games():
        if game["match_label"] not in labels:
            continue
        history["games"] += 1
        history["wins"] += bool(game["win"])
        history["players"].update(p["riot_id"] for p in game["enemy"])
        history["picks"].update(p["champion"] for p in game["enemy"])
        history["bans"].update(game["enemy_bans"])
    return history


def opponents_tab_updates(team_names: list[str]) -> list[dict]:
    """Title, instructions, headers and the given team names for a new "Przeciwnicy" tab."""
    updates = [
        {"range": "A1", "values": [["Przeciwnicy"]]},
        {"range": "A2", "values": [[
            "Jedna drużyna na wiersz: nazwa jak w kolumnie Przeciwnik w Meczach i linki multi op.gg z EUW i EUNE "
            "(albo Riot ID po przecinku). /scout i scouting przed oficjalami biorą graczy stąd."
        ]]},
        {"range": "A4:D4", "values": [OPPONENTS_HEADERS]},
    ]
    if team_names:
        last = OPPONENTS_FIRST_ROW + len(team_names) - 1
        updates.append({"range": f"A{OPPONENTS_FIRST_ROW}:A{last}", "values": [[name] for name in team_names]})
    return updates


def parse_opponent_rows(rows: list[list]) -> dict[str, dict]:
    """Teams from raw "Przeciwnicy" rows (header row first) by lowercase name: {"name", "players"}."""
    if not rows:
        return {}
    header = [str(cell).strip() for cell in rows[0]]
    if "Drużyna" not in header:
        print(f'"{OPPONENTS_TAB}" has no "Drużyna" column')
        return {}
    name_column = header.index("Drużyna")
    link_columns = [header.index(name) for name in ("Multi EUW", "Multi EUNE") if name in header]
    teams = {}
    for row in rows[1:]:
        name = str(row[name_column]).strip() if name_column < len(row) else ""
        links = " ".join(str(row[i]) for i in link_columns if i < len(row) and row[i])
        players = parse_players(links)
        if name and players:
            teams[name.lower()] = {"name": name, "players": players}
    return teams


async def opponent_team_names() -> list[str]:
    """Opponents in "Mecze", in order of first appearance, to pre-fill a new "Przeciwnicy" tab."""
    rows = await team_sheet.read_matches()
    if not rows or "Przeciwnik" not in rows[0]:
        return []
    column = rows[0].index("Przeciwnik")
    names = {}
    for row in rows[1:]:
        name = str(row[column]).strip() if column < len(row) else ""
        if name:
            names.setdefault(name.lower(), name)
    return list(names.values())


async def read_opponents_tab() -> dict[str, dict]:
    """
    Teams from the "Przeciwnicy" tab (cached for a few minutes); adds the tab, pre-filled
    with the opponents from "Mecze", when it's missing.
    """
    global _opponents_cache
    if not GOOGLE_SHEET_ID:
        return {}
    if _opponents_cache and time.monotonic() - _opponents_cache[0] < OPPONENTS_CACHE_SECONDS:
        return _opponents_cache[1]
    try:
        teams = parse_opponent_rows(await team_sheet.read_tab(OPPONENTS_TAB))
    except gspread.WorksheetNotFound:
        names = await opponent_team_names()
        await team_sheet.create_tab(OPPONENTS_TAB, opponents_tab_updates(names), 100, len(OPPONENTS_HEADERS), frozen_rows=4)
        print(f'Added the "{OPPONENTS_TAB}" tab')
        teams = {}
    except Exception as e:
        print(f'Could not read "{OPPONENTS_TAB}": {e}')
        return _opponents_cache[1] if _opponents_cache else {}
    _opponents_cache = (time.monotonic(), teams)
    return teams


async def known_players(team_name: str) -> tuple[list[str], str]:
    """
    The team's Riot IDs and where they came from: the "Przeciwnicy" tab, else saved with
    /scout, else the most frequent opponents in our games against it. Empty when unknown.
    """
    team = (await read_opponents_tab()).get(team_name.strip().lower())
    if team:
        return team["players"], f"zakładka {OPPONENTS_TAB}"
    saved = await db.get_opponent(team_name)
    if saved:
        return saved["players"], "zapisane z /scout"
    history = await history_against_us(team_name)
    if history["players"]:
        return [riot_id for riot_id, _ in history["players"].most_common(7)], f"nasze gry z nimi ({maps_count(history['games'])})"
    return [], ""


async def team_names() -> list[str]:
    """Teams with known players, for autocomplete: the "Przeciwnicy" tab and ones saved with /scout."""
    names = {team["name"].lower(): team["name"] for team in (await read_opponents_tab()).values()}
    for team in await db.get_opponents():
        names.setdefault(team["name"].lower(), team["name"])
    return list(names.values())


async def lookup_account(riot_id: str) -> dict:
    """PUUID, exact Riot ID and platform of an account, or {"error": ...}."""
    if "#" not in riot_id:
        return {"riot_id": riot_id, "error": "Zły format Riot ID, powinno być Nazwa#TAG"}
    name, tag = riot_id.rsplit("#", 1)
    try:
        account = await riot_api.get_account_by_riot_id(name, tag)
        platform = await riot_api.get_account_region(account["puuid"]) or "euw1"
    except RiotAPIError as e:
        return {"riot_id": riot_id, "error": "Nie znaleziono konta" if e.status == 404 else f"Błąd Riot API ({e.status})"}
    return {
        "riot_id": f"{account.get('gameName', name)}#{account.get('tagLine', tag)}",
        "puuid": account["puuid"],
        "platform": platform,
    }


def pool_text(table: list[dict], count: int) -> str:
    return ", ".join(f"{c['champion']} {games_count(c['games'])} {pct(c['wr'])}" for c in table[:count]) or "brak"


def summary_facts(report: dict) -> str:
    """The report as compact text for the model."""
    lines = [f"Drużyna przeciwnika: {report['team']}"]
    for player in report["players"]:
        if player.get("error"):
            continue
        s = player["summary"]
        if s["ranked_skipped"]:
            ranked = f"{s['ranked_label']}: {s['ranked_skipped']}"
        else:
            ranked = f"{s['ranked_label']} w ostatnich {s['months']} mies.: {pool_text(s['ranked'], 5)}"
            if s["recent_months"]:
                ranked += f"; {s['ranked_label']} w ostatnich {s['recent_months']} mies.: {pool_text(s['recent'], 3)}"
        lines.append(
            f"{s['role'] or '?'} {player_name(player)} ({PLATFORM_NAMES.get(player['platform'], player['platform'])}, "
            f"{rank_text(player['ranks'].get('solo'))}): {ranked}; "
            f"turnieje (kody turniejowe) w ostatnich {s['tourney_months']} mies.: {pool_text(s['tournament'], 5)}"
            + (f"; one-trick {s['one_trick']['champion']} ({pct(s['one_trick']['share'])} gier)" if s["one_trick"] else "")
        )
    if report["bans"]:
        lines.append("Sugerowane bany (z danych): " + ", ".join(f"{b['champion']} ({b['player']}: {b['reason']})" for b in report["bans"]))
    history = report["history"]
    if history["games"]:
        lines.append(
            f"Przeciwko nam: {maps_count(history['games'])}, nasz bilans {history['wins']}-{history['games'] - history['wins']}, "
            f"grali: {', '.join(c for c, _ in history['picks'].most_common(8))}; "
            f"banowali: {', '.join(c for c, _ in history['bans'].most_common(5)) or 'brak danych'}"
        )
    if report["pro_meta"]:
        top = sorted(report["pro_meta"].values(), key=lambda c: -(c["prio"] or 0))[:10]
        lines.append("Najwyższy priorytet w grze pro (gol.gg): " + ", ".join(f"{c['champion']} {pct(c['prio'])}" for c in top))
    return "\n".join(lines)


async def run_scouting(team_name: str, riot_ids: list[str], months: int = SCOUT_MONTHS, progress=None,
                       tourney_months: int = None) -> dict:
    """
    Collect the scouting report of a team from SoloQ games of the last months and tournament
    games of the last tourney_months (default: months), one scouting at a time. Games are
    fetched only once, so the first scouting of a team takes a while; progress (async,
    optional) gets the number of games to fetch before fetching starts.
    """
    tourney_months = tourney_months or months
    async with _scout_lock:
        await champions.refresh()
        now = datetime.now(TIMEZONE)
        year = now.year
        since = int(now.timestamp()) - months * MONTH_SECONDS
        tourney_since = int(now.timestamp()) - tourney_months * MONTH_SECONDS
        pro_meta = await fetch_pro_meta(year)

        players = [await lookup_account(riot_id) for riot_id in riot_ids]
        accounts = [p for p in players if not p.get("error")]
        for player in accounts:
            try:
                player["ranks"] = ranked_entries(await riot_api.get_league_entries(player["puuid"], player["platform"]))
            except RiotAPIError as e:
                print(f"Scouting: no ranks for {player['riot_id']}: {e}")
                player["ranks"] = {}
            player["policy"] = ranked_policy(player["platform"], player["ranks"].get("solo"))

        to_fetch = {}
        for player in accounts:
            to_fetch.update(dict.fromkeys(
                await new_match_ids(player["puuid"], since, player["policy"]["queues"], tourney_since)
            ))
        if progress:
            await progress(len(to_fetch))
        await store_scout_games(list(to_fetch), {p["puuid"] for p in accounts})

        for player in accounts:
            games = await db.get_scout_games(player["puuid"], min(since, tourney_since) * 1000)
            player["summary"] = summarize(games, int(now.timestamp()), months, pro_meta, player["policy"], tourney_months)

        role_order = {role: i for i, role in enumerate(ROLES)}
        players.sort(key=lambda p: role_order.get((p.get("summary") or {}).get("role"), len(ROLES)))
        report = {
            "team": team_name,
            "year": year,
            "months": months,
            "tourney_months": tourney_months,
            "created": datetime.now(TIMEZONE),
            "players": players,
            "fetched": len(to_fetch),
            "pro_meta": pro_meta,
            "history": await history_against_us(team_name),
        }
        report["bans"] = ban_suggestions(players)
        report["summary"] = None
        if llm.enabled() and any(p.get("summary") for p in players):
            report["summary"] = await llm.complete(SUMMARY_PROMPT, summary_facts(report), max_tokens=1500, temperature=0.4)
        return report


def champion_line(table: list[dict], count: int) -> str:
    return ", ".join(f"{c['champion']} {c['games']} ({pct(c['wr'])})" for c in table[:count]) or "brak gier"


def build_embed(report: dict) -> discord.Embed:
    """
    Short version of the report for Discord (full tables go to the sheet), with fewer
    champions per line when many accounts (e.g. EUW and EUNE ones) make it too long.
    """
    for count in EMBED_CHAMPIONS:
        embed = embed_with(report, count)
        if len(embed) <= EMBED_MAX_LENGTH:
            return embed
    if embed.description:
        room = max(0, EMBED_MAX_LENGTH - (len(embed) - len(embed.description)))
        embed.description = embed.description[:room] or None
    return embed


def embed_with(report: dict, count: int) -> discord.Embed:
    year = report["year"]
    embed = discord.Embed(
        title=f"🔎 Scouting: {report['team']}"[:256],
        color=discord.Color.purple(),
        description=(report["summary"] or "")[:2000] or None,
        timestamp=report["created"],
    )
    for player in report["players"][:10]:
        if player.get("error"):
            embed.add_field(name=player["riot_id"][:256], value=f"⚠ {player['error']}", inline=False)
            continue
        s = player["summary"]
        name = f"{s['role'] or '?'} · {player['riot_id']} · {PLATFORM_NAMES.get(player['platform'], player['platform'].upper())}"
        lines = [rank_text(player["ranks"].get("solo"))]
        if s["ranked_skipped"]:
            lines.append(f"**{s['ranked_label']}:** {s['ranked_skipped']}")
        else:
            lines.append(f"**{s['ranked_label']}, {s['months']} mies.:** {champion_line(s['ranked'], count)}")
            if s["recent_months"]:
                lines.append(f"**{s['ranked_label']}, {s['recent_months']} mies.:** {champion_line(s['recent'], count)}")
        lines.append(f"**Turnieje, {s['tourney_months']} mies.:** {champion_line(s['tournament'], count)}")
        if s["one_trick"]:
            lines.append(f"⚠ One-trick: {s['one_trick']['champion']} ({pct(s['one_trick']['share'])} gier w {s['months']} mies.)")
        embed.add_field(name=name[:256], value="\n".join(lines)[:1024], inline=False)

    if report["bans"]:
        embed.add_field(
            name="🚫 Sugerowane bany",
            value="\n".join(f"**{b['champion']}** ({b['player']}) {b['reason']}" for b in report["bans"])[:1024],
            inline=False,
        )
    history = report["history"]
    if history["games"]:
        embed.add_field(
            name="Przeciwko nam",
            value=(
                f"{maps_count(history['games'])}, nasz bilans {history['wins']}-{history['games'] - history['wins']}\n"
                f"Grali: {', '.join(c for c, _ in history['picks'].most_common(8))}\n"
                f"Banowali: {', '.join(c for c, _ in history['bans'].most_common(5)) or 'brak danych'}"
            )[:1024],
            inline=False,
        )
    embed.set_footer(text=(
        f"SoloQ z {report['months']} mies., gry turniejowe z {report['tourney_months']} mies. · "
        f"pro: gol.gg S{year - SEASON_OFFSET}"
        + (" · pełne tabele w zakładce Scouting" if GOOGLE_SHEET_ID else "")
    ))
    return embed


def champion_block_rows(table: list[dict]) -> list[list]:
    return [
        [c["champion"], c["games"], c["wins"], c["losses"], pct(c["wr"]), c["kda"], pct(c["pro_prio"]), pct(c["pro_wr"])]
        for c in table[:SHEET_CHAMPIONS]
    ]


def build_sheet_rows(report: dict) -> list[list]:
    """All rows of the Scouting tab, each SHEET_COLS wide."""
    year = report["year"]
    grid: list[list] = []

    def add(*cells_at: tuple[int, list]):
        row = [""] * SHEET_COLS
        for column, cells in cells_at:
            row[column:column + len(cells)] = cells
        grid.append(row)

    add((0, [f"Scouting: {report['team']}"]))
    add((0, [
        f"Wygenerowano {report['created']:%d.%m.%Y %H:%M} · SoloQ z ostatnich {report['months']} mies. "
        f"(konta spoza EUW tylko od Mastera) i osobno gry z kodów turniejowych z ostatnich {report['tourney_months']} mies. · "
        f"pro: gol.gg S{year - SEASON_OFFSET}, wszystkie turnieje"
    ]))
    add()
    add((0, ["Sugerowane bany"]))
    add((0, ["Champion", "Gracz", "Rola", "Ranked", "Ranked gry", "Ranked WR", "Turnieje gry", "Turnieje WR", "Pro prio", "Pro WR"]))
    for ban in report["bans"]:
        add((0, [
            ban["champion"], ban["player"], ban["role"], ban["ranked_label"], ban["ranked_games"], pct(ban["ranked_wr"]),
            ban["tourney_games"], pct(ban["tourney_wr"]), pct(ban["pro_prio"]), pct(ban["pro_wr"]),
        ]))
    if report["summary"]:
        add()
        add((0, ["Podsumowanie"]))
        for line in report["summary"].splitlines():
            if line.strip():
                add((0, [line.strip()]))
    history = report["history"]
    if history["games"]:
        add()
        add((0, ["Przeciwko nam", f"{maps_count(history['games'])}, nasz bilans {history['wins']}-{history['games'] - history['wins']}"]))
        add((0, ["Grali", ", ".join(f"{c} ({n})" for c, n in history["picks"].most_common())]))
        add((0, ["Banowali", ", ".join(f"{c} ({n})" for c, n in history["bans"].most_common()) or "brak danych"]))

    for player in report["players"]:
        add()
        if player.get("error"):
            add((0, [player["riot_id"], f"⚠ {player['error']}"]))
            continue
        s = player["summary"]
        add((0, [
            f"{s['role'] or '?'} · {player['riot_id']}",
            PLATFORM_NAMES.get(player["platform"], player["platform"].upper()),
            f"SoloQ: {rank_text(player['ranks'].get('solo'))}",
            f"One-trick: {s['one_trick']['champion']} ({pct(s['one_trick']['share'])} gier)" if s["one_trick"] else "",
        ]))
        if s["ranked_skipped"]:
            ranked_titles = [f"{s['ranked_label']}: {s['ranked_skipped']}", ""]
        else:
            ranked_titles = [
                f"{s['ranked_label']}, ostatnie {s['months']} mies. ({games_count(s['ranked_games'])})",
                f"{s['ranked_label']}, ostatnie {s['recent_months']} mies. ({games_count(s['recent_games'])})"
                if s["recent_months"] else "",
            ]
        titles = ranked_titles + [
            f"Turnieje (kody turniejowe), ostatnie {s['tourney_months']} mies. ({games_count(s['tournament_games'])})",
        ]
        blocks = [champion_block_rows(s["ranked"]), champion_block_rows(s["recent"]), champion_block_rows(s["tournament"])]
        add(*[(column, [title]) for column, title in zip(BLOCK_COLUMNS, titles)])
        add(*[(column, BLOCK_HEADERS) for column, block in zip(BLOCK_COLUMNS, blocks) if block])
        for i in range(max(len(block) for block in blocks)):
            add(*[(column, block[i]) for column, block in zip(BLOCK_COLUMNS, blocks) if i < len(block)])

    if report["pro_meta"]:
        add()
        add((0, [f"Meta pro (gol.gg S{year - SEASON_OFFSET}, wszystkie turnieje): top {PRO_META_ROWS} wg priorytetu"]))
        add((0, ["Champion", "Picki", "Bany", "Prio", "WR"]))
        for c in sorted(report["pro_meta"].values(), key=lambda c: -(c["prio"] or 0))[:PRO_META_ROWS]:
            add((0, [c["champion"], c["picks"], c["bans"], pct(c["prio"]), pct(c["wr"])]))
    return grid


async def write_sheet(report: dict) -> None:
    rows = build_sheet_rows(report)
    await team_sheet.replace_tab(
        SCOUTING_TAB, [{"range": f"A1:Z{len(rows)}", "values": rows}], len(rows), SHEET_COLS
    )


def fetch_estimate(games: int) -> str:
    """How long fetching games takes, e.g. "około 12 min"."""
    return f"około {max(1, round(games * SECONDS_PER_GAME / 60))} min"


async def post_scouting(channel, team_name: str, riot_ids: list[str], months: int = SCOUT_MONTHS,
                        announce_progress: bool = False, tourney_months: int = None) -> None:
    """Run the scouting, write the Scouting tab and post the report to the channel."""
    async def progress(games: int) -> None:
        if games > 50:
            await channel.send(
                f"⏳ {discord.utils.escape_markdown(team_name)}: do pobrania {games_count(games)}, {fetch_estimate(games)}."
            )

    try:
        report = await run_scouting(team_name, riot_ids, months, progress if announce_progress else None, tourney_months)
    except Exception as e:
        print(f"Scouting of {team_name} failed: {e}")
        await channel.send(f"❌ Scouting drużyny **{discord.utils.escape_markdown(team_name)}** nie wyszedł: {e}")
        return
    note = ""
    if GOOGLE_SHEET_ID:
        try:
            await write_sheet(report)
        except Exception as e:
            print(f"Could not write the Scouting tab: {e}")
            note = "⚠ Nie udało się zapisać zakładki Scouting."
    await channel.send(note or None, embed=build_embed(report))
    print(f"Scouted {team_name}: {len(riot_ids)} players, {report['fetched']} games fetched")


def start_scouting(channel, team_name: str, riot_ids: list[str], months: int = SCOUT_MONTHS,
                   announce_progress: bool = False, tourney_months: int = None) -> asyncio.Task:
    """Scout in the background (it can take a while) and post the report when done."""
    return asyncio.create_task(post_scouting(channel, team_name, riot_ids, months, announce_progress, tourney_months))


def is_running() -> bool:
    return _scout_lock.locked()
