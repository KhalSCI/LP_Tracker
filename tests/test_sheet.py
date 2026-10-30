"""Tests for the team Google Sheet sync."""
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

import cogs.sheet as sheet
import database as db
from config import TIMEZONE
from riot_api import RiotAPIError

GUILD_ID = 123456789
DAY_MS = 24 * 60 * 60 * 1000
NOW = datetime(2026, 10, 29, 18, 0, tzinfo=TIMEZONE)
NOW_MS = int(NOW.timestamp() * 1000)


def make_player(puuid: str, riot_id: str, tier="DIAMOND", rank="I", lp=50, wins=60, losses=40) -> dict:
    return {
        "puuid": puuid,
        "riot_id": riot_id,
        "discord_name": None,
        "tier": tier,
        "rank": rank,
        "lp": lp,
        "wins": wins,
        "losses": losses,
        "last_updated": "2026-10-29 16:40:00.000000",
    }


def make_entry(player: dict | None, nick: str, role: str = "Mid", riot_id: str = None, error: str = None) -> dict:
    return {
        "role": role,
        "nick": nick,
        "riot_id": riot_id or player["riot_id"],
        "player": player,
        "error": error,
    }


def stored_match(match_id: str, puuid: str, start_ms: int, win=True, champion="Ahri",
                 queue_id=420, team_id=100, remake=False) -> dict:
    """A row as returned from the matches table."""
    return {
        "match_id": match_id,
        "puuid": puuid,
        "queue_id": queue_id,
        "team_id": team_id,
        "champion_name": champion,
        "win": win,
        "remake": remake,
        "game_start": start_ms,
    }


def riot_match(match_id: str, start_ms: int, team_puuids: dict[str, int], queue_id: int = 420) -> dict:
    """A match-v5 payload with the given puuids on the given teams (team 100 wins)."""
    participants = [
        {
            "puuid": puuid,
            "teamId": team_id,
            "win": team_id == 100,
            "championName": "Ahri",
            "kills": 1,
            "deaths": 1,
            "assists": 1,
            "gameEndedInEarlySurrender": False,
        }
        for puuid, team_id in team_puuids.items()
    ]
    return {
        "metadata": {"matchId": match_id},
        "info": {
            "gameStartTimestamp": start_ms,
            "gameEndTimestamp": start_ms + 1800 * 1000,
            "gameDuration": 1800,
            "queueId": queue_id,
            "participants": participants,
        },
    }


class FakeRiot:
    """Stands in for the Riot account and match endpoints used by the sheet sync."""

    def __init__(self, accounts: dict = None, matches: list[dict] = None):
        self.accounts = accounts or {}
        self.matches = {m["metadata"]["matchId"]: m for m in matches or []}
        self.detail_calls = []

    async def get_player_full_info(self, game_name, tag_line):
        account = self.accounts.get(f"{game_name}#{tag_line}".lower())
        if isinstance(account, RiotAPIError):
            raise account
        if account is None:
            raise RiotAPIError(404, "Player not found")
        return account

    async def get_match_ids_by_puuid(self, puuid, queue=420, count=5, start_time=None, start=0):
        matches = sorted(self.matches.values(), key=lambda m: m["info"]["gameStartTimestamp"], reverse=True)
        matches = [
            m for m in matches
            if m["info"]["queueId"] == queue
            and any(p["puuid"] == puuid for p in m["info"]["participants"])
            and (not start_time or m["info"]["gameStartTimestamp"] >= start_time * 1000)
        ]
        return [m["metadata"]["matchId"] for m in matches[start:start + count]]

    async def get_match_details(self, match_id):
        self.detail_calls.append(match_id)
        return self.matches[match_id]


def account(puuid: str, riot_id: str, region: str = "euw1", ranked: dict = None) -> dict:
    game_name, tag_line = riot_id.rsplit("#", 1)
    return {"puuid": puuid, "game_name": game_name, "tag_line": tag_line, "region": region, "ranked": ranked}


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch, tmp_path):
    """Use a throwaway database and skip rate limit sleeps."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_lp_tracker.db")
    monkeypatch.setattr(sheet, "asyncio", SimpleNamespace(sleep=AsyncMock()))


@pytest.fixture
async def leaderboard_id():
    await db.init_db()
    return await db.create_leaderboard(GUILD_ID, "Team")


def use_riot(monkeypatch, fake: FakeRiot) -> None:
    for name in ("get_player_full_info", "get_match_ids_by_puuid", "get_match_details"):
        monkeypatch.setattr(sheet.riot_api, name, getattr(fake, name))


class TestLadderScore:
    """Test cases for ladder_score."""

    def test_promotion_to_master_counts_lp_across_tiers(self):
        """Test that Diamond I 90 LP -> Master 20 LP is a 30 LP gain."""
        assert sheet.ladder_score("MASTER", "I", 20) - sheet.ladder_score("DIAMOND", "I", 90) == 30

    def test_division_promotion(self):
        """Test that Emerald I 80 LP -> Diamond IV 10 LP is a 30 LP gain."""
        assert sheet.ladder_score("DIAMOND", "IV", 10) - sheet.ladder_score("EMERALD", "I", 80) == 30

    def test_apex_tiers_share_lp_scale(self):
        """Test that Grandmaster with more LP than a Master ranks above them on one scale."""
        assert sheet.ladder_score("GRANDMASTER", "I", 700) - sheet.ladder_score("MASTER", "I", 600) == 100

    def test_unranked(self):
        assert sheet.ladder_score(None, None, 0) is None


class TestTopChampions:
    """Test cases for top_champions."""

    def test_most_played_first_with_winrate(self):
        games = (
            [stored_match(f"a{i}", "p", 0, win=i < 3, champion="Ahri") for i in range(4)]
            + [stored_match(f"m{i}", "p", 0, win=True, champion="MissFortune") for i in range(2)]
            + [stored_match("w", "p", 0, win=False, champion="MonkeyKing")]
        )

        assert sheet.top_champions(games) == "Ahri 4 (75%) · Miss Fortune 2 (100%) · Wukong 1 (0%)"

    def test_ties_broken_by_winrate(self):
        games = [
            stored_match("1", "p", 0, win=False, champion="Zed"),
            stored_match("2", "p", 0, win=True, champion="Yasuo"),
        ]

        assert sheet.top_champions(games, count=1) == "Yasuo 1 (100%)"

    def test_no_games(self):
        assert sheet.top_champions([]) == ""


class TestTeamRows:
    """Test cases for build_team_rows."""

    def test_sorted_by_rank_with_failed_rows_last(self):
        high = make_player("p1", "High#EUW", tier="MASTER", lp=300)
        low = make_player("p2", "Low#EUW", tier="DIAMOND", rank="II", lp=10)
        unranked = make_player("p3", "New#EUW", tier=None, rank=None, lp=0, wins=0, losses=0)
        entries = [
            make_entry(None, "typo", riot_id="Typo#EUW", error="Nie znaleziono konta o tym Riot ID"),
            make_entry(unranked, "new"),
            make_entry(low, "low"),
            make_entry(high, "high"),
        ]

        rows = sheet.build_team_rows(entries, [], {}, NOW)

        assert [row[0] for row in rows] == ["high", "low", "new", "typo"]
        assert rows[0][3:8] == ["Master", 300, 60, 40, 0.6]
        assert rows[2][3:8] == ["Unranked", "", "", "", ""]
        assert rows[3][3] == "⚠ Nie znaleziono konta o tym Riot ID"

    def test_status_shown_with_role(self):
        player = make_player("p1", "Weres#Łeres")
        entries = [dict(make_entry(player, "Weres", role="Top"), status="Sub")]

        row = sheet.build_team_rows(entries, [], {}, NOW)[0]

        assert row[1] == "Top (Sub)"

    def test_week_activity_and_lp_change(self):
        player = make_player("p1", "Mid#EUW", tier="MASTER", lp=20)
        matches = [
            stored_match("new1", "p1", NOW_MS - 1 * DAY_MS, win=True),
            stored_match("new2", "p1", NOW_MS - 2 * DAY_MS, win=False),
            stored_match("remake", "p1", NOW_MS - 2 * DAY_MS, win=False, remake=True),
            stored_match("flex", "p1", NOW_MS - 3 * DAY_MS, win=True, queue_id=440),
            stored_match("old", "p1", NOW_MS - 10 * DAY_MS, win=True, champion="Syndra"),
        ]
        baselines = {"p1": {"tier": "DIAMOND", "rank": "I", "lp": 90}}

        row = sheet.build_team_rows([make_entry(player, "mid")], matches, baselines, NOW)[0]

        assert row[8:12] == [2, "1-1", 30, "Ahri 2 (50%) · Syndra 1 (100%)"]
        assert row[12] == sheet.to_sheet_datetime(sheet.from_timestamp_ms(NOW_MS - 1 * DAY_MS))

    def test_no_baseline_leaves_lp_change_blank(self):
        player = make_player("p1", "Mid#EUW")

        row = sheet.build_team_rows([make_entry(player, "mid")], [], {"p1": None}, NOW)[0]

        assert row[8:11] == [0, "", ""]


class TestDuoRows:
    """Test cases for build_duo_rows."""

    NAMES = {"a": "drewin", "b": "kizi", "c": "iym"}

    def test_counts_teammates_not_opponents(self):
        matches = [
            # a + b duo, won
            stored_match("m1", "a", NOW_MS - DAY_MS, win=True),
            stored_match("m1", "b", NOW_MS - DAY_MS, win=True),
            # a + b duo, lost
            stored_match("m2", "b", NOW_MS - 2 * DAY_MS, win=False),
            stored_match("m2", "a", NOW_MS - 2 * DAY_MS, win=False),
            # a against c
            stored_match("m3", "a", NOW_MS - DAY_MS, team_id=100),
            stored_match("m3", "c", NOW_MS - DAY_MS, team_id=200, win=False),
            # a + b duo over a week ago
            stored_match("m4", "a", NOW_MS - 8 * DAY_MS),
            stored_match("m4", "b", NOW_MS - 8 * DAY_MS),
        ]

        rows = sheet.build_duo_rows(matches, self.NAMES, NOW)

        assert rows == [[
            "drewin + kizi", "SoloQ (duo)", 2, 1, 1, 0.5,
            sheet.to_sheet_datetime(sheet.from_timestamp_ms(NOW_MS - DAY_MS)),
        ]]

    def test_flex_stack_grouped_separately(self):
        matches = [
            stored_match("f1", puuid, NOW_MS - DAY_MS, queue_id=440) for puuid in ("c", "a", "b")
        ] + [
            stored_match("s1", puuid, NOW_MS - DAY_MS) for puuid in ("a", "b")
        ]

        rows = sheet.build_duo_rows(matches, self.NAMES, NOW)

        assert sorted((row[0], row[1]) for row in rows) == [
            ("drewin + kizi", "SoloQ (duo)"),
            ("drewin + kizi + iym", "Flex"),
        ]

    def test_remakes_ignored(self):
        matches = [
            stored_match("m1", puuid, NOW_MS - DAY_MS, remake=True) for puuid in ("a", "b")
        ]

        assert sheet.build_duo_rows(matches, self.NAMES, NOW) == []


class TestSheetUpdates:
    """Test cases for build_sheet_updates."""

    def test_blanks_leftover_rows(self):
        player = make_player("p1", "Mid#EUW")
        updates = sheet.build_sheet_updates([make_entry(player, "mid")], [], {"p1": None}, NOW)
        by_range = {update["range"]: update["values"] for update in updates}

        team = by_range["A5:N16"]
        assert len(team) == 12 and all(len(row) == 14 for row in team)
        assert team[0][0] == "mid"
        assert team[1] == [""] * 14

        duo = by_range["A21:G45"]
        assert duo[0][0] == "Brak wspólnych gier w ostatnich 7 dniach"
        assert duo[1] == [""] * 7

    def test_status_notes_short_lp_history(self):
        baselines = {"p1": {"recorded_at": "2026-10-28 10:00:00.000000"}}

        status = sheet.sync_status(baselines, NOW)

        assert status.startswith("Ostatnia synchronizacja: 29.10.2026 18:00")
        assert "historia LP zapisywana od 28.10.2026" in status

    def test_status_without_note_after_a_week(self):
        baselines = {"p1": {"recorded_at": "2026-10-01 10:00:00.000000"}}

        assert "historia LP" not in sheet.sync_status(baselines, NOW)


class TestRosterSync:
    """Test cases for sync_roster."""

    async def test_adds_updates_and_keeps_players_left_off_roster(self, monkeypatch, leaderboard_id):
        """Test that roster players are added and updated, and others stay on the leaderboard."""
        await db.add_player(leaderboard_id, "Kept#EUW", "kept", discord_name="old")
        await db.add_player(leaderboard_id, "LeftTeam#EUW", "left", discord_name="atomek")
        use_riot(monkeypatch, FakeRiot(accounts={
            "new#euw": account("new", "New#EUW", ranked={"tier": "GOLD", "rank": "II", "lp": 5, "wins": 1, "losses": 0}),
        }))
        roster = [
            {"role": "Top", "nick": "kept_nick", "riot_id": "kept#euw"},
            {"role": "Mid", "nick": "new_nick", "riot_id": "New#EUW"},
        ]

        entries = await sheet.sync_roster(leaderboard_id, roster)

        players = {p["riot_id"]: p for p in await db.get_leaderboard_players(leaderboard_id)}
        assert set(players) == {"Kept#EUW", "New#EUW", "LeftTeam#EUW"}
        assert players["LeftTeam#EUW"]["discord_name"] == "atomek"
        assert players["Kept#EUW"]["discord_name"] == "kept_nick"
        assert players["New#EUW"]["discord_name"] == "new_nick"
        assert players["New#EUW"]["tier"] == "GOLD"
        assert all(entry["error"] is None for entry in entries)

    async def test_renamed_account_keeps_player(self, monkeypatch, leaderboard_id):
        """Test that a new Riot ID of a tracked account renames it instead of re-adding it."""
        player_id = await db.add_player(leaderboard_id, "OldName#EUW", "same-puuid", discord_name="kizi")
        use_riot(monkeypatch, FakeRiot(accounts={"newname#euw": account("same-puuid", "NewName#EUW")}))

        await sheet.sync_roster(leaderboard_id, [{"role": "Top", "nick": "kizi", "riot_id": "NewName#EUW"}])

        players = await db.get_leaderboard_players(leaderboard_id)
        assert [(p["id"], p["riot_id"]) for p in players] == [(player_id, "NewName#EUW")]

    async def test_bad_rows_reported(self, monkeypatch, leaderboard_id):
        use_riot(monkeypatch, FakeRiot(accounts={"eune#tag": account("x", "Eune#TAG", region="eun1")}))
        roster = [
            {"role": "Top", "nick": "a", "riot_id": "Missing#EUW"},
            {"role": "Mid", "nick": "b", "riot_id": "Eune#TAG"},
            {"role": "ADC", "nick": "c", "riot_id": "NoTag"},
        ]

        entries = await sheet.sync_roster(leaderboard_id, roster)

        assert [entry["error"] for entry in entries] == [
            "Nie znaleziono konta o tym Riot ID",
            "Konto nie jest z EUW (serwer: EUN1)",
            "Zły format Riot ID, powinno być Nazwa#TAG",
        ]
        assert await db.get_leaderboard_players(leaderboard_id) == []

    async def test_riot_outage_reported(self, monkeypatch, leaderboard_id):
        use_riot(monkeypatch, FakeRiot(accounts={"newname#euw": RiotAPIError(503, "Unavailable")}))

        entries = await sheet.sync_roster(leaderboard_id, [{"role": "Top", "nick": "a", "riot_id": "NewName#EUW"}])

        assert entries[0]["error"].startswith("Błąd Riot API (503)")
        assert await db.get_leaderboard_players(leaderboard_id) == []


class TestMatchSync:
    """Test cases for sync_player_matches."""

    async def test_duo_game_fetched_once_and_stored_for_both(self, monkeypatch, leaderboard_id):
        now = int(time.time())
        fake = FakeRiot(matches=[
            riot_match("duo", (now - 3600) * 1000, {"a": 100, "b": 100}),
            riot_match("flex", (now - 7200) * 1000, {"a": 200, "b": 100}, queue_id=440),
            riot_match("solo", (now - 9000) * 1000, {"b": 100}),
        ])
        use_riot(monkeypatch, fake)

        added_a = await sheet.sync_player_matches("a", {"a", "b"}, now)
        added_b = await sheet.sync_player_matches("b", {"a", "b"}, now)

        assert (added_a, added_b) == (2, 1)
        assert sorted(fake.detail_calls) == ["duo", "flex", "solo"]
        stored = await db.get_matches_since(["a", "b"], 0)
        assert sorted((m["match_id"], m["puuid"], m["team_id"]) for m in stored) == [
            ("duo", "a", 100), ("duo", "b", 100),
            ("flex", "a", 200), ("flex", "b", 100),
            ("solo", "b", 100),
        ]

    async def test_next_sync_only_fetches_new_games(self, monkeypatch, leaderboard_id):
        now = int(time.time())
        fake = FakeRiot(matches=[riot_match("old", (now - 3600) * 1000, {"a": 100})])
        use_riot(monkeypatch, fake)
        await sheet.sync_player_matches("a", {"a"}, now)

        fake.matches["new"] = riot_match("new", (now + 600) * 1000, {"a": 100})
        added = await sheet.sync_player_matches("a", {"a"}, now + 3600)

        assert added == 1
        assert fake.detail_calls == ["old", "new"]

    async def test_pages_through_long_history(self, monkeypatch, leaderboard_id):
        now = int(time.time())
        monkeypatch.setattr(sheet, "MATCH_IDS_PAGE", 2)
        use_riot(monkeypatch, FakeRiot(matches=[
            riot_match(f"m{i}", (now - 3600 * (i + 1)) * 1000, {"a": 100}) for i in range(5)
        ]))

        assert await sheet.sync_player_matches("a", {"a"}, now) == 5


class TestRankHistory:
    """Test cases for rank snapshots written by update_player_rank."""

    async def count_snapshots(self) -> int:
        async with aiosqlite.connect(db.DB_PATH) as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM rank_history")
            return (await cursor.fetchone())[0]

    async def test_snapshot_only_when_rank_changes(self, leaderboard_id):
        player_id = await db.add_player(leaderboard_id, "Player#EUW", "puuid")

        await db.update_player_rank(player_id, "DIAMOND", "I", 50, 10, 5)
        await db.update_player_rank(player_id, "DIAMOND", "I", 50, 10, 5, prev_lp=50)
        await db.update_player_rank(player_id, "DIAMOND", "I", 70, 11, 5, prev_lp=50)

        assert await self.count_snapshots() == 2

    async def test_baseline_before_cutoff_or_first_after(self, leaderboard_id):
        async with aiosqlite.connect(db.DB_PATH) as conn:
            for lp, days_ago in ((10, 9), (40, 6)):
                await conn.execute(
                    "INSERT INTO rank_history (puuid, tier, rank, lp, wins, losses, recorded_at) "
                    "VALUES ('p', 'DIAMOND', 'I', ?, 0, 0, ?)",
                    (lp, datetime.utcnow() - timedelta(days=days_ago))
                )
            await conn.commit()

        week_ago = await db.get_rank_baseline("p", datetime.utcnow() - timedelta(days=7))
        ten_days_ago = await db.get_rank_baseline("p", datetime.utcnow() - timedelta(days=10))

        assert week_ago["lp"] == 10
        assert ten_days_ago["lp"] == 10
        assert await db.get_rank_baseline("other", datetime.utcnow()) is None


class TestSyncTeamSheet:
    """Test cases for the full sync_team_sheet run."""

    async def test_roster_to_soloq_tab(self, monkeypatch, leaderboard_id):
        now = int(time.time())
        await db.add_player(leaderboard_id, "Iym#0509", "iym", discord_name="iym")
        player_id = await db.add_player(leaderboard_id, "Khal#EUW", "khal", discord_name="khal")
        await db.update_player_rank(player_id, "MASTER", "I", 210, 220, 229)
        use_riot(monkeypatch, FakeRiot(matches=[
            riot_match("duo", (now - 3600) * 1000, {"iym": 100, "khal": 100}),
        ]))
        fake_sheet = SimpleNamespace(
            read_roster=AsyncMock(return_value=[
                {"role": "Mid", "nick": "iym0409", "riot_id": "Iym#0509"},
                {"role": "ADC", "nick": "khal", "riot_id": "Khal#EUW"},
            ]),
            write_soloq=AsyncMock(),
        )
        monkeypatch.setattr(sheet, "team_sheet", fake_sheet)
        monkeypatch.setattr(sheet, "SHEET_GUILD_ID", GUILD_ID)
        monkeypatch.setattr(sheet, "SHEET_LEADERBOARD", "team")

        summary = await sheet.sync_team_sheet()

        assert summary == {"players": 2, "errors": 0, "new_games": 1}
        updates, rows, cols = fake_sheet.write_soloq.await_args.args
        assert (rows, cols) == (45, 14)
        by_range = {update["range"]: update["values"] for update in updates}
        assert [row[0] for row in by_range["A5:N16"][:2]] == ["khal", "iym0409"]
        assert by_range["A5:N16"][0][8:10] == [1, "1-0"]
        assert by_range["A21:G45"][0][:3] == ["iym0409 + khal", "SoloQ (duo)", 1]

    async def test_missing_leaderboard(self, monkeypatch, leaderboard_id):
        monkeypatch.setattr(sheet, "SHEET_GUILD_ID", GUILD_ID)
        monkeypatch.setattr(sheet, "SHEET_LEADERBOARD", "nope")

        with pytest.raises(ValueError):
            await sheet.sync_team_sheet()
