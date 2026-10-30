"""The "Statystyki" tab: our players' stats in scrims and officials, from the full game data (game_stats.py)."""
from collections import defaultdict
from datetime import datetime

from gspread.utils import rowcol_to_a1

import database as db
from config import TIMEZONE
from roasts import plural
from sheets import team_sheet
from team_games import ROLES

STATS_TAB = "Statystyki"
PLAYER_HEADERS = [
    "Gracz", "Rola", "Mapy", "W", "L", "WR", "KDA", "KP", "DMG%", "Gold%", "CS/min", "Wizja/min",
    "GD@10", "GD@15", "CSD@15", "XPD@15", "Śmierci <14 min", "First blood",
]
MAP_HEADERS = [
    "Data", "Mecz", "Wynik", "Strona", "Gracz", "Rola", "Champion", "Przeciwnik na linii", "K/D/A", "KP", "DMG%",
    "CS/min", "Wizja/min", "GD@10", "GD@15", "CSD@15", "XPD@15", "Śmierci <14 min",
]
SHEET_COLS = max(len(PLAYER_HEADERS), len(MAP_HEADERS))
NOTES = (
    "Średnie na mapę. GD/CSD/XPD@N: różnica golda, CS i XP do przeciwnika z tej samej linii w N. minucie "
    "(plus = przewaga). KP: udział w zabójstwach drużyny. DMG%/Gold%: udział w obrażeniach i goldzie drużyny. "
    "GD/CSD/XPD i śmierci przed 14. minutą są tylko z map z przebiegiem gry."
)


def pct(value: float | None) -> str:
    return f"{round(value * 100)}%" if value is not None else ""


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


def build_player_rows(rows: list[dict], player_order: list[str]) -> list[list]:
    """Our players' averages per (player, role), in roster order, then role order."""
    groups = defaultdict(list)
    for row in rows:
        if row["ours"]:
            groups[(row["player"], row["role"] or "?")].append(row)

    order = {player: i for i, player in enumerate(player_order)}
    role_order = {role: i for i, role in enumerate(ROLES)}
    result = []
    for (player, role), games in sorted(
        groups.items(), key=lambda item: (order.get(item[0][0], len(order)), item[0][0], role_order.get(item[0][1], len(ROLES)))
    ):
        n = len(games)
        wins = sum(bool(g["win"]) for g in games)
        kills, deaths, assists = (sum(g[key] for g in games) for key in ("kills", "deaths", "assists"))
        result.append([
            player, role, n, wins, n - wins, pct(wins / n),
            round((kills + assists) / max(deaths, 1), 2),
            pct(mean([g["kill_participation"] for g in games])),
            pct(mean([g["damage_share"] for g in games])),
            pct(mean([g["gold_share"] for g in games])),
            rounded(mean([g["cs_per_min"] for g in games]), 1),
            rounded(mean([vision_per_min(g) for g in games]), 2),
            rounded(mean([g["gold_diff_10"] for g in games])),
            rounded(mean([g["gold_diff_15"] for g in games])),
            rounded(mean([g["cs_diff_15"] for g in games])),
            rounded(mean([g["xp_diff_15"] for g in games])),
            rounded(mean([g["deaths_before_14"] for g in games]), 1),
            pct(sum(bool(g["first_blood"]) for g in games) / n),
        ])
    return result


def build_map_rows(rows: list[dict], labels: dict[str, str]) -> list[list]:
    """One row per our player per map, newest map first, players in role order."""
    role_order = {role: i for i, role in enumerate(ROLES)}
    ours = [row for row in rows if row["ours"]]
    ours.sort(key=lambda r: (-r["start_ms"], r["match_id"], role_order.get(r["role"], len(ROLES))))
    return [
        [
            f"{datetime.fromtimestamp(r['start_ms'] / 1000, TIMEZONE):%d.%m.%Y}",
            labels.get(r["match_id"], ""),
            "W" if r["win"] else "L",
            r["side"],
            r["player"],
            r["role"],
            r["champion"],
            r["lane_opponent"] or "",
            f"{r['kills']}/{r['deaths']}/{r['assists']}",
            pct(r["kill_participation"]),
            pct(r["damage_share"]),
            r["cs_per_min"],
            rounded(vision_per_min(r), 2),
            rounded(r["gold_diff_10"]),
            rounded(r["gold_diff_15"]),
            rounded(r["cs_diff_15"]),
            rounded(r["xp_diff_15"]),
            rounded(r["deaths_before_14"]),
        ]
        for r in ours
    ]


def build_stats_rows(rows: list[dict], team_games: list[dict], player_order: list[str], now: datetime) -> list[list]:
    """All rows of the Statystyki tab, each SHEET_COLS wide."""
    labels = {g["match_id"]: g["match_label"] for g in team_games}
    maps = {r["match_id"]: r for r in rows if r["ours"]}
    officials = sum(1 for r in maps.values() if r["tournament"])
    with_timeline = len({r["match_id"] for r in rows if r["ours"] and r["deaths_before_14"] is not None})

    grid = [
        ["Statystyki: scrimy i oficjale"],
        [f"Na podstawie {plural(len(maps), 'mapy', 'map', 'map')} ({plural(len(maps) - officials, 'scrim', 'scrimy', 'scrimów')}, "
         f"{plural(officials, 'oficjal', 'oficjale', 'oficjali')}), z przebiegiem gry: {with_timeline} · "
         f"aktualizacja {now:%d.%m.%Y %H:%M}"],
        [NOTES],
        PLAYER_HEADERS,
        *build_player_rows(rows, player_order),
        [],
        ["Mapy: nasi gracze w każdej grze"],
        MAP_HEADERS,
        *build_map_rows(rows, labels),
    ]
    return [row + [""] * (SHEET_COLS - len(row)) for row in grid]


async def update_stats_tab() -> int:
    """Rewrite the Statystyki tab (kept as the sheet's last tab). Returns the number of maps."""
    rows = await db.get_team_game_stats()
    grid = build_stats_rows(
        rows,
        await db.get_team_games(),
        [row["nick"] for row in await team_sheet.read_roster()],
        datetime.now(TIMEZONE),
    )
    await team_sheet.replace_tab(
        STATS_TAB, [{"range": f"A1:{rowcol_to_a1(len(grid), SHEET_COLS)}", "values": grid}],
        len(grid), SHEET_COLS, keep_last=True,
    )
    return len({r["match_id"] for r in rows if r["ours"]})
