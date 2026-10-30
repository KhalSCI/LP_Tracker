"""Pings for Main players who haven't filled in the "Dostępność" tab for the next days."""
from datetime import date, datetime, timedelta

from config import AVAILABILITY_DAYS
from reminders import from_sheet_serial, WEEKDAYS
from sheets import team_sheet

AVAILABILITY_TAB = "Dostępność"
# Player nicks are in row 5 (TRANSPOSE of the roster's nick column), dates in column "Data" below
HEADER_ROW = 5
DATE_HEADER = "Data"
MAIN_STATUS = "main"
# "nie" (can't play) is an answer; "?" (don't know yet) isn't
NO_ANSWER = {"", "?"}


def parse_day(value) -> date | None:
    """A "Data" cell as a date: a spreadsheet serial number or text like 31.10.2026."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return from_sheet_serial(value).date()
    try:
        return datetime.strptime(str(value).strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def days_to_check(today: date, days: int) -> list[date]:
    """The next days, starting tomorrow."""
    return [today + timedelta(days=i) for i in range(1, days + 1)]


def missing_availability(rows: list[list], nicks: list[str], days: list[date]) -> dict[str, list[date]]:
    """
    Which of the days each nick left without an answer (empty or "?"), from raw
    "Dostępność" rows (header row first). Nicks without missing days are left out.
    Nicks without a column and days without a row are skipped and logged.
    """
    if not rows:
        return {}
    header = [str(cell).strip().lower() for cell in rows[0]]
    date_column = header.index(DATE_HEADER.lower()) if DATE_HEADER.lower() in header else 0

    by_day = {}
    for row in rows[1:]:
        day = parse_day(row[date_column]) if date_column < len(row) else None
        if day:
            by_day.setdefault(day, row)
    for day in days:
        if day not in by_day:
            print(f"Availability: no row for {day:%d.%m.%Y} in {AVAILABILITY_TAB}")

    missing = {}
    for nick in nicks:
        if nick.lower() not in header:
            print(f"Availability: no column for '{nick}' in {AVAILABILITY_TAB}")
            continue
        column = header.index(nick.lower())
        empty = []
        for day in days:
            row = by_day.get(day)
            if row is None:
                continue
            value = row[column] if column < len(row) else ""
            if str(value if value is not None else "").strip() in NO_ANSWER:
                empty.append(day)
        if empty:
            missing[nick] = empty
    return missing


def main_players(roster: list[dict]) -> list[str]:
    """Nicks of the roster's Main players."""
    return [row["nick"] for row in roster if row["nick"] and row["status"].strip().lower() == MAIN_STATUS]


def format_day(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()]} {day:%d.%m}"


def create_availability_message(missing: list[tuple[str, list[date]]], tab_url: str) -> str:
    """The reminder, one line per player as (mention, missing days)."""
    lines = ["📅 Uzupełnijcie dostępność na najbliższe dni (godziny albo „nie”, „?” się nie liczy):"]
    for who, days in missing:
        lines.append(f"• {who}: {', '.join(format_day(day) for day in days)}")
    lines.append(f"Zakładka Dostępność: <{tab_url}>")
    return "\n".join(lines)


async def find_missing_availability(today: date) -> dict[str, list[date]]:
    """Main players without an answer in "Dostępność" for the next AVAILABILITY_DAYS days."""
    roster = await team_sheet.read_roster()
    rows = await team_sheet.read_tab(AVAILABILITY_TAB, HEADER_ROW)
    return missing_availability(rows, main_players(roster), days_to_check(today, AVAILABILITY_DAYS))
