import discord
from discord import app_commands
from discord.ext import commands

import database as db
import scouting


class ScoutingCog(commands.Cog):
    """Cog for opponent scouting."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def team_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """Saved opponent teams."""
        return [
            app_commands.Choice(name=team["name"], value=team["name"])
            for team in await db.get_opponents()
            if current.lower() in team["name"].lower()
        ][:25]

    @app_commands.command(name="scout", description="Scouting przeciwnika: pule championów z tego i poprzedniego sezonu, bany")
    @app_commands.describe(
        druzyna="Nazwa drużyny (jak w kolumnie Przeciwnik w zakładce Mecze)",
        gracze="Linki multi op.gg (np. EUW i EUNE, oddzielone spacją) albo Riot ID po przecinku; zapamiętam je",
    )
    @app_commands.autocomplete(druzyna=team_autocomplete)
    async def scout(self, interaction: discord.Interaction, druzyna: str, gracze: str = None):
        await interaction.response.defer()
        druzyna = druzyna.strip()

        if gracze:
            players = scouting.parse_players(gracze)
            if not players:
                await interaction.followup.send("❌ Nie widzę tu Riot ID. Podaj link(i) multi op.gg albo np. `Nick#EUW, Nick2#EUNE`.")
                return
            await db.save_opponent(druzyna, players)
        else:
            players = await scouting.known_players(druzyna)
            if not players:
                await interaction.followup.send(
                    f"❌ Nie znam składu **{discord.utils.escape_markdown(druzyna)}**. "
                    "Podaj `gracze`: link multi op.gg albo Riot ID po przecinku."
                )
                return

        queued = " Czekam w kolejce na poprzedni scouting." if scouting.is_running() else ""
        await interaction.followup.send(
            f"🔎 Scouting **{discord.utils.escape_markdown(druzyna)}** ({len(players)} kont: "
            f"{discord.utils.escape_markdown(', '.join(players))}).{queued} "
            "Za pierwszym razem może to potrwać kilkanaście minut, przy kontach z dwóch serwerów nawet pół godziny "
            "(limit Riot API). Raport wrzucę tutaj."
        )
        scouting.start_scouting(interaction.channel, druzyna, players)


async def setup(bot: commands.Bot):
    await bot.add_cog(ScoutingCog(bot))
