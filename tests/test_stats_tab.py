"""Tests for the Statystyki tab."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import cogs.scrims as scrims
import stats_tab
from config import TIMEZONE, SHEET_GUILD_ID
from sheets import TeamSheet
from stats_tab import build_player_rows, build_matchup_rows, build_stats_rows, build_format_requests, SHEET_COLS

DAY_MS = 24 * 60 * 60 * 1000
START = int(datetime(2026, 10, 29, 18, 0, tzinfo=TIMEZONE).timestamp() * 1000)


def stat(match_id="EUW1_1", player="khal", role="ADC", ours=True, win=True, start_ms=START, **overrides) -> dict:
    """A team_game_stats row."""
    row = {
        "match_id": match_id, "participant_id": 4, "start_ms": start_ms, "duration_s": 1800, "patch": "16.21",
        "tournament": False, "ours": ours, "team_id": 100, "side": "Blue", "player": player, "riot_id": "X#1",
        "role": role, "champion": "Ashe", "win": win, "kills": 4, "deaths": 2, "assists": 6,
        "kill_participation": 0.5, "cs": 270, "cs_per_min": 9.0, "gold": 12000, "gold_share": 0.25,
        "damage": 20000, "damage_share": 0.3, "damage_taken": 10000, "vision_score": 30, "wards_placed": 10,
        "wards_killed": 3, "control_wards": 2, "first_blood": False, "deaths_before_14": 1,
        "gold_diff_10": 200, "gold_diff_15": 600, "cs_diff_10": 5, "cs_diff_15": 10, "xp_diff_15": 300,
        "lane_opponent": "Jinx",
    }
    row.update(overrides)
    return row


class TestPlayerRows:
    """Test cases for build_player_rows."""

    def test_averages(self):
        rows = [
            stat("EUW1_1", win=True, gold_diff_15=600, kill_participation=0.5, first_blood=True),
            stat("EUW1_2", win=False, gold_diff_15=-200, kill_participation=0.7, deaths=4, deaths_before_14=None),
        ]

        [row] = build_player_rows(rows, ["khal"])

        assert row[:7] == ["khal", "ADC", 2, 1, 1, 0.5, round((8 + 12) / 6, 2)]
        assert row[7] == 0.6  # KP
        assert row[10:12] == [9.0, 1.0]  # CS/min, vision per minute
        assert row[12:16] == [200, 200, 10, 300]
        assert row[16:] == [1.0, 0.5]

    def test_zero_is_not_blank_and_missing_timeline_is(self):
        [row] = build_player_rows([stat(kill_participation=0.0, gold_diff_10=None, gold_diff_15=None)], ["khal"])
        assert row[7] == 0.0 and row[12:14] == ["", ""]

    def test_roster_order_then_role(self):
        rows = [stat(player="khal"), stat(player="drewin", role="Top"), stat(player="drewin", role="Mid"),
                stat(player="enemy", ours=False), stat(player="ex", role="Top")]
        assert [r[:2] for r in build_player_rows(rows, ["drewin", "khal"])] == [
            ["drewin", "Top"], ["drewin", "Mid"], ["khal", "ADC"], ["ex", "Top"]]


class TestMatchups:
    """Test cases for build_matchup_rows and the whole tab."""

    def test_grouped_by_champion_and_lane_opponent(self):
        rows = [
            stat("EUW1_1", champion="Ashe", lane_opponent="Jinx", gold_diff_15=600, win=True),
            stat("EUW1_2", champion="Ashe", lane_opponent="Jinx", gold_diff_15=-200, win=False),
            stat("EUW1_3", champion="Ashe", lane_opponent="Caitlyn", gold_diff_15=900),
            stat("EUW1_4", champion="Xayah", lane_opponent="Jinx", gold_diff_15=None, gold_diff_10=None),
            stat("EUW1_5", player="drewin", role="Top", champion="Aatrox", lane_opponent="Sylas"),
            stat("EUW1_6", lane_opponent=None),  # no lane opponent known
            stat("EUW1_1", ours=False, player="E#1", champion="Jinx", lane_opponent="Ashe"),
        ]

        result = build_matchup_rows(rows, ["drewin", "khal"])

        assert [r[:5] for r in result] == [
            ["drewin", "Top", "Aatrox", "Sylas", 1],
            ["khal", "ADC", "Ashe", "Jinx", 2],
            ["khal", "ADC", "Ashe", "Caitlyn", 1],
            ["khal", "ADC", "Xayah", "Jinx", 1],
        ]
        assert result[1][5:] == [1, 1, 0.5, round(20 / 4, 2), 200, 200, 10, 300, 1.0]
        assert result[3][9:11] == ["", ""]

    def test_tab(self):
        rows = [stat("EUW1_1"), stat("EUW1_2", tournament=True, deaths_before_14=None)]

        grid, layout = build_stats_rows(rows, ["khal"], datetime(2026, 10, 30, 18, 0, tzinfo=TIMEZONE))

        assert all(len(row) == SHEET_COLS for row in grid)
        assert grid[1][0].startswith("Na podstawie 2 map (1 scrim, 1 oficjal), z przebiegiem gry: 1")
        assert grid[3][0] == "Gracz" and grid[4][0] == "khal"
        assert grid[6][0] == "Matchupy na linii" and grid[7][:4] == ["Gracz", "Rola", "Champion", "Przeciwnik"]
        assert grid[8][:5] == ["khal", "ADC", "Ashe", "Jinx", 2] and len(grid) == 9
        assert layout == {"players": (3, 5), "matchups": (6, 7, 9)}


class TestFormatting:
    """Test cases for the tab's formatting."""

    def test_requests(self):
        requests = build_format_requests(7, {"players": (3, 9), "matchups": (10, 11, 20)})

        rules = [r["addConditionalFormatRule"]["rule"] for r in requests if "addConditionalFormatRule" in r]
        # Players: 4 difference columns + WR, two colors each; the same for matchups
        assert len(rules) == 20
        gd15 = [r for r in rules if r["ranges"][0]["startColumnIndex"] == 13 and r["ranges"][0]["startRowIndex"] == 4]
        assert {r["booleanRule"]["condition"]["type"] for r in gd15} == {"NUMBER_GREATER", "NUMBER_LESS"}
        wr = [r for r in rules if r["ranges"][0]["startColumnIndex"] == 7 and r["ranges"][0]["startRowIndex"] == 12]
        assert [r["booleanRule"]["condition"]["values"][0]["userEnteredValue"] for r in wr] == ["60%", "40%"]
        headers = [r["repeatCell"]["range"] for r in requests
                   if "repeatCell" in r and "backgroundColor" in r["repeatCell"]["cell"]["userEnteredFormat"]]
        assert [(h["startRowIndex"], h["endColumnIndex"]) for h in headers] == [(3, 18), (11, 14)]
        assert all(r.get("repeatCell", {}).get("range", {}).get("sheetId", 7) == 7 for r in requests)

    def test_empty_table_gets_only_a_header(self):
        requests = build_format_requests(7, {"players": (3, 4), "matchups": (5, 6, 6)})
        assert not [r for r in requests if "addConditionalFormatRule" in r]

    def test_old_formatting_is_cleared_first(self, monkeypatch):
        sent = []
        spreadsheet = SimpleNamespace(
            worksheet=lambda tab: SimpleNamespace(id=7),
            fetch_sheet_metadata=lambda params: {"sheets": [
                {"properties": {"sheetId": 1}, "conditionalFormats": [{}]},
                {"properties": {"sheetId": 7}, "conditionalFormats": [{}, {}]},
            ]},
            batch_update=lambda body: sent.append(body),
        )
        sheet = TeamSheet("id", "key.json")
        monkeypatch.setattr(sheet, "_open", lambda: spreadsheet)

        sheet._format_tab("Statystyki", lambda sheet_id: [{"mine": sheet_id}])

        requests = sent[0]["requests"]
        assert requests[:2] == [{"deleteConditionalFormatRule": {"sheetId": 7, "index": 0}}] * 2
        assert "unmergeCells" in requests[2] and requests[3]["repeatCell"]["fields"] == "userEnteredFormat"
        assert requests[-1] == {"mine": 7}


class TestUpdates:
    """Test cases for writing the tab."""

    async def test_kept_as_the_last_tab(self, monkeypatch):
        sheet = SimpleNamespace(replace_tab=AsyncMock(), format_tab=AsyncMock(),
                                read_roster=AsyncMock(return_value=[{"nick": "khal"}]))
        monkeypatch.setattr(stats_tab, "team_sheet", sheet)
        monkeypatch.setattr(stats_tab.db, "get_team_game_stats", AsyncMock(return_value=[stat(), stat(ours=False)]))

        assert await stats_tab.update_stats_tab() == 1

        tab, updates, rows, cols = sheet.replace_tab.await_args.args
        assert tab == "Statystyki" and sheet.replace_tab.await_args.kwargs == {"keep_last": True}
        assert updates[0]["range"] == f"A1:R{rows}" and cols == SHEET_COLS
        format_tab, build = sheet.format_tab.await_args.args
        assert format_tab == "Statystyki" and any("addConditionalFormatRule" in r for r in build(7))

    @pytest.mark.parametrize("summary, refreshed", [
        ({"added": [1], "timelines_added": 0}, True),
        ({"added": [], "timelines_added": 2}, True),
        ({"added": [], "timelines_added": 0}, False),
    ])
    async def test_refreshed_after_an_upload(self, monkeypatch, tmp_path, summary, refreshed):
        monkeypatch.setattr(scrims, "UPLOADS_DIR", tmp_path)
        monkeypatch.setattr(scrims, "ingest_client_upload", AsyncMock(return_value=summary))
        monkeypatch.setattr(scrims, "format_summary", lambda s: "ok")
        update = AsyncMock()
        monkeypatch.setattr(scrims, "update_stats_tab", update)
        attachment = SimpleNamespace(filename=scrims.UPLOAD_FILENAME, read=AsyncMock(return_value=b'{"games": []}'))
        message = SimpleNamespace(webhook_id=1, guild=SimpleNamespace(id=SHEET_GUILD_ID), attachments=[attachment],
                                  id=7, reply=AsyncMock())

        await scrims.ScrimsCog(None).on_message(message)

        message.reply.assert_awaited_once()
        assert update.await_count == (1 if refreshed else 0)
