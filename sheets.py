import asyncio
import threading
from pathlib import Path

import gspread
from gspread.utils import ValueRenderOption, DateTimeOption

from config import GOOGLE_SHEET_ID, GOOGLE_SERVICE_ACCOUNT_FILE

# "Skład" rows 8-19: Rola | Nick Discord | Imię i nazwisko | Status | Riot ID EUW
ROSTER_TAB = "Skład"
ROSTER_RANGE = "A8:E19"
SOLOQ_TAB = "SoloQ"
# "Mecze" has its column headers in row 4; reminders find their columns by header name
MATCHES_TAB = "Mecze"
MATCHES_HEADER_ROW = 4


class TeamSheet:
    """Client for the team's Google spreadsheet. gspread is blocking, so calls run in a thread."""

    def __init__(self, sheet_id: str = None, credentials_file: str = None):
        self.sheet_id = sheet_id or GOOGLE_SHEET_ID
        self.credentials_file = Path(credentials_file or GOOGLE_SERVICE_ACCOUNT_FILE).expanduser()
        self._spreadsheet: gspread.Spreadsheet | None = None
        # The sheet sync and the reminder check share one HTTP session from different threads
        self._lock = threading.Lock()

    def _open(self) -> gspread.Spreadsheet:
        if self._spreadsheet is None:
            client = gspread.service_account(filename=str(self.credentials_file))
            self._spreadsheet = client.open_by_key(self.sheet_id)
        return self._spreadsheet

    def _read_roster(self) -> list[dict]:
        with self._lock:
            rows = self._open().worksheet(ROSTER_TAB).get(ROSTER_RANGE)
        roster = []
        for row in rows:
            row = [cell.strip() for cell in row] + [""] * (5 - len(row))
            role, nick, _full_name, status, riot_id = row[:5]
            if riot_id:
                roster.append({"role": role, "nick": nick, "status": status, "riot_id": riot_id})
        return roster

    async def read_roster(self) -> list[dict]:
        """Get the players listed with an EUW Riot ID in the roster tab, in sheet order."""
        return await asyncio.to_thread(self._read_roster)

    def _read_matches(self) -> list[list]:
        with self._lock:
            rows = self._open().worksheet(MATCHES_TAB).get(
                value_render_option=ValueRenderOption.unformatted,
                date_time_render_option=DateTimeOption.serial_number,
            )
        return rows[MATCHES_HEADER_ROW - 1:]

    async def read_matches(self) -> list[list]:
        """
        Get the raw rows of the matches tab from its header row down, with dates and
        times as spreadsheet serial numbers.
        """
        return await asyncio.to_thread(self._read_matches)

    def _write_soloq(self, updates: list[dict], rows: int, cols: int) -> None:
        with self._lock:
            worksheet = self._open().worksheet(SOLOQ_TAB)
            if worksheet.row_count < rows or worksheet.col_count < cols:
                worksheet.resize(rows=max(rows, worksheet.row_count), cols=max(cols, worksheet.col_count))
            worksheet.batch_update(updates, value_input_option="RAW")

    async def write_soloq(self, updates: list[dict], rows: int, cols: int) -> None:
        """
        Write value ranges (e.g. {"range": "A5:N16", "values": [[...]]}) to the SoloQ tab
        in one request, first growing the tab to at least rows x cols.
        """
        await asyncio.to_thread(self._write_soloq, updates, rows, cols)


# Global instance
team_sheet = TeamSheet()
