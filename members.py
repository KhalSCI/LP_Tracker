"""Discord members of the team server, found by the nick written in the roster sheet."""
import asyncio

import discord

import database as db


async def find_member_id(guild: discord.Guild, nick: str) -> int | None:
    """
    The Discord user ID for a roster nick (username, display name or server nick), cached
    in the database. Searching by name works without the privileged members intent.
    """
    if not nick:
        return None
    cached = await db.get_discord_user_id(nick)
    if cached:
        return cached

    try:
        members = await guild.query_members(query=nick, limit=20)
    except (discord.ClientException, asyncio.TimeoutError) as e:
        print(f"Could not search Discord members for '{nick}': {e}")
        return await db.get_discord_user_id(nick, max_age_days=365)

    key = nick.lower()
    for member in members:
        names = {member.name, member.global_name, member.nick, member.display_name}
        if key in {name.lower() for name in names if name}:
            await db.set_discord_user_id(nick, member.id)
            return member.id
    print(f"No Discord member in {guild.name} named '{nick}'")
    return None


def mention(user_id: int | None, nick: str) -> str:
    """A ping for the user, or the nick in bold when the user wasn't found."""
    return f"<@{user_id}>" if user_id else f"**{discord.utils.escape_markdown(nick)}**"
