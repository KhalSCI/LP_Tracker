"""Comments on SoloQ games for the match history channel: streaks, int games, off-pool picks."""
from collections import defaultdict
from datetime import datetime, timedelta

import llm
import player_profiles
from champions import champions
from cogs.sheet import champion_display_name, SOLOQ_QUEUE
from config import TIMEZONE

# Streak lengths that get a comment
STREAK_MILESTONES = (3, 5, 7, 10, 15, 20)
# A streak at least this long gets a comment when it ends
STREAK_BREAK = 4
INT_DEATHS = 10
# Losses started between midnight and NIGHT_END_HOUR; the NIGHT_LOSSES-th gets a comment
NIGHT_END_HOUR = 6
NIGHT_LOSSES = 3
# "Off-pool" loss: the champion has at most OFF_POOL_GAMES other games in the history,
# while a comfort pick has at least COMFORT_GAMES games at COMFORT_WR or better
OFF_POOL_GAMES = 2
COMFORT_GAMES = 8
COMFORT_WR = 0.6
# Games in the history shown to the model
HISTORY_FOR_PROMPT = 10
# Match-v5 teamPosition -> role name
POSITIONS = {"TOP": "Top", "JUNGLE": "Jungle", "MIDDLE": "Mid", "BOTTOM": "ADC", "UTILITY": "Support"}
# A support's farm doesn't matter, so it isn't shown to the model
SUPPORT = "UTILITY"

SYSTEM_PROMPT = """Jesteś złośliwym, ale lubianym kumplem z uczelnianej drużyny League of Legends i piszesz na Discordzie drużyny.
Dostajesz dane o rankedowej grze kolegi i powód komentarza. Napisz krótki komentarz po polsku: 1-3 zdania, najwyżej 300 znaków.
Styl: luźny, ironiczny, lekko toksyczny jak na czacie znajomych, gamingowy slang (int, feed, tilt, diff, gap). Pasują teksty w stylu „mój stary by lepiej zagrał tym Aatroxem, zostań przy swojej Camille”.
Opieraj się wyłącznie na podanych danych, nie wymyślaj statystyk ani zdarzeń. Opis od drużyny to wasze wewnętrzne żarty i ksywki: możesz je czasem wpleść, ale nie cytuj go w całości. Przy złej passie daj konkretną radę z danych: champion z jego puli z dobrym WR, przerwa, sen. Farma supporta się nie liczy, nie komentuj jej. Przy dobrej passie ironiczna pochwała.
Lekkie przekleństwo może być, ale bez wyzwisk i bez żartów z wyglądu, pochodzenia, zdrowia czy orientacji. Nie zaczynaj od imienia ani pingu gracza (bot doda ping), najwyżej jedno emoji, bez cudzysłowu wokół całości."""


def plural(n: int, one: str, few: str, many: str) -> str:
    """Polish noun form for a count: 1 gra, 2 gry, 5 gier, 22 gry, 12 gier."""
    if n == 1:
        return f"{n} {one}"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} {few}"
    return f"{n} {many}"


def games_count(n: int) -> str:
    return plural(n, "gra", "gry", "gier")


def is_soloq_game(game: dict) -> bool:
    return game["queue_id"] == SOLOQ_QUEUE and not game["remake"]


def streak(history: list[dict]) -> tuple[bool, int]:
    """(won, length) of the streak ending with the newest game. History is newest first."""
    if not history:
        return False, 0
    won = bool(history[0]["win"])
    length = 0
    for game in history:
        if bool(game["win"]) != won:
            break
        length += 1
    return won, length


def champion_pool(history: list[dict]) -> list[dict]:
    """Champions in the history, most played first, as {"champion", "games", "wins", "wr"}."""
    played = defaultdict(lambda: [0, 0])
    for game in history:
        played[game["champion_name"]][0] += 1
        played[game["champion_name"]][1] += bool(game["win"])
    pool = [
        {"champion": champion_display_name(name), "key": name, "games": games, "wins": wins, "wr": wins / games}
        for name, (games, wins) in played.items()
    ]
    return sorted(pool, key=lambda c: (-c["games"], -c["wr"], c["champion"]))


def comfort_pick(pool: list[dict], exclude: str = None) -> dict | None:
    """The best-winrate champion with enough games, other than exclude (a match-v5 key)."""
    candidates = [c for c in pool if c["games"] >= COMFORT_GAMES and c["wr"] >= COMFORT_WR and c["key"] != exclude]
    return max(candidates, key=lambda c: (c["wr"], c["games"]), default=None)


def local_start(game: dict) -> datetime:
    return datetime.fromtimestamp(game["game_start"] / 1000, TIMEZONE)


def detect_trigger(stats: dict, history: list[dict]) -> dict | None:
    """
    What's worth a comment about the newest game, highest priority first, as
    {"kind", "reason"} (reason in Polish, for the model). stats: the game's player stats
    (extract_player_match_stats plus "penta_kills", "quadra_kills"); history: the
    player's SoloQ games without remakes, newest first, starting with this game.
    """
    if stats.get("remake") or not history:
        return None
    won, length = streak(history)
    kills, deaths, assists = stats["kills"], stats["deaths"], stats["assists"]
    kda = (kills + assists) / max(deaths, 1)

    if length in STREAK_MILESTONES:
        if won:
            return {"kind": "win_streak", "reason": f"{plural(length, 'wygrana', 'wygrane', 'wygranych')} z rzędu"}
        return {"kind": "lose_streak", "reason": f"{plural(length, 'porażka', 'porażki', 'porażek')} z rzędu"}

    if stats.get("penta_kills"):
        return {"kind": "highlight", "reason": "pentakill"}
    if stats.get("quadra_kills"):
        return {"kind": "highlight", "reason": "quadrakill"}
    if kda >= 8 and kills + assists >= 15:
        return {"kind": "highlight", "reason": f"gra życia: {kills}/{deaths}/{assists}"}

    if deaths >= INT_DEATHS and kda < 1:
        return {"kind": "int", "reason": f"int: {kills}/{deaths}/{assists}"}

    if not won:
        older = history[1:]
        pool = champion_pool(older)
        played_before = next((c["games"] for c in pool if c["key"] == stats["champion_name"]), 0)
        comfort = comfort_pick(pool, exclude=stats["champion_name"])
        if played_before <= OFF_POOL_GAMES and comfort:
            return {
                "kind": "off_pool",
                "reason": (
                    f"przegrana na championie spoza puli ({champion_display_name(stats['champion_name'])}, "
                    f"wcześniej {games_count(played_before)}), a {comfort['champion']} ma {games_count(comfort['games'])} "
                    f"i {round(comfort['wr'] * 100)}% WR"
                ),
            }

    previous_won, previous_length = streak(history[1:])
    if previous_length >= STREAK_BREAK and previous_won != won:
        if won:
            return {"kind": "streak_end", "reason": f"wreszcie wygrana po {plural(previous_length, 'porażce', 'porażkach', 'porażkach')} z rzędu"}
        return {"kind": "streak_end", "reason": f"koniec serii {previous_length} wygranych"}

    start = local_start(history[0])
    if not won and start.hour < NIGHT_END_HOUR:
        midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
        night_losses = [g for g in history if not g["win"] and midnight <= local_start(g) < midnight + timedelta(hours=NIGHT_END_HOUR)]
        if len(night_losses) == NIGHT_LOSSES:
            return {"kind": "night", "reason": f"{NIGHT_LOSSES}. przegrana tej nocy, jest {start:%H:%M}"}
    return None


def lane_opponent(match_data: dict, puuid: str) -> str | None:
    """Champion of the enemy player in the same position."""
    participants = match_data.get("info", {}).get("participants", [])
    me = next((p for p in participants if p.get("puuid") == puuid), None)
    if not me or not me.get("teamPosition"):
        return None
    for p in participants:
        if p.get("teamId") != me.get("teamId") and p.get("teamPosition") == me["teamPosition"]:
            return champions.name(p.get("championId"), p.get("championName"))
    return None


def multikills(match_data: dict, puuid: str) -> dict:
    participant = next((p for p in match_data.get("info", {}).get("participants", []) if p.get("puuid") == puuid), {})
    return {"penta_kills": participant.get("pentaKills", 0), "quadra_kills": participant.get("quadraKills", 0),
            "position": participant.get("teamPosition") or ""}


def format_game(game: dict) -> str:
    return (f"{'W' if game['win'] else 'L'} {champion_display_name(game['champion_name'])} "
            f"{game['kills']}/{game['deaths']}/{game['assists']}")


def build_prompt(nick: str, player: dict, stats: dict, trigger: dict, history: list[dict], opponent: str | None) -> str:
    """The facts the model gets about the game and the player."""
    pool = champion_pool(history)
    rank = f"{player['tier'].title()} {player['rank']} {player['lp']} LP" if player.get("tier") else "bez rangi"
    description = player_profiles.profile(nick)
    lines = [
        f"Gracz: {nick} ({rank})" + (f"\nOpis od drużyny: {description}" if description else ""),
        f"Powód komentarza: {trigger['reason']}",
        (
            f"Ta gra: {'wygrana' if stats['win'] else 'przegrana'} na {champion_display_name(stats['champion_name'])}"
            f"{' (' + POSITIONS.get(stats['position'], stats['position']) + ')' if stats.get('position') else ''}, "
            f"KDA {stats['kills']}/{stats['deaths']}/{stats['assists']}, "
            + (f"{stats['cs_per_min']} CS/min, " if stats.get("position") != SUPPORT else "")
            + f"{stats['game_duration_minutes']} min"
            + (f", przeciwnik na linii: {opponent}" if opponent else "")
        ),
        "Ostatnie gry (od najnowszej): " + ", ".join(format_game(g) for g in history[:HISTORY_FOR_PROMPT]),
        "Pula championów w SoloQ z 30 dni: " + ", ".join(
            f"{c['champion']} {games_count(c['games'])} {round(c['wr'] * 100)}% WR" for c in pool[:6]
        ),
    ]
    return "\n".join(lines)


def template_text(trigger: dict, stats: dict, history: list[dict]) -> str:
    """A comment without the model (no API key or a failed request)."""
    champion = champion_display_name(stats["champion_name"])
    comfort = comfort_pick(champion_pool(history), exclude=stats["champion_name"])
    advice = f" Może wróć na {comfort['champion']} ({round(comfort['wr'] * 100)}% WR)?" if comfort else ""
    kind = trigger["kind"]
    if kind == "lose_streak":
        return f"{trigger['reason'].capitalize()}. LP paruje szybciej niż woda z czajnika.{advice}"
    if kind == "win_streak":
        return f"{trigger['reason'].capitalize()}. Nawet ślepej kurze trafia się ziarno, ale niech będzie, szacun."
    if kind == "highlight":
        return f"{trigger['reason'].capitalize()} na {champion}. Zrzut ekranu i do CV."
    if kind == "int":
        return f"{trigger['reason'].capitalize()} na {champion}. Przeciwnicy dziękują za darmowe golda.{advice}"
    if kind == "off_pool":
        return f"Mój stary by to lepiej zagrał, {champion} to nie twoja bajka.{advice}"
    if kind == "streak_end":
        return f"{trigger['reason'].capitalize()}. Notujemy w kronikach."
    if kind == "night":
        return f"{trigger['reason'].capitalize()}. Idź spać, ranked jutro też będzie."
    return trigger["reason"]


async def write_comment(nick: str, player: dict, stats: dict, trigger: dict, history: list[dict], opponent: str | None) -> str:
    """The comment from the model, or from a template when the model isn't available."""
    text = await llm.complete(SYSTEM_PROMPT, build_prompt(nick, player, stats, trigger, history, opponent),
                              temperature=1.0, latin_only=True)
    if text:
        return text.strip().strip('"„”')[:600]
    return template_text(trigger, stats, history)
