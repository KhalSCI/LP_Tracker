import discord
from discord import app_commands
from discord.ext import commands

import database as db
import scouting
from config import SCOUT_MONTHS

PERIODS = [
    app_commands.Choice(name="Szybki: SoloQ 1 miesiąc, turnieje 12 miesięcy", value="quick"),
    app_commands.Choice(name="3 miesiące", value="3"),
    app_commands.Choice(name="6 miesięcy", value="6"),
    app_commands.Choice(name="12 miesięcy", value="12"),
]


class ScoutingCog(commands.Cog):
    """Cog for opponent scouting."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def team_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """Teams from the Przeciwnicy tab and ones saved with /scout."""
        return [
            app_commands.Choice(name=name, value=name)
            for name in await scouting.team_names()
            if current.lower() in name.lower()
        ][:25]

    @app_commands.command(name="scout", description="Scouting przeciwnika: pule championów z ostatnich miesięcy, bany")
    @app_commands.describe(
        druzyna="Nazwa drużyny (jak w zakładce Przeciwnicy i w kolumnie Przeciwnik w Meczach)",
        gracze="Zamiast zakładki Przeciwnicy: linki multi op.gg (EUW i EUNE, oddzielone spacją) albo Riot ID po przecinku",
        okres=f"Z jakiego okresu brać gry (domyślnie {SCOUT_MONTHS} miesięcy); szybki: SoloQ z miesiąca, turnieje z roku",
    )
    @app_commands.autocomplete(druzyna=team_autocomplete)
    @app_commands.choices(okres=PERIODS)
    async def scout(self, interaction: discord.Interaction, druzyna: str, gracze: str = None,
                    okres: app_commands.Choice[str] = None):
        await interaction.response.defer()
        druzyna = druzyna.strip()
        months, tourney_months = scouting.PERIODS[okres.value] if okres else (SCOUT_MONTHS, SCOUT_MONTHS)

        if gracze:
            players = scouting.parse_players(gracze)
            if not players:
                await interaction.followup.send("❌ Nie widzę tu Riot ID. Podaj link(i) multi op.gg albo np. `Nick#EUW, Nick2#EUNE`.")
                return
            await db.save_opponent(druzyna, players)
            source = "podane w komendzie"
            roles = await scouting.sheet_roles(druzyna)
        else:
            players, source, roles = await scouting.known_players(druzyna)
            if not players:
                await interaction.followup.send(
                    f"❌ Nie znam składu **{discord.utils.escape_markdown(druzyna)}**. Wpisz linki multi w zakładce "
                    f"{scouting.OPPONENTS_TAB} albo podaj `gracze`."
                )
                return

        queued = " Czekam w kolejce na poprzedni scouting." if scouting.is_running() else ""
        await interaction.followup.send(
            f"🔎 Scouting **{discord.utils.escape_markdown(druzyna)}**: SoloQ z {months} mies., "
            f"turnieje z {tourney_months} mies., "
            f"{len(players)} kont ({source}): {discord.utils.escape_markdown(', '.join(players))}.{queued} "
            "Gdy policzę gry do pobrania, napiszę, ile to potrwa; raport wrzucę tutaj."
        )
        scouting.start_scouting(interaction.channel, druzyna, players, months, announce_progress=True,
                                tourney_months=tourney_months, roles=roles)


async def setup(bot: commands.Bot):
    await bot.add_cog(ScoutingCog(bot))
