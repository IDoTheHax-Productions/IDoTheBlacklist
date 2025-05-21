import discord
from discord.ext import commands
from discord import app_commands

class AcceptUserCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    accept = app_commands.Group(name="accept", description="Accept users into the Private SMP")

    @accept.command(name="user", description="Accept a user into the SMP")
    @commands.has_permissions(administrator=True)
    async def accept_user(
        self, 
        interaction: discord.Interaction, 
        username: str, 
        invite_link: str = None
    ):
        """
        Accept a user into the SMP and send them a DM, or ping them in the channel if DMs are closed.
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

        # Build the message content
        message_content = (
            f"Congratulations {user.mention}, you have been accepted into the Private SMP!\n"
            "Please follow the instructions below to proceed.\n"
        )
        if invite_link:
            message_content += f"\nHere is your invite link: {invite_link}"

        # Try to send the DM
        try:
            await user.send(message_content)
            await interaction.response.send_message(
                f"User '{user.name}' has been accepted and notified via DM.",
                ephemeral=True
            )
        except discord.Forbidden:
            # Fallback: Send the message in the channel, pinging the user
            try:
                await interaction.channel.send(message_content)
                await interaction.response.send_message(
                    f"Could not DM the user '{user.name}'. They have been pinged in the channel instead.",
                    ephemeral=True
                )
            except discord.Forbidden:
                await interaction.response.send_message(
                    f"Could not DM or ping the user '{user.name}' in the channel. Please ensure the bot has permissions to send messages.",
                    ephemeral=True
                )

async def setup(bot):
    await bot.add_cog(AcceptUserCog(bot))