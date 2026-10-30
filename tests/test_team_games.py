"""Tests for team games (scrim uploads, officials) and the drafts analysis."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import database as db
import drafts
import team_games
from champions import champions
from config import TIMEZONE
from riot_api import RiotAPIError

CHAMPIONS = {1: "Annie", 2: "Olaf", 3: "Galio", 4: "Twisted Fate", 5: "Xin Zhao", 6: "Urgot",
             7: "LeBlanc", 8: "Vladimir", 9: "Fiddlesticks", 10: "Kayle", 11: "Master Yi", 12: "Alistar"}
OURS = [  # riot_id, nick, roster role
    ("Top#EUW", "drewin", "Top"),
    ("Jg#EUW", "freakk9", "Jungle"),
    ("Mid#EUW", "iym0409", "Mid"),
    ("Adc#EUW", "khal", "ADC"),
    ("Sup#EUW", "drukarz_lp", "Support"),
    ("Sup2#EUW", "magik9285", "Support"),
]
ENEMIES = ["E1#X", "E2#X", "E3#X", "E4#X", "E5#X"]
OTHER_ENEMIES = ["F1#Y", "F2#Y", "F3#Y", "F4#Y", "F5#Y"]
LANES = {"Top": ("TOP", "SOLO"), "Jungle": ("JUNGLE", "NONE"), "Mid": ("MIDDLE", "SOLO"),
         "ADC": ("BOTTOM", "DUO_CARRY"), "Support": ("BOTTOM", "DUO_SUPPORT")}
MATCHES_HEADER = ["Data", "Godz.", "Typ", "Przeciwnik", "Turniej / nazwa", "Wynik", "Rezultat",
                  "Przypomnienie", "Notatki", "Mecz (do Map)"]
MAPS_HEADER = ["Mecz", "Mapa", "Typ", "Przeciwnik", "Wynik", "Strona", "Top", "Top champ", "Jungle",
               "Jungle champ", "Mid", "Mid champ", "ADC", "ADC champ", "Support", "Support champ",
               "Picki przeciwnika", "Game ID", "Notatki / VOD", "", "Top", "Jungle", "Mid", "ADC", "Support", "Championy"]


def ms(day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(2026, 10, day, hour, minute, tzinfo=TIMEZONE).timestamp() * 1000)


def serial(day: int, hour: int = None) -> tuple:
    date = (datetime(2026, 10, day) - datetime(1899, 12, 30)).days
    return date, (hour / 24 if hour is not None else "")


def client_game(game_id: int, start_ms: int, ours: list[tuple], enemies: list[str], win: bool = True,
                our_team: int = 200, map_id: int = 11, lanes: bool = True) -> dict:
    """A LoL client match-history game; ours = [(riot_id, champion_id, role)]."""
    participants, identities = [], []
    players = [(riot_id, champ, role, our_team, win) for riot_id, champ, role in ours]
    enemy_team = 300 - our_team
    players += [(riot_id, 7 + i, None, enemy_team, not win) for i, riot_id in enumerate(enemies)]
    for pid, (riot_id, champ, role, team, won) in enumerate(players, start=1):
        name, tag = riot_id.split("#")
        lane, lane_role = LANES.get(role, ("NONE", "NONE")) if lanes else ("NONE", "NONE")
        participants.append({
            "participantId": pid, "teamId": team, "championId": champ,
            "spell1Id": 11 if role == "Jungle" else 4, "spell2Id": 14,
            "stats": {"kills": 1, "deaths": 2, "assists": 3, "win": won},
            "timeline": {"lane": lane, "role": lane_role},
        })
        identities.append({"participantId": pid, "player": {"gameName": name, "tagLine": tag}})
    return {
        "gameId": game_id, "platformId": "EUW1", "gameCreation": start_ms, "gameDuration": 1800,
        "gameType": "CUSTOM_GAME", "gameMode": "CLASSIC", "mapId": map_id, "queueId": 0,
        "participants": participants, "participantIdentities": identities,
        "teams": [{"teamId": our_team, "bans": [{"championId": 12}]}, {"teamId": enemy_team, "bans": [{"championId": 11}, {"championId": -1}]}],
    }


def five(roles=("Top", "Jungle", "Mid", "ADC", "Support"), support="Sup#EUW") -> list[tuple]:
    ids = {"Top": "Top#EUW", "Jungle": "Jg#EUW", "Mid": "Mid#EUW", "ADC": "Adc#EUW", "Support": support}
    return [(ids[role], i + 1, role) for i, role in enumerate(roles)]


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_lp_tracker.db")
    monkeypatch.setattr(champions, "by_key", dict(CHAMPIONS))
    monkeypatch.setattr(champions, "by_id", {"MonkeyKing": "Wukong"})
    monkeypatch.setattr(champions, "refresh", AsyncMock())


@pytest.fixture
def roster() -> team_games.TeamRoster:
    return team_games.TeamRoster([{"riot_id": r, "nick": n, "role": role, "puuid": f"puuid-{n}"} for r, n, role in OURS])


class FakeSheet:
    """Stands in for the team sheet's Mecze/Mapy reads and appends."""

    def __init__(self, matches: list[list] = None, maps: list[list] = None):
        self.tabs = {"Mecze": [MATCHES_HEADER] + (matches or []), "Mapy": [MAPS_HEADER] + (maps or [])}
        self.appended = {"Mecze": [], "Mapy": []}

    async def read_tab(self, tab):
        return self.tabs[tab]

    async def read_matches(self):
        return self.tabs["Mecze"]

    async def append_rows(self, tab, key_header, rows, value_input_option="RAW"):
        self.appended[tab] += rows
        header = self.tabs[tab][0]
        for row in rows:
            cells = [""] * len(header)
            for name, value in row.items():
                cells[header.index(name)] = value
            if tab == "Mecze":  # the sheet's label formula
                start = datetime.strptime(f"{row['Data']} {row['Godz.']}", "%d.%m.%Y %H:%M")
                date, time_of_day = serial(start.day, start.hour + start.minute / 60)
                cells[0], cells[1] = date, time_of_day
                cells[header.index("Mecz (do Map)")] = f"{start:%d.%m %H:%M} · {row['Typ']}"
            self.tabs[tab].append(cells)
        return 0

    async def read_roster(self):
        return [{"role": role, "nick": nick, "riot_id": riot_id} for riot_id, nick, role in OURS[:5]]


def match_row(day: int, hour: int, type_: str, opponent: str = "") -> list:
    date, time_of_day = serial(day, hour)
    label = f"{day:02d}.10 {hour:02d}:00 · {type_}"
    return [date, time_of_day, type_, opponent, "", "", "", False, "", label]


class TestNormalizeClientGame:
    """Test cases for normalize_client_game."""

    def test_our_side_roles_and_picks(self, roster):
        game = team_games.normalize_client_game(client_game(123, ms(29, 18, 5), five(), ENEMIES), roster)

        assert game["match_id"] == "EUW1_123"
        assert game["side"] == "Red" and game["win"] is True and game["source"] == "client"
        assert [(p["role"], p["player"], p["champion"]) for p in game["ours"]] == [
            ("Top", "drewin", "Annie"), ("Jungle", "freakk9", "Olaf"), ("Mid", "iym0409", "Galio"),
            ("ADC", "khal", "Twisted Fate"), ("Support", "drukarz_lp", "Xin Zhao"),
        ]
        assert [p["champion"] for p in game["enemy"]] == ["LeBlanc", "Vladimir", "Fiddlesticks", "Kayle", "Master Yi"]
        assert game["our_bans"] == ["Alistar"] and game["enemy_bans"] == ["Master Yi"]

    def test_off_role_player_placed_by_lane(self, roster):
        """Test that a support playing ADC (two roster supports) is placed by their in-game lane."""
        ours = [("Top#EUW", 1, "Top"), ("Jg#EUW", 2, "Jungle"), ("Mid#EUW", 3, "Mid"),
                ("Sup2#EUW", 4, "ADC"), ("Sup#EUW", 5, "Support")]

        game = team_games.normalize_client_game(client_game(1, ms(29, 18), ours, ENEMIES), roster)

        assert {p["player"]: p["role"] for p in game["ours"]}["magik9285"] == "ADC"

    def test_roster_roles_when_client_has_no_lanes(self, roster):
        game = team_games.normalize_client_game(client_game(1, ms(29, 18), five(), ENEMIES, lanes=False), roster)

        assert [p["player"] for p in game["ours"]] == ["drewin", "freakk9", "iym0409", "khal", "drukarz_lp"]
        assert all(p["role"] == "" for p in game["enemy"])  # no lanes and no smite: enemy roles unknown

    def test_not_our_game(self, roster):
        stand_ins = [("Top#EUW", 1, "Top"), ("Jg#EUW", 2, "Jungle"), ("X#1", 3, "Mid"), ("Y#1", 4, "ADC"), ("Z#1", 5, "Support")]

        assert team_games.normalize_client_game(client_game(1, ms(29, 18), stand_ins, ENEMIES), roster) is None
        assert team_games.normalize_client_game(client_game(1, ms(29, 18), five(), ENEMIES, map_id=12), roster) is None
        assert team_games.normalize_client_game(client_game(1, ms(29, 18), five(), ENEMIES[:3]), roster) is None


class TestNormalizeApiGame:
    """Test cases for normalize_api_game."""

    def test_tournament_game(self, roster):
        positions = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
        participants = [
            {"teamId": 100, "puuid": f"puuid-{nick}", "riotIdGameName": "x", "riotIdTagline": "y",
             "championId": i + 1, "championName": "?", "teamPosition": positions[i], "win": False}
            for i, (_, nick, _) in enumerate(OURS[:5])
        ] + [
            {"teamId": 200, "puuid": f"enemy-{i}", "riotIdGameName": f"E{i}", "riotIdTagline": "X",
             "championId": 0, "championName": "MonkeyKing", "teamPosition": positions[i], "win": True}
            for i in range(5)
        ]
        match = {"metadata": {"matchId": "EUW1_9"}, "info": {
            "mapId": 11, "gameStartTimestamp": ms(30, 17), "gameDuration": 1500, "tournamentCode": "ABC",
            "participants": participants, "teams": [{"teamId": 200, "bans": [{"championId": 1}]}]}}

        game = team_games.normalize_api_game(match, roster)

        assert game["tournament"] is True and game["side"] == "Blue" and game["win"] is False
        assert [p["player"] for p in game["ours"]] == ["drewin", "freakk9", "iym0409", "khal", "drukarz_lp"]
        assert game["enemy"][0] == {"role": "Top", "riot_id": "E0#X", "champion": "Wukong"}
        assert game["enemy_bans"] == ["Annie"]


class TestPickMatch:
    """Test cases for pick_match."""

    def game(self, start_ms, enemies=ENEMIES, tournament=False):
        return {"start_ms": start_ms, "tournament": tournament, "enemy": [{"riot_id": r} for r in enemies]}

    def test_two_scrim_blocks_in_one_day(self):
        entries = team_games.parse_match_entries([MATCHES_HEADER, match_row(29, 18, "Scrim", "Cockpit"), match_row(29, 20, "Scrim", "Francja")])

        assert team_games.pick_match(entries, self.game(ms(29, 18, 10)), {}) == "29.10 18:00 · Scrim"
        assert team_games.pick_match(entries, self.game(ms(29, 20, 40)), {}) == "29.10 20:00 · Scrim"

    def test_same_opponents_stay_in_their_match(self):
        """Test that a late game against the first block's team isn't moved to the second block."""
        entries = team_games.parse_match_entries([MATCHES_HEADER, match_row(29, 18, "Scrim"), match_row(29, 20, "Scrim")])
        enemies = {"29.10 18:00 · Scrim": {r.lower() for r in ENEMIES}}

        assert team_games.pick_match(entries, self.game(ms(29, 20, 30)), enemies) == "29.10 18:00 · Scrim"

    def test_new_opponents_get_a_new_match(self):
        entries = team_games.parse_match_entries([MATCHES_HEADER, match_row(29, 18, "Scrim")])
        enemies = {"29.10 18:00 · Scrim": {r.lower() for r in ENEMIES}}

        assert team_games.pick_match(entries, self.game(ms(29, 19, 30), OTHER_ENEMIES), enemies) is None
        assert team_games.pick_match(entries, self.game(ms(30, 18)), {}) is None

    def test_officials_go_to_official_matches(self):
        entries = team_games.parse_match_entries([MATCHES_HEADER, match_row(30, 16, "Scrim"), match_row(30, 17, "Quali")])
        enemies = {"30.10 17:00 · Quali": {r.lower() for r in OTHER_ENEMIES}}

        assert team_games.pick_match(entries, self.game(ms(30, 17, 50), tournament=True), enemies) == "30.10 17:00 · Quali"


class TestIngestGames:
    """Test cases for ingest_games: no duplicates, new matches for unknown scrims."""

    async def test_adds_maps_once(self, monkeypatch, roster):
        await db.init_db()
        sheet = FakeSheet(matches=[match_row(29, 18, "Scrim", "Cockpit")])
        monkeypatch.setattr(team_games, "team_sheet", sheet)
        games = [team_games.normalize_client_game(client_game(i, ms(29, 18, 10 * i), five(), ENEMIES, win=i == 1), roster) for i in (1, 2)]

        first = await team_games.ingest_games(games + games[:1])  # same game twice in one upload
        second = await team_games.ingest_games(games)

        assert len(first["added"]) == 2 and first["already_known"] == 1
        assert second["added"] == [] and second["already_known"] == 2
        rows = sheet.appended["Mapy"]
        assert [(r["Mecz"], r["Wynik"], r["Game ID"]) for r in rows] == [
            ("29.10 18:00 · Scrim", "W", "EUW1_1"), ("29.10 18:00 · Scrim", "L", "EUW1_2"),
        ]
        assert rows[0]["Top"] == "drewin" and rows[0]["Top champ"] == "Annie" and rows[0]["Strona"] == "Red"
        assert rows[0]["Picki przeciwnika"] == "LeBlanc, Vladimir, Fiddlesticks, Kayle, Master Yi"
        assert "Mapa" not in rows[0] and "Typ" not in rows[0]  # formula columns are left alone

    async def test_game_typed_into_sheet_is_skipped(self, monkeypatch, roster):
        await db.init_db()
        maps = [["29.10 18:00 · Scrim", 1, "Scrim", "", "W"] + [""] * 12 + ["EUW1_1"]]
        sheet = FakeSheet(matches=[match_row(29, 18, "Scrim")], maps=maps)
        monkeypatch.setattr(team_games, "team_sheet", sheet)
        game = team_games.normalize_client_game(client_game(1, ms(29, 18, 10), five(), ENEMIES), roster)

        summary = await team_games.ingest_games([game])

        assert summary["added"] == [] and sheet.appended["Mapy"] == []

    async def test_scrim_without_match_creates_one(self, monkeypatch, roster):
        await db.init_db()
        sheet = FakeSheet()
        monkeypatch.setattr(team_games, "team_sheet", sheet)
        games = [
            team_games.normalize_client_game(client_game(1, ms(29, 18, 7), five(), ENEMIES), roster),
            team_games.normalize_client_game(client_game(2, ms(29, 18, 52), five(), ENEMIES), roster),
            team_games.normalize_client_game(client_game(3, ms(29, 20, 3), five(), OTHER_ENEMIES), roster),
        ]

        summary = await team_games.ingest_games(games)

        assert [r["Godz."] for r in sheet.appended["Mecze"]] == ["18:07", "20:03"]
        assert sheet.appended["Mecze"][0]["Typ"] == "Scrim" and "E1#X" in sheet.appended["Mecze"][0]["Notatki"]
        assert [label for _, label in summary["added"]] == ["29.10 18:07 · Scrim", "29.10 18:07 · Scrim", "29.10 20:03 · Scrim"]

    async def test_official_without_match_is_left_for_later(self, monkeypatch, roster):
        await db.init_db()
        sheet = FakeSheet()
        monkeypatch.setattr(team_games, "team_sheet", sheet)
        game = team_games.normalize_client_game(client_game(1, ms(30, 17), five(), ENEMIES), roster)
        game["tournament"] = True

        summary = await team_games.ingest_games([game])

        assert summary["unlinked"] == [game] and sheet.appended == {"Mecze": [], "Mapy": []}
        assert await db.get_team_game_ids() == set()


class TestClientUpload:
    """Test cases for ingest_client_upload."""

    @pytest.fixture
    def sheet(self, monkeypatch):
        sheet = FakeSheet(matches=[match_row(29, 18, "Scrim")])
        monkeypatch.setattr(team_games, "team_sheet", sheet)
        monkeypatch.setattr(team_games, "load_team_roster", AsyncMock(return_value=team_games.TeamRoster(
            [{"riot_id": r, "nick": n, "role": role, "puuid": None} for r, n, role in OURS])))
        return sheet

    async def test_custom_games_from_client_data(self, monkeypatch, sheet):
        await db.init_db()
        monkeypatch.setattr(team_games.riot_api, "get_match_details", AsyncMock(side_effect=RiotAPIError(404, "")))
        payload = {"format": "lp-tracker-scrims", "games": [
            client_game(1, ms(29, 18, 5), five(), ENEMIES),
            client_game(2, ms(29, 18, 50), [("X#1", 1, "Top"), ("Y#1", 2, "Jungle")] + five()[2:4], ENEMIES),
        ]}

        summary = await team_games.ingest_client_upload(payload)
        again = await team_games.ingest_client_upload(payload)

        assert len(summary["added"]) == 1 and summary["not_ours"] == 1
        assert again["added"] == [] and again["already_known"] == 1

    async def test_wrong_file(self, sheet):
        with pytest.raises(ValueError):
            await team_games.ingest_client_upload({"games": []})


class TestDrafts:
    """Test cases for the Drafty tab."""

    def map_row(self, result, champs, enemy="Ahri, Zed", side="Blue", type_="Scrim", game_id="", players=None):
        players = players or ["drewin", "freakk9", "iym0409", "khal", "drukarz_lp"]
        row = ["m", 1, type_, "", result, side]
        for player, champ in zip(players, champs):
            row += [player, champ]
        return row + [enemy, game_id, ""]

    def maps(self):
        rows = [
            self.map_row("W", ["Aatrox", "Vi", "Ahri", "Jinx", "Nautilus"], game_id="EUW1_1"),
            self.map_row("L", ["Aatrox", "Vi", "Orianna", "Jinx", "Nautilus"], side="Red"),
            self.map_row("W", ["Gwen", "Vi", "Orianna", "Ezreal", "Karma"], type_="Quali", enemy="Zed"),
            self.map_row("", ["Gwen", "Vi", "Orianna", "Ezreal", "Karma"]),  # no result: not counted
        ]
        return drafts.parse_maps([MAPS_HEADER] + rows)

    def test_champion_rows(self):
        rows = drafts.build_champion_rows(self.maps(), ["drewin", "freakk9"], {("drewin", "Aatrox"): (10, 6)})

        assert rows[0] == ["drewin", "Top", "Aatrox", 2, 1, 1, 0.5, "1-1", "", "10 gier · 60%"]
        assert rows[1][:3] == ["drewin", "Top", "Gwen"] and rows[1][7:9] == ["", "1-0"]
        assert rows[2][:4] == ["freakk9", "Jungle", "Vi", 3]

    def test_pairs_enemies_sides_bans(self):
        maps = self.maps()

        assert drafts.build_pair_rows(maps, "ADC", "Support") == [["Jinx", "Nautilus", 2, 1, 0.5], ["Ezreal", "Karma", 1, 1, 1.0]]
        assert drafts.build_enemy_rows(maps)[0] == ["Zed", 3, 2, 1, 0.6667]
        assert drafts.build_side_rows(maps) == [["Blue", 2, 2, 0, 1.0], ["Red", 1, 0, 1, 0.0]]
        assert drafts.build_ban_rows(maps, {"EUW1_1": ["Yone", "Yone", "Zed"]}) == [["Yone", 1, 1.0], ["Zed", 1, 1.0]]

    def test_updates_fill_every_block(self):
        updates = drafts.build_drafts_updates(self.maps(), ["drewin"], {}, {}, datetime(2026, 10, 30, 12, 0, tzinfo=TIMEZONE))
        by_range = {u["range"]: u["values"] for u in updates}

        assert by_range["A2"][0][0].startswith("Na podstawie 3 map (2 scrimy, 1 oficjale)")
        assert by_range["A4:J4"] == [drafts.BLOCKS["champions"][2]]
        assert len(by_range["A5:J104"]) == 100 and by_range["A5:J104"][-1] == [""] * 10
        assert by_range["AJ4:AL4"] == [["Champion", "Bany", "% map"]]
