import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import aiohttp
import asyncio
from dotenv import load_dotenv
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PREMIUM_FILE = "settings/premium.json"
ALLOWED_ADMIN_IDS = [726721909374320640, 1362041490779672576]  # Admin user IDs
BUNQ_API_KEY = os.getenv("BUNQ_API_KEY")
BUNQ_API_URL = "https://api.bunq.com/v1"
BUNQ_SANDBOX_URL = "https://public-api.sandbox.bunq.com/v1"
BUNQ_PAYMENT_AMOUNT = 5  # €5 (~$5 USD)

def is_server_owner():
    """Custom check to restrict commands to server owners or users with manage_guild permission."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            logger.error(f"Command {interaction.command.name} run outside guild by user {interaction.user.id}")
            return False
        is_owner = interaction.user.id == interaction.guild.owner_id
        has_manage_guild = interaction.user.guild_permissions.manage_guild
        result = is_owner or has_manage_guild
        if not result:
            logger.warning(
                f"User {interaction.user.id} failed permission check in guild {interaction.guild.id}: "
                f"Owner={is_owner}, Manage Guild={has_manage_guild}"
            )
        return result
    return app_commands.check(predicate)

def is_premium_server():
    """Check if the server is paid or approved for premium features."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            logger.error(f"Command {interaction.command.name} run outside guild by user {interaction.user.id}")
            return False
        cog = interaction.client.get_cog("PremiumCog")
        if not cog:
            logger.error("PremiumCog not found")
            return False
        return interaction.guild.id in (cog.premium_config["paid_servers"] + cog.premium_config["approved_servers"])
    return app_commands.check(predicate)

def is_admin_user():
    """Check if the user is an admin (specific user IDs)."""
    async def predicate(interaction: discord.Interaction) -> bool:
        result = interaction.user.id in ALLOWED_ADMIN_IDS
        if not result:
            logger.warning(f"User {interaction.user.id} failed admin check")
        return result
    return app_commands.check(predicate)

class PremiumCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.premium_file = PREMIUM_FILE
        self.load_premium_config()
        self.bunq_session = aiohttp.ClientSession()

    def load_premium_config(self):
        """Load the premium config file, creating it if it doesn't exist."""
        os.makedirs(os.path.dirname(self.premium_file), exist_ok=True)
        if not os.path.exists(self.premium_file):
            with open(self.premium_file, "w") as f:
                json.dump({
                    "paid_servers": [],
                    "approved_servers": [],
                    "applications": [],
                    "pending_payments": {}
                }, f)
        with open(self.premium_file, "r") as f:
            self.premium_config = json.load(f)

    def save_premium_config(self):
        """Save the premium config file."""
        with open(self.premium_file, "w") as f:
            json.dump(self.premium_config, f, indent=4)

    async def create_bunq_payment_request(self, user_id, server_id, guild_name):
        """Create a bunq.me payment request for €5."""
        headers = {
            "X-Bunq-Client-Authentication": BUNQ_API_KEY,
            "Content-Type": "application/json"
        }
        payload = {
            "amount_inquired": {
                "value": str(BUNQ_PAYMENT_AMOUNT),
                "currency": "EUR"
            },
            "description": f"Premium customization for {guild_name} (Server ID: {server_id})",
            "alias": {
                "type": "EMAIL",
                "value": f"payment-{user_id}@yourbotdomain.com"  # Replace with your domain
            },
            "redirect_url": "https://yourbotdomain.com/thank-you"  # Replace with your domain
        }

        async with self.bunq_session.post(
            f"{BUNQ_SANDBOX_URL}/user/{{userID}}/monetary-account/{{monetary-accountID}}/request-inquiry",
            headers=headers,
            json=payload
        ) as response:
            if response.status != 200:
                logger.error(f"Failed to create bunq payment request: {response.status} {await response.text()}")
                return None
            data = await response.json()
            request_id = data["Response"][0]["Id"]["id"]
            return f"https://bunq.me/yourbot/{BUNQ_PAYMENT_AMOUNT}?description=Premium+for+{server_id}&request_id={request_id}"

    async def setup_bunq_webhook(self):
        """Set up a bunq webhook for payment notifications."""
        headers = {
            "X-Bunq-Client-Authentication": BUNQ_API_KEY,
            "Content-Type": "application/json"
        }
        payload = {
            "url": "https://yourbotdomain.com/bunq-webhook",  # Replace with your webhook endpoint
            "category": "MUTATION"
        }

        async with self.bunq_session.post(
            f"{BUNQ_SANDBOX_URL}/user/{{userID}}/notification-filter-url",
            headers=headers,
            json=payload
        ) as response:
            if response.status != 200:
                logger.error(f"Failed to setup bunq webhook: {response.status} {await response.text()}")
            return response.status == 200

    premium = app_commands.Group(name="premium", description="Manage premium bot customization")

    @premium.command(name="set_nickname", description="Set the bot's nickname in this server")
    @is_server_owner()
    @is_premium_server()
    async def set_nickname(self, interaction: discord.Interaction, nickname: str):
        """
        Set the bot's nickname in the current server (premium only).
        :param nickname: The new nickname (max 32 characters)
        """
        if len(nickname) > 32:
            await interaction.response.send_message(
                "Nickname must be 32 characters or less.", ephemeral=True
            )
            return

        try:
            await interaction.guild.me.edit(nick=nickname)
            await interaction.response.send_message(
                f"Bot nickname set to '{nickname}' in this server.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "Failed to set nickname. Ensure the bot has permission to change its nickname.", ephemeral=True
            )

    @premium.command(name="set_pfp", description="Set the bot's profile picture in this server")
    @is_server_owner()
    @is_premium_server()
    async def set_pfp(self, interaction: discord.Interaction, attachment: discord.Attachment):
        """
        Set the bot's server-specific profile picture (premium only).
        :param attachment: The image to set as the bot's avatar (PNG/JPEG)
        """
        if not attachment.content_type or not attachment.content_type.startswith("image/"):
            await interaction.response.send_message(
                "Please upload a valid image (PNG or JPEG).", ephemeral=True
            )
            return

        try:
            image_bytes = await attachment.read()
            await interaction.guild.me.edit(avatar=image_bytes)
            await interaction.response.send_message(
                "Bot profile picture updated in this server.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "Failed to set profile picture. Ensure the bot has permission to change its avatar.", ephemeral=True
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                "Failed to process the image. Ensure it's a valid PNG or JPEG.", ephemeral=True
            )

    @premium.command(name="apply", description="Apply for free premium customization")
    @is_server_owner()
    async def apply_premium(self, interaction: discord.Interaction, server_id: str, description: str):
        """
        Submit an application for free premium customization for an SMP server.
        :param server_id: The SMP server ID (numeric)
        :param description: Why your server should be approved (max 500 characters)
        """
        if not server_id.isdigit():
            await interaction.response.send_message(
                "Please provide a valid numeric server ID.", ephemeral=True
            )
            return

        server_id = int(server_id)
        guild = self.bot.get_guild(server_id)
        if not guild:
            await interaction.response.send_message(
                f"Cannot access server ID {server_id}. The bot is not in that server.", ephemeral=True
            )
            return

        # Fetch member to ensure fresh data
        try:
            member = await guild.fetch_member(interaction.user.id)
        except discord.NotFound:
            await interaction.response.send_message(
                f"You are not a member of server {guild.name} (ID: {server_id}).", ephemeral=True
            )
            return
        except discord.Forbidden:
            await interaction.response.send_message(
                f"Bot lacks permission to fetch members in server {guild.name} (ID: {server_id}).", ephemeral=True
            )
            return

        # Verify permissions
        is_owner = member.id == guild.owner_id
        has_manage_guild = member.guild_permissions.manage_guild
        if not (is_owner or has_manage_guild):
            logger.warning(
                f"User {interaction.user.id} failed permission check for server {guild.id}: "
                f"Owner={is_owner}, Manage Guild={has_manage_guild}"
            )
            await interaction.response.send_message(
                f"You must be the owner or have Manage Server permission in server {guild.name} (ID: {server_id}).", ephemeral=True
            )
            return

        # Check if the server is an SMP server
        smp_cog = self.bot.get_cog("ManageSMPServersCog")
        if not smp_cog or server_id not in smp_cog.config["smp_server_ids"]:
            await interaction.response.send_message(
                f"Server ID {server_id} is not an SMP server. Add it with /smp add first.", ephemeral=True
            )
            return

        if len(description) > 500:
            await interaction.response.send_message(
                "Description must be 500 characters or less.", ephemeral=True
            )
            return

        if any(app["server_id"] == server_id for app in self.premium_config["applications"]):
            await interaction.response.send_message(
                f"An application for server ID {server_id} is already pending.", ephemeral=True
            )
            return

        self.premium_config["applications"].append({
            "server_id": server_id,
            "owner_id": interaction.user.id,
            "description": description
        })
        self.save_premium_config()
        await interaction.response.send_message(
            f"Application submitted for server {guild.name}. You will be notified once reviewed.", ephemeral=True
        )

    @premium.command(name="review_application", description="Review a premium application")
    @is_admin_user()
    async def review_application(self, interaction: discord.Interaction, server_id: str, action: str):
        """
        Accept or reject a premium application.
        :param server_id: The server ID of the application (numeric)
        :param action: 'accept' or 'reject'
        """
        if not server_id.isdigit():
            await interaction.response.send_message(
                "Please provide a valid numeric server ID.", ephemeral=True
            )
            return

        server_id = int(server_id)
        action = action.lower()
        if action not in ["accept", "reject"]:
            await interaction.response.send_message(
                "Action must be 'accept' or 'reject'.", ephemeral=True
            )
            return

        application = next((app for app in self.premium_config["applications"] if app["server_id"] == server_id), None)
        if not application:
            await interaction.response.send_message(
                f"No application found for server ID {server_id}.", ephemeral=True
            )
            return

        guild = self.bot.get_guild(server_id)
        guild_name = guild.name if guild else "Unknown Server"
        owner_id = application["owner_id"]
        owner = await self.bot.fetch_user(owner_id) if owner_id else None

        self.premium_config["applications"].remove(application)
        if action == "accept":
            if server_id not in self.premium_config["approved_servers"]:
                self.premium_config["approved_servers"].append(server_id)
            message = f"Your application for premium customization in {guild_name} has been accepted!"
        else:
            message = f"Your application for premium customization in {guild_name} has been rejected."

        self.save_premium_config()

        if owner:
            try:
                await owner.send(message)
            except discord.Forbidden:
                pass

        await interaction.response.send_message(
            f"Application for server ID {server_id} ({guild_name}) has been {action}ed.", ephemeral=True
        )

    @premium.command(name="request_premium", description="Request premium features for a server")
    @is_server_owner()
    async def request_premium(self, interaction: discord.Interaction, server_id: str):
        """
        Request premium features by generating a payment link for €5.
        :param server_id: The server ID to unlock premium for (numeric)
        """
        if not server_id.isdigit():
            await interaction.response.send_message(
                "Please provide a valid numeric server ID.", ephemeral=True
            )
            return

        server_id = int(server_id)
        guild = self.bot.get_guild(server_id)
        if not guild:
            await interaction.response.send_message(
                f"Cannot access server ID {server_id}. The bot is not in that server.", ephemeral=True
            )
            return

        # Fetch member to ensure fresh data
        try:
            member = await guild.fetch_member(interaction.user.id)
        except discord.NotFound:
            await interaction.response.send_message(
                f"You are not a member of server {guild.name} (ID: {server_id}).", ephemeral=True
            )
            return
        except discord.Forbidden:
            await interaction.response.send_message(
                f"Bot lacks permission to fetch members in server {guild.name} (ID: {server_id}).", ephemeral=True
            )
            return

        # Verify permissions
        is_owner = member.id == guild.owner_id
        has_manage_guild = member.guild_permissions.manage_guild
        if not (is_owner or has_manage_guild):
            logger.warning(
                f"User {interaction.user.id} failed permission check for server {guild.id}: "
                f"Owner={is_owner}, Manage Guild={has_manage_guild}"
            )
            await interaction.response.send_message(
                f"You must be the owner or have Manage Server permission in server {guild.name} (ID: {server_id}).", ephemeral=True
            )
            return

        if server_id in (self.premium_config["paid_servers"] + self.premium_config["approved_servers"]):
            await interaction.response.send_message(
                "This server already has premium features unlocked.", ephemeral=True
            )
            return

        payment_link = await self.create_bunq_payment_request(interaction.user.id, server_id, guild.name)
        if not payment_link:
            await interaction.response.send_message(
                "Failed to generate payment link. Contact the bot admin.", ephemeral=True
            )
            return

        self.premium_config["pending_payments"][str(server_id)] = {
            "user_id": interaction.user.id,
            "amount": BUNQ_PAYMENT_AMOUNT,
            "guild_name": guild.name
        }
        self.save_premium_config()

        await interaction.response.send_message(
            f"Please complete the €5 payment to unlock premium features for {guild.name}:\n{payment_link}\n"
            "You will be notified once the payment is confirmed.",
            ephemeral=True
        )

    async def handle_bunq_webhook(self, payment_data):
        """Handle incoming bunq webhook for payment confirmation."""
        server_id = None
        for pending in self.premium_config["pending_payments"].values():
            if pending["amount"] == float(payment_data.get("amount", {}).get("value", 0)):
                server_id = next(
                    sid for sid, data in self.premium_config["pending_payments"].items()
                    if data == pending
                )
                break

        if not server_id:
            logger.warning("No matching payment found in webhook")
            return

        server_id = int(server_id)
        payment_info = self.premium_config["pending_payments"].pop(str(server_id))
        self.premium_config["paid_servers"].append(server_id)
        self.save_premium_config()

        owner = await self.bot.fetch_user(payment_info["user_id"])
        if owner:
            try:
                await owner.send(
                    f"Payment confirmed! Premium features unlocked for {payment_info['guild_name']}."
                    " You can now use /premium set_nickname and /premium set_pfp."
                )
            except discord.Forbidden:
                pass

    def cog_unload(self):
        """Clean up when the cog is unloaded."""
        asyncio.create_task(self.bunq_session.close())

async def setup(bot):
    await bot.add_cog(PremiumCog(bot))