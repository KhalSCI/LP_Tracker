"""
Full data of team games (scrims, officials): the raw Riot API / LoL client JSON with its
timeline is stored as is, and per-player stats for both teams are computed from it.
"""
import json
from collections import Counter
from pathlib import Path

import database as db
from champions import champions
from riot_api import riot_api, RiotAPIError

UPLOADS_DIR = Path(__file__).parent / "uploads"
ROLES = ["Top", "Jungle", "Mid", "ADC", "Support"]
MINUTE_MS = 60 * 1000
EARLY_DEATHS_MINUTES = 14
# Minutes used to tell lanes apart from positions, and jungle CS that marks a jungler
LANE_MINUTES = range(3, 11)
JUNGLER_CAMPS_AT_10 = 20
# Summoner's Rift is about 15000 x 15000, blue base bottom-left; lanes run along the edges and the diagonal
LANE_EDGE = 3500
MID_WIDTH = 2000


def lane_at(x: float, y: float) -> str | None:
    """Which lane a map position is on: "Top", "Mid", "Bot", or None (jungle, river, base)."""
    if (x < LANE_EDGE and y > LANE_EDGE) or (y > 15000 - LANE_EDGE and x < 15000 - LANE_EDGE):
        return "Top"
    if (y < LANE_EDGE and x > LANE_EDGE) or (x > 15000 - LANE_EDGE and y < 15000 - LANE_EDGE):
        return "Bot"
    if abs(x - y) < MID_WIDTH and 3000 < x < 12000:
        return "Mid"
    return None


def timeline_frames(timeline: dict | None) -> list[dict]:
    """Frames of a Riot API (info.frames) or LoL client (frames) timeline."""
    if not timeline:
        return []
    return (timeline.get("info") or timeline).get("frames") or []


def frame_at(frames: list[dict], minute: int) -> dict | None:
    """The frame closest to a minute, if the game lasted that long."""
    target = minute * MINUTE_MS
    if not frames or frames[-1].get("timestamp", 0) < target - MINUTE_MS // 2:
        return None
    return min(frames, key=lambda f: abs(f.get("timestamp", 0) - target))


def participant_frame(frame: dict | None, participant_id: int) -> dict | None:
    if not frame:
        return None
    frames = frame.get("participantFrames") or {}
    return frames.get(str(participant_id)) or frames.get(participant_id)


def frame_cs(pf: dict) -> int:
    return pf.get("minionsKilled", 0) + pf.get("jungleMinionsKilled", 0)


def infer_roles(players: list[dict], frames: list[dict]) -> None:
    """
    Fill in missing "role"s of one team from the timeline: the most jungle CS at 10 minutes
    is the jungler, others go by the lane they stood in, and of two bot laners the one with
    more minions is the ADC. A role is only given when exactly one player fits it. Without
    a timeline, only a single player missing a role gets the single free one.
    """
    missing = [p for p in players if not p.get("role")]
    free = [role for role in ROLES if role not in {p.get("role") for p in players}]
    if len(missing) == 1 and len(free) == 1:
        missing[0]["role"] = free[0]
        return
    if not missing or not frames:
        return

    at_10 = frame_at(frames, 10)
    if "Jungle" in free and at_10:
        by_camps = sorted(
            missing,
            key=lambda p: -(participant_frame(at_10, p["participant_id"]) or {}).get("jungleMinionsKilled", 0),
        )
        camps = [(participant_frame(at_10, p["participant_id"]) or {}).get("jungleMinionsKilled", 0) for p in by_camps]
        if camps[0] >= JUNGLER_CAMPS_AT_10 and (len(camps) == 1 or camps[1] < camps[0]):
            by_camps[0]["role"] = "Jungle"
            free.remove("Jungle")
            missing.remove(by_camps[0])

    lanes = {}
    for player in missing:
        seen = Counter()
        for minute in LANE_MINUTES:
            pf = participant_frame(frame_at(frames, minute), player["participant_id"])
            position = (pf or {}).get("position")
            if position:
                lane = lane_at(position.get("x", 0), position.get("y", 0))
                if lane:
                    seen[lane] += 1
        lanes[player["participant_id"]] = seen.most_common(1)[0][0] if seen else None

    for lane, role in (("Top", "Top"), ("Mid", "Mid")):
        on_lane = [p for p in missing if lanes[p["participant_id"]] == lane]
        if role in free and len(on_lane) == 1:
            on_lane[0]["role"] = role
            free.remove(role)
    bot = [p for p in missing if lanes[p["participant_id"]] == "Bot"]
    if len(bot) == 2 and "ADC" in free and "Support" in free and at_10:
        adc, support = sorted(bot, key=lambda p: -(participant_frame(at_10, p["participant_id"]) or {}).get("minionsKilled", 0))
        adc["role"], support["role"] = "ADC", "Support"
    elif len(bot) == 1 and len([r for r in ("ADC", "Support") if r in free]) == 1:
        bot[0]["role"] = "ADC" if "ADC" in free else "Support"


def deaths_before(timeline: dict | None, participant_id: int, minutes: int) -> int | None:
    """Times the participant died in the first minutes, from the timeline's kill events."""
    frames = timeline_frames(timeline)
    if not frames:
        return None
    return sum(
        1
        for frame in frames
        for event in frame.get("events") or []
        if event.get("type") == "CHAMPION_KILL" and event.get("victimId") == participant_id
        and event.get("timestamp", 0) < minutes * MINUTE_MS
    )


def api_participants(match_data: dict) -> list[dict]:
    """Common per-player fields from a Riot API match-v5 payload."""
    rows = []
    for p in match_data.get("info", {}).get("participants", []):
        rows.append({
            "participant_id": p.get("participantId"),
            "team_id": p.get("teamId"),
            "riot_id": f"{p.get('riotIdGameName')}#{p.get('riotIdTagline')}",
            "champion": champions.name(p.get("championId"), p.get("championName")),
            "win": bool(p.get("win")),
            "kills": p.get("kills", 0),
            "deaths": p.get("deaths", 0),
            "assists": p.get("assists", 0),
            "cs": p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0),
            "gold": p.get("goldEarned", 0),
            "damage": p.get("totalDamageDealtToChampions", 0),
            "damage_taken": p.get("totalDamageTaken", 0),
            "vision_score": p.get("visionScore", 0),
            "wards_placed": p.get("wardsPlaced", 0),
            "wards_killed": p.get("wardsKilled", 0),
            "control_wards": p.get("visionWardsBoughtInGame", 0),
            "first_blood": bool(p.get("firstBloodKill") or p.get("firstBloodAssist")),
        })
    return rows


def client_participants(game: dict) -> list[dict]:
    """Common per-player fields from a LoL client match-history game."""
    identities = {i.get("participantId"): i.get("player") or {} for i in game.get("participantIdentities", [])}
    rows = []
    for p in game.get("participants", []):
        player = identities.get(p.get("participantId"), {})
        name, tag = player.get("gameName"), player.get("tagLine")
        stats = p.get("stats") or {}
        rows.append({
            "participant_id": p.get("participantId"),
            "team_id": p.get("teamId"),
            "riot_id": f"{name}#{tag}" if name and tag else player.get("summonerName", ""),
            "champion": champions.name(p.get("championId")),
            "win": bool(stats.get("win")),
            "kills": stats.get("kills", 0),
            "deaths": stats.get("deaths", 0),
            "assists": stats.get("assists", 0),
            "cs": stats.get("totalMinionsKilled", 0) + stats.get("neutralMinionsKilled", 0),
            "gold": stats.get("goldEarned", 0),
            "damage": stats.get("totalDamageDealtToChampions", 0),
            "damage_taken": stats.get("totalDamageTaken", 0),
            "vision_score": stats.get("visionScore", 0),
            "wards_placed": stats.get("wardsPlaced", 0),
            "wards_killed": stats.get("wardsKilled", 0),
            "control_wards": stats.get("visionWardsBoughtInGame", 0),
            "first_blood": bool(stats.get("firstBloodKill") or stats.get("firstBloodAssist")),
        })
    return rows


def patch(version: str | None) -> str:
    """"16.21.712.1234" -> "16.21"."""
    return ".".join((version or "").split(".")[:2])


def compute_stats(team_game: dict, source: str, raw: dict, timeline: dict | None) -> list[dict]:
    """
    Per-player stats of both teams, from the raw game and the normalized team game
    (team_games.build_team_game), which says who is ours, their nicks and roles.
    """
    if source == "api":
        participants = api_participants(raw)
        info = raw.get("info", {})
        version, duration_s = info.get("gameVersion"), info.get("gameDuration") or team_game["duration_s"]
    else:
        participants = client_participants(raw)
        version, duration_s = raw.get("gameVersion"), raw.get("gameDuration") or team_game["duration_s"]
    if len(participants) != 10:
        return []

    by_riot_id = {}
    for side, players in (("ours", team_game["ours"]), ("enemy", team_game["enemy"])):
        for player in players:
            by_riot_id[player["riot_id"].lower()] = (side, player)
    our_team = next((p["team_id"] for p in participants if by_riot_id.get(p["riot_id"].lower(), ("",))[0] == "ours"), None)
    for p in participants:
        side, known = by_riot_id.get(p["riot_id"].lower(), (None, {}))
        p["ours"] = p["team_id"] == our_team
        p["player"] = known.get("player") or p["riot_id"]
        p["role"] = known.get("role") or ""

    frames = timeline_frames(timeline)
    teams = {}
    for p in participants:
        teams.setdefault(p["team_id"], []).append(p)
    for players in teams.values():
        infer_roles(players, frames)

    minutes = max(duration_s / 60, 1)
    rows = []
    for p in participants:
        team = teams[p["team_id"]]
        team_kills = sum(t["kills"] for t in team)
        team_gold = sum(t["gold"] for t in team)
        team_damage = sum(t["damage"] for t in team)
        opponent = next((o for o in participants if o["team_id"] != p["team_id"] and p["role"] and o["role"] == p["role"]), None)

        diffs = {}
        for minute in (10, 15):
            frame = frame_at(frames, minute)
            mine = participant_frame(frame, p["participant_id"])
            theirs = participant_frame(frame, opponent["participant_id"]) if opponent else None
            if mine and theirs:
                diffs[minute] = {
                    "gold": mine.get("totalGold", 0) - theirs.get("totalGold", 0),
                    "cs": frame_cs(mine) - frame_cs(theirs),
                    "xp": mine.get("xp", 0) - theirs.get("xp", 0),
                }

        rows.append({
            "match_id": team_game["match_id"],
            "participant_id": p["participant_id"],
            "start_ms": team_game["start_ms"],
            "duration_s": duration_s,
            "patch": patch(version),
            "tournament": team_game["tournament"],
            "ours": p["ours"],
            "team_id": p["team_id"],
            "side": "Blue" if p["team_id"] == 100 else "Red",
            "player": p["player"],
            "riot_id": p["riot_id"],
            "role": p["role"],
            "champion": p["champion"],
            "win": p["win"],
            "kills": p["kills"],
            "deaths": p["deaths"],
            "assists": p["assists"],
            "kill_participation": round((p["kills"] + p["assists"]) / team_kills, 3) if team_kills else None,
            "cs": p["cs"],
            "cs_per_min": round(p["cs"] / minutes, 2),
            "gold": p["gold"],
            "gold_share": round(p["gold"] / team_gold, 3) if team_gold else None,
            "damage": p["damage"],
            "damage_share": round(p["damage"] / team_damage, 3) if team_damage else None,
            "damage_taken": p["damage_taken"],
            "vision_score": p["vision_score"],
            "wards_placed": p["wards_placed"],
            "wards_killed": p["wards_killed"],
            "control_wards": p["control_wards"],
            "first_blood": p["first_blood"],
            "deaths_before_14": deaths_before(timeline, p["participant_id"], EARLY_DEATHS_MINUTES),
            "gold_diff_10": diffs.get(10, {}).get("gold"),
            "gold_diff_15": diffs.get(15, {}).get("gold"),
            "cs_diff_10": diffs.get(10, {}).get("cs"),
            "cs_diff_15": diffs.get(15, {}).get("cs"),
            "xp_diff_15": diffs.get(15, {}).get("xp"),
            "lane_opponent": opponent["champion"] if opponent else None,
        })
    return rows


async def store_game_data(team_game: dict, source: str, raw: dict, timeline: dict | None) -> None:
    """Store a team game's raw data and its per-player stats."""
    await db.save_team_game_raw(team_game["match_id"], source, raw, timeline)
    stored = await db.get_team_game_raw(team_game["match_id"])
    await db.save_team_game_stats(team_game["match_id"], compute_stats(team_game, source, stored["game"], stored["timeline"]))


async def store_new_game(team_game: dict, source: str, raw: dict, timeline: dict | None) -> None:
    """Store a just-added team game's full data, fetching the timeline of API games."""
    try:
        if source == "api" and timeline is None:
            timeline = await fetch_api_timeline(team_game["match_id"])
        await store_game_data(team_game, source, raw, timeline)
    except Exception as e:
        print(f"Could not store full data of {team_game['match_id']}: {e}")


async def add_client_timeline(match_id: str, raw: dict, timeline: dict | None) -> bool:
    """
    Store the timeline of a re-sent client game that was added before timelines were sent.
    Returns True if it was added.
    """
    if not timeline:
        return False
    stored = await db.get_team_game_raw(match_id)
    if stored and stored["timeline"]:
        return False
    team_game = next((g for g in await db.get_team_games() if g["match_id"] == match_id), None)
    if team_game is None or team_game["source"] != "client":
        return False
    await store_game_data(team_game, "client", raw, timeline)
    return True


async def fetch_api_timeline(match_id: str) -> dict | None:
    try:
        return await riot_api.get_match_timeline(match_id)
    except RiotAPIError as e:
        print(f"No timeline for {match_id}: {e}")
        return None


def find_uploaded_client_game(match_id: str) -> tuple[dict, dict | None] | None:
    """A client game (and its timeline) from the raw uploads kept in uploads/."""
    game_id = match_id.split("_", 1)[-1]
    for path in sorted(UPLOADS_DIR.glob("*.json"), reverse=True):
        try:
            payload = json.loads(path.read_bytes())
        except (OSError, ValueError):
            continue
        for game in payload.get("games", []):
            if str(game.get("gameId")) == game_id:
                return game, (payload.get("timelines") or {}).get(game_id)
    return None


async def backfill_game_data() -> int:
    """
    Store full data of team games added before it was kept: client games from the saved
    uploads, API games from the Riot API. Returns how many games were filled in.
    """
    stored = await db.get_team_game_raw_ids()
    missing = [g for g in await db.get_team_games() if g["match_id"] not in stored]
    if not missing:
        return 0
    await champions.refresh()
    filled = 0
    for game in missing:
        try:
            if game["source"] == "api":
                raw = await riot_api.get_match_details(game["match_id"])
                await store_game_data(game, "api", raw, await fetch_api_timeline(game["match_id"]))
            else:
                found = find_uploaded_client_game(game["match_id"])
                if found is None:
                    continue
                await store_game_data(game, "client", *found)
            filled += 1
        except Exception as e:
            print(f"Could not store full data of {game['match_id']}: {e}")
    return filled
