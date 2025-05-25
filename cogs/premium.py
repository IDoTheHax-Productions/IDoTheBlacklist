import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import aiohttp
import asyncio
from dotenv import load_dotenv
import logging
from datetime import datetime, timedelta
import stripe
from aiohttp import web

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PREMIUM_FILE = "settings/premium.json"
ALLOWED_ADMIN_IDS = [726721909374320640, 1362041490779672576]
STRIPE_API_KEY = os.getenv("STRIPE_API_KEY")  # Stripe sandbox API key
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")  # Stripe webhook secret
STRIPE_PAYMENT_AMOUNT = 500  # €5 in cents (~$5 USD)

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
        self.webhook_server = None
        self.webhook_port = 8000
        self.stripe = stripe
        stripe.api_key = STRIPE_API_KEY
        self.success_statuses = {}  # session_id -> (user_id, channel_id, status)

    async def start_webhook(self):
        """Start a small HTTP server to handle Stripe webhooks and result routes."""
        app = web.Application()
        app.router.add_post('/stripe-webhook', self.handle_stripe_webhook)
        app.router.add_get('/success', self.handle_success)
        app.router.add_get('/cancel', self.handle_cancel)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', self.webhook_port)
        await site.start()
        logger.info(f"Stripe webhook server running on port {self.webhook_port}")


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

    async def create_stripe_checkout_session(self, user_id, server_id, guild_name, channel_id):
        """Create a Stripe checkout session for €5."""
        try:
            session = self.stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=[{
                    'price_data': {
                        'currency': 'eur',
                        'product_data': {
                            'name': f'Premium Bot Features for {guild_name}',
                        },
                        'unit_amount': STRIPE_PAYMENT_AMOUNT,
                    },
                    'quantity': 1,
                }],
                mode='payment',
                success_url=f'https://your-ngrok-url.ngrok-free.app/success?session_id={{CHECKOUT_SESSION_ID}}',
                cancel_url=f'https://your-ngrok-url.ngrok-free.app/cancel?session_id={{CHECKOUT_SESSION_ID}}',
                metadata={
                    'user_id': str(user_id),
                    'server_id': str(server_id),
                    'guild_name': guild_name,
                    'channel_id': str(channel_id),
                }
            )
            self.premium_config["pending_payments"][session.id] = {
                "user_id": user_id,
                "server_id": server_id,
                "guild_name": guild_name,
                "amount": STRIPE_PAYMENT_AMOUNT / 100,  # euro
                "channel_id": channel_id
            }
            self.save_premium_config()
            return session.url
        except Exception as e:
            logger.error(f"Failed to create Stripe checkout session: {e}")
            return None

    async def handle_stripe_webhook(self, request):
        """Handle incoming Stripe webhook"""
        payload = await request.text()
        sig_header = request.headers.get('Stripe-Signature')

        try:
            event = stripe.Webhook.construct_event(
                payload,
                sig_header,
                STRIPE_WEBHOOK_SECRET
            )
        except ValueError as e:
            logger.error(f"Invalid payload: {e}")
            return web.Response(status=400)
        except stripe.error.SignatureVerificationError as e:
            logger.error(f"Invalid signature: {e}")
            return web.Response(status=400)

        if event['type'] == 'checkout.session.completed':
            session = event['data']['object']
            await self.process_payment_success(session)

        return web.Response(status=200)

    async def process_payment_success(self, session):
        """Process successful payment"""
        metadata = session.get('metadata', {})
        server_id = int(metadata.get('server_id'))
        user_id = int(metadata.get('user_id'))
        guild_name = metadata.get('guild_name', 'Unknown Server')

        if server_id not in self.premium_config["paid_servers"]:
            self.premium_config["paid_servers"].append(server_id)
            self.save_premium_config()

        # Notify user
        user = await self.bot.fetch_user(user_id)
        if user:
            try:
                await user.send(
                    f"🎉 Payment confirmed! Premium features unlocked for {guild_name}!\n"
                    "You can now use `/premium set_nickname` and `/premium set_pfp`."
                )
            except discord.Forbidden:
                logger.warning(f"Could not DM user {user_id}")

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
            "user_id": interaction.user.id,
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

        self.premium_config["applications"].remove(application)
        if action == "accept":
            if server_id not in self.premium_config["approved_servers"]:
                self.premium_config["approved_servers"].append(server_id)
            message = f"Your application for premium customization in {guild_name} has been accepted!"
        else:
            message = f"Your application for premium customization in {guild_name} has been rejected."

        self.save_premium_config()

        try:
            owner = await self.bot.fetch_user(application["user_id"])
            await owner.send(message)
        except discord.Forbidden:
            pass

        await interaction.followup.send(
            f"Application for server {guild_name} has been {action}ed.", ephemeral=True
        )

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

        payment_url = await self.create_stripe_checkout_session(
            interaction.user.id, server_id, guild.name, interaction.channel_id
        )
        if not payment_url:
            await interaction.followup.send(
                "Failed to generate payment link. Contact the bot admin.", ephemeral=True
            )
            return

        await interaction.followup.send(
            f"Please complete the €5 payment to unlock premium features for {guild.name}:\n{payment_url}\n"
            "You will be notified once the payment is confirmed.",
            ephemeral=True
        )

    async def handle_success(self, request):
        """Handle payment success redirect."""
        session_id = request.query.get('session_id')
        if not session_id:
            return web.Response(text="Missing session_id", status=400)
        pending = self.premium_config["pending_payments"].get(session_id)
        if not pending:
            return web.Response(text="Session not found or already processed.", status=404)
        user_id = pending["user_id"]
        channel_id = pending.get("channel_id")
        await self.notify_user(user_id, channel_id, True, pending["guild_name"])
        return web.Response(text="Payment successful! You will be notified in Discord.")

    async def handle_cancel(self, request):
        """Handle payment cancel redirect."""
        session_id = request.query.get('session_id')
        if not session_id:
            return web.Response(text="Missing session_id", status=400)
        pending = self.premium_config["pending_payments"].get(session_id)
        if not pending:
            return web.Response(text="Session not found or already processed.", status=404)
        user_id = pending["user_id"]
        channel_id = pending.get("channel_id")
        await self.notify_user(user_id, channel_id, False, pending["guild_name"])
        return web.Response(text="Payment cancelled. You can try again from Discord.")

    async def notify_user(self, user_id, channel_id, success, guild_name):
        """DM or ping user with payment status."""
        user = await self.bot.fetch_user(user_id)
        message = (
            f"✅ Payment successful! Premium features unlocked for {guild_name}."
            if success else
            f"❌ Payment was cancelled for {guild_name}."
        )
        # Try DM
        try:
            await user.send(message)
            return
        except Exception:
            pass  # DMs closed or otherwise failed
        # Try ping in channel if available
        if channel_id:
            channel = self.bot.get_channel(int(channel_id))
            if channel:
                try:
                    await channel.send(f"<@{user_id}> {message}")
                except Exception:
                    pass

    def cog_unload(self):
        asyncio.create_task(self.http_session.close())

async def setup(bot):
    await bot.add_cog(PremiumCog(bot))