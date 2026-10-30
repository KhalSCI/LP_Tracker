"""Tests for the Statystyki tab."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import cogs.scrims as scrims
import stats_tab
from config import TIMEZONE, SHEET_GUILD_ID
from stats_tab import build_player_rows, build_map_rows, build_stats_rows, SHEET_COLS

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

        assert row[:7] == ["khal", "ADC", 2, 1, 1, "50%", round((8 + 12) / 6, 2)]
        assert row[7] == "60%"  # KP
        assert row[10:12] == [9.0, 1.0]  # CS/min, vision per minute
        assert row[12:16] == [200, 200, 10, 300]
        assert row[16:] == [1.0, "50%"]

    def test_zero_is_not_blank_and_missing_timeline_is(self):
        [row] = build_player_rows([stat(kill_participation=0.0, gold_diff_10=None, gold_diff_15=None)], ["khal"])
        assert row[7] == "0%" and row[12:14] == ["", ""]

    def test_roster_order_then_role(self):
        rows = [stat(player="khal"), stat(player="drewin", role="Top"), stat(player="drewin", role="Mid"),
                stat(player="enemy", ours=False), stat(player="ex", role="Top")]
        assert [r[:2] for r in build_player_rows(rows, ["drewin", "khal"])] == [
            ["drewin", "Top"], ["drewin", "Mid"], ["khal", "ADC"], ["ex", "Top"]]


class TestMapRows:
    """Test cases for build_map_rows and the whole tab."""

    def test_newest_map_first_then_role(self):
        rows = [stat("EUW1_1", player="khal", role="ADC"), stat("EUW1_1", player="drewin", role="Top"),
                stat("EUW1_2", player="khal", role="ADC", start_ms=START + DAY_MS, win=False, lane_opponent=None)]

        result = build_map_rows(rows, {"EUW1_1": "29.10 18:00 · Scrim", "EUW1_2": "30.10 18:00 · Liga"})

        assert [(r[1], r[4]) for r in result] == [
            ("30.10 18:00 · Liga", "khal"), ("29.10 18:00 · Scrim", "drewin"), ("29.10 18:00 · Scrim", "khal")]
        assert result[0][:9] == ["30.10.2026", "30.10 18:00 · Liga", "L", "Blue", "khal", "ADC", "Ashe", "", "4/2/6"]

    def test_tab(self):
        rows = [stat("EUW1_1"), stat("EUW1_2", tournament=True, deaths_before_14=None)]
        games = [{"match_id": "EUW1_1", "match_label": "a"}, {"match_id": "EUW1_2", "match_label": "b"}]

        grid = build_stats_rows(rows, games, ["khal"], datetime(2026, 10, 30, 18, 0, tzinfo=TIMEZONE))

        assert all(len(row) == SHEET_COLS for row in grid)
        assert grid[1][0].startswith("Na podstawie 2 map (1 scrim, 1 oficjal), z przebiegiem gry: 1")
        assert grid[3][0] == "Gracz" and grid[4][0] == "khal"
        assert grid[6][0] == "Mapy: nasi gracze w każdej grze" and grid[7][0] == "Data" and len(grid) == 10


class TestUpdates:
    """Test cases for writing the tab."""

    async def test_kept_as_the_last_tab(self, monkeypatch):
        sheet = SimpleNamespace(replace_tab=AsyncMock(), read_roster=AsyncMock(return_value=[{"nick": "khal"}]))
        monkeypatch.setattr(stats_tab, "team_sheet", sheet)
        monkeypatch.setattr(stats_tab.db, "get_team_game_stats", AsyncMock(return_value=[stat(), stat(ours=False)]))
        monkeypatch.setattr(stats_tab.db, "get_team_games", AsyncMock(return_value=[]))

        assert await stats_tab.update_stats_tab() == 1

        tab, updates, rows, cols = sheet.replace_tab.await_args.args
        assert tab == "Statystyki" and sheet.replace_tab.await_args.kwargs == {"keep_last": True}
        assert updates[0]["range"] == f"A1:R{rows}" and cols == SHEET_COLS

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
