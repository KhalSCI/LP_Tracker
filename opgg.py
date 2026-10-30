"""
Champion matchups from OP.GG's public MCP API (Emerald+ data): a champion's games and wins
against every opponent in a position, from one lane matchup guide request per champion.
"""
import asyncio
import json
import re
import time

import aiohttp

API_URL = "https://mcp-api.op.gg/mcp"
POSITIONS = {"Top": "top", "Jungle": "jungle", "Mid": "mid", "ADC": "adc", "Support": "support"}
CACHE_SECONDS = 24 * 60 * 60
REQUEST_DELAY = 0.3
# The guide needs some opponent; its list of matchups is the same whichever it is
ANY_OPPONENT, OTHER_OPPONENT = "Ahri", "Lux"

_cache: dict[tuple[str, str], tuple[float, dict | None]] = {}


def opgg_name(champion: str) -> str:
    """OP.GG's champion name: "Kai'Sa" -> "KAISA", "Lee Sin" -> "LEE_SIN", "Nunu & Willump" -> "NUNU_WILLUMP"."""
    return "_".join(re.sub(r"['.&]", "", champion.upper()).split())


def parse_matchups(response: str) -> dict[str, tuple[int, int]] | None:
    """{opponent: (games, wins)} from a lane matchup guide response; None for an error."""
    match = re.search(r"\{.*\}", response, re.S)
    if not match:
        return None
    envelope = json.loads(match.group(0))
    content = (envelope.get("result") or {}).get("content") or []
    if "error" in envelope or not content:
        return None
    data = json.loads(content[0]["text"]).get("data") or {}
    return {c["champion_name"]: (c["play"], c["win"]) for c in data.get("counters") or []}


async def fetch_matchups(champion: str, role: str, session: aiohttp.ClientSession) -> dict[str, tuple[int, int]] | None:
    """A champion's games and wins against each opponent in a role (cached for a day); None when unavailable."""
    key = (champion, role)
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]
    opponent = OTHER_OPPONENT if champion == ANY_OPPONENT else ANY_OPPONENT
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "lol_get_lane_matchup_guide", "arguments": {
            "position": POSITIONS[role], "my_champion": opgg_name(champion), "opponent_champion": opgg_name(opponent),
        }},
    }
    try:
        await asyncio.sleep(REQUEST_DELAY)
        async with session.post(API_URL, json=body, headers={"Accept": "application/json, text/event-stream"}) as response:
            matchups = parse_matchups(await response.text())
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, IndexError, TypeError) as e:
        print(f"OP.GG matchups of {champion} ({role}) failed: {e}")
        return None
    if matchups is None:
        print(f"OP.GG has no matchups of {champion} ({role})")
    _cache[key] = (time.time(), matchups)
    return matchups


async def fetch_many(pairs: set[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """Matchups of several (champion, role) pairs, one after another; pairs without data are left out."""
    tables = {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        for champion, role in sorted(pairs):
            matchups = await fetch_matchups(champion, role, session)
            if matchups:
                tables[(champion, role)] = matchups
    return tables


def win_rate(tables: dict, champion: str, opponent: str, role: str) -> tuple[float, int] | None:
    """
    champion's win rate against opponent in role and the number of games, from either
    champion's table (the opponent's one read from the other side); None without data.
    """
    mine = tables.get((champion, role), {}).get(opponent)
    if mine and mine[0]:
        return mine[1] / mine[0], mine[0]
    theirs = tables.get((opponent, role), {}).get(champion)
    if theirs and theirs[0]:
        return 1 - theirs[1] / theirs[0], theirs[0]
    return None
