from discord.ext import commands
from discord import app_commands
import discord
import json
import os
from datetime import datetime
import asyncio
import logging

# Configure logging for debugging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

CONFIG_FILE = "settings/user_info.json"
SMP_CORE_CHANNEL_ID = 1376528950557282474

class SetupModal(discord.ui.Modal, title='SMP Setup'):
    def __init__(self, cog, smp_name):
        super().__init__()
        self.cog = cog
        self.smp_name = smp_name

        self.role_input = discord.ui.TextInput(
            label='SMP Member Role ID',
            placeholder='Enter the role ID for SMP members (right-click role → Copy ID)',
            required=True,
            max_length=20
        )

        self.invite_input = discord.ui.TextInput(
            label='Private Server Invite Link',
            placeholder='Enter your private Discord server invite link',
            required=True,
            max_length=200
        )
        
        self.add_item(self.role_input)
        self.add_item(self.invite_input)

    async def on_submit(self, interaction: discord.Interaction):
        role_id = self.role_input.value.strip()
        invite_link = self.invite_input.value.strip()

        # Validate role ID
        if not role_id.isdigit():
            await interaction.response.send_message(
                "❌ Invalid role ID format. Please enter a valid numeric role ID.", 
                ephemeral=True
            )
            return
        
        role = interaction.guild.get_role(int(role_id))
        if not role:
            await interaction.response.send_message(
                f"❌ Role with ID {role_id} not found in this server.", 
                ephemeral=True
            )
            return

        # Validate invite link
        if not (invite_link.startswith('https://discord.gg/') or invite_link.startswith('https://discord.com/invite/')):
            await interaction.response.send_message(
                "❌ Invalid invite link format. Please use a valid Discord invite link.", 
                ephemeral=True
            )
            return

        # Store SMP configuration
        server_id = str(interaction.guild.id)
        if "smp_configs" not in self.cog.config:
            self.cog.config["smp_configs"] = {}
        
        self.cog.config["smp_configs"][server_id] = {
            "name": self.smp_name,
            "member_role_id": int(role_id),
            "member_role_name": role.name,
            "invite_link": invite_link,
            "setup_by": interaction.user.id,
            "setup_date": datetime.now().isoformat(),
            "approved": False,
            "server_id": interaction.guild.id
        }
        
        self.cog.save_config()
        logger.info(f"SMP setup completed for server {server_id} with name {self.smp_name}")

        embed = discord.Embed(
            title="✅ SMP Setup Complete!",
            description=f"**SMP Name:** {self.smp_name}\n**Member Role:** {role.mention} ({role.name})\n**Invite Link:** {invite_link}",
            color=discord.Color.green()
        )
        embed.add_field(
            name="Next Steps",
            value="Your SMP is now configured! Users can apply using `/apply` command.",
            inline=False
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

class ApplicationView(discord.ui.View):
    def __init__(self, cog, application_data):
        super().__init__(timeout=None)
        self.cog = cog
        self.application_data = application_data

    @discord.ui.button(label='Accept', style=discord.ButtonStyle.success, emoji='✅')
    async def accept_application(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need administrator permissions to approve applications.", ephemeral=True)
            return

        server_id = str(self.application_data["server_id"])
        smp_name = self.application_data["smp_name"]
        
        # Ensure smp_server_ids exists
        if "smp_server_ids" not in self.cog.config:
            self.cog.config["smp_server_ids"] = []
            self.cog.save_config()
            logger.warning(f"Initialized missing smp_server_ids in config for server {server_id}")

        if server_id in self.cog.config.get("smp_configs", {}):
            self.cog.config["smp_configs"][server_id]["approved"] = True
            if self.application_data["server_id"] not in self.cog.config["smp_server_ids"]:
                self.cog.config["smp_server_ids"].append(self.application_data["server_id"])
                self.cog.save_config()
                logger.info(f"Added server ID {self.application_data['server_id']} to smp_server_ids")

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.add_field(name="Status", value=f"✅ **APPROVED** by {interaction.user.mention}", inline=False)
        
        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(embed=embed, view=self)
        
        try:
            applicant = self.cog.bot.get_user(self.application_data["applicant_id"])
            if applicant:
                notify_embed = discord.Embed(
                    title="🎉 SMP Application Approved!",
                    description=f"Your SMP **{smp_name}** has been approved and is now listed!",
                    color=discord.Color.green()
                )
                await applicant.send(embed=notify_embed)
        except Exception as e:
            logger.error(f"Failed to notify applicant {self.application_data['applicant_id']}: {e}")

    @discord.ui.button(label='Deny', style=discord.ButtonStyle.danger, emoji='❌')
    async def deny_application(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need administrator permissions to deny applications.", ephemeral=True)
            return

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.red()
        embed.add_field(name="Status", value=f"❌ **DENIED** by {interaction.user.mention}", inline=False)
        
        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(embed=embed, view=self)
        
        try:
            applicant = self.cog.bot.get_user(self.application_data["applicant_id"])
            if applicant:
                notify_embed = discord.Embed(
                    title="❌ SMP Application Denied",
                    description=f"Your SMP **{self.application_data['smp_name']}** application has been denied.",
                    color=discord.Color.red()
                )
                await applicant.send(embed=notify_embed)
        except Exception as e:
            logger.error(f"Failed to notify applicant {self.application_data['applicant_id']}: {e}")

class ManageSMPServersCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config_file = CONFIG_FILE
        self.load_config()

    def load_config(self):
        os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
        default_config = {
            "smp_server_ids": [],
            "smp_member_role": "SMP Member",
            "smp_members": {},
            "user_logs": {},
            "smp_configs": {}
        }

        if not os.path.exists(self.config_file):
            with open(self.config_file, "w") as f:
                json.dump(default_config, f, indent=4)
                logger.info(f"Created new config file at {self.config_file}")

        try:
            with open(self.config_file, "r") as f:
                self.config = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"Failed to load config file: {e}")
            self.config = default_config
            self.save_config()

        # Ensure all required keys exist
        for key, value in default_config.items():
            if key not in self.config:
                self.config[key] = value
                logger.warning(f"Initialized missing config key: {key}")
        self.save_config()

    def save_config(self):
        try:
            with open(self.config_file, "w") as f:
                json.dump(self.config, f, indent=4)
        except Exception as e:
            logger.error(f"Failed to save config file: {e}")

    async def sync_smp_members(self):
        for server_id in self.config.get("smp_server_ids", []):
            server_id = str(server_id)
            guild = self.bot.get_guild(int(server_id))
            if not guild:
                logger.warning(f"Guild {server_id} not found during sync")
                continue

            role = None
            if server_id in self.config.get("smp_configs", {}):
                role_id = self.config["smp_configs"][server_id].get("member_role_id")
                if role_id:
                    role = guild.get_role(int(role_id))
                    logger.debug(f"Role ID {role_id} for server {server_id}: {'Found' if role else 'Not found'}")
            
            if not role:
                role_name = self.config["smp_configs"].get(server_id, {}).get("member_role_name", self.config["smp_member_role"])
                role = discord.utils.get(guild.roles, name=role_name)
                logger.debug(f"Role name {role_name} for server {server_id}: {'Found' if role else 'Not found'}")
            
            if not role:
                logger.warning(f"No valid role found for server {server_id}")
                continue

            current_members = [member.id for member in guild.members if role in member.roles]
            if server_id not in self.config["smp_members"]:
                self.config["smp_members"][server_id] = []

            self.config["smp_members"][server_id] = list(
                set(self.config["smp_members"][server_id]) & set(current_members)
            ) + [mid for mid in current_members if mid not in self.config["smp_members"][server_id]]
            self.save_config()
            logger.info(f"Synced members for server {server_id}")

    async def get_smp_servers_for_user(self, user: discord.User):
        smp_servers = []
        for server_id in self.config.get("smp_server_ids", []):
            server_id = str(server_id)
            guild = self.bot.get_guild(int(server_id))
            if guild and guild.get_member(user.id):
                smp_name = guild.name
                if server_id in self.config.get("smp_configs", {}):
                    smp_name = self.config["smp_configs"][server_id].get("name", guild.name)
                smp_servers.append(smp_name)
        return smp_servers

    async def get_user_logs(self, user: discord.User):
        user_id = str(user.id)
        logs = []
        for server_id in self.config.get("smp_server_ids", []):
            server_id = str(server_id)
            if server_id in self.config.get("user_logs", {}) and user_id in self.config["user_logs"][server_id]:
                logs.extend(self.config["user_logs"][server_id][user_id])
        return logs

    def get_approved_smps(self):
        approved_smps = []
        for server_id, config in self.config.get("smp_configs", {}).items():
            if config.get("approved", False):
                approved_smps.append({
                    "name": config["name"],
                    "server_id": int(server_id),
                    "member_role_id": config.get("member_role_id"),
                    "member_role_name": config.get("member_role_name", "SMP Member")
                })
        return approved_smps

    smp = app_commands.Group(name="smp", description="Manage SMP server IDs, roles, and user info")

    @smp.command(name="info", description="Show SMP and moderation info for a user")
    async def info(self, interaction: discord.Interaction, user: discord.User):
        await interaction.response.defer()

        smp_servers = await self.get_smp_servers_for_user(user)
        user_logs = await self.get_user_logs(user)

        embed1 = discord.Embed(title=f"SMP Info for {user.name}", color=discord.Color.blue())
        embed1.set_footer(text="Page 1/2")
        embed1.add_field(
            name="SMP Servers", 
            value="\n".join(smp_servers) if smp_servers else "Not in any SMP servers.", 
            inline=False
        )

        embed2 = discord.Embed(title=f"Moderation Log for {user.name}", color=discord.Color.orange())
        embed2.set_footer(text="Page 2/2")
        embed2.add_field(
            name="Logs", 
            value="\n".join(user_logs) if user_logs else "No moderation logs found.", 
            inline=False
        )

        view = discord.ui.View()
        left_button = discord.ui.Button(style=discord.ButtonStyle.primary, emoji="⬅️", disabled=True)
        right_button = discord.ui.Button(style=discord.ButtonStyle.primary, emoji="➡️")
        view.add_item(left_button)
        view.add_item(right_button)

        original_user_id = interaction.user.id
        current_page = 1

        async def left_button_callback(interaction_left):
            nonlocal current_page
            if interaction_left.user.id != original_user_id:
                await interaction_left.response.send_message("You are not allowed to use these buttons.", ephemeral=True)
                return
            if current_page == 1:
                return
            current_page = 1
            left_button.disabled = True
            right_button.disabled = False
            await interaction_left.response.edit_message(embed=embed1, view=view)

        async def right_button_callback(interaction_right):
            nonlocal current_page
            if interaction_right.user.id != original_user_id:
                await interaction_right.response.send_message("You are not allowed to use these buttons.", ephemeral=True)
                return
            if current_page == 2:
                return
            current_page = 2
            left_button.disabled = False
            right_button.disabled = True
            await interaction_right.response.edit_message(embed=embed2, view=view)

        left_button.callback = left_button_callback
        right_button.callback = right_button_callback

        await interaction.followup.send(embed=embed1, view=view)

    @smp.command(name="roster", description="List the whole roster for an SMP")
    async def roster(self, interaction: discord.Interaction, smp_name: str):
        await interaction.response.defer()

        target_server_id = None
        target_config = None
        for server_id, config in self.config.get("smp_configs", {}).items():
            if config.get("approved", False) and config["name"].lower() == smp_name.lower():
                target_server_id = int(server_id)
                target_config = config
                break
        
        if not target_server_id:
            await interaction.followup.send(f"❌ SMP '{smp_name}' not found or not approved.", ephemeral=True)
            return

        guild = self.bot.get_guild(target_server_id)
        if not guild:
            await interaction.followup.send(f"❌ Cannot access the SMP server.", ephemeral=True)
            return

        role = None
        role_id = target_config.get("member_role_id")
        if role_id:
            role = guild.get_role(int(role_id))
            logger.debug(f"Role ID {role_id} for server {target_server_id}: {'Found' if role else 'Not found'}")
        
        if not role and target_config.get("member_role_name"):
            role_name = target_config["member_role_name"]
            role = discord.utils.get(guild.roles, name=role_name)
            logger.debug(f"Role name {role_name} for server {target_server_id}: {'Found' if role else 'Not found'}")
            
        if not role:
            role_id = target_config.get("member_role_id", "Unknown")
            role_name = target_config.get("member_role_name", "SMP Member")
            await interaction.followup.send(
                f"❌ SMP member role not found. Could not locate role with ID {role_id} or name '{role_name}' in the server.", 
                ephemeral=True
            )
            return

        members = [member for member in guild.members if role in member.roles and not member.bot]
        
        if not members:
            embed = discord.Embed(
                title=f"📋 {smp_name} Roster",
                description="No members found.",
                color=discord.Color.blue()
            )
            await interaction.followup.send(embed=embed)
            return

        embed = discord.Embed(
            title=f"📋 {smp_name} Roster",
            description=f"**Total Members:** {len(members)}",
            color=discord.Color.blue()
        )

        member_list = [f"{i+1}. {member.display_name} ({member.name})" for i, member in enumerate(sorted(members, key=lambda m: m.display_name.lower()))]
        chunk_size = 20
        for i in range(0, len(member_list), chunk_size):
            chunk = member_list[i:i+chunk_size]
            field_name = f"Members {i+1}-{min(i+chunk_size, len(member_list))}"
            embed.add_field(name=field_name, value="\n".join(chunk), inline=True)

        embed.set_footer(text=f"Server: {guild.name}")
        await interaction.followup.send(embed=embed)

    @smp.command(name="setup", description="Set up your SMP server configuration")
    async def setup(self, interaction: discord.Interaction, smp_name: str):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ You need administrator permissions to set up an SMP.", ephemeral=True)
            return

        server_id = str(interaction.guild.id)
        if server_id in self.config.get("smp_configs", {}):
            existing_config = self.config["smp_configs"][server_id]
            embed = discord.Embed(
                title="⚠️ SMP Already Configured",
                description=f"This server already has an SMP configuration:\n\n**Name:** {existing_config['name']}\n**Member Role:** {existing_config.get('member_role_name', 'Unknown Role')}\n**Status:** {'✅ Approved' if existing_config.get('approved') else '⏳ Pending Approval'}",
                color=discord.Color.orange()
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        modal = SetupModal(self, smp_name)
        await interaction.response.send_modal(modal)

    @smp.command(name="apply", description="Apply to get your SMP listed")
    async def apply(self, interaction: discord.Interaction, smp_name: str):
        server_id = str(interaction.guild.id)
        if server_id not in self.config.get("smp_configs", {}):
            await interaction.response.send_message("❌ This SMP is not set up yet. Use `/smp setup` first!", ephemeral=True)
            return

        smp_config = self.config["smp_configs"][server_id]
        
        if smp_config.get("approved", False):
            await interaction.response.send_message("✅ This SMP is already approved and listed!", ephemeral=True)
            return

        if smp_config["name"].lower() != smp_name.lower():
            await interaction.response.send_message(f"❌ SMP name doesn't match. Configured name: **{smp_config['name']}**", ephemeral=True)
            return

        smp_core_channel = self.bot.get_channel(SMP_CORE_CHANNEL_ID)
        if not smp_core_channel:
            await interaction.response.send_message("❌ SMP Core channel not configured. Please contact an administrator.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🔔 New SMP Application",
            color=discord.Color.blue()
        )
        embed.add_field(name="SMP Name", value=smp_config["name"], inline=True)
        embed.add_field(name="Server Name", value=interaction.guild.name, inline=True)
        embed.add_field(name="Server ID", value=interaction.guild.id, inline=True)
        embed.add_field(name="Member Role", value=f"<@&{smp_config.get('member_role_id', 'Unknown')}> ({smp_config.get('member_role_name', 'Unknown Role')})", inline=True)
        embed.add_field(name="Invite Link", value=smp_config["invite_link"], inline=False)
        embed.add_field(name="Applied By", value=f"{interaction.user.mention} ({interaction.user.name})", inline=True)
        embed.add_field(name="Member Count", value=interaction.guild.member_count, inline=True)
        embed.set_footer(text=f"Application Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        application_data = {
            "smp_name": smp_config["name"],
            "server_id": interaction.guild.id,
            "applicant_id": interaction.user.id
        }

        view = ApplicationView(self, application_data)
        await smp_core_channel.send(embed=embed, view=view)

        await interaction.response.send_message("✅ Your SMP application has been submitted to SMP Core for review!", ephemeral=True)

    @smp.command(name="add", description="Add an SMP server ID")
    async def add_smp_server(self, interaction: discord.Interaction, server_id: str):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You need to be an administrator to use this command.", ephemeral=True)
            return

        if not server_id.isdigit():
            await interaction.response.send_message("Please provide a valid numeric server ID.", ephemeral=True)
            return

        server_id = int(server_id)
        if server_id in self.config.get("smp_server_ids", []):
            await interaction.response.send_message(f"Server ID {server_id} is already in the SMP list.", ephemeral=True)
            return

        guild = self.bot.get_guild(server_id)
        if not guild:
            await interaction.response.send_message(f"Cannot add server ID {server_id}. The bot is not in that server.", ephemeral=True)
            return

        self.config["smp_server_ids"].append(server_id)
        self.save_config()
        logger.info(f"Added server ID {server_id} to smp_server_ids")
        await interaction.response.send_message(f"Added server ID {server_id} ({guild.name}) to the SMP list.", ephemeral=True)

    @smp.command(name="remove", description="Remove an SMP server ID")
    async def remove_smp_server(self, interaction: discord.Interaction, server_id: str):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You need to be an administrator to use this command.", ephemeral=True)
            return

        if not server_id.isdigit():
            await interaction.response.send_message("Please provide a valid numeric server ID.", ephemeral=True)
            return

        server_id = int(server_id)
        if server_id not in self.config.get("smp_server_ids", []):
            await interaction.response.send_message(f"Server ID {server_id} is not in the SMP list.", ephemeral=True)
            return

        self.config["smp_server_ids"].remove(server_id)
        self.save_config()
        logger.info(f"Removed server ID {server_id} from smp_server_ids")
        await interaction.response.send_message(f"Removed server ID {server_id} from the SMP list.", ephemeral=True)

    @smp.command(name="log", description="Add a log entry for a user")
    async def log_user(self, interaction: discord.Interaction, user: discord.User, server_id: str, log_entry: str):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You need to be an administrator to use this command.", ephemeral=True)
            return

        if not server_id.isdigit():
            await interaction.response.send_message("Please provide a valid numeric server ID.", ephemeral=True)
            return

        server_id = int(server_id)
        if server_id not in self.config.get("smp_server_ids", []):
            await interaction.response.send_message(f"Server ID {server_id} is not in the SMP list.", ephemeral=True)
            return

        server_id_str = str(server_id)
        user_id_str = str(user.id)

        if server_id_str not in self.config.get("user_logs", {}):
            self.config["user_logs"][server_id_str] = {}

        if user_id_str not in self.config["user_logs"][server_id_str]:
            self.config["user_logs"][server_id_str][user_id_str] = []

        self.config["user_logs"][server_id_str][user_id_str].append(log_entry)
        self.save_config()
        logger.info(f"Added log entry for user {user.id} in server {server_id}: {log_entry}")

        await interaction.response.send_message(f"Added log entry for {user.name} in server {server_id}: {log_entry}", ephemeral=True)
        
async def setup(bot):
    await bot.add_cog(ManageSMPServersCog(bot))