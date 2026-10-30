"""The "Drafty" tab: what our picks do in scrims and officials, from the "Mapy" tab."""
import time
from collections import defaultdict
from datetime import datetime

from gspread.utils import rowcol_to_a1

import database as db
from champions import champions
from cogs.sheet import win_rate, pad_rows, SOLOQ_QUEUE, MATCH_HISTORY_DAYS, DAY_SECONDS
from config import TIMEZONE
from sheets import team_sheet, MAPS_TAB, DRAFTS_TAB
from team_games import ROLES, SCRIM_TYPE, load_team_roster

FIRST_ROW = 5
MAX_ROWS = 100
# Blocks side by side: (first column, title, headers); data from FIRST_ROW down
BLOCKS = {
    "champions": (1, "Nasze championy", ["Gracz", "Rola", "Champion", "Gry", "W", "L", "WR", "Scrimy", "Oficjale", "SoloQ (30 dni)"]),
    "enemies": (12, "Championy przeciwnika", ["Champion", "Gry", "Nasze W", "Nasze L", "Nasz WR"]),
    "bot_lane": (18, "Bot lane", ["ADC", "Support", "Gry", "W", "WR"]),
    "jungle_mid": (24, "Jungla + mid", ["Jungle", "Mid", "Gry", "W", "WR"]),
    "sides": (30, "Strona", ["Strona", "Gry", "W", "L", "WR"]),
    "bans": (36, "Bany przeciwko nam", ["Champion", "Bany", "% map"]),
}
SHEET_ROWS = FIRST_ROW + MAX_ROWS - 1
SHEET_COLS = max(start + len(headers) - 1 for start, _, headers in BLOCKS.values())


def parse_maps(rows: list[list]) -> list[dict]:
    """Played maps (with a W/L result) from raw "Mapy" rows, header row first."""
    if not rows:
        return []
    header = [str(cell).strip() for cell in rows[0]]
    columns = {}
    for i, name in enumerate(header):
        columns.setdefault(name, i)  # first column with each name; hidden lists reuse role names

    def cell(row: list, name: str) -> str:
        i = columns.get(name)
        return str(row[i]).strip() if i is not None and i < len(row) and row[i] is not None else ""

    maps = []
    for row in rows[1:]:
        result = cell(row, "Wynik")
        if result not in ("W", "L"):
            continue
        maps.append({
            "type": cell(row, "Typ"),
            "win": result == "W",
            "side": cell(row, "Strona"),
            "players": {role: cell(row, role) for role in ROLES},
            "champions": {role: cell(row, f"{role} champ") for role in ROLES},
            "enemy": [c.strip() for c in cell(row, "Picki przeciwnika").split(",") if c.strip()],
            "game_id": cell(row, "Game ID"),
        })
    return maps


def record(counter: dict, key, win: bool) -> None:
    counter[key][0] += 1
    counter[key][1] += win


def score(item: tuple) -> tuple:
    """Sort key: most games first, then best winrate."""
    _, (games, wins) = item
    return -games, -wins / games


def win_loss(games: int, wins: int) -> str:
    return f"{wins}-{games - wins}" if games else ""


def build_champion_rows(maps: list[dict], player_order: list[str], soloq: dict) -> list[list]:
    """One row per (player, champion) we played, by roster order then most played."""
    total = defaultdict(lambda: [0, 0])
    by_type = defaultdict(lambda: [0, 0])
    roles = defaultdict(lambda: defaultdict(int))
    for game in maps:
        official = game["type"] not in (SCRIM_TYPE, "", "?")
        for role in ROLES:
            player, champion = game["players"][role], game["champions"][role]
            if not player or not champion:
                continue
            record(total, (player, champion), game["win"])
            record(by_type, (player, champion, official), game["win"])
            roles[(player, champion)][role] += 1

    order = {player: i for i, player in enumerate(player_order)}
    rows = []
    for (player, champion), (games, wins) in sorted(
        total.items(), key=lambda item: (order.get(item[0][0], len(order)), item[0][0]) + score(item)
    ):
        scrim = by_type.get((player, champion, False), [0, 0])
        official = by_type.get((player, champion, True), [0, 0])
        soloq_games, soloq_wins = soloq.get((player, champion), (0, 0))
        rows.append([
            player,
            max(roles[(player, champion)], key=roles[(player, champion)].get),
            champion,
            games,
            wins,
            games - wins,
            win_rate(wins, games),
            win_loss(*scrim),
            win_loss(*official),
            f"{soloq_games} gier · {round(soloq_wins / soloq_games * 100)}%" if soloq_games else "",
        ])
    return rows


def build_pair_rows(maps: list[dict], first: str, second: str) -> list[list]:
    """Our champion pairs on two roles, e.g. ADC + Support."""
    pairs = defaultdict(lambda: [0, 0])
    for game in maps:
        a, b = game["champions"][first], game["champions"][second]
        if a and b:
            record(pairs, (a, b), game["win"])
    return [[a, b, games, wins, win_rate(wins, games)] for (a, b), (games, wins) in sorted(pairs.items(), key=score)]


def build_enemy_rows(maps: list[dict]) -> list[list]:
    """Enemy champions we faced, with our results against them."""
    faced = defaultdict(lambda: [0, 0])
    for game in maps:
        for champion in set(game["enemy"]):
            record(faced, champion, game["win"])
    return [[champion, games, wins, games - wins, win_rate(wins, games)] for champion, (games, wins) in sorted(faced.items(), key=score)]


def build_side_rows(maps: list[dict]) -> list[list]:
    sides = defaultdict(lambda: [0, 0])
    for game in maps:
        if game["side"] in ("Blue", "Red"):
            record(sides, game["side"], game["win"])
    return [[side, games, wins, games - wins, win_rate(wins, games)] for side, (games, wins) in sorted(sides.items())]


def build_ban_rows(maps: list[dict], bans_by_game: dict[str, list]) -> list[list]:
    """Champions the opponents banned against us, in maps with ban data."""
    counts = defaultdict(int)
    maps_with_bans = 0
    for game in maps:
        bans = bans_by_game.get(game["game_id"])
        if bans:
            maps_with_bans += 1
            for champion in set(bans):
                counts[champion] += 1
    return [
        [champion, n, round(n / maps_with_bans, 4)]
        for champion, n in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def build_drafts_updates(maps: list[dict], player_order: list[str], soloq: dict,
                         bans_by_game: dict[str, list], now: datetime) -> list[dict]:
    """All value ranges written to the Drafty tab in one sync."""
    scrims = sum(1 for game in maps if game["type"] == SCRIM_TYPE)
    with_picks = [game for game in maps if any(game["champions"].values())]
    if with_picks:
        status = (
            f"Na podstawie {len(maps)} map ({scrims} scrimy, {len(maps) - scrims} oficjale), "
            f"picki z {len(with_picks)} · aktualizacja {now:%d.%m.%Y %H:%M}"
        )
    else:
        status = f"Brak map z championami: wyślij scrimy skryptem albo wpisz championy w Mapach · {now:%d.%m.%Y %H:%M}"

    data = {
        "champions": build_champion_rows(with_picks, player_order, soloq),
        "enemies": build_enemy_rows(with_picks),
        "bot_lane": build_pair_rows(with_picks, "ADC", "Support"),
        "jungle_mid": build_pair_rows(with_picks, "Jungle", "Mid"),
        "sides": build_side_rows(maps),
        "bans": build_ban_rows(maps, bans_by_game),
    }

    updates = [
        {"range": "A1", "values": [["Drafty: scrimy i oficjale"]]},
        {"range": "A2", "values": [[status]]},
    ]
    for key, (start, title, headers) in BLOCKS.items():
        width = len(headers)
        updates += [
            {"range": rowcol_to_a1(FIRST_ROW - 2, start), "values": [[title]]},
            {"range": f"{rowcol_to_a1(FIRST_ROW - 1, start)}:{rowcol_to_a1(FIRST_ROW - 1, start + width - 1)}", "values": [headers]},
            {
                "range": f"{rowcol_to_a1(FIRST_ROW, start)}:{rowcol_to_a1(SHEET_ROWS, start + width - 1)}",
                "values": pad_rows(data[key], MAX_ROWS, width),
            },
        ]
    return updates


async def soloq_champion_stats(roster) -> dict:
    """(nick, champion) -> (games, wins) in SoloQ over the stored match history."""
    nicks = {m["puuid"]: m["nick"] for m in roster.members if m.get("puuid")}
    since_ms = (int(time.time()) - MATCH_HISTORY_DAYS * DAY_SECONDS) * 1000
    stats = defaultdict(lambda: [0, 0])
    for game in await db.get_matches_since(list(nicks), since_ms):
        if game["queue_id"] == SOLOQ_QUEUE and not game["remake"]:
            record(stats, (nicks[game["puuid"]], champions.name(champion_key=game["champion_name"])), bool(game["win"]))
    return {key: tuple(value) for key, value in stats.items()}


async def update_drafts_tab() -> int:
    """Recompute the Drafty tab and refresh the champion list behind the "Mapy" dropdowns. Returns the map count."""
    roster = await load_team_roster()
    await champions.refresh()
    maps = parse_maps(await team_sheet.read_tab(MAPS_TAB))
    bans_by_game = {game["match_id"]: game["enemy_bans"] for game in await db.get_team_games()}
    player_order = [row["nick"] for row in await team_sheet.read_roster()]
    now = datetime.now(TIMEZONE)

    updates = build_drafts_updates(maps, player_order, await soloq_champion_stats(roster), bans_by_game, now)
    await team_sheet.write(DRAFTS_TAB, updates, SHEET_ROWS, SHEET_COLS)

    if champions.names:
        await team_sheet.write(MAPS_TAB, [{
            "range": f"Z4:Z{4 + len(champions.names)}",
            "values": [["Championy"]] + [[name] for name in champions.names],
        }], 4 + len(champions.names), 26)
    return len(maps)
