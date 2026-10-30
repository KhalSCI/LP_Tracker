import asyncio
import threading
from pathlib import Path

import gspread
from gspread.utils import ValueRenderOption, DateTimeOption, rowcol_to_a1

from config import GOOGLE_SHEET_ID, GOOGLE_SERVICE_ACCOUNT_FILE

# "Skład" rows 8-19: Rola | Nick Discord | Imię i nazwisko | Status | Riot ID EUW
ROSTER_TAB = "Skład"
ROSTER_RANGE = "A8:E19"
SOLOQ_TAB = "SoloQ"
# "Mecze", "Mapy" and "Drafty" have their column headers in row 4; code finds columns by header name
HEADER_ROW = 4
MATCHES_TAB = "Mecze"
MAPS_TAB = "Mapy"
DRAFTS_TAB = "Drafty"


class TeamSheet:
    """Client for the team's Google spreadsheet. gspread is blocking, so calls run in a thread."""

    def __init__(self, sheet_id: str = None, credentials_file: str = None):
        self.sheet_id = sheet_id or GOOGLE_SHEET_ID
        self.credentials_file = Path(credentials_file or GOOGLE_SERVICE_ACCOUNT_FILE).expanduser()
        self._spreadsheet: gspread.Spreadsheet | None = None
        # The sheet sync, reminders and uploads share one HTTP session from different threads
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

    def _read_tab(self, tab: str) -> list[list]:
        with self._lock:
            rows = self._open().worksheet(tab).get(
                value_render_option=ValueRenderOption.unformatted,
                date_time_render_option=DateTimeOption.serial_number,
            )
        return rows[HEADER_ROW - 1:]

    async def read_tab(self, tab: str) -> list[list]:
        """
        Get the raw rows of a tab from its header row down, with dates and times as
        spreadsheet serial numbers.
        """
        return await asyncio.to_thread(self._read_tab, tab)

    async def read_matches(self) -> list[list]:
        """Get the raw rows of the matches tab from its header row down."""
        return await self.read_tab(MATCHES_TAB)

    def _append_rows(self, tab: str, key_header: str, rows: list[dict], value_input_option: str) -> int:
        with self._lock:
            worksheet = self._open().worksheet(tab)
            values = worksheet.get(value_render_option=ValueRenderOption.unformatted)
            header = [str(cell).strip() for cell in values[HEADER_ROW - 1]]
            key = header.index(key_header)

            last_row = HEADER_ROW
            for row_number, row in enumerate(values[HEADER_ROW:], start=HEADER_ROW + 1):
                if key < len(row) and row[key] not in ("", None):
                    last_row = row_number
            first_row = last_row + 1

            # Only the first column with each header: hidden helper lists reuse names like "Top".
            # Cells left as None are skipped, so formula columns keep working.
            columns = {name: header.index(name) for row in rows for name in row}
            width = max(columns.values()) + 1
            data = []
            for row in rows:
                cells = [None] * width
                for name, value in row.items():
                    cells[columns[name]] = value
                data.append(cells)

            end = rowcol_to_a1(first_row + len(rows) - 1, width)
            worksheet.batch_update(
                [{"range": f"A{first_row}:{end}", "values": data}],
                value_input_option=value_input_option,
            )
            return first_row

    async def append_rows(self, tab: str, key_header: str, rows: list[dict], value_input_option: str = "RAW") -> int:
        """
        Write rows ({header: value}) below the last row whose key_header cell is filled.
        Returns the first written row number.
        """
        return await asyncio.to_thread(self._append_rows, tab, key_header, rows, value_input_option)

    def _write(self, tab: str, updates: list[dict], rows: int, cols: int) -> None:
        with self._lock:
            worksheet = self._open().worksheet(tab)
            if worksheet.row_count < rows or worksheet.col_count < cols:
                worksheet.resize(rows=max(rows, worksheet.row_count), cols=max(cols, worksheet.col_count))
            worksheet.batch_update(updates, value_input_option="RAW")

    async def write(self, tab: str, updates: list[dict], rows: int, cols: int) -> None:
        """
        Write value ranges (e.g. {"range": "A5:N16", "values": [[...]]}) to a tab in one
        request, first growing the tab to at least rows x cols.
        """
        await asyncio.to_thread(self._write, tab, updates, rows, cols)

    async def write_soloq(self, updates: list[dict], rows: int, cols: int) -> None:
        """Write value ranges to the SoloQ tab (see write)."""
        await self.write(SOLOQ_TAB, updates, rows, cols)


# Global instance
team_sheet = TeamSheet()
