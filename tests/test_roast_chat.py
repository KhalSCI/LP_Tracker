"""Tests for conversations with the bot under SoloQ games."""
import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot as bot_module
import cogs.roasts as roasts_cog
import database as db
import player_profiles
import roast_chat
from config import TIMEZONE
from roast_chat import (
    build_game_card, build_messages, player_knowledge, author_profiles, standouts, HISTORY_MESSAGES,
)

PUUID = "test-puuid"
MATCH_ID = "EUW1_123"
START = datetime(2026, 10, 30, 21, 0, tzinfo=TIMEZONE)
ROLES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
ROSTER = [{"nick": "khal", "riot_id": "OpenYourMind#MAGIK", "role": "ADC", "status": "Main"},
          {"nick": "kizi", "riot_id": "kizi4880#EUW", "role": "Top", "status": "Main"}]
OWNER_ID = 42


def participant(pid: int, team: int, position: str, champion: str, champion_id: int, **extra) -> dict:
    return {"participantId": pid, "puuid": f"p{pid}", "teamId": team, "teamPosition": position, "win": team == 200,
            "championName": champion, "championId": champion_id, "kills": 2, "deaths": 2, "assists": 2,
            "totalMinionsKilled": 150, "neutralMinionsKilled": 0, "goldEarned": 9000,
            "totalDamageDealtToChampions": 10000, "totalDamageTaken": 15000, "visionScore": 15,
            "wardsPlaced": 8, "wardsKilled": 1, "visionWardsBoughtInGame": 1, **extra}


def match_payload() -> dict:
    """A 30-minute loss of khal's Ashe against Kalista."""
    players = [participant(i + 1, 100, role, "Garen", 86) for i, role in enumerate(ROLES)]
    players += [participant(i + 6, 200, role, "Darius", 122) for i, role in enumerate(ROLES)]
    players[3] = participant(4, 100, "BOTTOM", "Ashe", 22, puuid=PUUID, kills=2, deaths=6, assists=3,
                             totalMinionsKilled=200, goldEarned=10500, totalDamageDealtToChampions=15000,
                             visionScore=12, item0=3031, item1=3006, item6=3340, challenges={"soloKills": 1})
    players[8] = participant(9, 200, "BOTTOM", "Kalista", 429, kills=8, deaths=1, assists=4)
    objectives = {"dragon": {"kills": 1}, "baron": {"kills": 0}, "tower": {"kills": 2}}
    return {
        "metadata": {"matchId": MATCH_ID},
        "info": {
            "gameStartTimestamp": int(START.timestamp() * 1000), "gameDuration": 1800, "gameVersion": "16.21.712.1",
            "queueId": 420, "participants": players,
            "teams": [{"teamId": 100, "objectives": objectives},
                      {"teamId": 200, "objectives": dict(objectives, dragon={"kills": 3}, tower={"kills": 9})}],
        },
    }


def timeline_payload() -> dict:
    """Frames at 0, 10 and 15 minutes; khal (4) dies twice to Kalista (9) before 14 minutes."""
    def frames(gold: int, cs: int, xp: int) -> dict:
        return {"participantFrames": {"4": {"totalGold": gold, "minionsKilled": cs, "jungleMinionsKilled": 0, "xp": xp},
                                      "9": {"totalGold": gold + 500, "minionsKilled": cs + 10, "jungleMinionsKilled": 0, "xp": xp + 200}}}
    kill = lambda ms: {"type": "CHAMPION_KILL", "victimId": 4, "killerId": 9, "timestamp": ms}
    return {"info": {"frames": [
        dict(frames(500, 0, 0), timestamp=0, events=[]),
        dict(frames(4000, 70, 4000), timestamp=600_000, events=[kill(300_000), kill(500_000)]),
        dict(frames(6000, 110, 7000), timestamp=900_000, events=[kill(900_000)]),
    ]}}


def write_profiles(text: str) -> None:
    player_profiles.PROFILES_PATH.write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test_lp_tracker.db")
    monkeypatch.setattr(roast_chat.champions, "items", {3031: "Infinity Edge", 3006: "Berserker's Greaves", 3340: "Stealth Ward"})


class TestGameCard:
    """Test cases for build_game_card."""

    def test_player_lane_opponent_and_teams(self):
        card = build_game_card(match_payload(), timeline_payload(), PUUID, "khal")

        assert card.splitlines()[0] == "Gra: SoloQ, 30.10.2026 21:00, przegrana khal, 30 min, patch 16.21"
        assert card.splitlines()[1] == ("Co się wyróżnia: najwięcej zgonów w swojej drużynie (6); "
                                        "najwięcej obrażeń w całej grze; +1.7 CS/min względem rywala z linii")
        # Wards are only for jungle and support
        assert "wizja" not in card and "control" not in card
        assert ("khal: Ashe (ADC), KDA 2/6/3, CS 200 (6.7/min), gold 10.5k, obrażenia 15.0k (27% drużyny), "
                "KP 50%") in card
        assert "solo kille 1" in card
        assert "Build khal: Infinity Edge, Berserker's Greaves, Stealth Ward" in card
        assert "Rywal z linii: Kalista (ADC), KDA 8/1/4" in card
        assert ("Faza linii (khal minus rywal): 10. min: gold -500, CS -10, XP -200; "
                "15. min: gold -500, CS -10, XP -200") in card
        assert "Zgony khal przed 14. minutą: 2" in card
        assert "Drużyna khal: Top Garen 2/2/2" in card
        assert "Obiektywy: drużyna khal: smoki 1, barony 0, wieże 2; przeciwnicy: smoki 3, barony 0, wieże 9" in card

    def test_without_a_timeline(self):
        card = build_game_card(match_payload(), None, PUUID, "khal")

        assert "Rywal z linii: Kalista" in card
        assert "Faza linii" not in card
        assert "Zgony khal" not in card

    def test_player_not_in_the_game(self):
        with pytest.raises(ValueError):
            build_game_card(match_payload(), None, "someone-else", "khal")

    def test_vision_for_jungle_and_support(self):
        card = build_game_card(match_payload(), None, "p2", "kuba")

        assert "kuba: Garen (Jungle), KDA 2/2/2" in card
        assert "wizja 0.50/min (wardy 8, zniszczone 1, control wardy 1)" in card


def lane_player(position="TOP", win=False, **extra) -> dict:
    return participant(1, 100, position, "Aatrox", 266, win=win, **extra)


class TestStandouts:
    """Test cases for standouts (the hooks in the game card)."""

    def test_lost_after_winning_the_lane(self):
        me = lane_player(challenges={"turretPlatesTaken": 1})
        opponent = participant(6, 200, "TOP", "Jax", 24, challenges={"turretPlatesTaken": 6})
        fed = participant(7, 200, "JUNGLE", "Viego", 234, kills=12, deaths=3)
        allies = [me] + [participant(i, 100, "MIDDLE", "Ahri", 103, kills=3) for i in (2, 3, 4)]
        facts = standouts(me, opponent, allies, [opponent, fed], {15: {"gold": 1200, "cs": 5, "xp": 300}}, 4, 30)

        assert "wygrał linię (+1200 golda na 15. min), a przegrał grę" in facts
        assert "4 zgony przed 14. minutą" in facts
        assert "rywal z linii zebrał 6 płytek wież, on 1" in facts
        assert "najbardziej nakarmiony przeciwnik: Viego 12/3/2" in facts

    def test_carried_win(self):
        me = lane_player(win=True, kills=1, assists=1, totalDamageDealtToChampions=5000)
        carry = participant(2, 100, "MIDDLE", "Ahri", 103, win=True, kills=15, totalDamageDealtToChampions=40000)
        support = participant(3, 100, "UTILITY", "Bard", 432, win=True, kills=0, totalDamageDealtToChampions=3000)
        facts = standouts(me, None, [me, carry, support], [], {}, None, 20)

        assert facts == ["najmniej obrażeń w drużynie, nie licząc supporta", "tylko 12% KP",
                         "grę wyniósł Ahri (15/2/2, 83% obrażeń drużyny)", "gra skończona w 20 min"]


def chat_message(role: str, content: str, author_id: int = None, author: str = None) -> dict:
    return {"role": role, "content": content, "author_id": author_id, "author": author}


class TestBuildMessages:
    """Test cases for build_messages (what the model gets)."""

    def test_game_player_and_comment_are_pinned(self):
        history = [chat_message("game", ""), chat_message("comment", "3 porażki, LP paruje."),
                   chat_message("user", "co robię źle?", OWNER_ID, "khal")]
        messages = build_messages("KARTA", "WIEDZA", history, OWNER_ID)

        system = messages[0]["content"]
        assert messages[0]["role"] == "system"
        assert "Zawsze zostajesz w roli" in system
        assert "KARTA GRY (o tej grze jest rozmowa)\nKARTA" in system
        assert "WIEDZA O GRACZU\nWIEDZA" in system
        assert system.endswith("TWÓJ KOMENTARZ POD TĄ GRĄ (od niego zaczęła się rozmowa)\n3 porażki, LP paruje.")
        assert messages[1:] == [{"role": "user", "content": "khal (grał tę grę): co robię źle?"}]

    def test_short_term_memory_keeps_the_latest_messages(self):
        history = [chat_message("comment", "roast")]
        for i in range(30):
            history += [chat_message("user", f"pytanie {i}", 7, "kuba"), chat_message("assistant", f"odpowiedź {i}")]
        history.append(chat_message("user", "ostatnie", 7, "kuba"))
        messages = build_messages("KARTA", "WIEDZA", history, OWNER_ID)

        assert "roast" in messages[0]["content"]
        # The window starts with someone's message, the reply to answer is last
        assert messages[1] == {"role": "user", "content": "kuba: pytanie 21"}
        assert messages[-1] == {"role": "user", "content": "kuba: ostatnie"}
        assert len(messages) - 1 == HISTORY_MESSAGES - 1

    def test_without_a_comment(self):
        messages = build_messages("KARTA", "WIEDZA", [chat_message("game", ""), chat_message("user", "i?", 7, "kuba")], None)

        assert "TWÓJ KOMENTARZ" not in messages[0]["content"]
        assert messages[1:] == [{"role": "user", "content": "kuba: i?"}]


def soloq_row(i: int, win: bool, champion: str = "Ashe") -> dict:
    start = START - timedelta(hours=i)
    return {"match_id": f"EUW1_{i}", "puuid": PUUID, "queue_id": 420, "team_id": 100, "champion_name": champion,
            "win": win, "remake": False, "kills": 2, "deaths": 6, "assists": 3,
            "game_start": int(start.timestamp() * 1000), "game_duration": 1800}


def team_stat(match_id: str, win: bool, **overrides) -> dict:
    row = {"match_id": match_id, "participant_id": 4, "start_ms": 0, "duration_s": 1800, "ours": True,
           "player": "khal", "riot_id": "OpenYourMind#MAGIK", "role": "ADC", "champion": "Jinx", "win": win,
           "kills": 4, "deaths": 2, "assists": 6, "kill_participation": 0.5, "cs_per_min": 9.0, "damage_share": 0.3,
           "vision_score": 30, "deaths_before_14": 1, "gold_diff_15": 600, "cs_diff_15": 10}
    row.update(overrides)
    return row


async def add_player(tier="EMERALD", rank="II", lp=45) -> dict:
    lb_id = await db.create_leaderboard(1, "pyry")
    player_id = await db.add_player(lb_id, "OpenYourMind#MAGIK", PUUID, "khal")
    if tier:
        await db.update_player_rank(player_id, tier, rank, lp, 30, 25)
    return await db.get_player_by_puuid(PUUID)


class TestPlayerKnowledge:
    """Test cases for player_knowledge."""

    async def test_rank_form_pool_team_games_and_comments(self):
        await db.init_db()
        player = await add_player()
        await db.save_matches([soloq_row(0, False), soloq_row(1, False), soloq_row(2, True, "Jinx")])
        await db.save_team_game_stats("T1", [team_stat("T1", True)])
        await db.save_team_game_stats("T2", [team_stat("T2", False, gold_diff_15=-200)])
        await db.save_team_game_stats("T3", [team_stat("T3", True, ours=False, player="enemy", riot_id="E#1")])
        await db.mark_roasted("EUW1_0", PUUID, "int")

        text = await player_knowledge(player, "khal", "ADC", START + timedelta(hours=1), "EUW1_0")

        assert "Gracz: khal, rola w drużynie: ADC, ranga SoloQ: Emerald II 45 LP, sezon 30-25" in text
        assert "SoloQ z 30 dni: 3 gry, 1-2 (33% WR), teraz 2 porażki z rzędu" in text
        assert "Ostatnie gry SoloQ (od najnowszej): L Ashe 2/6/3, L Ashe 2/6/3, W Jinx 2/6/3" in text
        assert "Pula SoloQ: Ashe 2 gry 0% WR, Jinx 1 gra 100% WR" in text
        assert "Gry drużynowe (scrimy i oficjalne): 2 gry, 1-1, KDA 5.0, KP 50%" in text
        assert "GD@15 +200" in text
        assert "championy: Jinx 2" in text
        assert "Komentarze bota po jego grach z 7 dni: int ×1" in text
        assert "Ashe z tej gry: 2 gry w SoloQ z 30 dni, 0% WR" in text
        # Vision averages only from jungle and support games, and khal plays ADC
        assert "wizja" not in text
        assert text.endswith("Teraz jest 30.10.2026 22:00")

    async def test_only_game_on_a_champion_and_support_vision(self):
        await db.init_db()
        player = await add_player()
        await db.save_matches([soloq_row(0, False), soloq_row(2, True, "Jinx")])
        await db.save_team_game_stats("T1", [team_stat("T1", True, role="Support")])

        text = await player_knowledge(player, "khal", "ADC", START + timedelta(hours=1), "EUW1_2")

        assert "Jinx z tej gry to jego jedyna gra tym championem w SoloQ z 30 dni" in text
        assert "wizja/min (jungle i support) 1.00" in text

    async def test_team_description(self):
        await db.init_db()
        player = await add_player()
        write_profiles("# Profile\n\n## Khal\nADC i kapitan,\nlubi się wywrócić.\n\n## kizi\nTop.\n")

        text = await player_knowledge(player, "khal", "ADC", START)

        assert text.splitlines()[1] == "Opis od drużyny: ADC i kapitan, lubi się wywrócić."

    async def test_unranked_without_games(self):
        await db.init_db()
        player = await add_player(tier=None)

        text = await player_knowledge(player, "khal", None, START)

        assert "ranga SoloQ: bez rangi" in text
        assert "SoloQ z 30 dni: brak gier w bazie" in text
        assert "Gry drużynowe" not in text


async def add_kizi() -> None:
    lb_id = await db.create_leaderboard(1, "inna")
    player_id = await db.add_player(lb_id, "kizi4880#EUW", "kizi-puuid", "kizi")
    await db.update_player_rank(player_id, "MASTER", "I", 124, 100, 90)
    await db.save_matches([dict(soloq_row(i, False, "Yone"), puuid="kizi-puuid") for i in range(3)])


class TestAuthorProfiles:
    """Test cases for author_profiles."""

    async def test_roster_players_other_than_the_games_player(self):
        await db.init_db()
        await add_player()
        await add_kizi()

        text = await author_profiles({"Kizi", "khal", "random"}, ROSTER, "OpenYourMind#MAGIK", START + timedelta(hours=1))

        assert text == ("kizi: Top, Master 124 LP, sezon 100-90, SoloQ z 30 dni: 3 gry, 0-3 (0% WR), "
                        "teraz 3 porażki z rzędu, pula: Yone 3 gry 0% WR")

    async def test_description_without_a_tracked_account(self):
        await db.init_db()
        write_profiles("## kizi\nLuźny dzieciak, Akali i Gwen.\n")

        text = await author_profiles({"kizi"}, ROSTER, "OpenYourMind#MAGIK", START)

        assert text == "kizi: Top, opis od drużyny: Luźny dzieciak, Akali i Gwen."

    async def test_nobody_from_the_roster(self):
        await db.init_db()
        assert await author_profiles({"random"}, ROSTER, "OpenYourMind#MAGIK", START) is None

    def test_profiles_reach_the_model(self):
        messages = build_messages("KARTA", "WIEDZA", [chat_message("user", "xd", 7, "kizi")], OWNER_ID, "kizi: Top")

        assert "KTO JESZCZE PISZE (profile z bazy)\nkizi: Top" in messages[0]["content"]


@pytest.fixture
async def chat_cog(monkeypatch):
    await db.init_db()
    await add_player()
    lp_bot = bot_module.LPTrackerBot()
    monkeypatch.setattr(bot_module.team_sheet, "read_roster_cached", AsyncMock(return_value=ROSTER))
    monkeypatch.setattr(bot_module, "GOOGLE_SHEET_ID", "sheet")
    monkeypatch.setattr(roasts_cog, "find_member_id", AsyncMock(return_value=OWNER_ID))
    monkeypatch.setattr(roasts_cog, "ROASTS_ENABLED", True)
    monkeypatch.setattr(roasts_cog.llm, "enabled", lambda: True)
    monkeypatch.setattr(roasts_cog.llm, "chat", AsyncMock(return_value="„Kalista cię zjadła, kup control warda.”"))
    monkeypatch.setattr(roast_chat.riot_api, "get_match_details", AsyncMock(return_value=match_payload()))
    monkeypatch.setattr(roast_chat.riot_api, "get_match_timeline", AsyncMock(return_value=timeline_payload()))
    monkeypatch.setattr(roast_chat.champions, "refresh", AsyncMock())
    return roasts_cog.RoastsCog(lp_bot)


NOTIFICATION_ID, COMMENT_ID = 1000, 1001


async def start_chat(comment: bool = True) -> int:
    chat_id = await db.create_chat(NOTIFICATION_ID, MATCH_ID, PUUID)
    if comment:
        await db.add_chat_message(chat_id, COMMENT_ID, "comment", "3 porażki z rzędu, LP paruje.")
    return chat_id


def reply_to(reference_id: int, message_id: int, author_id: int = 7, name: str = "Kuba",
             content: str = "czemu przegrał?", channel_id: int = 1, bot: bool = False) -> SimpleNamespace:
    """A Discord message replying to reference_id; its reply gets message_id + 1."""
    return SimpleNamespace(
        id=message_id, author=SimpleNamespace(id=author_id, display_name=name, bot=bot),
        guild=SimpleNamespace(name="PYRY"), channel=SimpleNamespace(id=channel_id, typing=nullcontext),
        reference=SimpleNamespace(message_id=reference_id, channel_id=1), clean_content=content,
        reply=AsyncMock(return_value=SimpleNamespace(id=message_id + 1)), add_reaction=AsyncMock(),
    )


class TestConversation:
    """Test cases for answering replies in cogs/roasts.py."""

    async def test_reply_to_the_comment_gets_an_answer(self, chat_cog):
        chat_id = await start_chat()
        message = reply_to(COMMENT_ID, 2000)
        await chat_cog.on_message(message)

        message.reply.assert_awaited_once()
        assert message.reply.await_args.args[0] == "Kalista cię zjadła, kup control warda."
        assert message.reply.await_args.kwargs["allowed_mentions"].users is False
        messages = roasts_cog.llm.chat.await_args.args[0]
        assert "Rywal z linii: Kalista" in messages[0]["content"]
        assert "WIEDZA O GRACZU\nGracz: khal, rola w drużynie: ADC" in messages[0]["content"]
        assert "3 porażki z rzędu, LP paruje." in messages[0]["content"]
        assert messages[1:] == [{"role": "user", "content": "Kuba: czemu przegrał?"}]
        assert roasts_cog.llm.chat.await_args.kwargs["latin_only"] is True
        stored = await db.get_chat_messages(chat_id)
        assert [(m["message_id"], m["role"]) for m in stored] == [
            (NOTIFICATION_ID, "game"), (COMMENT_ID, "comment"), (2000, "user"), (2001, "assistant")]

    async def test_conversation_keeps_the_game_and_the_previous_messages(self, chat_cog):
        await start_chat()
        await chat_cog.on_message(reply_to(COMMENT_ID, 2000))
        # A reply to the bot's answer continues the same conversation
        await chat_cog.on_message(reply_to(2001, 2002, author_id=OWNER_ID, name="khal", content="sam jesteś feeder"))

        messages = roasts_cog.llm.chat.await_args.args[0]
        assert "Rywal z linii: Kalista" in messages[0]["content"]
        assert messages[1:] == [
            {"role": "user", "content": "Kuba: czemu przegrał?"},
            {"role": "assistant", "content": "Kalista cię zjadła, kup control warda."},
            {"role": "user", "content": "khal (grał tę grę): sam jesteś feeder"},
        ]
        # The game is fetched once, then kept with the conversation
        roast_chat.riot_api.get_match_details.assert_awaited_once()
        assert "Faza linii" in (await db.get_chat_by_bot_message(2003))["game_card"]

    async def test_teammate_gets_answered_with_their_stats(self, chat_cog):
        await add_kizi()
        await start_chat()
        await chat_cog.on_message(reply_to(COMMENT_ID, 2000, author_id=9, name="kizi", content="ale on intuje"))

        system = roasts_cog.llm.chat.await_args.args[0][0]["content"]
        assert "KTO JESZCZE PISZE (profile z bazy)\nkizi: Top, Master 124 LP" in system
        assert "Ashe z tej gry" not in system  # the game isn't in khal's stored games here

    async def test_reply_to_a_notification_without_a_comment(self, chat_cog):
        await start_chat(comment=False)
        message = reply_to(NOTIFICATION_ID, 2000)
        await chat_cog.on_message(message)

        message.reply.assert_awaited_once()
        assert "TWÓJ KOMENTARZ" not in roasts_cog.llm.chat.await_args.args[0][0]["content"]

    @pytest.mark.parametrize("message", [
        reply_to(999, 2000),  # not a message under a game
        reply_to(2000, 2002),  # someone's reply in the conversation, not the bot's message
        reply_to(COMMENT_ID, 2004, bot=True),
        reply_to(COMMENT_ID, 2006, channel_id=2),  # forwarded to another channel
        reply_to(COMMENT_ID, 2008, content=" "),
    ])
    async def test_other_messages_are_ignored(self, chat_cog, message):
        await start_chat()
        await chat_cog.on_message(reply_to(COMMENT_ID, 2000))
        roasts_cog.llm.chat.reset_mock()
        await chat_cog.on_message(message)

        roasts_cog.llm.chat.assert_not_awaited()

    async def test_without_the_model_nothing_happens(self, monkeypatch, chat_cog):
        monkeypatch.setattr(roasts_cog.llm, "enabled", lambda: False)
        await start_chat()
        message = reply_to(COMMENT_ID, 2000)
        await chat_cog.on_message(message)

        message.reply.assert_not_awaited()

    async def test_failed_request_stores_nothing(self, monkeypatch, chat_cog):
        monkeypatch.setattr(roasts_cog.llm, "chat", AsyncMock(return_value=None))
        chat_id = await start_chat()
        message = reply_to(COMMENT_ID, 2000)
        await chat_cog.on_message(message)

        message.reply.assert_not_awaited()
        assert len(await db.get_chat_messages(chat_id)) == 2

    async def test_opted_out_player_talks_only_with_themself(self, chat_cog):
        await db.set_roast_optout(OWNER_ID, True)
        await start_chat()
        other = reply_to(COMMENT_ID, 2000)
        owner = reply_to(COMMENT_ID, 2002, author_id=OWNER_ID, name="khal")
        await chat_cog.on_message(other)
        await chat_cog.on_message(owner)

        other.reply.assert_not_awaited()
        owner.reply.assert_awaited_once()

    async def test_player_not_in_roster(self, monkeypatch, chat_cog):
        monkeypatch.setattr(bot_module.team_sheet, "read_roster_cached", AsyncMock(return_value=[]))
        await start_chat()
        message = reply_to(COMMENT_ID, 2000)
        await chat_cog.on_message(message)

        message.reply.assert_not_awaited()

    async def test_daily_limit(self, monkeypatch, chat_cog):
        monkeypatch.setattr(roasts_cog, "CHAT_REPLIES_PER_DAY", 1)
        await start_chat()
        first, second = reply_to(COMMENT_ID, 2000), reply_to(2001, 2002)
        await chat_cog.on_message(first)
        await chat_cog.on_message(second)

        first.reply.assert_awaited_once()
        second.reply.assert_not_awaited()
        second.add_reaction.assert_awaited_once_with(roasts_cog.LIMIT_REACTION)


class TestCommentStartsTheConversation:
    """Test cases for the comment joining the notification's conversation (bot.py)."""

    async def test_comment_is_stored_with_the_notification(self, monkeypatch, chat_cog):
        monkeypatch.setattr(bot_module, "find_member_id", AsyncMock(return_value=OWNER_ID))
        monkeypatch.setattr(bot_module, "ROASTS_ENABLED", True)
        monkeypatch.setattr(bot_module.champions, "refresh", AsyncMock())
        monkeypatch.setattr(bot_module, "write_comment", AsyncMock(return_value="Trzecia porażka, gratulacje."))
        player = await db.get_player_by_puuid(PUUID)
        now_ms = int(time.time() * 1000) - 10 * 60 * 60 * 1000
        for i in range(3):
            data = match_payload()
            data["metadata"]["matchId"] = f"EUW1_{i}"
            data["info"]["gameStartTimestamp"] = now_ms + i * 60 * 60 * 1000
            stats = bot_module.riot_api.extract_player_match_stats(data, PUUID)
            message = SimpleNamespace(id=1000 + 10 * i, guild=SimpleNamespace(name="PYRY"),
                                      reply=AsyncMock(return_value=SimpleNamespace(id=1001 + 10 * i)))
            chat_id = await db.create_chat(message.id, f"EUW1_{i}", PUUID)
            await chat_cog.bot.comment_on_game(message, player, data, stats, {PUUID})

        assert [(m["message_id"], m["role"], m["content"]) for m in await db.get_chat_messages(chat_id)] == [
            (1020, "game", ""), (1021, "comment", "Trzecia porażka, gratulacje.")]
