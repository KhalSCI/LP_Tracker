import asyncio
import threading
import time
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
        self._roster_cache: tuple[float, list[dict]] | None = None

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
        roster = await asyncio.to_thread(self._read_roster)
        self._roster_cache = (time.monotonic(), roster)
        return roster

    async def read_roster_cached(self, max_age: float = 3600) -> list[dict]:
        """The roster as last read, if not older than max_age seconds (for checks every few minutes)."""
        if self._roster_cache and time.monotonic() - self._roster_cache[0] < max_age:
            return self._roster_cache[1]
        return await self.read_roster()

    def _read_tab(self, tab: str, header_row: int) -> list[list]:
        with self._lock:
            rows = self._open().worksheet(tab).get(
                value_render_option=ValueRenderOption.unformatted,
                date_time_render_option=DateTimeOption.serial_number,
            )
        return rows[header_row - 1:]

    async def read_tab(self, tab: str, header_row: int = HEADER_ROW) -> list[list]:
        """
        Get the raw rows of a tab from its header row down, with dates and times as
        spreadsheet serial numbers.
        """
        return await asyncio.to_thread(self._read_tab, tab, header_row)

    def _tab_url(self, tab: str) -> str:
        with self._lock:
            worksheet_id = self._open().worksheet(tab).id
        return f"https://docs.google.com/spreadsheets/d/{self.sheet_id}/edit#gid={worksheet_id}"

    async def tab_url(self, tab: str) -> str:
        """Link that opens the sheet on this tab."""
        return await asyncio.to_thread(self._tab_url, tab)

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

    def _create_tab(self, tab: str, updates: list[dict], rows: int, cols: int, frozen_rows: int) -> bool:
        with self._lock:
            spreadsheet = self._open()
            try:
                spreadsheet.worksheet(tab)
                return False
            except gspread.WorksheetNotFound:
                worksheet = spreadsheet.add_worksheet(tab, rows=rows, cols=cols)
            worksheet.batch_update(updates, value_input_option="RAW")
            if frozen_rows:
                worksheet.freeze(rows=frozen_rows)
            return True

    async def create_tab(self, tab: str, updates: list[dict], rows: int, cols: int, frozen_rows: int = 0) -> bool:
        """Add a tab with initial values (e.g. headers) unless it exists. Returns True if it was added."""
        return await asyncio.to_thread(self._create_tab, tab, updates, rows, cols, frozen_rows)

    def _replace_tab(self, tab: str, updates: list[dict], rows: int, cols: int, keep_last: bool) -> None:
        with self._lock:
            spreadsheet = self._open()
            try:
                worksheet = spreadsheet.worksheet(tab)
            except gspread.WorksheetNotFound:
                worksheet = spreadsheet.add_worksheet(tab, rows=rows, cols=cols)
            worksheet.clear()
            if worksheet.row_count < rows or worksheet.col_count < cols:
                worksheet.resize(rows=max(rows, worksheet.row_count), cols=max(cols, worksheet.col_count))
            worksheet.batch_update(updates, value_input_option="RAW")
            if keep_last:
                last = len(spreadsheet.worksheets(exclude_hidden=False)) - 1
                if worksheet.index != last:
                    worksheet.update_index(last)

    async def replace_tab(self, tab: str, updates: list[dict], rows: int, cols: int, keep_last: bool = False) -> None:
        """
        Clear a tab written only by the bot (creating it if missing) and write value
        ranges to it, growing it to at least rows x cols. With keep_last the tab is
        moved to the end when other tabs were added after it.
        """
        await asyncio.to_thread(self._replace_tab, tab, updates, rows, cols, keep_last)

    def _format_tab(self, tab: str, build_requests) -> None:
        with self._lock:
            spreadsheet = self._open()
            sheet_id = spreadsheet.worksheet(tab).id
            metadata = spreadsheet.fetch_sheet_metadata(
                params={"fields": "sheets(properties.sheetId,conditionalFormats)"}
            )
            rules = next(
                (len(sheet.get("conditionalFormats", [])) for sheet in metadata["sheets"]
                 if sheet["properties"]["sheetId"] == sheet_id),
                0,
            )
            whole = {"sheetId": sheet_id}
            reset = [{"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}} for _ in range(rules)]
            reset += [
                {"unmergeCells": {"range": whole}},
                {"repeatCell": {"range": whole, "cell": {}, "fields": "userEnteredFormat"}},
            ]
            spreadsheet.batch_update({"requests": reset + build_requests(sheet_id)})

    async def format_tab(self, tab: str, build_requests) -> None:
        """
        Replace the formatting of a tab written only by the bot (cell formats, merges and
        conditional colors) with the requests build_requests(sheet_id) returns.
        """
        await asyncio.to_thread(self._format_tab, tab, build_requests)

    async def write_soloq(self, updates: list[dict], rows: int, cols: int) -> None:
        """Write value ranges to the SoloQ tab (see write)."""
        await self.write(SOLOQ_TAB, updates, rows, cols)


# Global instance
team_sheet = TeamSheet()
