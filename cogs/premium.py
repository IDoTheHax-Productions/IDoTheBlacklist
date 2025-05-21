import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import aiohttp
import asyncio
from dotenv import load_dotenv
import logging
import base64
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from datetime import datetime, timedelta
import time

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PREMIUM_FILE = "settings/premium.json"
ALLOWED_ADMIN_IDS = [726721909374320640, 1362041490779672576]
BUNQ_API_KEY = os.getenv("BUNQ_API_KEY")  # Sandbox API key
BUNQ_API_URL = "https://public-api.sandbox.bunq.com/v1"  # Sandbox API
BUNQ_PAYMENT_AMOUNT = 5  # €5 (~$5 USD)
PAYMENT_EMAIL = "hax@idothehax.com"
WEBHOOK_URL = "https://8646-84-86-117-4.ngrok-free.app/bunq-webhook"

def is_server_owner():
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
        self.user_id = None
        self.monetary_account_id = None
        self.session_token = None
        self.session_expiry = None
        self.private_key_pem = None
        self.public_key_pem = None
        self.installation_token = None
        self.generate_key_pair()

    def generate_key_pair(self):
        """Generate or load RSA key pair for signing requests."""
        private_key_file = 'private_key.pem'
        public_key_file = 'public_key.pem'
        
        if os.path.exists(private_key_file) and os.path.exists(public_key_file):
            with open(private_key_file, 'r') as private_file:
                self.private_key_pem = private_file.read()
            with open(public_key_file, 'r') as public_file:
                self.public_key_pem = public_file.read()
            logger.info("bunq - using existing keypair")
        else:
            private_key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=2048
            )
            public_key = private_key.public_key()
            self.private_key_pem = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()
            ).decode('utf-8')
            self.public_key_pem = public_key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo
            ).decode('utf-8')
            with open(private_key_file, 'w') as private_file:
                private_file.write(self.private_key_pem)
            with open(public_key_file, 'w') as public_file:
                public_file.write(self.public_key_pem)
            logger.info("bunq - creating new keypair [KEEP THESE FILES SAFE]")

    def sign_request(self, body: str) -> str:
        """Sign request body with private key, following bunq's requirements."""
        if not self.private_key_pem:
            logger.error("Private key not initialized")
            return ""
        try:
            # Ensure body is a valid JSON string
            body_dict = json.loads(body) if body else {}
            body_serialized = json.dumps(body_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            encoded_data = body_serialized.encode('utf-8')
            logger.debug(f"Serialized body for signing: {body_serialized}")
            logger.debug(f"Body bytes (hex): {encoded_data.hex()}")
            private_key = load_pem_private_key(self.private_key_pem.encode(), password=None)
            signature = private_key.sign(
                encoded_data,
                padding.PKCS1v15(),
                hashes.SHA256()
            )
            signature_b64 = base64.b64encode(signature).decode('utf-8')
            logger.debug(f"Generated signature: {signature_b64}")
            return signature_b64
        except json.JSONDecodeError as e:
            logger.error(f"Failed to serialize JSON for signing: {e}")
            return ""
        except Exception as e:
            logger.error(f"Failed to sign request: {e}")
            return ""

    async def make_bunq_request(self, method: str, url: str, headers: dict, payload: dict = None, retries: int = 3, backoff: int = 5):
        """Make a bunq API request with retry logic for 429 errors."""
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":")) if payload else ""
        # Add this check:
        if not payload_json:
            logger.warning("Payload is empty, signature will be based on an empty string.")
    
        headers["X-Bunq-Client-Signature"] = self.sign_request(payload_json)
        logger.info(f"{method} payload: {payload_json}")
        logger.info(f"{method} signature: {headers['X-Bunq-Client-Signature']}")
        
        try:
            async with self.bunq_session.request(method, url, headers=headers, data=payload_json) as response:
                logger.info(f"Request to {url} returned status {response.status}")
                if response.status == 429 and retries > 0:
                    logger.warning(f"Rate limited. Retrying in {backoff} seconds...")
                    await asyncio.sleep(backoff)
                    return await self.make_bunq_request(method, url, headers, payload, retries - 1, backoff * 2)

                if response.status != 200:
                    error_message = await response.text()
                    logger.error(f"Request to {url} failed: {response.status} - {error_message}")
                    return None

                try:
                    data = await response.json()
                    return data
                except json.JSONDecodeError:
                    logger.error(f"Failed to decode JSON response from {url}")
                    return None

        except aiohttp.ClientError as e:
            logger.error(f"AIOHTTP error: {e}")
            return None
        except Exception as e:
            logger.error(f"An unexpected error occurred: {e}")
            return None


    async def initialize_bunq(self):
        """Create API context: installation, device-server, session-server."""
        if not BUNQ_API_KEY:
            logger.error("BUNQ_API_KEY not set in .env")
            return False

        headers = {
            "Content-Type": "application/json",
            "Cache-Control": "none",
            "User-Agent": "Sempy",
            "X-Bunq-Client-Request-Id": str(os.urandom(16).hex()),
            "X-Bunq-Language": "en_US",
            "X-Bunq-Region": "en_US",
            "X-Bunq-Geolocation": "0 0 0 0 000"
        }

        # Step 1: Installation
        payload = {"client_public_key": self.public_key_pem}
        data = await self.make_bunq_request("POST", f"{BUNQ_API_URL}/installation", headers, payload)
        if not data:
            return False
        self.installation_token = data["Response"][1]["Token"]["token"]
        logger.info("Installation successful")

        # Step 2: Device Server
        headers["X-Bunq-Client-Authentication"] = self.installation_token
        payload = {
            "description": "Sempy",
            "secret": BUNQ_API_KEY,
            "permitted_ips": ["*"]
        }
        data = await self.make_bunq_request("POST", f"{BUNQ_API_URL}/device-server", headers, payload)
        if not data:
            return False
        logger.info("Device server registered")

        # Step 3: Session Server
        headers["X-Bunq-Client-Authentication"] = self.installation_token
        payload = {"secret": BUNQ_API_KEY}
        data = await self.make_bunq_request("POST", f"{BUNQ_API_URL}/session-server", headers, payload)
        if not data:
            return False
        self.session_token = data["Response"][1]["Token"]["token"]
        self.user_id = data["Response"][2]["UserPerson"]["id"]
        self.session_expiry = datetime.utcnow() + timedelta(minutes=55)
        logger.info(f"Session created: user_id={self.user_id}")

        # Step 4: Fetch monetary account
        headers["X-Bunq-Client-Authentication"] = self.session_token
        try:
            async with self.bunq_session.get(
                f"{BUNQ_API_URL}/user/{self.user_id}/monetary-account",
                headers=headers
            ) as response:
                if response.status != 200:
                    logger.error(f"Failed to fetch monetary account: {response.status} {await response.text()}")
                    return False
                data = await response.json()
                accounts = [acc for acc in data["Response"] if acc["MonetaryAccountBank"]["status"] == "ACTIVE"]
                if not accounts:
                    logger.error("No active monetary account found")
                    return False
                self.monetary_account_id = accounts[0]["MonetaryAccountBank"]["id"]
                logger.info(f"Monetary account fetched: monetary_account_id={self.monetary_account_id}")
        except Exception as e:
            logger.error(f"Error fetching monetary account: {e}")
            return False

        return True

    async def refresh_session_if_needed(self):
        """Refresh session token if expired or near expiry."""
        if self.session_token and self.session_expiry and datetime.utcnow() < self.session_expiry:
            return True
        logger.info("Refreshing bunq session")
        return await self.initialize_bunq()

    def load_premium_config(self):
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
        with open(self.premium_file, "w") as f:
            json.dump(self.premium_config, f, indent=4)

    async def create_bunq_payment_request(self, user_id, server_id, guild_name):
        """Create a bunq.me payment request for €5."""
        if not await self.refresh_session_if_needed():
            logger.error("Failed to refresh bunq session.")
            return None

        headers = {
            "Content-Type": "application/json",
            "X-Bunq-Client-Authentication": self.session_token,
            "X-Bunq-Client-Request-Id": str(os.urandom(16).hex()),
            "X-Bunq-Language": "en_US",
            "X-Bunq-Region": "en_US",
            "X-Bunq-Geolocation": "0 0 0 0 000"
        }
        payload = {
            "amount_inquired": {
                "value": str(BUNQ_PAYMENT_AMOUNT),
                "currency": "EUR"
            },
            "description": f"Premium customization for {guild_name} (Server ID: {server_id})",
            "counterparty_alias": {
                "type": "EMAIL",
                "value": PAYMENT_EMAIL
            },
            "allow_bunqme": True  # Assuming you want to allow bunq.me
        }

        try:
            data = await self.make_bunq_request(
                "POST",
                f"{BUNQ_API_URL}/user/{self.user_id}/monetary-account/{self.monetary_account_id}/request-inquiry",
                headers,
                payload
            )

            if not data:
                logger.error("Failed to create bunq payment request: No data received.")
                return None

            if "Error" in data:
                logger.error(f"Failed to create bunq payment request: API Error - {data['Error']}")
                return None

            request_id = data["Response"][0]["Id"]["id"]
            payment_link = f"https://bunq.me/sempy/{BUNQ_PAYMENT_AMOUNT}?description=Premium+for+{server_id}&request_id={request_id}"
            logger.info(f"Generated payment link: {payment_link}")  # Log the generated link
            return payment_link

        except Exception as e:
            logger.error(f"An unexpected error occurred while creating payment request: {e}")
            return None

    async def setup_bunq_webhook(self):
        """Set up a bunq webhook for payment notifications."""
        if not await self.refresh_session_if_needed():
            logger.error("Failed to refresh session before setting up webhook.")
            return False

        headers = {
            "Content-Type": "application/json",
            "X-Bunq-Client-Authentication": self.session_token,
            "X-Bunq-Client-Request-Id": str(os.urandom(16).hex()),
            "X-Bunq-Language": "en_US",
            "X-Bunq-Region": "en_US",
            "X-Bunq-Geolocation": "0 0 0 0 000"
        }
        payload = {
            "url": WEBHOOK_URL,
            "category": "MUTATION"
        }
        try:
            data = await self.make_bunq_request("POST", f"{BUNQ_API_URL}/user/{self.user_id}/notification-filter-url", headers, payload)
            if not data:
                logger.error("Failed to setup bunq webhook: No data received.")
                return False

            if "Error" in data:
                logger.error(f"Failed to setup bunq webhook: API Error - {data['Error']}")
                return False

            logger.info("bunq webhook setup successful")
            return True
        except Exception as e:
            logger.error(f"An error occurred during webhook setup: {e}")
            return False

    premium = app_commands.Group(name="premium", description="Manage premium bot customization")

    @premium.command(name="set_nickname", description="Set the bot's nickname in this server")
    @is_server_owner()
    @is_premium_server()
    async def set_nickname(self, interaction: discord.Interaction, nickname: str):
        """
        Set the bot's nickname in the current server (premium only).
        :param nickname: The new nickname (max 32 characters)
        """
        await interaction.response.defer(ephemeral=True)
        if len(nickname) > 32:
            await interaction.followup.send(
                "Nickname must be 32 characters or less.", ephemeral=True
            )
            return

        try:
            await interaction.guild.me.edit(nick=nickname)
            await interaction.followup.send(
                f"Bot nickname set to '{nickname}' in this server.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.followup.send(
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
        await interaction.response.defer(ephemeral=True)
        if not attachment.content_type or not attachment.content_type.startswith("image/"):
            await interaction.followup.send(
                "Please upload a valid image (PNG or JPEG).", ephemeral=True
            )
            return

        try:
            image_bytes = await attachment.read()
            await interaction.guild.me.edit(avatar=image_bytes)
            await interaction.followup.send(
                "Bot profile picture updated in this server.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Failed to set profile picture. Ensure the bot has permission to change its avatar.", ephemeral=True
            )
        except discord.HTTPException:
            await interaction.followup.send(
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
        await interaction.response.defer(ephemeral=True)
        if not server_id.isdigit():
            await interaction.followup.send(
                "Please provide a valid numeric server ID.", ephemeral=True
            )
            return

        server_id = int(server_id)
        guild = self.bot.get_guild(server_id)
        if not guild:
            await interaction.followup.send(
                f"Cannot access server ID {server_id}. The bot is not in that server.", ephemeral=True
            )
            return

        try:
            member = await guild.fetch_member(interaction.user.id)
        except discord.NotFound:
            await interaction.followup.send(
                f"You are not a member of server {guild.name} (ID: {server_id}).", ephemeral=True
            )
            return
        except discord.Forbidden:
            await interaction.followup.send(
                f"Bot lacks permission to fetch members in server {guild.name} (ID: {server_id}).", ephemeral=True
            )
            return

        is_owner = member.id == guild.owner_id
        has_manage_guild = member.guild_permissions.manage_guild
        if not (is_owner or has_manage_guild):
            logger.warning(
                f"User {interaction.user.id} failed permission check for server {guild.id}: "
                f"Owner={is_owner}, Manage Guild={has_manage_guild}"
            )
            await interaction.followup.send(
                f"You must be the owner or have Manage Server permission in server {guild.name} (ID: {server_id}).", ephemeral=True
            )
            return

        smp_cog = self.bot.get_cog("ManageSMPServersCog")
        if not smp_cog or server_id not in smp_cog.config["smp_server_ids"]:
            await interaction.followup.send(
                f"Server ID {server_id} is not an SMP server. Add it with /smp add first.", ephemeral=True
            )
            return

        if len(description) > 500:
            await interaction.followup.send(
                "Description must be 500 characters or less.", ephemeral=True
            )
            return

        if any(app["server_id"] == server_id for app in self.premium_config["applications"]):
            await interaction.followup.send(
                f"An application for server ID {server_id} is already pending.", ephemeral=True
            )
            return

        self.premium_config["applications"].append({
            "server_id": server_id,
            "owner_name": interaction.user.name,  # Store username instead of ID
            "description": description
        })
        self.save_premium_config()
        await interaction.followup.send(
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
        await interaction.response.defer(ephemeral=True)
        if not server_id.isdigit():
            await interaction.followup.send(
                "Please provide a valid numeric server ID.", ephemeral=True
            )
            return

        server_id = int(server_id)
        action = action.lower()
        if action not in ["accept", "reject"]:
            await interaction.followup.send(
                "Action must be 'accept' or 'reject'.", ephemeral=True
            )
            return

        application = next((app for app in self.premium_config["applications"] if app["server_id"] == server_id), None)
        if not application:
            await interaction.followup.send(
                f"No application found for server ID {server_id}.", ephemeral=True
            )
            return

        guild = self.bot.get_guild(server_id)
        guild_name = guild.name if guild else "Unknown Server"
        if application:
            owner_name = application["owner_name"]  # Get username from application data
            # ... display the owner_name in the review message
            await interaction.followup.send(
                f"Application for server {guild_name} by {owner_name} has been {action}ed.", ephemeral=True
            )

        self.premium_config["applications"].remove(application)
        if action == "accept":
            if server_id not in self.premium_config["approved_servers"]:
                self.premium_config["approved_servers"].append(server_id)
            message = f"Your application for premium customization in {guild_name} has been accepted!"
        else:
            message = f"Your application for premium customization in {guild_name} has been rejected."

        self.save_premium_config()

        # Fetching user is not required to send a message
        # owner = await self.bot.fetch_user(owner_id) if owner_id else None
        # Instead of fetching the user, directly send the message
        # if owner:
        try:
            # Use user_id stored in application data
            owner = await self.bot.fetch_user(application["owner_id"])
            await owner.send(message)
        except discord.Forbidden:
            pass

    @premium.command(name="request_premium", description="Request premium features for a server")
    @is_server_owner()
    async def request_premium(self, interaction: discord.Interaction, server_id: str):
        """
        Request premium features by generating a payment link for €5.
        :param server_id: The server ID to unlock premium for (numeric)
        """
        await interaction.response.defer(ephemeral=True)
        if not server_id.isdigit():
            await interaction.followup.send(
                "Please provide a valid numeric server ID.", ephemeral=True
            )
            return

        server_id = int(server_id)
        guild = self.bot.get_guild(server_id)
        if not guild:
            await interaction.followup.send(
                f"Cannot access server ID {server_id}. The bot is not in that server.", ephemeral=True
            )
            return

        try:
            member = await guild.fetch_member(interaction.user.id)
        except discord.NotFound:
            await interaction.followup.send(
                f"You are not a member of server {guild.name} (ID: {server_id}).", ephemeral=True
            )
            return
        except discord.Forbidden:
            await interaction.followup.send(
                f"Bot lacks permission to fetch members in server {guild.name} (ID: {server_id}).", ephemeral=True
            )
            return

        is_owner = member.id == guild.owner_id
        has_manage_guild = member.guild_permissions.manage_guild
        if not (is_owner or has_manage_guild):
            logger.warning(
                f"User {interaction.user.id} failed permission check for server {guild.id}: "
                f"Owner={is_owner}, Manage Guild={has_manage_guild}"
            )
            await interaction.followup.send(
                f"You must be the owner or have Manage Server permission in server {guild.name} (ID: {server_id}).", ephemeral=True
            )
            return

        if server_id in (self.premium_config["paid_servers"] + self.premium_config["approved_servers"]):
            await interaction.followup.send(
                "This server already has premium features unlocked.", ephemeral=True
            )
            return

        payment_link = await self.create_bunq_payment_request(interaction.user.id, server_id, guild.name)
        if not payment_link:
            await interaction.followup.send(
                "Failed to generate payment link. Contact the bot admin.", ephemeral=True
            )
            return

        self.premium_config["pending_payments"][str(server_id)] = {
            "user_id": interaction.user.id,
            "amount": BUNQ_PAYMENT_AMOUNT,
            "guild_name": guild.name
        }
        self.save_premium_config()

        await interaction.followup.send(
            f"Please complete the €5 payment to unlock premium features for {guild.name}:\n{payment_link}\n"
            "You will be notified once the payment is confirmed. (This is a Sandbox link, no real payment needed.)",
            ephemeral=True
        )

    async def handle_bunq_webhook(self, payment_data):
        """Handle incoming bunq webhook for payment confirmation."""
        logger.info(f"Received webhook data: {payment_data}")  # Log the entire webhook data

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
        asyncio.create_task(self.bunq_session.close())

async def setup(bot):
    await bot.add_cog(PremiumCog(bot))