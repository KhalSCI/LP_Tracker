import discord
from discord import app_commands
from discord.ext import commands

import database as db


class RoastsCog(commands.Cog):
    """Cog for turning the comments on SoloQ games on and off."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    roast_group = app_commands.Group(name="roast", description="Komentarze bota po twoich grach SoloQ")

    @roast_group.command(name="off", description="Wyłącz komentarze bota po twoich grach SoloQ")
    async def roast_off(self, interaction: discord.Interaction):
        await db.set_roast_optout(interaction.user.id, True)
        await interaction.response.send_message("🔕 Ok, bez komentarzy po twoich grach. Włączysz je z powrotem przez `/roast on`.", ephemeral=True)

    @roast_group.command(name="on", description="Włącz komentarze bota po twoich grach SoloQ")
    async def roast_on(self, interaction: discord.Interaction):
        await db.set_roast_optout(interaction.user.id, False)
        await interaction.response.send_message("🔔 Komentarze po twoich grach włączone. Sam chciałeś.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RoastsCog(bot))
