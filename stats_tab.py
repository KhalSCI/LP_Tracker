"""The "Statystyki" tab: our players' stats in scrims and officials, from the full game data (game_stats.py)."""
from collections import defaultdict
from datetime import datetime

from gspread.utils import rowcol_to_a1

import database as db
from config import TIMEZONE
from roasts import plural
from sheet_format import (
    GRAY, grid_range, cell_format, table_format, column_widths, row_height, frozen_rows,
)
from sheets import team_sheet
from team_games import ROLES

STATS_TAB = "Statystyki"
# Columns as (header, kind); the kind sets the number format and coloring
PLAYER_COLUMNS = [
    ("Gracz", "name"), ("Rola", "text"), ("Mapy", "int"), ("W", "int"), ("L", "int"), ("WR", "wr"),
    ("KDA", "dec2"), ("KP", "pct"), ("DMG%", "pct"), ("Gold%", "pct"), ("CS/min", "dec1"), ("Wizja/min", "dec2"),
    ("GD@10", "diff"), ("GD@15", "diff"), ("CSD@15", "diff"), ("XPD@15", "diff"), ("Śmierci <14 min", "dec1"),
    ("First blood", "pct"),
]
MATCHUP_COLUMNS = [
    ("Gracz", "name"), ("Rola", "text"), ("Champion", "text"), ("Przeciwnik", "text"), ("Gry", "int"), ("W", "int"),
    ("L", "int"), ("WR", "wr"), ("KDA", "dec2"), ("GD@10", "diff"), ("GD@15", "diff"), ("CSD@15", "diff"),
    ("XPD@15", "diff"), ("Śmierci <14 min", "dec1"),
]
SHEET_COLS = max(len(PLAYER_COLUMNS), len(MATCHUP_COLUMNS))
NOTES = (
    "Średnie na mapę. Matchupy: nasz champion przeciwko championowi z tej samej linii u przeciwnika; im więcej gier, "
    "tym pewniejszy obraz. GD/CSD/XPD@N: różnica golda, CS i XP do przeciwnika z linii w N. minucie (plus = przewaga). "
    "KP: udział w zabójstwach drużyny. DMG%/Gold%: udział w obrażeniach i goldzie drużyny. "
    "GD/CSD/XPD i śmierci przed 14. minutą są tylko z map z przebiegiem gry."
)

# Formatting
COLUMN_WIDTHS = {0: 120, 1: 70, 2: 105, 3: 105}
DEFAULT_WIDTH = 72
NOTES_HEIGHT = 48
FROZEN_ROWS = 4


def mean(values: list) -> float | None:
    """Mean of the values that aren't None; None without any."""
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def rounded(value: float | None, digits: int = 0):
    """A number for a cell, blank for None."""
    if value is None:
        return ""
    return round(value) if digits == 0 else round(value, digits)


def vision_per_min(row: dict) -> float | None:
    return row["vision_score"] / (row["duration_s"] / 60) if row["duration_s"] else None


def kda(games: list[dict]) -> float:
    kills, deaths, assists = (sum(g[key] for g in games) for key in ("kills", "deaths", "assists"))
    return round((kills + assists) / max(deaths, 1), 2)


def roster_sort_key(player: str, role: str, player_order: list[str]) -> tuple:
    """Roster order (others after, by name), then role order."""
    order = {name: i for i, name in enumerate(player_order)}
    return order.get(player, len(order)), player, ROLES.index(role) if role in ROLES else len(ROLES)


def build_player_rows(rows: list[dict], player_order: list[str]) -> list[list]:
    """Our players' averages per (player, role), in roster order, then role order."""
    groups = defaultdict(list)
    for row in rows:
        if row["ours"]:
            groups[(row["player"], row["role"] or "?")].append(row)

    result = []
    for (player, role), games in sorted(groups.items(), key=lambda item: roster_sort_key(*item[0], player_order)):
        n = len(games)
        wins = sum(bool(g["win"]) for g in games)
        result.append([
            player, role, n, wins, n - wins, round(wins / n, 3), kda(games),
            rounded(mean([g["kill_participation"] for g in games]), 3),
            rounded(mean([g["damage_share"] for g in games]), 3),
            rounded(mean([g["gold_share"] for g in games]), 3),
            rounded(mean([g["cs_per_min"] for g in games]), 1),
            rounded(mean([vision_per_min(g) for g in games]), 2),
            rounded(mean([g["gold_diff_10"] for g in games])),
            rounded(mean([g["gold_diff_15"] for g in games])),
            rounded(mean([g["cs_diff_15"] for g in games])),
            rounded(mean([g["xp_diff_15"] for g in games])),
            rounded(mean([g["deaths_before_14"] for g in games]), 1),
            round(sum(bool(g["first_blood"]) for g in games) / n, 3),
        ])
    return result


def build_matchup_rows(rows: list[dict], player_order: list[str]) -> list[list]:
    """
    Our players' lane matchups (our champion against the enemy one in the same role), by
    roster and role order, then most games and best gold difference at 15 minutes.
    """
    groups = defaultdict(list)
    for row in rows:
        if row["ours"] and row["lane_opponent"]:
            groups[(row["player"], row["role"] or "?", row["champion"], row["lane_opponent"])].append(row)

    def sort_key(item):
        (player, role, champion, opponent), games = item
        gd15 = mean([g["gold_diff_15"] for g in games])
        return (*roster_sort_key(player, role, player_order), -len(games), -(gd15 if gd15 is not None else float("-inf")),
                champion, opponent)

    result = []
    for (player, role, champion, opponent), games in sorted(groups.items(), key=sort_key):
        n = len(games)
        wins = sum(bool(g["win"]) for g in games)
        result.append([
            player, role, champion, opponent, n, wins, n - wins, round(wins / n, 3), kda(games),
            rounded(mean([g["gold_diff_10"] for g in games])),
            rounded(mean([g["gold_diff_15"] for g in games])),
            rounded(mean([g["cs_diff_15"] for g in games])),
            rounded(mean([g["xp_diff_15"] for g in games])),
            rounded(mean([g["deaths_before_14"] for g in games]), 1),
        ])
    return result


def build_stats_rows(rows: list[dict], player_order: list[str], now: datetime) -> tuple[list[list], dict]:
    """
    All rows of the Statystyki tab (each SHEET_COLS wide) and where its tables are, as
    0-based row indexes: {"players": (header, end), "matchups": (title, header, end)}.
    """
    maps = {r["match_id"]: r for r in rows if r["ours"]}
    officials = sum(1 for r in maps.values() if r["tournament"])
    with_timeline = len({r["match_id"] for r in rows if r["ours"] and r["deaths_before_14"] is not None})
    players = build_player_rows(rows, player_order)
    matchups = build_matchup_rows(rows, player_order)

    grid = [
        ["Statystyki: scrimy i oficjale"],
        [f"Na podstawie {plural(len(maps), 'mapy', 'map', 'map')} ({plural(len(maps) - officials, 'scrim', 'scrimy', 'scrimów')}, "
         f"{plural(officials, 'oficjal', 'oficjale', 'oficjali')}), z przebiegiem gry: {with_timeline} · "
         f"aktualizacja {now:%d.%m.%Y %H:%M}"],
        [NOTES],
        [name for name, _ in PLAYER_COLUMNS],
        *players,
        [],
        ["Matchupy na linii"],
        [name for name, _ in MATCHUP_COLUMNS],
        *matchups,
    ]
    players_header = FROZEN_ROWS - 1
    matchups_title = players_header + 1 + len(players) + 1
    layout = {
        "players": (players_header, players_header + 1 + len(players)),
        "matchups": (matchups_title, matchups_title + 1, matchups_title + 2 + len(matchups)),
    }
    return [row + [""] * (SHEET_COLS - len(row)) for row in grid], layout


def build_format_requests(sheet_id: int, layout: dict) -> list[dict]:
    """Formatting of the Statystyki tab for the layout from build_stats_rows."""
    players_header, players_end = layout["players"]
    matchups_title, matchups_header, matchups_end = layout["matchups"]
    requests = [
        cell_format(grid_range(sheet_id, 0, 1, 0, 1), {"textFormat": {"bold": True, "fontSize": 14}}),
        cell_format(grid_range(sheet_id, 1, 2, 0, 1), {"textFormat": {"italic": True, "foregroundColor": GRAY}}),
        {"mergeCells": {"range": grid_range(sheet_id, 2, 3, 0, SHEET_COLS), "mergeType": "MERGE_ALL"}},
        cell_format(grid_range(sheet_id, 2, 3, 0, SHEET_COLS), {
            "wrapStrategy": "WRAP", "verticalAlignment": "TOP",
            "textFormat": {"italic": True, "fontSize": 9, "foregroundColor": GRAY},
        }),
        row_height(sheet_id, 2, NOTES_HEIGHT),
        cell_format(grid_range(sheet_id, matchups_title, matchups_title + 1, 0, 1), {"textFormat": {"bold": True, "fontSize": 12}}),
        # Only rows: a frozen column can't cut through the merged notes row
        frozen_rows(sheet_id, FROZEN_ROWS),
        *column_widths(sheet_id, SHEET_COLS, DEFAULT_WIDTH, COLUMN_WIDTHS),
    ]
    requests += table_format(sheet_id, players_header, players_end, [kind for _, kind in PLAYER_COLUMNS])
    requests += table_format(sheet_id, matchups_header, matchups_end, [kind for _, kind in MATCHUP_COLUMNS])
    return requests


async def update_stats_tab() -> int:
    """Rewrite and format the Statystyki tab (kept as the sheet's last tab). Returns the number of maps."""
    rows = await db.get_team_game_stats()
    grid, layout = build_stats_rows(rows, [row["nick"] for row in await team_sheet.read_roster()], datetime.now(TIMEZONE))
    await team_sheet.replace_tab(
        STATS_TAB, [{"range": f"A1:{rowcol_to_a1(len(grid), SHEET_COLS)}", "values": grid}],
        len(grid), SHEET_COLS, keep_last=True,
    )
    await team_sheet.format_tab(STATS_TAB, lambda sheet_id: build_format_requests(sheet_id, layout))
    return len({r["match_id"] for r in rows if r["ours"]})
