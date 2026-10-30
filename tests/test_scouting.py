"""Tests for opponent scouting."""
from collections import Counter
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import gspread
import pytest
from aioresponses import aioresponses

import bot as bot_module
import database as db
import scouting
from champions import champions
from config import TIMEZONE
from scouting import (
    parse_players, season_of, parse_patches, parse_champion_table, combine_pro_tables, champion_key, summarize,
    ban_suggestions, recent_months, build_sheet_rows, build_sheet_format, scout_rows, parse_opponent_rows,
    ranked_policy, group_accounts, merged_summary, account_text, similar_names, role_evidence, SHEET_COLS, MONTH_SECONDS,
)

YEAR = 2026
NOW = int(datetime(2026, 10, 30, 18, 0, tzinfo=TIMEZONE).timestamp())
DAY = 24 * 60 * 60
GOLGG_HTML = """
<table class="region_filter"><tr><td>Season:</td></tr></table>
<select id='patch' class='form-control' name='patch'><option value='ALL'>-- ALL --</option><option value='16.20.'>16.20</option>
<option value='16.18.'>16.18</option><option value='16.18.'>16.18</option><option value='16.17.'>16.17</option>
<option value='16.16.'>16.16</option></select>
<table class='table_list playerslist tablesaw trhover'>
<thead><tr><th>Champion</th><th>Picks</th><th>Bans</th><th>PrioScore</th><th>Wins</th><th>Losses</th><th>Winrate</th></tr></thead>
<tbody>
<tr><td><a href='./champion-stats/1/'>Orianna</a></td><td>6</td><td>3</td><td>68%</td><td>4</td><td>2</td><td>67%</td></tr>
<tr><td><a href='./champion-stats/2/'>Nunu</a></td><td>2</td><td>1</td><td>0%</td><td>1</td><td>1</td><td>50%</td></tr>
<tr><td><a href='./champion-stats/3/'>Kaisa</a></td><td>12</td><td>0</td><td>14%</td><td>6</td><td>6</td><td>50%</td></tr>
</tbody></table>
"""


def game(champion: str, win=True, days_ago=10, queue=420, role="Top", season=YEAR, puuid="p1", n=0) -> dict:
    return {"puuid": puuid, "match_id": f"EUW1_{puuid}_{champion}_{days_ago}_{queue}_{n}", "queue_id": queue,
            "game_start": (NOW - days_ago * DAY) * 1000, "season": season, "champion": champion, "role": role,
            "win": win, "kills": 5, "deaths": 2, "assists": 5}


def games(champion: str, wins: int, losses: int, **kwargs) -> list[dict]:
    return [game(champion, True, n=i, **kwargs) for i in range(wins)] + [game(champion, False, n=100 + i, **kwargs) for i in range(losses)]


def account(riot_id: str = "Top#EUW", platform: str = "euw1", tier: str = None) -> dict:
    """A looked-up account with its ranks and SoloQ policy (no summary yet)."""
    solo = {"tier": tier, "rank": "I", "leaguePoints": 50, "wins": 10, "losses": 10} if tier else None
    return {"riot_id": riot_id, "puuid": riot_id, "platform": platform, "ranks": {"solo": solo} if solo else {},
            "policy": ranked_policy(platform, solo)}


def player(summary: dict, *accounts: dict) -> dict:
    """A report player: one account (Top#EUW by default) or several merged."""
    accounts = list(accounts) or [account()]
    return {"name": " / ".join(a["riot_id"].split("#")[0] for a in accounts), "accounts": accounts, "summary": summary}


def empty_history() -> dict:
    return {"games": 0, "wins": 0, "players": Counter(), "picks": Counter(), "bans": Counter()}


def report_with(players: list[dict], **fields) -> dict:
    report = {"team": "AGH", "year": YEAR, "months": 6, "tourney_months": 6,
              "created": datetime(2026, 10, 30, 16, 0, tzinfo=TIMEZONE), "players": players,
              "bans": ban_suggestions(players), "summary": None, "pro_meta": {}, "pro_patches": ["16.20", "16.18", "16.17"],
              "pro_games": 1000, "history": empty_history()}
    report.update(fields)
    return report


class TestParsePlayers:
    """Test cases for parse_players."""

    def test_multi_opgg_link(self):
        link = "https://op.gg/lol/multisearch/euw?summoners=iym%230509%2CRychu+Peja%23SLUG%2CKiziu%23Gwali"
        assert parse_players(link) == ["iym#0509", "Rychu Peja#SLUG", "Kiziu#Gwali"]

    def test_euw_and_eune_links(self):
        text = ("https://op.gg/lol/multisearch/euw?summoners=Top%23EUW%2CMid%23EUW "
                "https://op.gg/lol/multisearch/eune?summoners=TopSmurf%23EUNE%2CMid%23EUW")
        assert parse_players(text) == ["Top#EUW", "Mid#EUW", "TopSmurf#EUNE"]

    def test_profile_links_and_list(self):
        text = ("https://op.gg/lol/summoners/eune/Some%20Name-EUNE\n"
                "Rychu Peja#SLUG, alive n HAPPY33#frk; rychu peja#slug")
        assert parse_players(text) == ["Some Name#EUNE", "Rychu Peja#SLUG", "alive n HAPPY33#frk"]

    def test_nothing_useful(self):
        assert parse_players("ekipa z AGH") == []


class TestProMeta:
    """Test cases for the gol.gg pro meta of the last patches."""

    def test_patches(self):
        assert parse_patches(GOLGG_HTML) == ["16.20.", "16.18.", "16.17.", "16.16."]
        assert parse_patches("<html></html>") == []

    def test_table(self):
        table = parse_champion_table(GOLGG_HTML)

        assert table["orianna"] == {"champion": "Orianna", "picks": 6, "bans": 3, "wins": 4, "losses": 2}
        assert table[champion_key("Nunu & Willump")]["picks"] == 2
        assert table[champion_key("Kai'Sa")]["wins"] == 6
        assert parse_champion_table("<html>Cloudflare</html>") == {}

    def test_combined_patches(self):
        table = parse_champion_table(GOLGG_HTML)

        champions, games = combine_pro_tables([table, table])

        assert games == 4  # 40 picks, ten per game
        assert champions["orianna"]["picks"] == 12 and champions["orianna"]["presence"] == (12 + 6) / 4
        assert champions["orianna"]["wr"] == 8 / 12
        assert champions["nunu"]["wr"] is None  # 4 picks, too few for a winrate

    async def test_fetch_last_three_patches_with_roles(self, monkeypatch):
        monkeypatch.setattr(scouting, "_pro_meta_cache", {})
        monkeypatch.setattr(scouting, "GOLGG_DELAY", 0)
        url = scouting.GOLGG_URL.format(season=16)
        with aioresponses() as mocked:
            mocked.get(url, body=GOLGG_HTML)
            for _ in range(3 * 6):
                mocked.post(url, body=GOLGG_HTML)

            meta = await scouting.fetch_pro_meta(2026)

            posts = [call.kwargs["data"] for (method, _), calls in mocked.requests.items() if method == "POST" for call in calls]
        assert meta["patches"] == ["16.20", "16.18", "16.17"] and meta["games"] == 6
        assert posts[:6] == [{"patch": "16.20.", "role": role} for role in ("ALL", "TOP", "JUNGLE", "MID", "ADC", "SUPPORT")]
        assert len(posts) == 18 and posts[6]["patch"] == "16.18."
        assert meta["champions"]["orianna"]["roles"] == {role: 18 for role in ("Top", "Jungle", "Mid", "ADC", "Support")}
        assert await scouting.fetch_pro_meta(2026) is meta  # cached

    def test_timing_and_roles_combined(self):
        first = {"orianna": {"champion": "Orianna", "picks": 4, "bans": 2, "wins": 2, "losses": 2, "ban_turn": 2.0, "pick_round": 1.5}}
        second = {"orianna": {"champion": "Orianna", "picks": 6, "bans": 6, "wins": 4, "losses": 2, "ban_turn": 4.0, "pick_round": 2.0}}
        roles = {"Mid": [{"orianna": {"champion": "Orianna", "picks": 8}}], "Top": [{"orianna": {"champion": "Orianna", "picks": 2}}]}

        champions, games = combine_pro_tables([first, second], roles)

        orianna = champions["orianna"]
        assert games == 1 and orianna["ban_turn"] == (2 * 2 + 4 * 6) / 8 and orianna["pick_round"] == (1.5 * 4 + 2 * 6) / 10
        assert orianna["roles"] == {"Mid": 8, "Top": 2} and scouting.flex_roles(orianna["roles"]) == ["Mid", "Top"]

    def test_table_with_timing_columns(self):
        html = GOLGG_HTML.replace("<th>Winrate</th>", "<th>Winrate</th><th>Avg BT</th><th>Avg RP</th>")
        html = html.replace("<td>67%</td></tr>", "<td>67%</td><td>2.5</td><td>1.40</td></tr>")
        html = html.replace("<td>50%</td></tr>", "<td>50%</td><td>-</td><td>2.00</td></tr>")
        table = parse_champion_table(html)
        assert (table["orianna"]["ban_turn"], table["orianna"]["pick_round"]) == (2.5, 1.4)
        assert table["nunu"]["ban_turn"] is None

    async def test_unavailable(self, monkeypatch):
        monkeypatch.setattr(scouting, "_pro_meta_cache", {})
        with aioresponses() as mocked:
            mocked.get(scouting.GOLGG_URL.format(season=16), status=503)
            assert await scouting.fetch_pro_meta(2026) == scouting.EMPTY_PRO_META

    def test_season(self):
        assert season_of("16.21.712.4512") == 2026
        assert season_of("15.24.1.1") == 2025
        assert season_of("") is None


class TestDraftContext:
    """Test cases for what the draft plan's model gets."""

    def test_meta_facts(self):
        meta = {
            "nocturne": {"champion": "Nocturne", "picks": 50, "bans": 300, "presence": 0.76, "wr": 0.5,
                         "ban_turn": 3.8, "pick_round": 2.1, "roles": {"Jungle": 50}},
            "rumble": {"champion": "Rumble", "picks": 40, "bans": 20, "presence": 0.44, "wr": None,
                       "ban_turn": None, "pick_round": 1.6, "roles": {"Top": 20, "Jungle": 12, "Mid": 8}},
        }
        lines = scouting.meta_facts(report_with([], pro_meta=meta))

        assert lines[1] == "- Nocturne [Jungle]: P+B 76%, WR 50%, śr. tura bana 3.8, śr. runda picku 2.1"
        assert lines[2] == "- Rumble [Top/Jungle/Mid]: P+B 44%, WR -, śr. tura bana -, śr. runda picku 1.6"
        assert "Najczęstsze picki pro na Jungle: Nocturne 50, Rumble 12" in lines
        assert lines[-1] == "Flex picki pro: Rumble (Top/Jungle/Mid)"
        assert scouting.meta_facts(report_with([])) == []

    def test_guide_file(self, tmp_path, monkeypatch):
        guide = tmp_path / "draft_guide.md"
        monkeypatch.setattr(scouting, "DRAFT_GUIDE", guide)
        assert scouting.draft_guide() == "" and "Wytyczne drużyny:" not in scouting.draft_system_prompt()

        guide.write_text("  Banujemy pod gracza.\n", encoding="utf-8")
        assert scouting.draft_system_prompt().endswith("Wytyczne drużyny:\nBanujemy pod gracza.")

    async def test_our_team(self, monkeypatch):
        import team_games
        monkeypatch.setattr(scouting, "GOOGLE_SHEET_ID", "sheet")
        monkeypatch.setattr(team_games, "load_team_roster", AsyncMock(return_value=team_games.TeamRoster([
            {"riot_id": "Mid#EUW", "nick": "iym", "role": "Mid", "puuid": "m"},
            {"riot_id": "Top#EUW", "nick": "drewin", "role": "Top", "puuid": "t"},
            {"riot_id": "Old#EUW", "nick": "ex", "role": None, "puuid": "o"},
        ])))
        monkeypatch.setattr(scouting.team_sheet, "read_roster_cached", AsyncMock(return_value=[
            {"riot_id": "Mid#EUW", "status": "Main"}, {"riot_id": "Top#EUW", "status": "Próba"}]))
        soloq = [{"puuid": "m", "queue_id": 420, "remake": False, "champion_name": "Ahri", "win": w} for w in (1, 1, 0)]
        soloq.append({"puuid": "m", "queue_id": 440, "remake": False, "champion_name": "Zed", "win": 1})
        monkeypatch.setattr(scouting.db, "get_matches_since", AsyncMock(return_value=soloq))
        monkeypatch.setattr(scouting.db, "get_team_game_stats", AsyncMock(return_value=[
            {"ours": True, "player": "drewin", "champion": "Aatrox", "win": True, "lane_opponent": "Gnar"},
            {"ours": False, "player": "E#1", "champion": "Jax", "win": False, "lane_opponent": "Aatrox"}]))

        our = await scouting.our_roster_games(NOW * 1000)
        lines = scouting.our_team_facts(our)

        assert [m["nick"] for m in our] == ["drewin", "iym"]
        assert our[1]["soloq"][0] == {"champion": "Ahri", "opponent": None, "win": True}
        assert lines == [
            "Nasza drużyna (role ze Składu):",
            "- Top drewin (Próba): SoloQ 30 dni: brak; scrimy i oficjale: Aatrox 1 (100%)",
            "- Mid iym (Main): SoloQ 30 dni: Ahri 3 (67%); scrimy i oficjale: brak",
        ]

    def test_embed_summary(self):
        assert scouting.embed_summary("### Bany\n• Viktor") == "**Bany**\n• Viktor"
        assert scouting.embed_summary("x" * 50, limit=10) == "x" * 9 + "…"
        assert scouting.embed_summary(None) is None


def our_member(nick: str, role: str, scrims=(), soloq=()) -> dict:
    """Our roster player with games given as (champion, opponent, win)."""
    def games_of(items):
        return [{"champion": c, "opponent": o, "win": w} for c, o, w in items]
    return {"nick": nick, "role": role, "status": "Main", "scrims": games_of(scrims), "soloq": games_of(soloq)}


class TestAnswers:
    """Test cases for our answers per role and the tags of suggested bans."""

    def enemy_mid(self) -> dict:
        history = games("Viktor", 6, 2, role="Mid") + games("Veigar", 4, 4, role="Mid") + games("Syndra", 2, 0, queue=3130, role="Mid")
        return player(summarize(history, NOW, 6, {}), account("Maly#pit"))

    def our_mid(self) -> list[dict]:
        return [our_member("iym", "Mid",
                           scrims=[("Orianna", "Viktor", True)],
                           soloq=[("Syndra", "Viktor", True), ("Syndra", "Viktor", False), ("Syndra", "Lux", True),
                                  ("Syndra", "Lux", True), ("Ahri", None, True), ("Ahri", None, False), ("Akali", "Veigar", True)])]

    def tables(self) -> dict:
        return {
            ("Viktor", "Mid"): {"Syndra": (1000, 480), "Orianna": (800, 400), "Ahri": (2000, 1100)},
            ("Veigar", "Mid"): {"Akali": (500, 200)},
            ("Syndra", "Mid"): {"Kassadin": (600, 270), "Viktor": (1000, 520)},
            ("Orianna", "Mid"): {"Fizz": (50, 10)},
        }

    def test_role_pool_counts_scrims_more(self):
        assert scouting.role_pool(self.our_mid(), "Mid", 3) == ["Syndra", "Orianna", "Ahri"]

    def test_their_picks_put_tournaments_first(self):
        assert [p["champion"] for p in scouting.their_picks(self.enemy_mid())] == ["Syndra", "Viktor", "Veigar"]

    def test_answers(self):
        report = report_with([self.enemy_mid()])

        [mid] = scouting.role_answers(report, self.our_mid(), self.tables())

        viktor = next(p for p in mid["picks"] if p["champion"] == "Viktor")
        assert (mid["role"], mid["player"], mid["our_players"]) == ("Mid", "Maly", ["iym"])
        assert [(a["champion"], round(a["wr"], 2), a["games"]) for a in viktor["answers"]] == [
            ("Syndra", 0.52, 1000), ("Orianna", 0.5, 800), ("Ahri", 0.45, 2000)]
        assert viktor["answers"][0]["ours"] == {"scrims": (0, 0), "soloq": (2, 1)}
        assert viktor["answers"][1]["ours"] == {"scrims": (1, 1), "soloq": (0, 0)}
        veigar = next(p for p in mid["picks"] if p["champion"] == "Veigar")
        assert [(a["champion"], a["wr"]) for a in veigar["answers"]] == [("Akali", 0.6)]
        assert mid["picks"][0]["champion"] == "Syndra" and mid["picks"][0]["answers"] == []
        assert scouting.answer_line(mid) == "**Mid**: Viktor → Syndra 52%, Orianna 50%, Ahri 45%"

    def test_counters_and_tags(self):
        report = report_with([self.enemy_mid()])
        counters = scouting.counters_to_us(report, self.our_mid(), self.tables())

        assert set(counters) == {"Kassadin"}  # beats our Syndra at 45% over 600 games; Fizz has too few
        assert counters["Kassadin"]["vs"] == "Syndra" and counters["Kassadin"]["their_games"] == 0

        pro = {"viktor": {"presence": 0.5, "wr": 0.5, "ban_turn": 3.0}}
        bans = ban_suggestions(report["players"], counters, pro)
        by_name = {b["champion"]: b for b in bans}
        assert by_name["Viktor"]["tags"] == ["komfort", "meta"]
        assert by_name["Kassadin"]["tags"] == ["kontruje nas"] and by_name["Kassadin"]["counter"] == "Syndra ma 45% (600 gier)"
        assert scouting.ban_detail(by_name["Kassadin"]) == "kontra: Syndra ma 45% (600 gier)"
        assert scouting.tag_icons(by_name["Viktor"]) == "🎯📈"

    def test_answers_table_in_the_sheet(self):
        report = report_with([self.enemy_mid()])
        report["answers"] = scouting.role_answers(report, self.our_mid(), self.tables())

        rows, layout = build_sheet_rows(report)

        label = next(i for i, row in enumerate(rows) if row[0].startswith("Mid · ich gracz: Maly"))
        assert rows[label + 1][:3] == ["Ich pick", "Turnieje", "SoloQ"] and rows[label + 1][9] == "Nasza odpowiedź"
        viktor = next(i for i in range(label, len(rows)) if rows[i][0] == "Viktor")
        assert rows[viktor][:3] == ["Viktor", "", 8] and rows[viktor][9:14] == ["Syndra", 0.52, 1000, "", "1-1"]
        assert rows[viktor + 1][0] == "" and rows[viktor + 1][9] == "Orianna"
        assert rows[label + 2][0] == "Syndra" and rows[label + 2][9] == "brak danych"
        assert label in layout["labels"] and (label + 1, 9) in [(t[0], t[2]) for t in layout["tables"]]


class TestSummarize:
    """Test cases for summarize and ban_suggestions."""

    def test_period_and_recent_months(self):
        history = (games("Camille", 6, 1, days_ago=20) + games("Jax", 1, 2, days_ago=120)
                   + games("Aatrox", 3, 3, days_ago=200)
                   + games("Gnar", 2, 0, queue=3130, days_ago=50)
                   + games("Orianna", 3, 0, queue=700))  # Clash isn't scouted
        history.append(game("Camille", win=None, n=999))  # remake
        history.append(game("Teemo", season=None, n=998))  # not on Summoner's Rift

        s = summarize(history, NOW, 6, {"camille": {"presence": 0.2, "wr": 0.5}})

        assert [(c["champion"], c["games"], c["wins"]) for c in s["ranked"]] == [("Camille", 7, 6), ("Jax", 3, 1)]
        assert [c["champion"] for c in s["recent"]] == ["Camille"]
        assert [c["champion"] for c in s["tournament"]] == ["Gnar"]
        assert s["ranked"][0]["pro_presence"] == 0.2 and s["ranked"][0]["kda"] == 5.0
        assert (s["months"], s["recent_months"], s["ranked_games"], s["recent_games"]) == (6, 3, 10, 7)
        assert s["role"] == "Top" and s["one_trick"] is None

        longer = summarize(history, NOW, 12, {})
        assert [c["champion"] for c in longer["ranked"]] == ["Camille", "Aatrox", "Jax"]

    def test_flex_is_left_out(self):
        s = summarize(games("Camille", 3, 0) + games("Jax", 5, 0, queue=440), NOW, 6, {})
        assert [c["champion"] for c in s["ranked"]] == ["Camille"] and s["tournament"] == []
        assert s["ranked_label"] == "SoloQ" and s["ranked_skipped"] is None

    def test_ranked_policy(self):
        assert ranked_policy("euw1", None)["queues"] == {420}
        assert ranked_policy("eun1", {"tier": "MASTER"})["queues"] == {420}
        assert ranked_policy("eun1", {"tier": "CHALLENGER"})["skipped"] is None
        diamond = ranked_policy("eun1", {"tier": "DIAMOND"})
        assert diamond["queues"] == set() and diamond["skipped"] == "nie liczę, konto EUNE poniżej Mastera"
        assert ranked_policy("eun1", None)["queues"] == set()

    def test_eune_below_master_counts_only_tournaments(self):
        policy = ranked_policy("eun1", {"tier": "DIAMOND"})
        s = summarize(games("Camille", 8, 2) + games("Gnar", 2, 0, queue=3130), NOW, 6, {}, policy)
        assert s["ranked"] == [] and s["recent"] == [] and [c["champion"] for c in s["tournament"]] == ["Gnar"]
        assert s["ranked_skipped"] == "nie liczę, konto EUNE poniżej Mastera"

    def test_recent_months(self):
        assert [recent_months(m) for m in (1, 3, 6, 12)] == [None, 1, 3, 3]

    def test_quick_period(self):
        history = (games("Camille", 3, 1, days_ago=10) + games("Jax", 4, 0, days_ago=60)
                   + games("Gnar", 2, 0, queue=3130, days_ago=300) + games("Azir", 1, 0, queue=3130, days_ago=400))
        months, tourney_months = scouting.PERIODS["quick"]

        s = summarize(history, NOW, months, {}, tourney_months=tourney_months)

        assert [c["champion"] for c in s["ranked"]] == ["Camille"] and s["recent_months"] is None and s["recent"] == []
        assert [c["champion"] for c in s["tournament"]] == ["Gnar"] and s["tourney_months"] == 12

    def test_one_trick(self):
        s = summarize(games("Camille", 10, 5) + games("Jax", 2, 2), NOW, 6, {})
        assert s["one_trick"]["champion"] == "Camille"

    def test_ban_suggestions(self):
        top = player(summarize(games("Camille", 8, 2) + games("Jax", 1, 3), NOW, 6, {}))
        mid = player(summarize(games("Ahri", 5, 5, role="Mid") + games("Azir", 2, 1, queue=3130, role="Mid"), NOW, 6, {}),
                     account("Mid#EUW"))
        error = {"riot_id": "X#1", "error": "Nie znaleziono konta"}

        bans = ban_suggestions([top, mid, error])

        assert [b["champion"] for b in bans] == ["Camille", "Azir", "Ahri"]
        assert bans[0]["reason"] == "SoloQ: 10 gier, 80% WR" and bans[0]["player"] == "Top"
        assert bans[1]["reason"] == "turnieje: 3 gry, 67% WR"
        assert (bans[1]["ranked_games"], bans[1]["tourney_games"], bans[1]["tourney_wr"]) == (0, 3, 2 / 3)


class TestFivePlayers:
    """Test cases for splitting a team's accounts into five players."""

    def team(self, eune_order: list[str]) -> tuple[list[dict], dict]:
        """Five EUW accounts in role order and five EUNE ones (in eune_order) with their games."""
        euw = {role: account(f"{role}#EUW") for role in ("Top", "Jungle", "Mid", "ADC", "Support")}
        eune = {role: account(f"e{role}#EUNE", "eun1") for role in ("Top", "Jungle", "Mid", "ADC", "Support")}
        played = {a["puuid"]: games("Ahri", 20, 0, role=role, puuid=a["puuid"]) for role, a in euw.items()}
        played[eune["Top"]["puuid"]] = games("Gnar", 8, 0, role="Top", puuid=eune["Top"]["puuid"])
        played[eune["Jungle"]["puuid"]] = []  # nothing to go by
        for role in ("Mid", "ADC", "Support"):
            puuid = eune[role]["puuid"]
            played[puuid] = games("Azir", 10, 0, queue=3130, role=role, puuid=puuid, days_ago=30)
        return list(euw.values()) + [eune[role] for role in eune_order], played

    def split(self, accounts: list[dict], played: dict, roles: dict = None) -> list[tuple]:
        players = group_accounts(accounts, played, NOW, 6, {}, 6, roles)
        return [(p["summary"]["role"], [a["riot_id"] for a in p["accounts"]]) for p in players]

    def test_ten_accounts_become_five_players(self):
        accounts, played = self.team(["ADC", "Jungle", "Mid", "Support", "Top"])

        assert self.split(accounts, played) == [
            ("Top", ["Top#EUW", "eTop#EUNE"]),
            ("Jungle", ["Jungle#EUW", "eJungle#EUNE"]),
            ("Mid", ["Mid#EUW", "eMid#EUNE"]),
            ("ADC", ["ADC#EUW", "eADC#EUNE"]),
            ("Support", ["Support#EUW", "eSupport#EUNE"]),
        ]
        assert accounts[6]["role_note"] == "brak gier, rola z dopasowania"  # eJungle

    def test_accounts_from_one_game_are_two_players(self):
        accounts, played = self.team(["Top", "Jungle", "Mid", "ADC", "Support"])
        shared = dict(games("Orianna", 1, 0, role="Mid", days_ago=5)[0], match_id="EUW1_shared")
        for puuid in ("Mid#EUW", "eMid#EUNE"):
            played[puuid] = played[puuid] + [dict(shared, puuid=puuid)]

        split = dict(self.split(accounts, played))

        assert "eMid#EUNE" not in split["Mid"]

    def test_roles_from_the_sheet_are_kept(self):
        accounts, played = self.team(["Top", "Jungle", "Mid", "ADC", "Support"])

        split = dict(self.split(accounts, played, {"ADC": ["eJungle"], "Jungle": ["eADC#EUNE"]}))

        assert split["ADC"] == ["ADC#EUW", "eJungle#EUNE"] and split["Jungle"] == ["Jungle#EUW", "eADC#EUNE"]
        assert next(a for a in accounts if a["riot_id"] == "eADC#EUNE")["role_note"] == "rola z arkusza"

    def test_fewer_than_five_accounts_stay_apart(self):
        accounts = [account("A#EUW"), account("B#EUNE", "eun1"), account("C#EUW")]
        played = {a["puuid"]: games("Ahri", 5, 0, role="Mid", puuid=a["puuid"]) for a in accounts}
        assert sorted(len(p["accounts"]) for p in group_accounts(accounts, played, NOW, 6, {}, 6)) == [1, 1, 1]

    def test_recent_games_decide_the_role(self):
        played = games("Ahri", 40, 0, role="Mid", days_ago=200) + games("Gnar", 6, 0, role="Top", days_ago=5)
        assert role_evidence(played, NOW * 1000).most_common(1)[0][0] == "Top"
        assert role_evidence(played).most_common(1)[0][0] == "Mid"
        # Too few recent games: all of them count
        assert role_evidence(played[:40] + played[-2:], NOW * 1000).most_common(1)[0][0] == "Mid"

    def test_similar_names(self):
        assert similar_names("iym#0509", "iym999#0409")
        assert not similar_names("OpenYourMind#MAGIK", "Khal#UwU")

    def test_merged_summary(self):
        euw, eune = account("Sredni#pit"), account("Lumino#INT", "eun1", "DIAMOND")
        played = {
            euw["puuid"]: games("Lee Sin", 5, 1, role="Jungle", puuid=euw["puuid"])
            + games("Xin Zhao", 2, 0, queue=3130, role="Jungle", puuid=euw["puuid"]),
            # EUNE below Master: its SoloQ doesn't count, its tournament games do
            eune["puuid"]: games("Graves", 10, 0, role="Jungle", puuid=eune["puuid"])
            + games("Xin Zhao", 1, 1, queue=3130, role="Jungle", puuid=eune["puuid"], days_ago=40),
        }

        summary = merged_summary([euw, eune], played, NOW, 6, {}, 6)

        assert [(c["champion"], c["games"]) for c in summary["ranked"]] == [("Lee Sin", 6)]
        assert [(c["champion"], c["games"], c["wins"]) for c in summary["tournament"]] == [("Xin Zhao", 4, 3)]

    def test_eune_master_soloq_counts_when_merged(self):
        euw, eune = account("A#EUW"), account("B#EUNE", "eun1", "MASTER")
        played = {euw["puuid"]: games("Ahri", 2, 0, role="Mid", puuid="A#EUW"),
                  eune["puuid"]: games("Syndra", 3, 0, role="Mid", puuid="B#EUNE")}
        summary = merged_summary([euw, eune], played, NOW, 6, {}, 6)
        assert [c["champion"] for c in summary["ranked"]] == ["Syndra", "Ahri"]

    def test_account_text(self):
        eune = account("Lumino#INT", "eun1", "DIAMOND")
        assert account_text(eune, merged=False) == "Lumino#INT (EUNE, SoloQ: Diamond I 50 LP, 10-10 (50%))"
        assert account_text(eune, merged=True).endswith(", nie liczę, konto EUNE poniżej Mastera)")
        eune["role_note"] = "gra głównie Mid"
        assert account_text(eune, merged=False).endswith(", gra głównie Mid)")


class TestSheetRows:
    """Test cases for build_sheet_rows and the tab's formatting."""

    def test_layout(self):
        top = player(summarize(games("Camille", 8, 2) + games("Gnar", 1, 0, queue=3130), NOW, 6, {}))
        report = report_with([top, {"riot_id": "X#1", "error": "Nie znaleziono konta"}], summary="• banujcie Camille")

        rows, layout = build_sheet_rows(report)

        assert all(len(row) == SHEET_COLS for row in rows)
        assert rows[0][0] == "Scouting: AGH" and "patche 16.20, 16.18, 16.17" in rows[1][0]
        bans = next(i for i, row in enumerate(rows) if row[:2] == ["Champion", "Tagi"])
        assert rows[bans][:9] == ["Champion", "Tagi", "SoloQ gry", "SoloQ WR", "Turnieje gry", "Turnieje WR", "Pro P+B", "Rola", "Dlaczego"]
        assert rows[bans + 1][:9] == ["Camille", "🎯", 10, 0.8, "", "", "", "Top", "Top: SoloQ: 10 gier, 80% WR"]
        assert rows.index(["Najważniejsze"] + [""] * (SHEET_COLS - 1)) < bans
        band = next(row for row in rows if row[0].startswith("Top · "))
        assert band[0] == "Top · Top#EUW (EUW, SoloQ: Unranked)"
        titles = next(row for row in rows if row[0].startswith("SoloQ, ostatnie"))
        assert [titles[0], titles[9], titles[18]] == [
            "SoloQ, ostatnie 6 mies. (10 gier)", "SoloQ, ostatnie 3 mies. (10 gier)",
            "Turnieje (kody turniejowe), ostatnie 6 mies. (1 gra)"]
        header = next(i for i, row in enumerate(rows) if row[0] == "Champion" and row[9] == "Champion")
        assert rows[header + 1][:8] == ["Camille", 10, 8, 2, 0.8, 5.0, "", ""]
        assert rows[header + 1][18:20] == ["Gnar", 1]
        assert rows[-1][0] == "X#1: ⚠ Nie znaleziono konta"
        assert bans - 1 in layout["sections"] and rows.index(band) in layout["bands"]
        assert ["• banujcie Camille"] == [row[0] for row in rows if row[0].startswith("•")]
        assert (bans, bans + 2, 0) in [t[:3] for t in layout["tables"]]
        assert [t[2] for t in layout["tables"] if t[0] == header] == [0, 9, 18]

    def test_summary_headings_are_bold_rows(self):
        rows, layout = build_sheet_rows(report_with([], summary="### Bany – faza 1\n• **Viktor**: komfort"))
        heading = next(i for i, row in enumerate(rows) if row[0] == "Bany – faza 1")
        assert heading in layout["labels"] and rows[heading + 1][0] == "• Viktor: komfort"

    def test_merged_player_band(self):
        euw, eune = account("Sredni#pit", tier="MASTER"), account("Lumino#INT", "eun1", "DIAMOND")
        report = report_with([player(summarize(games("Lee Sin", 3, 1, role="Jungle"), NOW, 6, {}), euw, eune)])

        rows, _ = build_sheet_rows(report)
        embed = scouting.build_embed(report)

        band = next(row for row in rows if row[0].startswith("Jungle · "))[0]
        assert band == ("Jungle · Sredni#pit (EUW, SoloQ: Master 50 LP, 10-10 (50%)) + "
                        "Lumino#INT (EUNE, SoloQ: Diamond I 50 LP, 10-10 (50%), nie liczę, konto EUNE poniżej Mastera)")
        assert embed.fields[0].name == "Jungle · Sredni#pit (EUW) + Lumino#INT (EUNE)"
        assert "EUNE: Diamond I 50 LP, 10-10 (50%) · SoloQ nie liczę, konto EUNE poniżej Mastera" in embed.fields[0].value

    def test_pro_meta_block(self):
        meta = {"orianna": {"champion": "Orianna", "picks": 20, "bans": 30, "presence": 0.5, "wr": 0.55,
                            "ban_turn": 2.44, "pick_round": 1.31, "roles": {"Mid": 20}},
                "ahri": {"champion": "Ahri", "picks": 10, "bans": 5, "presence": 0.15, "wr": None,
                         "ban_turn": None, "pick_round": None, "roles": {}}}
        rows, layout = build_sheet_rows(report_with([], pro_meta=meta))

        title = next(i for i, row in enumerate(rows) if row[0].startswith("Meta pro"))
        assert rows[title][0].startswith("Meta pro (gol.gg, patche 16.20, 16.18, 16.17, 1000 gier)")
        assert rows[title + 2][:8] == ["Orianna", 20, 30, 0.5, 0.55, 2.4, 1.3, "Mid"]
        assert rows[title + 3][:8] == ["Ahri", 10, 5, 0.15, "", "", "", "?"]
        assert title in layout["sections"]

    def test_format_requests(self):
        layout = {"sections": [3], "bands": [10], "block_titles": [11], "labels": [], "tables": [(4, 7, 0, ["name", "wr"]), (12, 15, 9, ["name", "wr"])]}

        requests = build_sheet_format(5, layout)

        bands = [r["repeatCell"] for r in requests if "repeatCell" in r and "backgroundColor" in r["repeatCell"]["cell"]["userEnteredFormat"]
                 and r["repeatCell"]["range"]["startRowIndex"] == 10]
        assert bands and bands[0]["range"]["endColumnIndex"] == SHEET_COLS
        rules = [r["addConditionalFormatRule"]["rule"]["ranges"][0] for r in requests if "addConditionalFormatRule" in r]
        assert {(r["startRowIndex"], r["startColumnIndex"]) for r in rules} == {(5, 1), (13, 10)}

    def test_quick_layout(self):
        top = player(summarize(games("Camille", 3, 1) + games("Gnar", 1, 0, queue=3130, days_ago=200), NOW, 1, {},
                               tourney_months=12))
        report = report_with([top], months=1, tourney_months=12)

        titles = next(row for row in build_sheet_rows(report)[0] if row[0].startswith("SoloQ, ostatnie"))
        embed = scouting.build_embed(report)

        assert [titles[0], titles[9], titles[18]] == [
            "SoloQ, ostatnie 1 mies. (4 gry)", "", "Turnieje (kody turniejowe), ostatnie 12 mies. (1 gra)"]
        assert "**SoloQ, 1 mies.:** Camille 4 (75%)" in embed.fields[0].value
        assert "**Turnieje, 12 mies.:** Gnar 1 (100%)" in embed.fields[0].value
        assert embed.footer.text.startswith("SoloQ z 1 mies., gry turniejowe z 12 mies. · pro: gol.gg, patche 16.20")

    def test_skipped_soloq(self):
        smurf = account("Smurf#EUNE", "eun1")
        report = report_with([player(summarize(games("Camille", 8, 2), NOW, 6, {}, smurf["policy"]), smurf)])

        titles = next(row for row in build_sheet_rows(report)[0] if row[18].startswith("Turnieje"))
        embed = scouting.build_embed(report)

        assert titles[0] == "SoloQ: nie liczę, konto EUNE poniżej Mastera" and titles[9] == ""
        assert "**SoloQ:** nie liczę, konto EUNE poniżej Mastera" in embed.fields[0].value
        assert "**Turnieje, 6 mies.:** brak gier" in embed.fields[0].value


class TestEmbed:
    """Test cases for build_embed."""

    def report(self, accounts: int, summary: str = None) -> dict:
        champs = ["Camille", "Jax", "Aatrox", "Gnar", "Fiora", "Renekton", "Ksante", "Rumble"]
        history = [g for i, c in enumerate(champs) for g in games(c, 5 + i, 3)]
        history += [g for c in champs for g in games(c, 2, 2, days_ago=150)]
        history += [g for c in champs for g in games(c, 1, 1, queue=3130)]
        players = [player(summarize(history, NOW, 6, {}), account(f"Very Long Player Name {i}#EUNE", "eun1", "MASTER"))
                   for i in range(accounts)]
        return report_with(players, summary=summary)

    def test_five_accounts_get_four_champions(self):
        embed = scouting.build_embed(self.report(5))
        assert embed.fields[0].name.startswith("🚫 Bany")
        value = next(f for f in embed.fields if f.name.startswith("Top · ")).value
        assert value.split("\n", 1)[1].count("(") == 4 * 3
        assert "**SoloQ, 6 mies.:**" in value and "**SoloQ, 3 mies.:**" in value and "**Turnieje, 6 mies.:**" in value

    def test_ten_accounts_fit(self):
        embed = scouting.build_embed(self.report(10, summary="• " + "banujcie Camille " * 200))
        assert len(embed) <= scouting.EMBED_MAX_LENGTH
        assert len(embed.fields) == 11


class FakeRiot:
    """Match IDs per (puuid, type) honoring paging, and match payloads."""

    def __init__(self, matches: dict[str, dict], ids: dict[tuple, list[str]]):
        self.matches, self.ids, self.details_calls, self.id_calls = matches, ids, [], []

    async def get_match_ids(self, puuid, match_type=None, start_time=None, start=0, count=100, queue=None):
        self.id_calls.append((puuid, match_type or queue, start_time, start))
        return self.ids.get((puuid, match_type or queue), [])[start:start + count]

    async def get_match_details(self, match_id):
        self.details_calls.append(match_id)
        return self.matches[match_id]


def payload(match_id: str, players: dict[str, str], queue=420) -> dict:
    return {"metadata": {"matchId": match_id}, "info": {
        "mapId": 11, "queueId": queue, "gameVersion": "16.21.1.1", "gameStartTimestamp": (NOW - 10 * DAY) * 1000,
        "participants": [{"puuid": puuid, "championId": 0, "championName": champ, "teamPosition": "TOP",
                          "win": True, "kills": 1, "deaths": 1, "assists": 1} for puuid, champ in players.items()]}}


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_lp_tracker.db")
    monkeypatch.setattr(champions, "by_key", {})
    monkeypatch.setattr(champions, "by_id", {})
    monkeypatch.setattr(champions, "refresh", AsyncMock())
    monkeypatch.setattr(scouting, "_opponents_cache", None)


class TestFetching:
    """Test cases for listing, fetching and storing scouted games."""

    @pytest.fixture
    def fake(self, monkeypatch):
        matches = {
            "EUW1_1": payload("EUW1_1", {"a": "Camille", "b": "Ahri"}),
            "EUW1_2": payload("EUW1_2", {"a": "Jax"}),
            "EUW1_3": payload("EUW1_3", {"b": "Azir"}, queue=3130),
        }
        fake = FakeRiot(matches, {("a", 420): ["EUW1_1", "EUW1_2"], ("b", 420): ["EUW1_1"],
                                  ("b", "tourney"): ["EUW1_3"], ("c", 420): ["EUW1_9"], ("c", "tourney"): ["EUW1_3"]})
        monkeypatch.setattr(scouting.riot_api, "get_match_ids", fake.get_match_ids)
        monkeypatch.setattr(scouting.riot_api, "get_match_details", fake.get_match_details)
        return fake

    async def test_new_ids_and_shared_games(self, fake):
        await db.init_db()
        since = NOW - 6 * MONTH_SECONDS

        assert await scouting.new_match_ids("a", since, {420}) == ["EUW1_1", "EUW1_2"]
        await scouting.store_scout_games(["EUW1_1", "EUW1_2"], {"a", "b"})
        assert await scouting.new_match_ids("b", since, {420}) == ["EUW1_3"]
        await scouting.store_scout_games(["EUW1_3"], {"a", "b"})
        assert await scouting.new_match_ids("a", since, {420}) == []
        # An EUNE account below Master: only tournament-code games are listed
        assert await scouting.new_match_ids("c", since, set()) == ["EUW1_3"]

        assert fake.details_calls == ["EUW1_1", "EUW1_2", "EUW1_3"]
        assert ("a", 420, since, 0) in fake.id_calls
        b_games = await db.get_scout_games("b")
        assert {(g["champion"], g["queue_id"]) for g in b_games} == {("Ahri", 420), ("Azir", 3130)}

    async def test_paging_has_no_game_limit(self, monkeypatch):
        fake = FakeRiot({}, {("a", 420): [f"EUW1_{i}" for i in range(250)]})
        monkeypatch.setattr(scouting.riot_api, "get_match_ids", fake.get_match_ids)

        assert len(await scouting.fetch_match_ids("a", NOW, queue=420)) == 250
        assert [call[3] for call in fake.id_calls] == [0, 100, 200]

    async def test_quick_scouting_lists_tournaments_further_back(self, monkeypatch, fake):
        await db.init_db()
        await scouting.new_match_ids("b", NOW - MONTH_SECONDS, {420}, NOW - 12 * MONTH_SECONDS)

        assert ("b", 420, NOW - MONTH_SECONDS, 0) in fake.id_calls
        assert ("b", "tourney", NOW - 12 * MONTH_SECONDS, 0) in fake.id_calls

    async def test_run_reports_games_to_fetch(self, monkeypatch, fake):
        await db.init_db()
        monkeypatch.setattr(scouting, "lookup_account", AsyncMock(side_effect=lambda r: {
            "riot_id": r, "puuid": r.split("#")[0], "platform": "euw1"}))
        monkeypatch.setattr(scouting.riot_api, "get_league_entries", AsyncMock(return_value=[]))
        monkeypatch.setattr(scouting, "fetch_pro_meta", AsyncMock(return_value=scouting.EMPTY_PRO_META))
        monkeypatch.setattr(scouting, "GOOGLE_SHEET_ID", "")
        monkeypatch.setattr(scouting.llm, "enabled", lambda: False)
        progress = AsyncMock()

        report = await scouting.run_scouting("AGH", ["a#1", "b#1"], 6, progress)

        progress.assert_awaited_once_with(3)
        assert report["fetched"] == 3 and report["months"] == 6
        b = next(p for p in report["players"] if p["name"] == "b")
        assert [c["champion"] for c in b["summary"]["tournament"]] == ["Azir"]

    def test_remake_row(self):
        data = payload("EUW1_9", {"a": "Camille"})
        data["info"]["participants"][0]["gameEndedInEarlySurrender"] = True
        assert scout_rows("EUW1_9", data, {"a"})[0]["win"] is None

    def test_estimate(self):
        assert scouting.fetch_estimate(10) == "około 1 min"
        assert scouting.fetch_estimate(600) == "około 13 min"


OPPONENTS_ROWS = [
    ["Drużyna", "Multi EUW", "Multi EUNE", "Notatki"],
    ["Cockpit", "https://op.gg/lol/multisearch/euw?summoners=A%23EUW%2CB%23EUW",
     "https://op.gg/lol/multisearch/eune?summoners=C%23EUNE", ""],
    ["Francja", "", "", "jeszcze nie wiemy"],
    ["AGH", "D#EUW, E#EUW", "", ""],
]


class TestKnownPlayers:
    """Test cases for finding an opponent's players."""

    def test_parse_opponent_rows(self):
        teams = parse_opponent_rows(OPPONENTS_ROWS)
        assert teams == {"cockpit": {"name": "Cockpit", "players": ["A#EUW", "B#EUW", "C#EUNE"], "roles": {}},
                         "agh": {"name": "AGH", "players": ["D#EUW", "E#EUW"], "roles": {}}}

    def test_role_columns(self):
        rows = [OPPONENTS_ROWS[0] + ["Top", "Jungle", "Mid", "ADC", "Support"],
                ["Cockpit", "A#EUW, B#EUW", "", "", "", "Lumino#INT; B", "", "", ""]]
        teams = parse_opponent_rows(rows)
        assert teams["cockpit"]["roles"] == {"Jungle": ["Lumino#INT", "B"]}
        assert teams["cockpit"]["players"] == ["A#EUW", "B#EUW", "Lumino#INT"]

    @pytest.fixture
    def sheet(self, monkeypatch):
        monkeypatch.setattr(scouting, "GOOGLE_SHEET_ID", "sheet")
        sheet = SimpleNamespace(read_tab=AsyncMock(return_value=OPPONENTS_ROWS), read_matches=AsyncMock(return_value=[]),
                                create_tab=AsyncMock(return_value=True))
        monkeypatch.setattr(scouting, "team_sheet", sheet)
        return sheet

    async def test_sheet_first(self, sheet):
        await db.init_db()
        await db.save_opponent("Cockpit", ["Old#1"])
        assert await scouting.known_players(" cockpit ") == (["A#EUW", "B#EUW", "C#EUNE"], "zakładka Przeciwnicy", {})

    async def test_saved_players_when_not_in_sheet(self, sheet):
        await db.init_db()
        await db.save_opponent("AGH Esports", ["A#1", "B#2"])
        assert await scouting.known_players("agh esports") == (["A#1", "B#2"], "zapisane z /scout", {})
        assert set(await scouting.team_names()) == {"Cockpit", "AGH", "AGH Esports"}

    async def test_missing_tab_is_added_with_opponents_from_mecze(self, sheet):
        await db.init_db()
        sheet.read_tab.side_effect = gspread.WorksheetNotFound("Przeciwnicy")
        sheet.read_matches.return_value = [["Data", "Przeciwnik"], [1, "Cockpit"], [2, "francja"], [3, "Cockpit"], [4, ""]]

        assert await scouting.known_players("Cockpit") == ([], "", {})

        tab, updates, *_ = sheet.create_tab.await_args.args
        assert tab == "Przeciwnicy"
        assert {"range": "A4:I4", "values": [["Drużyna", "Multi EUW", "Multi EUNE", "Notatki",
                                               "Top", "Jungle", "Mid", "ADC", "Support"]]} in updates
        assert {"range": "A5:A6", "values": [["Cockpit"], ["francja"]]} in updates

    async def test_from_our_games_against_them(self, monkeypatch, sheet):
        await db.init_db()
        sheet.read_tab.return_value = [OPPONENTS_ROWS[0]]
        sheet.read_matches.return_value = [
            ["Data", "Godz.", "Typ", "Przeciwnik", "Mecz (do Map)"],
            [1, 0.5, "Liga", "Cockpit", "01.10 18:00 · Liga"], [2, 0.5, "Scrim", "PWr", "02.10 18:00 · Scrim"]]
        for match_id, label, enemies in (("EUW1_1", "01.10 18:00 · Liga", ["E1#X", "E2#X"]),
                                         ("EUW1_2", "01.10 18:00 · Liga", ["E1#X", "E3#X"]),
                                         ("EUW1_3", "02.10 18:00 · Scrim", ["P1#Y"])):
            await db.save_team_game({"match_id": match_id, "source": "api", "start_ms": 0, "win": True,
                                     "enemy": [{"riot_id": r, "champion": "Ahri"} for r in enemies],
                                     "enemy_bans": ["Camille"]}, label)

        players, source, _ = await scouting.known_players("cockpit")
        history = await scouting.history_against_us("Cockpit")

        assert players[0] == "E1#X" and set(players) == {"E1#X", "E2#X", "E3#X"}
        assert source == "nasze gry z nimi (2 mapy)"
        assert history["games"] == 2 and history["bans"]["Camille"] == 2


class TestAutoScouting:
    """Test cases for scouting after the reminder the day before an official."""

    @pytest.fixture
    def lp_bot(self, monkeypatch):
        lp_bot = bot_module.LPTrackerBot()
        channel = SimpleNamespace(send=AsyncMock())
        monkeypatch.setattr(lp_bot, "get_reminder_channel", AsyncMock(return_value=channel))
        monkeypatch.setattr(bot_module.scouting, "known_players", AsyncMock(return_value=(["A#1"], "zakładka Przeciwnicy", {})))
        monkeypatch.setattr(bot_module.scouting, "start_scouting", lambda *args, **kwargs: lp_bot.started.append(args))
        lp_bot.started = []
        return lp_bot

    async def reminders(self, monkeypatch, lp_bot, type_: str, hours_before: int):
        await db.init_db()
        start = datetime(2026, 10, 31, 17, 0, tzinfo=TIMEZONE)
        match = {"start": start, "has_time": True, "type": type_, "event": "Liga", "opponent": "AGH", "notes": ""}
        monkeypatch.setattr(bot_module, "team_sheet", SimpleNamespace(read_matches=AsyncMock(return_value=[])))
        monkeypatch.setattr(bot_module, "parse_planned_matches", lambda rows: [match])
        now = start.replace(hour=17 - hours_before) if hours_before < 17 else datetime(2026, 10, 30, 18, 0, tzinfo=TIMEZONE)
        monkeypatch.setattr(bot_module, "datetime", SimpleNamespace(now=lambda tz=None: now))
        await lp_bot.check_match_reminders()

    async def test_official_is_scouted_the_day_before(self, monkeypatch, lp_bot):
        await self.reminders(monkeypatch, lp_bot, "Liga", 23)
        assert [args[1:] for args in lp_bot.started] == [("AGH", ["A#1"])]

    async def test_not_on_the_1h_reminder(self, monkeypatch, lp_bot):
        await self.reminders(monkeypatch, lp_bot, "Liga", 1)
        assert lp_bot.started == []

    async def test_not_for_scrims(self, monkeypatch, lp_bot):
        await self.reminders(monkeypatch, lp_bot, "Scrim", 23)
        assert lp_bot.started == []
