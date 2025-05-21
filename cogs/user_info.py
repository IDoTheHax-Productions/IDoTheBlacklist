import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
from datetime import timedelta

CONFIG_FILE = "settings/user_info.json"

def is_server_owner():
    """Custom check to restrict commands to server owners or users with manage_guild permission."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:  # Ensure command is run in a guild
            return False
        return (interaction.user.id == interaction.guild.owner_id or
                interaction.user.guild_permissions.manage_guild)
    return app_commands.check(predicate)

class ManageSMPServersCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config_file = CONFIG_FILE
        self.load_config()
        self.check_smp_members.start()

    def load_config(self):
        """Load the config file, creating it and its directory if they don't exist."""
        os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
        if not os.path.exists(self.config_file):
            with open(self.config_file, "w") as f:
                json.dump({
                    "smp_server_ids": [],
                    "smp_member_role": "SMP Member",
                    "smp_members": {}
                }, f)
        with open(self.config_file, "r") as f:
            self.config = json.load(f)

    def save_config(self):
        """Save the config file."""
        with open(self.config_file, "w") as f:
            json.dump(self.config, f, indent=4)

    @tasks.loop(hours=24)
    async def check_smp_members(self):
        """Check SMP servers every 24 hours and update the smp_members list."""
        for server_id in self.config["smp_server_ids"]:
            server_id = str(server_id)
            guild = self.bot.get_guild(int(server_id))
            if not guild:
                continue

            # Get members with the SMP member role
            role = discord.utils.get(guild.roles, name=self.config["smp_member_role"])
            if not role:
                continue

            current_members = [member.id for member in guild.members if role in member.roles]
            if server_id not in self.config["smp_members"]:
                self.config["smp_members"][server_id] = []

            # Update smp_members: add new members, remove those without the role
            self.config["smp_members"][server_id] = list(
                set(self.config["smp_members"][server_id]) & set(current_members)
            ) + [mid for mid in current_members if mid not in self.config["smp_members"][server_id]]

            # Clean up empty server entries
            if not self.config["smp_members"][server_id]:
                del self.config["smp_members"][server_id]

        self.save_config()

    @check_smp_members.before_loop
    async def before_check_smp_members(self):
        """Ensure the bot is ready before starting the task."""
        await self.bot.wait_until_ready()

    smp = app_commands.Group(name="smp", description="Manage SMP server IDs and roles (server owners only)")

    @smp.command(name="add", description="Add an SMP server ID")
    @is_server_owner()
    async def add_smp_server(self, interaction: discord.Interaction, server_id: str):
        """
        Add a server ID to the SMP server list.
        :param server_id: The server ID to add (numeric)
        """
        if not server_id.isdigit():
            await interaction.response.send_message(
                "Please provide a valid numeric server ID.", ephemeral=True
            )
            return

        server_id = int(server_id)
        if server_id in self.config["smp_server_ids"]:
            await interaction.response.send_message(
                f"Server ID {server_id} is already in the SMP list.", ephemeral=True
            )
            return

        # Verify the bot is in the server
        guild = self.bot.get_guild(server_id)
        if not guild:
            await interaction.response.send_message(
                f"Cannot add server ID {server_id}. The bot is not in that server.", ephemeral=True
            )
            return

        # Verify the user is the owner or has manage_guild in the target server
        member = guild.get_member(interaction.user.id)
        if not member or not (member.id == guild.owner_id or member.guild_permissions.manage_guild):
            await interaction.response.send_message(
                f"You must be the owner or have Manage Server permission in server {guild.name} to add it.", ephemeral=True
            )
            return

        self.config["smp_server_ids"].append(server_id)
        self.save_config()
        await interaction.response.send_message(
            f"Added server ID {server_id} ({guild.name}) to the SMP list.", ephemeral=True
        )

    @smp.command(name="remove", description="Remove an SMP server ID")
    @is_server_owner()
    async def remove_smp_server(self, interaction: discord.Interaction, server_id: str):
        """
        Remove a server ID from the SMP server list.
        :param server_id: The server ID to remove (numeric)
        """
        if not server_id.isdigit():
            await interaction.response.send_message(
                "Please provide a valid numeric server ID.", ephemeral=True
            )
            return

        server_id = int(server_id)
        if server_id not in self.config["smp_server_ids"]:
            await interaction.response.send_message(
                f"Server ID {server_id} is not in the SMP list.", ephemeral=True
            )
            return

        # Verify the user is the owner or has manage_guild in the target server
        guild = self.bot.get_guild(server_id)
        if guild:
            member = guild.get_member(interaction.user.id)
            if not member or not (member.id == guild.owner_id or member.guild_permissions.manage_guild):
                await interaction.response.send_message(
                    f"You must be the owner or have Manage Server permission in server {guild.name} to remove it.", ephemeral=True
                )
                return

        self.config["smp_server_ids"].remove(server_id)
        if str(server_id) in self.config["smp_members"]:
            del self.config["smp_members"][str(server_id)]
        self.save_config()
        guild_name = guild.name if guild else "Unknown Server"
        await interaction.response.send_message(
            f"Removed server ID {server_id} ({guild_name}) from the SMP list.", ephemeral=True
        )

    @smp.command(name="list", description="List all SMP server IDs")
    @is_server_owner()
    async def list_smp_servers(self, interaction: discord.Interaction):
        """
        Show all SMP server IDs in the list.
        """
        if not self.config["smp_server_ids"]:
            await interaction.response.send_message(
                "No SMP server IDs are currently set.", ephemeral=True
            )
            return

        response = "**SMP Server IDs**:\n"
        for server_id in self.config["smp_server_ids"]:
            guild = self.bot.get_guild(server_id)
            guild_name = guild.name if guild else "Unknown Server"
            response += f"- {server_id} ({guild_name})\n"
        await interaction.response.send_message(response, ephemeral=True)

    @smp.command(name="set_role", description="Set the SMP member role for a user")
    @is_server_owner()
    async def set_smp_role(self, interaction: discord.Interaction, member: discord.Member, server_id: str):
        """
        Assign the SMP member role to a user in a specific SMP server and track them.
        :param member: The user to assign the role to
        :param server_id: The SMP server ID (numeric)
        """
        if not server_id.isdigit():
            await interaction.response.send_message(
                "Please provide a valid numeric server ID.", ephemeral=True
            )
            return

        server_id = int(server_id)
        if server_id not in self.config["smp_server_ids"]:
            await interaction.response.send_message(
                f"Server ID {server_id} is not in the SMP list.", ephemeral=True
            )
            return

        guild = self.bot.get_guild(server_id)
        if not guild:
            await interaction.response.send_message(
                f"Cannot access server ID {server_id}. The bot is not in that server.", ephemeral=True
            )
            return

        # Verify the user is the owner or has manage_guild in the target server
        caller = guild.get_member(interaction.user.id)
        if not caller or not (caller.id == guild.owner_id or caller.guild_permissions.manage_guild):
            await interaction.response.send_message(
                f"You must be the owner or have Manage Server permission in server {guild.name} to set roles.", ephemeral=True
            )
            return

        # Get the SMP member role
        role = discord.utils.get(guild.roles, name=self.config["smp_member_role"])
        if not role:
            await interaction.response.send_message(
                f"The role '{self.config['smp_member_role']}' does not exist in server {guild.name}.", ephemeral=True
            )
            return

        # Get the member in the guild
        guild_member = guild.get_member(member.id)
        if not guild_member:
            await interaction.response.send_message(
                f"{member.name} is not a member of server {guild.name}.", ephemeral=True
            )
            return

        # Assign the role
        try:
            await guild_member.add_roles(role)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"Failed to assign role '{role.name}' to {member.name}. Check bot permissions.", ephemeral=True
            )
            return

        # Update smp_members
        server_id_str = str(server_id)
        if server_id_str not in self.config["smp_members"]:
            self.config["smp_members"][server_id_str] = []
        if member.id not in self.config["smp_members"][server_id_str]:
            self.config["smp_members"][server_id_str].append(member.id)
            self.save_config()

        await interaction.response.send_message(
            f"Assigned SMP member role to {member.name} in {guild.name} and added to member list.", ephemeral=True
        )

async def setup(bot):
    await bot.add_cog(ManageSMPServersCog(bot))