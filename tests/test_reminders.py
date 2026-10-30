"""Tests for match reminders from the team sheet."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot as bot_module
import database as db
from cogs.sheet import to_sheet_datetime
from config import TIMEZONE
from reminders import (
    parse_planned_matches, due_reminders, reminder_key, describe_start, create_reminder_message,
)

ROLE_ID = 1564938781096681563
CHANNEL_ID = 555
# Thursday 29.10.2026 16:00
NOW = datetime(2026, 10, 29, 16, 0, tzinfo=TIMEZONE)
FIVE_PM = 17 / 24


# Row 4 of the "Mecze" tab
HEADER = ["Data", "Godz.", "Typ", "Przeciwnik", "Turniej / nazwa", "Wynik", "Rezultat",
          "Przypomnienie", "Notatki", "Mecz (do Map)"]


def sheet_row(date: str, time=FIVE_PM, type_="Quali", event="Zapka Quali #1", opponent="",
              status="Zaplanowany", notes="Prawdopodobny mecz", remind=True) -> list:
    """A raw "Mecze" row as read with unformatted values, in HEADER order."""
    serial = (datetime.strptime(date, "%d.%m.%Y") - datetime(1899, 12, 30)).days
    return [serial, time, type_, opponent, event, "", status, remind, notes, "label"]


def match_at(start: datetime, has_time: bool = True, **fields) -> dict:
    return {"start": start, "has_time": has_time, "type": "Quali", "event": "Zapka Quali #1",
            "opponent": "", "notes": "", **fields}


class TestParsePlannedMatches:
    """Test cases for parse_planned_matches."""

    def test_reads_date_and_time(self):
        matches = parse_planned_matches([HEADER, sheet_row("30.10.2026", opponent="AGH")])

        assert matches[0]["start"] == datetime(2026, 10, 30, 17, 0, tzinfo=TIMEZONE)
        assert matches[0]["has_time"] is True
        assert matches[0]["event"] == "Zapka Quali #1"
        assert matches[0]["opponent"] == "AGH"
        assert matches[0]["notes"] == "Prawdopodobny mecz"

    def test_missing_time(self):
        matches = parse_planned_matches([HEADER, sheet_row("26.11.2026", time="")])

        assert matches[0]["start"] == datetime(2026, 11, 26, 0, 0, tzinfo=TIMEZONE)
        assert matches[0]["has_time"] is False

    def test_skips_played_and_empty_rows(self):
        rows = [
            HEADER,
            sheet_row("20.10.2026", status="W"),
            sheet_row("21.10.2026", status="Brak wyniku"),
            [],
            ["", "", "Scrim"],
        ]

        assert parse_planned_matches(rows) == []

    def test_only_ticked_reminder_checkbox(self):
        """Test that matches without the reminder checkbox ticked stay in the sheet but get no reminders."""
        rows = [
            HEADER,
            sheet_row("30.10.2026", event="Zapka Quali #1", remind=True),
            sheet_row("31.10.2026", event="Zapka Quali #2", remind=False),
            sheet_row("06.11.2026", event="Zapka Quali #3", remind=""),  # checkbox cell empty
        ]

        assert [match["event"] for match in parse_planned_matches(rows)] == ["Zapka Quali #1"]

    def test_columns_found_by_header(self):
        """Test that moving columns around in the sheet doesn't break reminders."""
        order = [8, 0, 6, 1, 7, 2, 3, 4, 5, 9]
        row = sheet_row("30.10.2026")

        matches = parse_planned_matches([[HEADER[i] for i in order], [row[i] for i in order]])

        assert matches[0]["event"] == "Zapka Quali #1"
        assert matches[0]["start"] == datetime(2026, 10, 30, 17, 0, tzinfo=TIMEZONE)

    def test_missing_column_is_an_error(self):
        header = [name for name in HEADER if name != "Przypomnienie"]

        with pytest.raises(ValueError, match="Przypomnienie"):
            parse_planned_matches([header])

    def test_empty_tab(self):
        assert parse_planned_matches([]) == []


class TestDueReminders:
    """Test cases for due_reminders."""

    START = datetime(2026, 10, 30, 17, 0, tzinfo=TIMEZONE)

    def kinds_at(self, now: datetime, match: dict = None) -> list[str]:
        return [key.rsplit("|", 1)[1] for key, _ in due_reminders([match or match_at(self.START)], now)]

    def test_day_before(self):
        assert self.kinds_at(self.START - timedelta(hours=24)) == ["24h"]
        assert self.kinds_at(self.START - timedelta(hours=2)) == ["24h"]

    def test_hour_before_replaces_day_before(self):
        """Test that a match first seen within the last hour gets only the 1h reminder."""
        assert self.kinds_at(self.START - timedelta(minutes=59)) == ["1h"]

    def test_nothing_too_early_or_after_start(self):
        assert self.kinds_at(self.START - timedelta(hours=25)) == []
        assert self.kinds_at(self.START + timedelta(minutes=1)) == []

    def test_untimed_match_reminded_evening_before(self):
        match = match_at(datetime(2026, 11, 26, 0, 0, tzinfo=TIMEZONE), has_time=False)

        assert self.kinds_at(datetime(2026, 11, 25, 17, 59, tzinfo=TIMEZONE), match) == []
        assert self.kinds_at(datetime(2026, 11, 25, 18, 0, tzinfo=TIMEZONE), match) == ["day"]
        assert self.kinds_at(datetime(2026, 11, 26, 20, 0, tzinfo=TIMEZONE), match) == ["day"]
        assert self.kinds_at(datetime(2026, 11, 27, 0, 0, tzinfo=TIMEZONE), match) == []

    def test_rescheduled_match_gets_new_reminders(self):
        moved = match_at(self.START + timedelta(hours=1))

        assert reminder_key(match_at(self.START), "24h") != reminder_key(moved, "24h")


class TestReminderMessage:
    """Test cases for describe_start and create_reminder_message."""

    def test_describe_start(self):
        assert describe_start(match_at(datetime(2026, 10, 30, 17, 0, tzinfo=TIMEZONE)), NOW) == "Jutro o 17:00"
        assert describe_start(match_at(datetime(2026, 10, 29, 17, 0, tzinfo=TIMEZONE)), NOW) == "Dziś o 17:00"
        assert describe_start(match_at(datetime(2026, 11, 6, 17, 0, tzinfo=TIMEZONE)), NOW) == "Piątek 06.11 o 17:00"
        assert describe_start(
            match_at(datetime(2026, 10, 30, 0, 0, tzinfo=TIMEZONE), has_time=False), NOW
        ) == "Jutro, godzina jeszcze nieznana"

    def test_pings_role(self):
        match = match_at(datetime(2026, 10, 30, 17, 0, tzinfo=TIMEZONE), notes="Prawdopodobny mecz")

        content, embed = create_reminder_message(match, NOW, ROLE_ID)

        assert content == f"<@&{ROLE_ID}> ⏰ Jutro o 17:00: **Zapka Quali #1**"
        fields = {field.name: field.value for field in embed.fields}
        assert fields["Notatki"] == "Prawdopodobny mecz"
        assert fields["Start"].startswith("<t:")

    def test_opponent_in_title_and_no_role(self):
        match = match_at(datetime(2026, 10, 30, 17, 0, tzinfo=TIMEZONE), event="Liga", opponent="AGH")

        content, embed = create_reminder_message(match, NOW)

        assert content == "⏰ Jutro o 17:00: **Liga vs AGH**"
        assert embed.title == "Liga vs AGH"


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_lp_tracker.db")


class TestReminderLoop:
    """Test cases for check_match_reminders in bot.py."""

    @pytest.fixture
    def channel(self):
        return SimpleNamespace(send=AsyncMock())

    @pytest.fixture
    def lp_bot(self, monkeypatch, channel):
        lp_bot = bot_module.LPTrackerBot()
        monkeypatch.setattr(lp_bot, "get_channel", lambda channel_id: channel if channel_id == CHANNEL_ID else None)
        monkeypatch.setattr(bot_module, "REMINDER_CHANNEL_ID", CHANNEL_ID)
        monkeypatch.setattr(bot_module, "REMINDER_ROLE_ID", ROLE_ID)
        return lp_bot

    def use_sheet(self, monkeypatch, start: datetime) -> None:
        serial = to_sheet_datetime(start)
        row = [int(serial), serial % 1, "Quali", "", "Zapka Quali #1", "", "Zaplanowany", True, "", "label"]
        monkeypatch.setattr(bot_module, "team_sheet", SimpleNamespace(read_matches=AsyncMock(return_value=[HEADER, row])))

    async def test_sends_reminder_once_with_role_ping(self, monkeypatch, lp_bot, channel):
        await db.init_db()
        self.use_sheet(monkeypatch, datetime.now(TIMEZONE) + timedelta(hours=20))

        await lp_bot.check_match_reminders()
        await lp_bot.check_match_reminders()

        channel.send.assert_awaited_once()
        content = channel.send.await_args.args[0]
        assert content.startswith(f"<@&{ROLE_ID}> ⏰ ")
        assert channel.send.await_args.kwargs["allowed_mentions"].roles[0].id == ROLE_ID

    async def test_no_reminder_far_ahead(self, monkeypatch, lp_bot, channel):
        await db.init_db()
        self.use_sheet(monkeypatch, datetime.now(TIMEZONE) + timedelta(days=3))

        await lp_bot.check_match_reminders()

        channel.send.assert_not_awaited()

    async def test_defaults_to_team_notification_channel(self, monkeypatch, lp_bot, channel):
        await db.init_db()
        lb_id = await db.create_leaderboard(1, "Team")
        await db.set_leaderboard_notification_channel(1, "Team", CHANNEL_ID)
        monkeypatch.setattr(bot_module, "REMINDER_CHANNEL_ID", 0)
        monkeypatch.setattr(bot_module, "SHEET_GUILD_ID", 1)
        monkeypatch.setattr(bot_module, "SHEET_LEADERBOARD", "team")
        self.use_sheet(monkeypatch, datetime.now(TIMEZONE) + timedelta(minutes=30))

        await lp_bot.check_match_reminders()

        channel.send.assert_awaited_once()
