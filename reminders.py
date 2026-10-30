from datetime import datetime, timedelta

import discord

from config import TIMEZONE

# Day zero of spreadsheet date serial numbers
SHEET_EPOCH = datetime(1899, 12, 30)
# Headers of the "Mecze" columns used for reminders
MATCH_COLUMNS = {
    "date": "Data",
    "time": "Godz.",
    "type": "Typ",
    "opponent": "Przeciwnik",
    "event": "Turniej / nazwa",
    "status": "Rezultat",
    "remind": "Przypomnienie",
    "notes": "Notatki",
}
REQUIRED_COLUMNS = ("date", "status", "remind")
# "Rezultat" of matches that haven't been played yet
PLANNED_STATUS = "Zaplanowany"
# Reminders for matches with a start time, as (kind, how long before the start). Each one is
# only sent until the next one is due, so a match first seen 30 minutes ahead gets one ping.
TIMED_REMINDERS = [("24h", timedelta(hours=24)), ("1h", timedelta(hours=1))]
# A match without a start time gets one reminder at this hour the day before
UNTIMED_REMINDER_HOUR = 18
WEEKDAYS = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]


def from_sheet_serial(serial: float) -> datetime:
    """Convert a spreadsheet date serial number to a local datetime, to the minute."""
    return (SHEET_EPOCH + timedelta(minutes=round(serial * 24 * 60))).replace(tzinfo=TIMEZONE)


def parse_planned_matches(rows: list[list]) -> list[dict]:
    """
    Planned matches with the reminder checkbox ticked, from raw "Mecze" rows starting
    with the header row. Columns are found by their headers, so they can be moved.
    """
    if not rows:
        return []
    header = [str(cell).strip() for cell in rows[0]]
    columns = {key: header.index(name) for key, name in MATCH_COLUMNS.items() if name in header}
    missing = [MATCH_COLUMNS[key] for key in REQUIRED_COLUMNS if key not in columns]
    if missing:
        raise ValueError(f'"Mecze" has no column {", ".join(missing)}')

    def cell(row: list, key: str):
        i = columns.get(key)
        return row[i] if i is not None and i < len(row) else ""

    matches = []
    for row in rows[1:]:
        date, start_time = cell(row, "date"), cell(row, "time")
        if not isinstance(date, (int, float)) or cell(row, "status") != PLANNED_STATUS or cell(row, "remind") is not True:
            continue
        has_time = isinstance(start_time, (int, float))
        matches.append({
            "start": from_sheet_serial(int(date) + (start_time % 1 if has_time else 0)),
            "has_time": has_time,
            "type": str(cell(row, "type")).strip(),
            "event": str(cell(row, "event")).strip(),
            "opponent": str(cell(row, "opponent")).strip(),
            "notes": str(cell(row, "notes")).strip(),
        })
    return matches


def reminder_windows(match: dict) -> list[tuple[str, datetime, datetime]]:
    """When each reminder for the match may be sent, as (kind, from, until)."""
    start = match["start"]
    if not match["has_time"]:
        day_before = (start - timedelta(days=1)).replace(hour=UNTIMED_REMINDER_HOUR)
        return [("day", day_before, start + timedelta(days=1))]

    windows = []
    for i, (kind, before) in enumerate(TIMED_REMINDERS):
        until = start - TIMED_REMINDERS[i + 1][1] if i + 1 < len(TIMED_REMINDERS) else start
        windows.append((kind, start - before, until))
    return windows


def reminder_key(match: dict, kind: str) -> str:
    """Identifies one reminder; changing the match's date, time or name makes it a new one."""
    start = f"{match['start']:%Y-%m-%d %H:%M}" if match["has_time"] else f"{match['start']:%Y-%m-%d}"
    return "|".join([start, match["type"], match["event"], match["opponent"], kind])


def due_reminders(matches: list[dict], now: datetime) -> list[tuple[str, dict]]:
    """Reminders that should be sent now, as (key, match)."""
    due = []
    for match in matches:
        for kind, since, until in reminder_windows(match):
            if since <= now < until:
                due.append((reminder_key(match, kind), match))
    return due


def match_title(match: dict) -> str:
    title = match["event"] or match["type"] or "Mecz"
    if match["opponent"]:
        title += f" vs {match['opponent']}"
    return title


def describe_start(match: dict, now: datetime) -> str:
    """When the match starts, from now: "Jutro o 17:00", "Dziś o 17:00", "Piątek 06.11, godzina jeszcze nieznana"."""
    start = match["start"]
    days = (start.date() - now.date()).days
    day = {0: "Dziś", 1: "Jutro"}.get(days, f"{WEEKDAYS[start.weekday()].capitalize()} {start:%d.%m}")
    if not match["has_time"]:
        return f"{day}, godzina jeszcze nieznana"
    return f"{day} o {start:%H:%M}"


def create_reminder_message(match: dict, now: datetime, role_id: int = 0) -> tuple[str, discord.Embed]:
    """Create the text (with the role ping) and embed of a match reminder."""
    title = match_title(match)
    mention = f"<@&{role_id}> " if role_id else ""
    content = f"{mention}⏰ {describe_start(match, now)}: **{discord.utils.escape_markdown(title)}**"

    embed = discord.Embed(title=title, color=discord.Color.orange())
    start = match["start"]
    if match["has_time"]:
        timestamp = int(start.timestamp())
        # Discord shows these in each reader's own time zone and language
        embed.add_field(name="Start", value=f"<t:{timestamp}:F> (<t:{timestamp}:R>)", inline=False)
    else:
        embed.add_field(name="Dzień", value=f"{WEEKDAYS[start.weekday()]} {start:%d.%m.%Y}, godzina nieznana", inline=False)
    if match["type"]:
        embed.add_field(name="Typ", value=match["type"], inline=True)
    if match["notes"]:
        embed.add_field(name="Notatki", value=match["notes"][:1024], inline=False)
    embed.set_footer(text="Z zakładki Mecze w arkuszu drużyny")
    return content, embed
