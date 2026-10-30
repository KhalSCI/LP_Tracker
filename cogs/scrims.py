import json
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands

from config import SHEET_GUILD_ID
from team_games import ingest_client_upload, format_summary
from stats_tab import update_stats_tab

# File name the Windows script uploads through a Discord webhook
UPLOAD_FILENAME = "lp-tracker-scrims.json"
# Raw uploads are kept here for troubleshooting
UPLOADS_DIR = Path(__file__).parent.parent / "uploads"


class ScrimsCog(commands.Cog):
    """Cog for scrim uploads from the Windows script."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Add games from a script upload: a webhook message in the team server with the upload file."""
        if message.webhook_id is None or not message.guild or message.guild.id != SHEET_GUILD_ID:
            return
        attachment = next((a for a in message.attachments if a.filename == UPLOAD_FILENAME), None)
        if attachment is None:
            return

        summary = None
        try:
            raw = await attachment.read()
            UPLOADS_DIR.mkdir(exist_ok=True)
            (UPLOADS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}-{message.id}.json").write_bytes(raw)
            summary = await ingest_client_upload(json.loads(raw))
            text = format_summary(summary)
            print(f"Scrim upload: {len(summary['added'])} maps added, {summary['already_known']} already known")
        except Exception as e:
            text = f"❌ Nie udało się dodać gier: {e}"
            print(f"Error processing scrim upload: {e}")
        await message.reply(text, mention_author=False)

        # New maps or timelines change the stats, so the tab is refreshed now instead of within the hour
        if summary and (summary["added"] or summary.get("timelines_added")):
            try:
                await update_stats_tab()
            except Exception as e:
                print(f"Error updating stats after an upload: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(ScrimsCog(bot))
