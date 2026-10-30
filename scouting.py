"""
Opponent scouting for draft prep: each account's champions in ranked this season and last
season (games, winrate, KDA) and in tournament-code and Clash games, from the Riot API
(EUW and EUNE accounts, e.g. two multi op.gg links in one scouting),
with pro-play priority from gol.gg, suggested bans and what they played against us.
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

import database as db
import llm
from champions import champions
from cogs.sheet import format_tier
from config import TIMEZONE, SCOUT_MAX_GAMES, GOOGLE_SHEET_ID
from riot_api import riot_api, RiotAPIError
from sheets import team_sheet

SCOUTING_TAB = "Scouting"
RANKED_QUEUES = {420: "SoloQ", 440: "Flex"}
CLASH_QUEUE = 700
SUMMONERS_RIFT = 11
ROLES = ["Top", "Jungle", "Mid", "ADC", "Support"]
POSITIONS = {"TOP": "Top", "JUNGLE": "Jungle", "MIDDLE": "Mid", "BOTTOM": "ADC", "UTILITY": "Support"}
PLATFORM_NAMES = {"euw1": "EUW", "eun1": "EUNE"}
# match-v5 patch 16.x is season 2026 (gol.gg calls it S16)
SEASON_OFFSET = 2010
IDS_PAGE = 100
# Tournament-code games and Clash games read per account since last season
MAX_TOURNEY_GAMES = 100
GOLGG_URL = "https://gol.gg/champion/list/season-S{season}/split-ALL/tournament-ALL/"
PRO_META_TTL = 24 * 60 * 60
PRO_META_ROWS = 20
# Champions per player shown in the sheet, and in the embed (fewer when the embed gets too long)
SHEET_CHAMPIONS = 10
EMBED_CHAMPIONS = (4, 3, 2)
# Discord rejects embeds over 6000 characters
EMBED_MAX_LENGTH = 5900
BAN_SUGGESTIONS = 5
# Ranked games this season (or tournament games) a champion needs to be suggested as a ban
MIN_BAN_GAMES = 5
MIN_BAN_TOURNEY_GAMES = 2
TOURNEY_WEIGHT = 3
# One champion with this share of a player's season games (and enough games) is a one-trick
ONE_TRICK_SHARE = 0.4
ONE_TRICK_MIN_GAMES = 15
# gol.gg writes some champions differently than Data Dragon
CHAMPION_ALIASES = {"nunuwillump": "nunu"}
# Columns of the three champion blocks per player in the sheet (A, J, S) and their headers
BLOCK_COLUMNS = (0, 9, 18)
BLOCK_HEADERS = ["Champion", "Gry", "W", "L", "WR", "KDA", "Pro prio", "Pro WR"]
SHEET_COLS = 26

SUMMARY_PROMPT = """Jesteś analitykiem draftu w uczelnianej drużynie League of Legends.
Dostajesz dane o drużynie przeciwnika: pule championów graczy z rankedów (ten i poprzedni sezon), gry turniejowe, sugerowane bany i priorytet championów w grze profesjonalnej.
Napisz po polsku 3-5 krótkich punktów do przygotowania draftu: co banować, na kim się skupić, comfort picki i one-tricki, na co uważać.
Opieraj się wyłącznie na podanych danych, nie wymyślaj. Każdy punkt w osobnej linii zaczynającej się od "• ", razem najwyżej 700 znaków."""

_scout_lock = asyncio.Lock()
_pro_meta_cache: dict[int, tuple[float, dict]] = {}


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


def year_start(year: int) -> int:
    """Unix seconds of 1 January, local time."""
    return int(datetime(year, 1, 1, tzinfo=TIMEZONE).timestamp())


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


async def fetch_match_ids(puuid: str, match_type: str | None, start_time: int, end_time: int | None, limit: int,
                          queue: int = None) -> list[str]:
    """Up to limit match IDs of one type (or queue) in a time window, newest first."""
    ids = []
    while len(ids) < limit:
        count = min(IDS_PAGE, limit - len(ids))
        page = await riot_api.get_match_ids(
            puuid, match_type=match_type, start_time=start_time, end_time=end_time, start=len(ids), count=count,
            queue=queue,
        )
        ids += page
        if len(page) < count:
            break
    return ids


async def sync_scout_games(puuid: str, scouted: set[str], year: int) -> int:
    """
    Store an account's ranked games this season and last season (up to SCOUT_MAX_GAMES each,
    newest first) and tournament-code and Clash games since last season. Games stored
    before aren't fetched again. Returns how many games were fetched.
    """
    known = await db.get_scout_match_ids(puuid)
    ids = await fetch_match_ids(puuid, "ranked", year_start(year), None, SCOUT_MAX_GAMES)
    ids += await fetch_match_ids(puuid, "ranked", year_start(year - 1), year_start(year), SCOUT_MAX_GAMES)
    ids += await fetch_match_ids(puuid, "tourney", year_start(year - 1), None, MAX_TOURNEY_GAMES)
    ids += await fetch_match_ids(puuid, None, year_start(year - 1), None, MAX_TOURNEY_GAMES, queue=CLASH_QUEUE)

    new = [match_id for match_id in dict.fromkeys(ids) if match_id not in known]
    for match_id in new:
        try:
            match_data = await riot_api.get_match_details(match_id)
        except RiotAPIError as e:
            if e.status == 404:
                continue
            raise
        # Saved for every scouted player in the game, so a game of two of them is fetched once
        await db.save_scout_games(scout_rows(match_id, match_data, scouted))
    return len(new)


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


def summarize(games: list[dict], year: int, pro_meta: dict) -> dict:
    """A player's champions this season and last season in ranked, tournament games, main role, one-trick."""
    counted = [g for g in games if g["win"] is not None and g["season"]]
    current = [g for g in counted if g["queue_id"] in RANKED_QUEUES and g["season"] == year]
    previous = [g for g in counted if g["queue_id"] in RANKED_QUEUES and g["season"] == year - 1]
    tournament = [g for g in counted if g["queue_id"] not in RANKED_QUEUES and g["season"] in (year, year - 1)]

    roles = Counter(g["role"] for g in current + previous + tournament if g["role"])
    role, role_games = roles.most_common(1)[0] if roles else ("", 0)
    current_table = champion_table(current, pro_meta)
    one_trick = None
    if current_table and len(current) >= ONE_TRICK_MIN_GAMES and current_table[0]["games"] / len(current) >= ONE_TRICK_SHARE:
        one_trick = {"champion": current_table[0]["champion"], "share": current_table[0]["games"] / len(current)}
    return {
        "current": current_table,
        "previous": champion_table(previous, pro_meta),
        "tournament": champion_table(tournament, pro_meta),
        "current_games": len(current),
        "previous_games": len(previous),
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
    Champions worth banning: players' comfort picks this season, weighted by games and a
    smoothed winrate, with tournament games counting TOURNEY_WEIGHT times.
    """
    candidates = []
    for player in players:
        summary = player.get("summary")
        if not summary:
            continue
        tourney = {c["champion"]: c for c in summary["tournament"]}
        ranked = {c["champion"]: c for c in summary["current"]}
        for champion in set(ranked) | set(tourney):
            r, t = ranked.get(champion), tourney.get(champion)
            if (r is None or r["games"] < MIN_BAN_GAMES) and (t is None or t["games"] < MIN_BAN_TOURNEY_GAMES):
                continue
            games = (r["games"] if r else 0) + TOURNEY_WEIGHT * (t["games"] if t else 0)
            wins = (r["wins"] if r else 0) + TOURNEY_WEIGHT * (t["wins"] if t else 0)
            reasons = []
            if r:
                reasons.append(f"{r['games']} gier, {pct(r['wr'])} WR w tym sezonie")
            if t:
                reasons.append(f"{t['games']} w turniejach/Clashu ({pct(t['wr'])} WR)")
            candidates.append({
                "champion": champion,
                "player": player_name(player),
                "role": player["summary"]["role"],
                "score": games * (wins + 1) / (games + 2),
                "reason": ", ".join(reasons),
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


async def known_players(team_name: str) -> list[str]:
    """The team's Riot IDs: saved with /scout, else the most frequent opponents in our games against it."""
    saved = await db.get_opponent(team_name)
    if saved:
        return saved["players"]
    history = await history_against_us(team_name)
    return [riot_id for riot_id, _ in history["players"].most_common(7)]


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
    return ", ".join(f"{c['champion']} {c['games']} gier {pct(c['wr'])}" for c in table[:count]) or "brak"


def summary_facts(report: dict) -> str:
    """The report as compact text for the model."""
    year = report["year"]
    lines = [f"Drużyna przeciwnika: {report['team']}"]
    for player in report["players"]:
        if player.get("error"):
            continue
        s = player["summary"]
        lines.append(
            f"{s['role'] or '?'} {player_name(player)} ({rank_text(player['ranks'].get('solo'))}): "
            f"sezon {year}: {pool_text(s['current'], 5)}; sezon {year - 1}: {pool_text(s['previous'], 3)}; "
            f"turnieje i Clash: {pool_text(s['tournament'], 3)}"
            + (f"; one-trick {s['one_trick']['champion']} ({pct(s['one_trick']['share'])} gier)" if s["one_trick"] else "")
        )
    if report["bans"]:
        lines.append("Sugerowane bany (z danych): " + ", ".join(f"{b['champion']} ({b['player']}: {b['reason']})" for b in report["bans"]))
    history = report["history"]
    if history["games"]:
        lines.append(
            f"Przeciwko nam: {history['games']} map, nasz bilans {history['wins']}-{history['games'] - history['wins']}, "
            f"grali: {', '.join(c for c, _ in history['picks'].most_common(8))}; "
            f"banowali: {', '.join(c for c, _ in history['bans'].most_common(5)) or 'brak danych'}"
        )
    if report["pro_meta"]:
        top = sorted(report["pro_meta"].values(), key=lambda c: -(c["prio"] or 0))[:10]
        lines.append("Najwyższy priorytet w grze pro (gol.gg): " + ", ".join(f"{c['champion']} {pct(c['prio'])}" for c in top))
    return "\n".join(lines)


async def run_scouting(team_name: str, riot_ids: list[str]) -> dict:
    """Collect the scouting report of a team (one scouting at a time; the first one of a team takes a while)."""
    async with _scout_lock:
        await champions.refresh()
        year = datetime.now(TIMEZONE).year
        pro_meta = await fetch_pro_meta(year)

        players = [await lookup_account(riot_id) for riot_id in riot_ids]
        scouted = {p["puuid"] for p in players if p.get("puuid")}
        fetched = 0
        for player in players:
            if player.get("error"):
                continue
            fetched += await sync_scout_games(player["puuid"], scouted, year)
            try:
                player["ranks"] = ranked_entries(await riot_api.get_league_entries(player["puuid"], player["platform"]))
            except RiotAPIError as e:
                print(f"Scouting: no ranks for {player['riot_id']}: {e}")
                player["ranks"] = {}
            games = await db.get_scout_games(player["puuid"], year_start(year - 1) * 1000)
            player["summary"] = summarize(games, year, pro_meta)

        role_order = {role: i for i, role in enumerate(ROLES)}
        players.sort(key=lambda p: role_order.get((p.get("summary") or {}).get("role"), len(ROLES)))
        report = {
            "team": team_name,
            "year": year,
            "created": datetime.now(TIMEZONE),
            "players": players,
            "fetched": fetched,
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
        lines = [
            rank_text(player["ranks"].get("solo")),
            f"**{year}:** {champion_line(s['current'], count)}",
            f"**{year - 1}:** {champion_line(s['previous'], count)}",
        ]
        if s["tournament"]:
            lines.append(f"**Turnieje i Clash:** {champion_line(s['tournament'], count)}")
        if s["one_trick"]:
            lines.append(f"⚠ One-trick: {s['one_trick']['champion']} ({pct(s['one_trick']['share'])} gier w sezonie)")
        embed.add_field(name=name[:256], value="\n".join(lines)[:1024], inline=False)

    if report["bans"]:
        embed.add_field(
            name="🚫 Sugerowane bany",
            value="\n".join(f"**{b['champion']}** ({b['player']}): {b['reason']}" for b in report["bans"])[:1024],
            inline=False,
        )
    history = report["history"]
    if history["games"]:
        embed.add_field(
            name="Przeciwko nam",
            value=(
                f"{history['games']} map, nasz bilans {history['wins']}-{history['games'] - history['wins']}\n"
                f"Grali: {', '.join(c for c, _ in history['picks'].most_common(8))}\n"
                f"Banowali: {', '.join(c for c, _ in history['bans'].most_common(5)) or 'brak danych'}"
            )[:1024],
            inline=False,
        )
    embed.set_footer(text=(
        f"Ranked: do {SCOUT_MAX_GAMES} ostatnich gier na sezon · pro: gol.gg S{year - SEASON_OFFSET}"
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
        f"Wygenerowano {report['created']:%d.%m.%Y %H:%M} · ranked (SoloQ i Flex) z sezonów {year} i {year - 1}, "
        f"do {SCOUT_MAX_GAMES} ostatnich gier na sezon, plus gry z kodów turniejowych i Clash · "
        f"pro: gol.gg S{year - SEASON_OFFSET}, wszystkie turnieje"
    ]))
    add()
    add((0, ["Sugerowane bany"]))
    add((0, ["Champion", "Gracz", "Rola", "Dlaczego", "Pro prio", "Pro WR"]))
    for ban in report["bans"]:
        add((0, [ban["champion"], ban["player"], ban["role"], ban["reason"], pct(ban["pro_prio"]), pct(ban["pro_wr"])]))
    if report["summary"]:
        add()
        add((0, ["Podsumowanie"]))
        for line in report["summary"].splitlines():
            if line.strip():
                add((0, [line.strip()]))
    history = report["history"]
    if history["games"]:
        add()
        add((0, ["Przeciwko nam", f"{history['games']} map, nasz bilans {history['wins']}-{history['games'] - history['wins']}"]))
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
            f"Flex: {rank_text(player['ranks'].get('flex'))}",
            f"One-trick: {s['one_trick']['champion']} ({pct(s['one_trick']['share'])} gier)" if s["one_trick"] else "",
        ]))
        titles = [
            f"Sezon {year} (ranked, {s['current_games']} gier)",
            f"Sezon {year - 1} (ranked, {s['previous_games']} gier)",
            "Turnieje (kody turniejowe) i Clash",
        ]
        add(*[(column, [title]) for column, title in zip(BLOCK_COLUMNS, titles)])
        add(*[(column, BLOCK_HEADERS) for column in BLOCK_COLUMNS])
        blocks = [champion_block_rows(s["current"]), champion_block_rows(s["previous"]), champion_block_rows(s["tournament"])]
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


async def post_scouting(channel, team_name: str, riot_ids: list[str]) -> None:
    """Run the scouting, write the Scouting tab and post the report to the channel."""
    try:
        report = await run_scouting(team_name, riot_ids)
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


def start_scouting(channel, team_name: str, riot_ids: list[str]) -> asyncio.Task:
    """Scout in the background (it can take minutes) and post the report when done."""
    return asyncio.create_task(post_scouting(channel, team_name, riot_ids))


def is_running() -> bool:
    return _scout_lock.locked()
