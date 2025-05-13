import discord
from discord.ext import commands
from discord import app_commands

class AcceptUserCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    accept = app_commands.Group(name="accept", description="Accept users into the private SMP")

    @accept.command(name="user", description="Accept a user into the SMP")
    @commands.has_permissions(administrator=True)
    async def accept_user(
        self, 
        interaction: discord.Interaction, 
        username: str, 
        invite_link: str = None
    ):
        """
        Accept a user into the SMP and send them a DM.
        :param interaction: The Discord interaction object
        :param username: The Discord username of the user (e.g., cooluser) or user ID
        :param invite_link: (Optional) A Discord invite link to include in the DM
        """
        user = None

        # Check if the input is a user ID (numeric)
        if username.isdigit():
            try:
                user = await self.bot.fetch_user(int(username))
            except discord.NotFound:
                pass

        # If not a user ID, try to find by username
        if not user:
            # Try to find the user in the bot's user cache
            user = discord.utils.get(self.bot.users, name=username)

            # If not found in cache, try fetching from Discord API (modern usernames)
            if not user:
                try:
                    # Note: Discord API doesn't directly support username search anymore,
                    # so this is a fallback for users in the bot's cache or guild.
                    # For guild-specific search:
                    guild = interaction.guild
                    if guild:
                        member = discord.utils.get(guild.members, name=username)
                        if member:
                            user = member.user
                except Exception:
                    pass

        if not user:
            await interaction.response.send_message(
                f"Could not find a user with the username or ID '{username}'. Please ensure the input is correct.",
                ephemeral=True
            )
            return

        # Build the DM message
        dm_message = (
            f"Congratulations {user.name}, you have been accepted into the private SMP!\n"
            "Please follow the instructions below to proceed.\n"
        )
        if invite_link:
            dm_message += f"\nHere is your invite link: {invite_link}"

        # Try to send the DM
        try:
            await user.send(dm_message)
            await interaction.response.send_message(
                f"User '{user.name}' has been accepted and notified via DM.",
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"Could not DM the user '{user.name}'. They may have DMs disabled or blocked the bot.",
                ephemeral=True
            )

async def setup(bot):
    await bot.add_cog(AcceptUserCog(bot))