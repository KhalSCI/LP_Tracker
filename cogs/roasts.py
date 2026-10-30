import asyncio
from collections import defaultdict
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import database as db
import llm
import roast_chat
from config import TIMEZONE, ROASTS_ENABLED, CHAT_REPLIES_PER_DAY
from members import find_member_id

# Reaction instead of an answer once someone used up CHAT_REPLIES_PER_DAY
LIMIT_REACTION = "🤐"


class RoastsCog(commands.Cog):
    """Cog for the comments on SoloQ games: turning them on and off, and conversations under games."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # One answer at a time per conversation, so each one sees the previous exchange
        self.chat_locks = defaultdict(asyncio.Lock)

    roast_group = app_commands.Group(name="roast", description="Komentarze bota po twoich grach SoloQ")

    @roast_group.command(name="off", description="Wyłącz komentarze bota po twoich grach SoloQ")
    async def roast_off(self, interaction: discord.Interaction):
        await db.set_roast_optout(interaction.user.id, True)
        await interaction.response.send_message("🔕 Ok, bez komentarzy po twoich grach. Włączysz je z powrotem przez `/roast on`.", ephemeral=True)

    @roast_group.command(name="on", description="Włącz komentarze bota po twoich grach SoloQ")
    async def roast_on(self, interaction: discord.Interaction):
        await db.set_roast_optout(interaction.user.id, False)
        await interaction.response.send_message("🔔 Komentarze po twoich grach włączone. Sam chciałeś.", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Answer a reply to the bot's message under a SoloQ game (the notification, the comment or an answer)."""
        reference = message.reference
        if (message.author.bot or not message.guild or reference is None or reference.message_id is None
                or reference.channel_id != message.channel.id or not ROASTS_ENABLED or not llm.enabled()):
            return
        chat = await db.get_chat_by_bot_message(reference.message_id)
        if chat is None or not message.clean_content.strip():
            return
        try:
            await self.answer(message, chat)
        except Exception as e:
            print(f"Error answering in the conversation about {chat['match_id']}: {e}")

    async def answer(self, message: discord.Message, chat: dict) -> None:
        # Only games of roster players, like the comments
        player = await db.get_player_by_puuid(chat["puuid"])
        entry = await self.bot.roster_entry(player) if player else None
        if entry is None:
            return
        nick = entry["nick"]
        owner_id = await find_member_id(message.guild, nick)
        # After /roast off only the player themself can talk about their games
        if owner_id and message.author.id != owner_id and await db.is_roast_optout(owner_id):
            return
        midnight = datetime.now(TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0)
        if await db.count_chat_replies_since(message.author.id, midnight.astimezone(timezone.utc)) >= CHAT_REPLIES_PER_DAY:
            await message.add_reaction(LIMIT_REACTION)
            return

        async with self.chat_locks[chat["id"]], message.channel.typing():
            game_card = chat["game_card"]
            if not game_card:
                game_card = await roast_chat.load_game_card(chat["match_id"], chat["puuid"], nick)
                await db.set_chat_game_card(chat["id"], game_card)
            now = datetime.now(TIMEZONE)
            knowledge = await roast_chat.player_knowledge(player, nick, entry.get("role"), now, chat["match_id"])
            reply = {
                "role": "user",
                "author_id": message.author.id,
                "author": await db.get_discord_nick(message.author.id) or message.author.display_name,
                "content": message.clean_content[:roast_chat.MAX_MESSAGE_CHARS],
            }
            history = await db.get_chat_messages(chat["id"]) + [reply]
            # Teammates writing in the conversation, so the bot can answer them with their own stats
            authors = {m["author"] for m in roast_chat.recent_messages(history) if m["author_id"] != owner_id}
            profiles = await roast_chat.author_profiles(authors, await self.bot.roster(), player["riot_id"], now)
            messages = roast_chat.build_messages(game_card, knowledge, history, owner_id, profiles)
            text = await llm.chat(messages, max_tokens=1500, latin_only=True)
            if not text:
                return
            text = roast_chat.clean_answer(text)
            answer = await message.reply(text, mention_author=True, allowed_mentions=discord.AllowedMentions(
                everyone=False, users=False, roles=False, replied_user=True
            ))
            await db.add_chat_message(chat["id"], message.id, **reply)
            await db.add_chat_message(chat["id"], answer.id, "assistant", text)
        print(f"Answered {reply['author']} about {nick}'s game {chat['match_id']}")


async def setup(bot: commands.Bot):
    await bot.add_cog(RoastsCog(bot))
