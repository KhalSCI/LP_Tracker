"""
Opponent scouting for draft prep: each account's champions in ranked and tournament-code
games of the last months (games, winrate, KDA), from the Riot API: SoloQ of EUW accounts
(on other servers like EUNE only from Master up) and tournament-code games, kept apart,
with pro-play priority from gol.gg, suggested bans and what they played against us.
Opponents' accounts come from the "Przeciwnicy" tab (multi op.gg links per team).
Posted to Discord and written to the bot-only "Scouting" tab.
"""
import asyncio
import difflib
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
from sheet_format import BAND_BACKGROUND, GRAY, grid_range, cell_format, table_format, column_widths
from sheets import team_sheet

SCOUTING_TAB = "Scouting"
# Filled in by hand: one row per team (name as in the "Przeciwnik" column of "Mecze"), headers in row 4
OPPONENTS_TAB = "Przeciwnicy"
# Role columns are optional: a player's accounts per role, when the automatic split gets them wrong
OPPONENTS_HEADERS = ["Drużyna", "Multi EUW", "Multi EUNE", "Notatki", "Top", "Jungle", "Mid", "ADC", "Support"]
OPPONENTS_FIRST_ROW = 5
OPPONENTS_INSTRUCTIONS = (
    "Jedna drużyna na wiersz: nazwa jak w kolumnie Przeciwnik w Meczach i linki multi op.gg z EUW i EUNE "
    "(albo Riot ID po przecinku), najlepiej w kolejności Top, Jungle, Mid, ADC, Support. /scout i scouting przed "
    "oficjalami biorą graczy stąd i same rozdzielają konta na 5 graczy. Opcjonalnie w kolumnach Top-Support wpisz "
    "konta danego gracza (Riot ID albo sama nazwa, po przecinku), jeśli bot rozdzieli je źle."
)
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
DAY_MS = 24 * 60 * 60 * 1000
# Besides the whole period, the report shows the most recent months on their own
RECENT_MONTHS = 3
# Periods offered by /scout as (SoloQ months, tournament months); "quick" is fast to fetch
PERIODS = {"quick": (1, 12), "3": (3, 3), "6": (6, 6), "12": (12, 12)}
# Rough time to fetch one game with the Riot API rate limit (100 requests per 2 minutes)
SECONDS_PER_GAME = 1.3
GOLGG_URL = "https://gol.gg/champion/list/season-S{season}/split-ALL/tournament-ALL/"
PRO_META_TTL = 24 * 60 * 60
# Pro play counts only from the newest patches; older metas don't apply
PRO_META_PATCHES = 3
# A pro winrate needs at least this many picks to be shown
MIN_PRO_PICKS_FOR_WR = 5
PRO_META_ROWS = 20
EMPTY_PRO_META = {"patches": [], "games": 0, "champions": {}}
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
# Splitting a team's accounts into five players: role evidence from games (tournament games
# count TOURNEY_ROLE_WEIGHT times, as SoloQ roles are often off-role), confident from
# ROLE_CONFIDENT_GAMES weighted games; accounts with less than ROLE_PROBE_BELOW get their last
# ROLE_PROBE_GAMES SoloQ games fetched just to see their role
TOURNEY_ROLE_WEIGHT = 3
# Roles come from recent games when there are enough: SoloQ of the last ROLE_SOLOQ_DAYS and
# tournaments of the last ROLE_TOURNEY_DAYS (all stored games otherwise)
ROLE_SOLOQ_DAYS = 30
ROLE_TOURNEY_DAYS = 90
ROLE_CONFIDENT_GAMES = 10
ROLE_PROBE_BELOW = 5
ROLE_PROBE_GAMES = 10
# Signals that two accounts are one player (added to the role fit); playing in the same game
# means two people, so it outweighs everything else
SIMILAR_NAME_BONUS = 0.6
SAME_SERVER_PENALTY = 0.4
SAME_GAME_PENALTY = 5.0
SIMILAR_NAME_RATIO = 0.6
# A server's list of five accounts in role order (Top to Support) says each account's role
# and, with both lists in order, which EUW and EUNE accounts go together. A list is taken as
# in order unless an account with ORDER_CHECK_GAMES tournament games plays its position's role
# in less than ORDER_MIN_SHARE of them.
LIST_ROLE_PRIOR = 0.15
SAME_LIST_POSITION_BONUS = 0.3
ORDER_CHECK_GAMES = 5
ORDER_MIN_SHARE = 0.25
# Search limit for the split (10 accounts take far less)
ASSIGNMENT_NODE_LIMIT = 2_000_000
# gol.gg writes some champions differently than Data Dragon
CHAMPION_ALIASES = {"nunuwillump": "nunu"}
# The three champion blocks per player in the sheet start in columns A, J and S
BLOCK_STARTS = (0, 9, 18)
# Sheet tables' columns as (header, kind), kinds as in sheet_format; long names go last so they can overflow
BLOCK_COLUMNS = [("Champion", "name"), ("Gry", "int"), ("W", "int"), ("L", "int"), ("WR", "wr"), ("KDA", "dec2"),
                 ("Pro P+B", "pct"), ("Pro WR", "pct")]
BAN_COLUMNS = [("Champion", "name"), ("SoloQ gry", "int"), ("SoloQ WR", "wr"), ("Turnieje gry", "int"),
               ("Turnieje WR", "wr"), ("Pro P+B", "pct"), ("Pro WR", "pct"), ("Rola", "text"), ("Gracz", "text")]
PRO_COLUMNS = [("Champion", "name"), ("Picki", "int"), ("Bany", "int"), ("P+B", "pct"), ("WR", "pct")]
SHEET_COLS = 26
COLUMN_WIDTHS = {start: 130 for start in BLOCK_STARTS}
DEFAULT_WIDTH = 62

SUMMARY_PROMPT = """Jesteś analitykiem draftu w uczelnianej drużynie League of Legends.
Dostajesz dane o drużynie przeciwnika: pule championów graczy z rankedów z ostatnich miesięcy, gry turniejowe, sugerowane bany i obecność championów w draftach gry profesjonalnej z ostatnich patchy.
Gracz może mieć dwa konta (EUW i EUNE); wtedy jego gry są już zsumowane.
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


def parse_patches(html: str) -> list[str]:
    """Patches in gol.gg's "Game version" filter, newest first, as its values like "16.20."."""
    select = re.search(r"<select id='patch'.*?</select>", html, re.S)
    return list(dict.fromkeys(re.findall(r"<option value='(\d[\d.]*)'", select.group(0)))) if select else []


def parse_champion_table(html: str) -> dict[str, dict]:
    """gol.gg champion stats by champion_key: picks, bans, wins and losses."""
    parser = _ChampionTableParser()
    parser.feed(html)
    if not parser.rows:
        return {}
    header = parser.rows[0]
    needed = ("Champion", "Picks", "Bans", "Wins", "Losses")
    if any(name not in header for name in needed):
        print(f"gol.gg champion table has unexpected columns: {header}")
        return {}
    column = {name: header.index(name) for name in needed}
    return {
        champion_key(row[column["Champion"]]): {
            "champion": row[column["Champion"]],
            **{name.lower(): int(re.sub(r"\D", "", row[column[name]]) or 0) for name in needed[1:]},
        }
        for row in parser.rows[1:] if len(row) >= len(header)
    }


def combine_pro_tables(tables: list[dict]) -> tuple[dict[str, dict], int]:
    """
    Champion stats summed over patches, with presence (picks and bans per game) and winrate
    (from MIN_PRO_PICKS_FOR_WR picks), and the number of games (ten picks each).
    """
    totals = {}
    for table in tables:
        for key, stats in table.items():
            total = totals.setdefault(key, {"champion": stats["champion"], "picks": 0, "bans": 0, "wins": 0, "losses": 0})
            for name in ("picks", "bans", "wins", "losses"):
                total[name] += stats[name]
    games = sum(total["picks"] for total in totals.values()) // 10
    champions = {
        key: {
            "champion": t["champion"],
            "picks": t["picks"],
            "bans": t["bans"],
            "presence": (t["picks"] + t["bans"]) / games if games else None,
            "wr": t["wins"] / (t["wins"] + t["losses"]) if t["picks"] >= MIN_PRO_PICKS_FOR_WR and t["wins"] + t["losses"] else None,
        }
        for key, t in totals.items()
    }
    return champions, games


async def fetch_pro_meta(season: int) -> dict:
    """
    Pro-play champion stats of the season's last PRO_META_PATCHES patches from gol.gg, cached
    for a day: {"patches", "games", "champions" by champion_key}. gol.gg keeps the patch
    filter in its session, so the patches are posted one by one in one session.
    """
    major = season - SEASON_OFFSET
    cached = _pro_meta_cache.get(major)
    if cached and time.time() - cached[0] < PRO_META_TTL:
        return cached[1]
    url = GOLGG_URL.format(season=major)
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120), headers={"User-Agent": "LP-Tracker (team Discord bot)"}
        ) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                patches = parse_patches(await response.text())[:PRO_META_PATCHES]
            tables = []
            for patch in patches:
                async with session.post(url, data={"patch": patch}) as response:
                    response.raise_for_status()
                    tables.append(parse_champion_table(await response.text()))
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"Could not load pro meta from gol.gg: {e}")
        return cached[1] if cached else EMPTY_PRO_META
    champions, games = combine_pro_tables(tables)
    meta = {"patches": [patch.rstrip(".") for patch in patches], "games": games, "champions": champions}
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
            "pro_presence": pro.get("presence"),
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


def account_name(account: dict) -> str:
    return account["riot_id"].split("#")[0]


def server_name(platform: str) -> str:
    return PLATFORM_NAMES.get(platform, platform.upper())


def is_tourney_game(game: dict) -> bool:
    return game["queue_id"] not in RANKED_QUEUES and game["queue_id"] != CLASH_QUEUE


def role_evidence(games: list[dict], now_ms: int = None) -> Counter:
    """
    Weighted games per role (tournament-code games count TOURNEY_ROLE_WEIGHT times). With
    now_ms, from SoloQ of the last ROLE_SOLOQ_DAYS and tournaments of the last
    ROLE_TOURNEY_DAYS if that's at least ROLE_PROBE_BELOW; otherwise from all games.
    """
    def weighted(selected: list[dict]) -> Counter:
        evidence = Counter()
        for game in selected:
            if game["role"] and game["season"] and game["queue_id"] != CLASH_QUEUE:
                evidence[game["role"]] += TOURNEY_ROLE_WEIGHT if is_tourney_game(game) else 1
        return evidence

    if now_ms is not None:
        recent = weighted([
            g for g in games
            if g["game_start"] >= now_ms - (ROLE_TOURNEY_DAYS if is_tourney_game(g) else ROLE_SOLOQ_DAYS) * DAY_MS
        ])
        if sum(recent.values()) >= ROLE_PROBE_BELOW:
            return recent
    return weighted(games)


def role_fit(evidence: Counter) -> dict[str, float]:
    """How well an account fits each role: its share of games there, scaled down with few games."""
    total = sum(evidence.values())
    if not total:
        return {}
    confidence = min(1.0, total / ROLE_CONFIDENT_GAMES)
    return {role: count / total * confidence for role, count in evidence.items()}


def name_key(riot_id: str) -> str:
    return re.sub(r"[\W_]+", "", riot_id.split("#")[0].lower())


def similar_names(a: str, b: str) -> bool:
    """"iym#0509" and "iym999#0409": one name contains the other, or they're close."""
    a, b = name_key(a), name_key(b)
    if not a or not b:
        return False
    shorter, longer = sorted((a, b), key=len)
    return (len(shorter) >= 3 and shorter in longer) or difflib.SequenceMatcher(None, a, b).ratio() >= SIMILAR_NAME_RATIO


def list_positions(accounts: list[dict]) -> list[int]:
    """Each account's place among the accounts of its server (EUW or not), in the order they were given."""
    seen = Counter()
    positions = []
    for account in accounts:
        server = account["platform"] == MAIN_PLATFORM
        positions.append(seen[server])
        seen[server] += 1
    return positions


def pair_score(a: dict, b: dict, a_games: set[str], b_games: set[str], same_position: bool) -> float:
    """
    How likely two accounts are one player, as a bonus (or penalty) for putting them
    together; same_position: the same place in EUW and EUNE lists that are in role order.
    """
    if a_games & b_games:
        return -SAME_GAME_PENALTY
    score = SIMILAR_NAME_BONUS if similar_names(a["riot_id"], b["riot_id"]) else 0.0
    if (a["platform"] == MAIN_PLATFORM) == (b["platform"] == MAIN_PLATFORM):
        score -= SAME_SERVER_PENALTY
    elif same_position:
        score += SAME_LIST_POSITION_BONUS
    return score


def tourney_roles(games: list[dict], now_ms: int = None) -> Counter:
    """Tournament games per role: of the last ROLE_TOURNEY_DAYS when there are enough, else all."""
    tourney = [g for g in games if g["role"] and g["season"] and is_tourney_game(g)]
    if now_ms is not None:
        recent = [g for g in tourney if g["game_start"] >= now_ms - ROLE_TOURNEY_DAYS * DAY_MS]
        if len(recent) >= ORDER_CHECK_GAMES:
            tourney = recent
    return Counter(g["role"] for g in tourney)


def in_role_order(indexes: list[int], positions: list[int], games: list[list[dict]], now_ms: int = None) -> bool:
    """
    Whether a server's list of five accounts goes Top to Support: yes unless an account with
    ORDER_CHECK_GAMES tournament games plays its position's role in under ORDER_MIN_SHARE of them.
    """
    for i in indexes:
        roles = tourney_roles(games[i], now_ms)
        total = sum(roles.values())
        if total >= ORDER_CHECK_GAMES and roles[ROLES[positions[i]]] / total < ORDER_MIN_SHARE:
            return False
    return True


def assign_roles(accounts: list[dict], games_by_puuid: dict, now_ms: int = None,
                 fixed: dict[int, str] = None) -> dict[str, list[int]]:
    """
    Split a team's accounts into one player per role (as many players as roles, or as
    accounts when fewer; at most ceil(accounts / 5) accounts each), as role -> account
    indexes. fixed: accounts whose role is already known (from the sheet). Picks the split
    with the best total of role fit (recent games) and pair_score within players, so e.g.
    ten accounts become five players of two accounts.
    """
    n = len(accounts)
    if not n:
        return {}
    fixed = fixed or {}
    games = [games_by_puuid.get(a["puuid"], []) for a in accounts]
    fits = [role_fit(role_evidence(g, now_ms)) for g in games]
    played = [{g["match_id"] for g in account_games} for account_games in games]
    positions = list_positions(accounts)
    servers = [a["platform"] == MAIN_PLATFORM for a in accounts]
    ordered = {}
    for server in (True, False):
        indexes = [i for i in range(n) if servers[i] == server]
        ordered[server] = len(indexes) == len(ROLES) and in_role_order(indexes, positions, games, now_ms)
        for i in indexes if ordered[server] else []:
            fits[i][ROLES[positions[i]]] = fits[i].get(ROLES[positions[i]], 0) + LIST_ROLE_PRIOR
    pairs = [
        [pair_score(accounts[i], accounts[j], played[i], played[j],
                    ordered[True] and ordered[False] and positions[i] == positions[j]) if i != j else 0.0
         for j in range(n)]
        for i in range(n)
    ]

    members = {role: [i for i, r in fixed.items() if r == role] for role in ROLES}
    cap = max(-(-n // len(ROLES)), max(len(m) for m in members.values()))
    order = sorted((i for i in range(n) if i not in fixed), key=lambda i: -max(fits[i].values(), default=0))
    best_gain = [max(fits[i].values(), default=0) + max(0.0, max(pairs[i])) * (cap - 1) for i in range(n)]
    remaining = [sum(best_gain[i] for i in order[k:]) for k in range(len(order) + 1)]
    best = {"score": float("-inf"), "split": None}
    nodes = 0

    def search(k: int, score: float) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > ASSIGNMENT_NODE_LIMIT or score + remaining[k] <= best["score"]:
            return
        if k == len(order):
            best["score"], best["split"] = score, {role: list(m) for role, m in members.items() if m}
            return
        i = order[k]
        empty = sum(1 for m in members.values() if not m)
        for role in sorted(ROLES, key=lambda r: -fits[i].get(r, 0)):
            if len(members[role]) >= cap:
                continue
            # With at least five accounts every role needs one, so the roles still empty must not
            # outnumber the accounts still to place
            if n >= len(ROLES) and empty - (0 if members[role] else 1) > len(order) - k - 1:
                continue
            gain = fits[i].get(role, 0) + sum(pairs[i][j] for j in members[role])
            members[role].append(i)
            search(k + 1, score + gain)
            members[role].pop()

    search(0, 0.0)
    return best["split"] or {role: [i] for role, i in zip(ROLES, range(n))}


def merged_summary(accounts: list[dict], games_by_puuid: dict, now: int, months: int, pro_meta: dict,
                   tourney_months: int) -> dict:
    """One summary of a player's accounts, each account's SoloQ counted by its own policy."""
    games = [
        game for account in accounts for game in games_by_puuid[account["puuid"]]
        if game["queue_id"] not in RANKED_QUEUES or game["queue_id"] in account["policy"]["queues"]
    ]
    counted = any(account["policy"]["queues"] for account in accounts)
    policy = {"queues": {SOLOQ_QUEUE} if counted else set(), "label": "SoloQ",
              "skipped": None if counted else accounts[0]["policy"]["skipped"]}
    return summarize(games, now, months, pro_meta, policy, tourney_months)


async def probe_roles(accounts: list[dict], games_by_puuid: dict, now_ms: int = None) -> None:
    """
    Fetch the last ROLE_PROBE_GAMES SoloQ games of accounts with too few recent games to tell
    their role (e.g. an EUNE account whose SoloQ isn't scouted); they're only used for the role.
    """
    scouted = {a["puuid"] for a in accounts}
    probed = False
    for account in accounts:
        if sum(role_evidence(games_by_puuid[account["puuid"]], now_ms).values()) >= ROLE_PROBE_BELOW:
            continue
        try:
            ids = await riot_api.get_match_ids(account["puuid"], queue=SOLOQ_QUEUE, count=ROLE_PROBE_GAMES)
        except RiotAPIError as e:
            print(f"Scouting: no recent games of {account['riot_id']}: {e}")
            continue
        known = {g["match_id"] for g in games_by_puuid[account["puuid"]]}
        new = [match_id for match_id in ids if match_id not in known]
        if new:
            await store_scout_games(new, scouted)
            probed = True
    if probed:
        # A fetched game may have other scouted accounts in it too
        for account in accounts:
            games_by_puuid[account["puuid"]] = await db.get_scout_games(account["puuid"])


def fixed_role_indexes(accounts: list[dict], roles: dict[str, list[str]]) -> dict[int, str]:
    """Accounts named in the sheet's role columns (full Riot ID or just the name), as index -> role."""
    fixed = {}
    for role, names in (roles or {}).items():
        for name in names:
            key = name.strip().lower()
            matches = [
                i for i, a in enumerate(accounts)
                if a["riot_id"].lower() == key or ("#" not in key and account_name(a).lower() == key)
            ]
            if matches:
                fixed[matches[0]] = role
            else:
                print(f"Scouting: no account '{name}' for {role}")
    return fixed


def group_accounts(looked_up: list[dict], games_by_puuid: dict, now: int, months: int, pro_meta: dict,
                   tourney_months: int, roles: dict[str, list[str]] = None) -> list[dict]:
    """
    The report's players: the accounts split into one player per role (assign_roles, with
    the sheet's roles fixed), as {"name", "accounts", "summary"} in role order, each account
    with a "role_note" when it was put outside its usual role; failed lookups as
    {"riot_id", "error"} last.
    """
    accounts = [a for a in looked_up if not a.get("error")]
    now_ms = now * 1000
    fixed = fixed_role_indexes(accounts, roles)
    players = []
    split = assign_roles(accounts, games_by_puuid, now_ms, fixed)
    for role, indexes in sorted(split.items(), key=lambda item: ROLES.index(item[0])):
        group = [accounts[i] for i in indexes]
        group.sort(key=lambda a: (a["platform"] != MAIN_PLATFORM,
                                  -sum(role_evidence(games_by_puuid.get(a["puuid"], []), now_ms).values())))
        for account in group:
            evidence = role_evidence(games_by_puuid.get(account["puuid"], []), now_ms)
            main = evidence.most_common(1)[0][0] if evidence else None
            if accounts.index(account) in fixed:
                account["role_note"] = "rola z arkusza" if main != role else None
            elif not evidence:
                account["role_note"] = "brak gier, rola z dopasowania"
            elif main != role:
                account["role_note"] = f"gra głównie {main}"
            else:
                account["role_note"] = None
        summary = merged_summary(group, games_by_puuid, now, months, pro_meta, tourney_months)
        summary["role"] = role
        players.append({"name": " / ".join(account_name(a) for a in group), "accounts": group, "summary": summary})
    return players + [{"riot_id": a["riot_id"], "error": a["error"]} for a in looked_up if a.get("error")]


def account_text(account: dict, merged: bool) -> str:
    """An account with its server and SoloQ rank; in a merged player also when its SoloQ isn't counted."""
    text = f"{account['riot_id']} ({server_name(account['platform'])}, SoloQ: {rank_text(account['ranks'].get('solo'))}"
    if merged and account["policy"]["skipped"]:
        text += f", {account['policy']['skipped']}"
    if account.get("role_note"):
        text += f", {account['role_note']}"
    return text + ")"


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
                "player": player["name"],
                "role": summary["role"],
                "score": games * (wins + 1) / (games + 2),
                "reason": " · ".join(reasons),
                "ranked_games": r["games"] if r else 0,
                "ranked_wr": r["wr"] if r else None,
                "tourney_games": t["games"] if t else 0,
                "tourney_wr": t["wr"] if t else None,
                "pro_presence": (r or t)["pro_presence"],
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
        {"range": "A2", "values": [[OPPONENTS_INSTRUCTIONS]]},
        {"range": f"A4:{chr(ord('A') + len(OPPONENTS_HEADERS) - 1)}4", "values": [OPPONENTS_HEADERS]},
    ]
    if team_names:
        last = OPPONENTS_FIRST_ROW + len(team_names) - 1
        updates.append({"range": f"A{OPPONENTS_FIRST_ROW}:A{last}", "values": [[name] for name in team_names]})
    return updates


def parse_opponent_rows(rows: list[list]) -> dict[str, dict]:
    """
    Teams from raw "Przeciwnicy" rows (header row first) by lowercase name: {"name",
    "players", "roles"}; roles are the optional role columns (role -> account names), and
    full Riot IDs there count as players too.
    """
    if not rows:
        return {}
    header = [str(cell).strip() for cell in rows[0]]
    if "Drużyna" not in header:
        print(f'"{OPPONENTS_TAB}" has no "Drużyna" column')
        return {}
    name_column = header.index("Drużyna")
    link_columns = [header.index(name) for name in ("Multi EUW", "Multi EUNE") if name in header]
    role_columns = {role: header.index(role) for role in ROLES if role in header}
    teams = {}
    for row in rows[1:]:
        def cell(i: int) -> str:
            return str(row[i]).strip() if i < len(row) and row[i] is not None else ""

        name = cell(name_column)
        players = parse_players(" ".join(cell(i) for i in link_columns))
        roles = {}
        for role, column in role_columns.items():
            names = [n.strip() for n in re.split(r"[,;\n]", cell(column)) if n.strip()]
            if names:
                roles[role] = names
                known = {p.lower() for p in players}
                players += [n for n in names if "#" in n and n.lower() not in known]
        if name and players:
            teams[name.lower()] = {"name": name, "players": players, "roles": roles}
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


async def known_players(team_name: str) -> tuple[list[str], str, dict[str, list[str]]]:
    """
    The team's Riot IDs, where they came from, and the sheet's role columns: the "Przeciwnicy"
    tab, else saved with /scout, else the most frequent opponents in our games against it.
    Empty when unknown.
    """
    team = (await read_opponents_tab()).get(team_name.strip().lower())
    if team:
        return team["players"], f"zakładka {OPPONENTS_TAB}", team["roles"]
    saved = await db.get_opponent(team_name)
    if saved:
        return saved["players"], "zapisane z /scout", {}
    history = await history_against_us(team_name)
    if history["players"]:
        return [riot_id for riot_id, _ in history["players"].most_common(7)], f"nasze gry z nimi ({maps_count(history['games'])})", {}
    return [], "", {}


async def sheet_roles(team_name: str) -> dict[str, list[str]]:
    """The team's role columns in the "Przeciwnicy" tab (empty without them)."""
    team = (await read_opponents_tab()).get(team_name.strip().lower())
    return team["roles"] if team else {}


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
        accounts = "; ".join(account_text(a, len(player["accounts"]) > 1) for a in player["accounts"])
        lines.append(
            f"{s['role'] or '?'} {player['name']} ({accounts}): {ranked}; "
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
        top = sorted(report["pro_meta"].values(), key=lambda c: -(c["presence"] or 0))[:10]
        lines.append(
            f"Obecność w draftach pro (pick + ban, gol.gg, patche {', '.join(report['pro_patches'])}): "
            + ", ".join(f"{c['champion']} {pct(c['presence'])}" for c in top)
        )
    return "\n".join(lines)


async def run_scouting(team_name: str, riot_ids: list[str], months: int = SCOUT_MONTHS, progress=None,
                       tourney_months: int = None, roles: dict[str, list[str]] = None) -> dict:
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
        now_ts = int(now.timestamp())
        since = now_ts - months * MONTH_SECONDS
        tourney_since = now_ts - tourney_months * MONTH_SECONDS
        pro = await fetch_pro_meta(now.year)

        looked_up = [await lookup_account(riot_id) for riot_id in riot_ids]
        accounts = [a for a in looked_up if not a.get("error")]
        for account in accounts:
            try:
                account["ranks"] = ranked_entries(await riot_api.get_league_entries(account["puuid"], account["platform"]))
            except RiotAPIError as e:
                print(f"Scouting: no ranks for {account['riot_id']}: {e}")
                account["ranks"] = {}
            account["policy"] = ranked_policy(account["platform"], account["ranks"].get("solo"))

        to_fetch = {}
        for account in accounts:
            to_fetch.update(dict.fromkeys(
                await new_match_ids(account["puuid"], since, account["policy"]["queues"], tourney_since)
            ))
        if progress:
            await progress(len(to_fetch))
        await store_scout_games(list(to_fetch), {a["puuid"] for a in accounts})

        # All stored games, also older ones: they only help tell roles and who played with whom
        games = {account["puuid"]: await db.get_scout_games(account["puuid"]) for account in accounts}
        await probe_roles(accounts, games, now_ts * 1000)
        players = group_accounts(looked_up, games, now_ts, months, pro["champions"], tourney_months, roles)

        report = {
            "team": team_name,
            "year": now.year,
            "months": months,
            "tourney_months": tourney_months,
            "created": now,
            "players": players,
            "fetched": len(to_fetch),
            "pro_meta": pro["champions"],
            "pro_patches": pro["patches"],
            "pro_games": pro["games"],
            "history": await history_against_us(team_name),
        }
        report["bans"] = ban_suggestions(players)
        report["summary"] = None
        if llm.enabled() and any(p.get("summary") for p in players):
            report["summary"] = await llm.complete(SUMMARY_PROMPT, summary_facts(report), max_tokens=1500, temperature=0.4)
        return report


def champion_line(table: list[dict], count: int) -> str:
    return ", ".join(f"{c['champion']} {c['games']} ({pct(c['wr'])})" for c in table[:count]) or "brak gier"


def build_embed(report: dict, url: str = None) -> discord.Embed:
    """
    Short version of the report for Discord (full report on the web page at url, tables in
    the sheet), with fewer champions per line when many accounts make it too long.
    """
    for count in EMBED_CHAMPIONS:
        embed = embed_with(report, count)
        if url:
            embed.url = url
            embed.add_field(name="📄 Pełny raport", value=url, inline=False)
        if len(embed) <= EMBED_MAX_LENGTH:
            return embed
    if embed.description:
        room = max(0, EMBED_MAX_LENGTH - (len(embed) - len(embed.description)))
        embed.description = embed.description[:room] or None
    return embed


def pro_note(report: dict) -> str:
    if not report["pro_patches"]:
        return "pro: brak danych z gol.gg"
    return f"pro: gol.gg, patche {', '.join(report['pro_patches'])}"


def embed_with(report: dict, count: int) -> discord.Embed:
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
        accounts = player["accounts"]
        name = f"{s['role'] or '?'} · " + " + ".join(f"{a['riot_id']} ({server_name(a['platform'])})" for a in accounts)
        if len(accounts) == 1:
            lines = [rank_text(accounts[0]["ranks"].get("solo"))
                     + (f" · {accounts[0]['role_note']}" if accounts[0].get("role_note") else "")]
        else:
            lines = [
                f"{server_name(a['platform'])}: {rank_text(a['ranks'].get('solo'))}"
                + (f" · SoloQ {a['policy']['skipped']}" if a["policy"]["skipped"] else "")
                + (f" · {a['role_note']}" if a.get("role_note") else "")
                for a in accounts
            ]
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
        f"SoloQ z {report['months']} mies., gry turniejowe z {report['tourney_months']} mies. · {pro_note(report)}"
        + (" · pełne tabele w zakładce Scouting" if GOOGLE_SHEET_ID else "")
    ))
    return embed


def share(value: float | None):
    """A fraction for a percent-formatted cell, blank for None."""
    return "" if value is None else round(value, 3)


def champion_block_rows(table: list[dict]) -> list[list]:
    return [
        [c["champion"], c["games"], c["wins"], c["losses"], share(c["wr"]), c["kda"], share(c["pro_presence"]), share(c["pro_wr"])]
        for c in table[:SHEET_CHAMPIONS]
    ]


def build_sheet_rows(report: dict) -> tuple[list[list], dict]:
    """
    All rows of the Scouting tab (each SHEET_COLS wide) and, for the formatting, 0-based
    indexes of its "sections", player "bands", "block_titles" and "labels" rows, and its
    "tables" as (header row, end row, first column, column kinds).
    """
    grid: list[list] = []
    layout = {"sections": [], "bands": [], "block_titles": [], "labels": [], "tables": []}

    def add(*cells_at: tuple[int, list]) -> int:
        row = [""] * SHEET_COLS
        for column, cells in cells_at:
            row[column:column + len(cells)] = cells
        grid.append(row)
        return len(grid) - 1

    def add_table(columns: list[tuple[str, str]], rows: list[list]) -> None:
        header = add((0, [name for name, _ in columns]))
        for row in rows:
            add((0, row))
        layout["tables"].append((header, len(grid), 0, [kind for _, kind in columns]))

    add((0, [f"Scouting: {report['team']}"]))
    add((0, [
        f"Wygenerowano {report['created']:%d.%m.%Y %H:%M} · SoloQ z ostatnich {report['months']} mies. "
        f"(konta spoza EUW tylko od Mastera) i osobno gry z kodów turniejowych z ostatnich {report['tourney_months']} mies. · "
        f"{pro_note(report)} · konta rozdzielone na 5 graczy (rola z gier, wspólne gry, nazwy, kolejność w linkach)"
    ]))

    add()
    layout["sections"].append(add((0, ["Sugerowane bany"])))
    if report["bans"]:
        add_table(BAN_COLUMNS, [
            [b["champion"], b["ranked_games"] or "", share(b["ranked_wr"]), b["tourney_games"] or "", share(b["tourney_wr"]),
             share(b["pro_presence"]), share(b["pro_wr"]), b["role"], b["player"]]
            for b in report["bans"]
        ])
    else:
        add((0, ["Za mało gier, żeby coś zasugerować"]))

    if report["summary"]:
        add()
        layout["sections"].append(add((0, ["Podsumowanie"])))
        for line in report["summary"].splitlines():
            if line.strip():
                add((0, [line.strip().replace("**", "")]))

    history = report["history"]
    if history["games"]:
        add()
        layout["sections"].append(add((0, ["Przeciwko nam"])))
        layout["labels"] += [
            add((0, ["Mapy", f"{maps_count(history['games'])}, nasz bilans {history['wins']}-{history['games'] - history['wins']}"])),
            add((0, ["Grali", ", ".join(f"{c} ({n})" for c, n in history["picks"].most_common())])),
            add((0, ["Banowali", ", ".join(f"{c} ({n})" for c, n in history["bans"].most_common()) or "brak danych"])),
        ]

    for player in report["players"]:
        add()
        if player.get("error"):
            layout["bands"].append(add((0, [f"{player['riot_id']}: ⚠ {player['error']}"])))
            continue
        s = player["summary"]
        merged = len(player["accounts"]) > 1
        band = f"{s['role'] or '?'} · " + " + ".join(account_text(a, merged) for a in player["accounts"])
        if s["one_trick"]:
            band += f" · one-trick: {s['one_trick']['champion']} ({pct(s['one_trick']['share'])} gier)"
        layout["bands"].append(add((0, [band])))

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
        layout["block_titles"].append(add(*[(column, [title]) for column, title in zip(BLOCK_STARTS, titles)]))
        blocks = [champion_block_rows(s["ranked"]), champion_block_rows(s["recent"]), champion_block_rows(s["tournament"])]
        if not any(blocks):
            continue
        header = add(*[(column, [name for name, _ in BLOCK_COLUMNS]) for column, block in zip(BLOCK_STARTS, blocks) if block])
        for i in range(max(len(block) for block in blocks)):
            add(*[(column, block[i]) for column, block in zip(BLOCK_STARTS, blocks) if i < len(block)])
        layout["tables"] += [
            (header, header + 1 + len(block), column, [kind for _, kind in BLOCK_COLUMNS])
            for column, block in zip(BLOCK_STARTS, blocks) if block
        ]

    if report["pro_meta"]:
        add()
        layout["sections"].append(add((0, [
            f"Meta pro (gol.gg, patche {', '.join(report['pro_patches'])}, {report['pro_games']} gier): "
            f"top {PRO_META_ROWS} wg obecności w draftach (pick + ban)"
        ])))
        add_table(PRO_COLUMNS, [
            [c["champion"], c["picks"], c["bans"], share(c["presence"]), share(c["wr"])]
            for c in sorted(report["pro_meta"].values(), key=lambda c: -(c["presence"] or 0))[:PRO_META_ROWS]
        ])
    return grid, layout


def build_sheet_format(sheet_id: int, layout: dict) -> list[dict]:
    """Formatting of the Scouting tab for the layout from build_sheet_rows."""
    requests = [
        cell_format(grid_range(sheet_id, 0, 1, 0, 1), {"textFormat": {"bold": True, "fontSize": 16}}),
        cell_format(grid_range(sheet_id, 1, 2, 0, 1), {"textFormat": {"italic": True, "foregroundColor": GRAY}}),
        *column_widths(sheet_id, SHEET_COLS, DEFAULT_WIDTH, COLUMN_WIDTHS),
    ]
    for row in layout["sections"]:
        requests.append(cell_format(grid_range(sheet_id, row, row + 1, 0, 1), {"textFormat": {"bold": True, "fontSize": 12}}))
    for row in layout["bands"]:
        requests.append(cell_format(grid_range(sheet_id, row, row + 1, 0, SHEET_COLS), {
            "backgroundColor": BAND_BACKGROUND, "textFormat": {"bold": True, "fontSize": 11},
        }))
    for row in layout["block_titles"]:
        requests.append(cell_format(grid_range(sheet_id, row, row + 1, 0, SHEET_COLS), {
            "textFormat": {"bold": True, "italic": True, "foregroundColor": GRAY},
        }))
    for row in layout["labels"]:
        requests.append(cell_format(grid_range(sheet_id, row, row + 1, 0, 1), {"textFormat": {"bold": True}}))
    for header, end, first_col, kinds in layout["tables"]:
        requests += table_format(sheet_id, header, end, kinds, first_col)
    return requests


async def write_sheet(report: dict) -> None:
    rows, layout = build_sheet_rows(report)
    await team_sheet.replace_tab(
        SCOUTING_TAB, [{"range": f"A1:Z{len(rows)}", "values": rows}], len(rows), SHEET_COLS
    )
    await team_sheet.format_tab(SCOUTING_TAB, lambda sheet_id: build_sheet_format(sheet_id, layout))


def fetch_estimate(games: int) -> str:
    """How long fetching games takes, e.g. "około 12 min"."""
    return f"około {max(1, round(games * SECONDS_PER_GAME / 60))} min"


async def post_scouting(channel, team_name: str, riot_ids: list[str], months: int = SCOUT_MONTHS,
                        announce_progress: bool = False, tourney_months: int = None,
                        roles: dict[str, list[str]] = None) -> None:
    """Run the scouting, write the Scouting tab and post the report to the channel."""
    async def progress(games: int) -> None:
        if games > 50:
            await channel.send(
                f"⏳ {discord.utils.escape_markdown(team_name)}: do pobrania {games_count(games)}, {fetch_estimate(games)}."
            )

    try:
        report = await run_scouting(team_name, riot_ids, months, progress if announce_progress else None, tourney_months, roles)
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
    url = None
    try:
        # Imported here: scouting_web renders with this module's helpers
        from scouting_web import publish_report
        url = publish_report(report)
    except Exception as e:
        print(f"Could not publish the scouting page: {e}")
        note = (note + " " if note else "") + "⚠ Nie udało się opublikować strony z raportem."
    await channel.send(note or None, embed=build_embed(report, url))
    print(f"Scouted {team_name}: {len(riot_ids)} players, {report['fetched']} games fetched")


def start_scouting(channel, team_name: str, riot_ids: list[str], months: int = SCOUT_MONTHS,
                   announce_progress: bool = False, tourney_months: int = None,
                   roles: dict[str, list[str]] = None) -> asyncio.Task:
    """Scout in the background (it can take a while) and post the report when done."""
    return asyncio.create_task(
        post_scouting(channel, team_name, riot_ids, months, announce_progress, tourney_months, roles)
    )


def is_running() -> bool:
    return _scout_lock.locked()
