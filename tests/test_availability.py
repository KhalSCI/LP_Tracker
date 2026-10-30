"""Tests for the pings about the "Dostępność" tab."""
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot as bot_module
import database as db
from availability import (
    missing_availability, main_players, days_to_check, parse_day, create_availability_message,
)

CHANNEL_ID = 555
# Row 5 of the tab: dates, weekdays, matches, then player nicks
HEADER = ["Data", "Dzień", "Mecz (z zakładki Mecze)", "drewin", "kizi4880", "Weres", "freakk9", "",
          "iym0409", "khal", "drukarz_lp", "magik9285", "", "Mogą", "Pełny skład (godz.)", "Notatki"]


def serial(day: str) -> int:
    return (datetime.strptime(day, "%d.%m.%Y") - datetime(1899, 12, 30)).days


def row(day: str, **hours) -> list:
    """A raw row with the given hours per nick, e.g. row("31.10.2026", khal="18-22")."""
    cells = [serial(day), "", ""] + [""] * (len(HEADER) - 3)
    for nick, value in hours.items():
        cells[HEADER.index(nick)] = value
    return cells


ROWS = [
    HEADER,
    row("30.10.2026", drewin="1-24", khal="18-22"),
    row("31.10.2026", drewin="1-17", iym0409="nie", khal="?", drukarz_lp="nie"),
    row("01.11.2026", drewin="?", iym0409="nie", khal="?", drukarz_lp="?"),
    row("02.11.2026", drewin="16-24", iym0409="1-24", khal="18-22", drukarz_lp="18-24"),
]
DAYS = days_to_check(date(2026, 10, 30), 3)


class TestMissingAvailability:
    """Test cases for missing_availability."""

    def test_checks_the_next_three_days(self):
        assert DAYS == [date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 2)]

    def test_only_empty_cells_are_missing(self):
        missing = missing_availability(ROWS, ["drewin", "iym0409", "khal", "drukarz_lp", "freakk9"], DAYS)

        assert missing == {"freakk9": DAYS}

    def test_nie_and_question_mark_are_answers(self):
        rows = [HEADER, row("31.10.2026", khal="nie", drewin=" ? ")]
        assert missing_availability(rows, ["khal", "drewin"], DAYS[:1]) == {}

    def test_nick_without_column_is_skipped(self):
        assert missing_availability(ROWS, ["atomek"], DAYS) == {}

    def test_day_without_row_is_skipped(self):
        missing = missing_availability(ROWS, ["freakk9"], days_to_check(date(2026, 11, 1), 3))

        assert missing == {"freakk9": [date(2026, 11, 2)]}

    def test_nick_case_is_ignored(self):
        assert missing_availability(ROWS, ["FREAKK9"], DAYS) == {"FREAKK9": DAYS}

    def test_text_dates(self):
        rows = [HEADER, ["31.10.2026", "", ""] + [""] * (len(HEADER) - 3)]

        assert missing_availability(rows, ["khal"], DAYS) == {"khal": [date(2026, 10, 31)]}


class TestHelpers:
    """Test cases for roster and message helpers."""

    def test_main_players(self):
        roster = [
            {"nick": "drewin", "status": "Main"},
            {"nick": "kizi4880", "status": "Próba"},
            {"nick": "Weres", "status": "Sub"},
            {"nick": "khal", "status": " main "},
        ]

        assert main_players(roster) == ["drewin", "khal"]

    def test_parse_day(self):
        assert parse_day(serial("02.11.2026")) == date(2026, 11, 2)
        assert parse_day("02.11.2026") == date(2026, 11, 2)
        assert parse_day("Data") is None

    def test_message(self):
        text = create_availability_message(
            [("<@1>", [date(2026, 10, 31), date(2026, 11, 1)]), ("**drewin**", [date(2026, 11, 1)])],
            "https://sheet",
        )

        assert "• <@1>: sobota 31.10, niedziela 01.11" in text
        assert "• **drewin**: niedziela 01.11" in text
        assert "<https://sheet>" in text


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_lp_tracker.db")


@pytest.fixture
def channel():
    guild = SimpleNamespace(name="PYRY")
    return SimpleNamespace(send=AsyncMock(), guild=guild, name="przypomnienia")


@pytest.fixture
def lp_bot(monkeypatch, channel):
    lp_bot = bot_module.LPTrackerBot()
    monkeypatch.setattr(lp_bot, "get_reminder_channel", AsyncMock(return_value=channel))
    monkeypatch.setattr(bot_module, "find_missing_availability",
                        AsyncMock(return_value={"khal": [date(2026, 10, 31)], "drewin": [date(2026, 11, 1)]}))
    monkeypatch.setattr(bot_module, "find_member_id", AsyncMock(side_effect=lambda guild, nick: 42 if nick == "khal" else None))
    monkeypatch.setattr(bot_module.team_sheet, "tab_url", AsyncMock(return_value="https://sheet"))
    return lp_bot


def at(hour: int) -> datetime:
    return datetime(2026, 10, 30, hour, 0, tzinfo=bot_module.TIMEZONE)


class TestAvailabilityLoop:
    """Test cases for the daily check in bot.py."""

    async def test_pings_once_a_day(self, monkeypatch, lp_bot, channel):
        await db.init_db()
        monkeypatch.setattr(bot_module, "datetime", SimpleNamespace(now=lambda tz=None: at(13)))

        await lp_bot.check_availability()
        await lp_bot.check_availability()

        channel.send.assert_awaited_once()
        content = channel.send.await_args.args[0]
        assert "<@42>" in content and "**drewin**" in content
        assert [u.id for u in channel.send.await_args.kwargs["allowed_mentions"].users] == [42]

    async def test_waits_for_the_hour(self, monkeypatch, lp_bot, channel):
        await db.init_db()
        monkeypatch.setattr(bot_module, "datetime", SimpleNamespace(now=lambda tz=None: at(9)))

        await lp_bot.check_availability()

        channel.send.assert_not_awaited()

    async def test_nothing_sent_when_everyone_filled_in(self, monkeypatch, lp_bot, channel):
        await db.init_db()
        monkeypatch.setattr(bot_module, "datetime", SimpleNamespace(now=lambda tz=None: at(13)))
        monkeypatch.setattr(bot_module, "find_missing_availability", AsyncMock(return_value={}))

        await lp_bot.check_availability()

        channel.send.assert_not_awaited()
