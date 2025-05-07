import discord
from discord.ext import commands
from discord import app_commands

class AcceptUserCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # Create a group for the accept commands
    accept = app_commands.Group(name="accept", description="Accept users into the private SMP")

    @accept.command(name="user", description="Accept a user into the SMP")
    @commands.has_permissions(administrator=True)
    async def accept_user(
        self, 
        interaction: discord.Interaction, 
        discord_name: str, 
        invite_link: str = None
    ):
        """
        Accept a user into the SMP and send them a DM.
        :param interaction: The Discord interaction object
        :param discord_name: The Discord username of the user (e.g., User#1234)
        :param invite_link: (Optional) A Discord invite link to include in the DM
        """
        # Find the user by their Discord name
        user = discord.utils.get(self.bot.users, name=discord_name.split("#")[0], discriminator=discord_name.split("#")[1])

        if not user:
            await interaction.response.send_message(
                f"Could not find a user with the Discord name '{discord_name}'. Please ensure the name is correct.",
                ephemeral=True
            )
            return

        # Build the DM message
        dm_message = (
            f"Congratulations {discord_name}, you have been accepted into the private SMP!\n"
            "Please follow the instructions below to proceed.\n"
        )
        if invite_link:
            dm_message += f"\nHere is your invite link: {invite_link}"

        # Try to send the DM
        try:
            await user.send(dm_message)
            await interaction.response.send_message(
                f"User '{discord_name}' has been accepted and notified via DM.",
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"Could not DM the user '{discord_name}'. They may have DMs disabled or blocked the bot.",
                ephemeral=True
            )

async def setup(bot):
    await bot.add_cog(AcceptUserCog(bot))
