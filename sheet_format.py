"""Formatting requests (Sheets API batchUpdate) for the tabs the bot writes; used with team_sheet.format_tab."""

HEADER_BACKGROUND = {"red": 0.20, "green": 0.27, "blue": 0.36}
BAND_BACKGROUND = {"red": 0.87, "green": 0.92, "blue": 0.98}
WHITE = {"red": 1, "green": 1, "blue": 1}
GRAY = {"red": 0.4, "green": 0.4, "blue": 0.4}
GOOD = {"red": 0.83, "green": 0.93, "blue": 0.83}
BAD = {"red": 0.96, "green": 0.82, "blue": 0.82}
# Column kinds: "name" (bold text), "text", "int", "pct", "wr" (percent, colored), "dec1", "dec2",
# "diff" (signed, colored). Patterns use "." whatever the sheet's locale.
NUMBER_FORMATS = {
    "pct": {"type": "PERCENT", "pattern": "0%"},
    "wr": {"type": "PERCENT", "pattern": "0%"},
    "dec1": {"type": "NUMBER", "pattern": "0.0"},
    "dec2": {"type": "NUMBER", "pattern": "0.00"},
    "diff": {"type": "NUMBER", "pattern": "+0;-0;0"},
}
# Winrates colored at or beyond these (written as percentages so the sheet's locale parses them)
GOOD_WR, BAD_WR = "60%", "40%"


def grid_range(sheet_id: int, start_row: int, end_row: int, start_col: int, end_col: int) -> dict:
    """Rows and columns as 0-based indexes, ends exclusive."""
    return {"sheetId": sheet_id, "startRowIndex": start_row, "endRowIndex": end_row,
            "startColumnIndex": start_col, "endColumnIndex": end_col}


def cell_format(cells: dict, fmt: dict) -> dict:
    return {"repeatCell": {"range": cells, "cell": {"userEnteredFormat": fmt},
                           "fields": "userEnteredFormat(" + ",".join(fmt) + ")"}}


def color_rule(cells: dict, condition: str, value: str, color: dict) -> dict:
    return {"addConditionalFormatRule": {"index": 0, "rule": {
        "ranges": [cells],
        "booleanRule": {"condition": {"type": condition, "values": [{"userEnteredValue": value}]},
                        "format": {"backgroundColor": color}},
    }}}


def table_format(sheet_id: int, header: int, end: int, kinds: list[str], first_col: int = 0) -> list[dict]:
    """
    Header style, number formats and colors of a table: header row index, end row index
    (exclusive) and one kind per column, starting at first_col.
    """
    requests = [cell_format(grid_range(sheet_id, header, header + 1, first_col, first_col + len(kinds)), {
        "backgroundColor": HEADER_BACKGROUND, "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
        "wrapStrategy": "WRAP", "textFormat": {"bold": True, "foregroundColor": WHITE},
    })]
    if end <= header + 1:
        return requests
    for i, kind in enumerate(kinds, start=first_col):
        cells = grid_range(sheet_id, header + 1, end, i, i + 1)
        if kind == "name":
            requests.append(cell_format(cells, {"textFormat": {"bold": True}}))
        elif kind != "text":
            fmt = {"horizontalAlignment": "CENTER"}
            if kind in NUMBER_FORMATS:
                fmt["numberFormat"] = NUMBER_FORMATS[kind]
            requests.append(cell_format(cells, fmt))
        if kind == "diff":
            requests += [color_rule(cells, "NUMBER_GREATER", "0", GOOD), color_rule(cells, "NUMBER_LESS", "0", BAD)]
        elif kind == "wr":
            requests += [color_rule(cells, "NUMBER_GREATER_THAN_EQ", GOOD_WR, GOOD),
                         color_rule(cells, "NUMBER_LESS_THAN_EQ", BAD_WR, BAD)]
    return requests


def column_widths(sheet_id: int, count: int, default: int, widths: dict[int, int]) -> list[dict]:
    """Every column of the first count at default pixels, except those in widths."""
    def width(start: int, end: int, pixels: int) -> dict:
        return {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": start, "endIndex": end},
            "properties": {"pixelSize": pixels}, "fields": "pixelSize",
        }}
    return [width(0, count, default)] + [width(column, column + 1, pixels) for column, pixels in widths.items()]


def row_height(sheet_id: int, row: int, pixels: int) -> dict:
    return {"updateDimensionProperties": {
        "range": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": row, "endIndex": row + 1},
        "properties": {"pixelSize": pixels}, "fields": "pixelSize",
    }}


def frozen_rows(sheet_id: int, rows: int) -> dict:
    return {"updateSheetProperties": {
        "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": rows, "frozenColumnCount": 0}},
        "fields": "gridProperties(frozenRowCount,frozenColumnCount)",
    }}
