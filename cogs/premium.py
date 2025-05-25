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
STRIPE_PAYMENT_AMOUNT = 500  # 5 in cents (~$5 USD)

def is_server_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            logger.error("is_server_owner: not in a guild")
            return False
        is_owner = interaction.user.id == interaction.guild.owner_id
        has_manage_guild = interaction.user.guild_permissions.manage_guild
        logger.info(f"is_server_owner: is_owner={is_owner}, has_manage_guild={has_manage_guild}")
        return is_owner or has_manage_guild
    return app_commands.check(predicate)

def is_premium_server():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            logger.error("is_premium_server: not in a guild")
            return False
        cog = interaction.client.get_cog("PremiumCog")
        if not cog:
            logger.error("is_premium_server: PremiumCog not found")
            return False
        logger.info(f"is_premium_server: guild_id={interaction.guild.id}, paid={cog.premium_config['paid_servers']}, approved={cog.premium_config['approved_servers']}")
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
        """Load premium configuration with better error handling."""
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.premium_file), exist_ok=True)
            
            # Check if file exists and create default if not
            if not os.path.exists(self.premium_file):
                logger.info(f"Premium config file doesn't exist, creating default at {self.premium_file}")
                default_config = {
                    "paid_servers": [],
                    "approved_servers": [],
                    "applications": [],
                    "pending_payments": {}
                }
                with open(self.premium_file, "w", encoding='utf-8') as f:
                    json.dump(default_config, f, indent=4)
                self.premium_config = default_config
                logger.info("Default premium config created successfully")
                return
            
            # Load existing file
            with open(self.premium_file, "r", encoding='utf-8') as f:
                self.premium_config = json.load(f)
            
            # Validate config structure
            required_keys = ["paid_servers", "approved_servers", "applications", "pending_payments"]
            for key in required_keys:
                if key not in self.premium_config:
                    logger.warning(f"Missing key '{key}' in premium config, adding default")
                    if key in ["paid_servers", "approved_servers", "applications"]:
                        self.premium_config[key] = []
                    else:  # pending_payments
                        self.premium_config[key] = {}
            
            logger.info("Premium config loaded successfully")
            
        except PermissionError as e:
            logger.error(f"Permission denied when accessing {self.premium_file}: {e}")
            raise
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in {self.premium_file}: {e}")
            # Backup corrupted file and create new one
            backup_file = f"{self.premium_file}.backup"
            try:
                os.rename(self.premium_file, backup_file)
                logger.info(f"Backed up corrupted file to {backup_file}")
            except Exception as backup_error:
                logger.error(f"Failed to backup corrupted file: {backup_error}")
            
            # Create new default config
            self.premium_config = {
                "paid_servers": [],
                "approved_servers": [],
                "applications": [],
                "pending_payments": {}
            }
            self.save_premium_config()
        except Exception as e:
            logger.error(f"Unexpected error loading premium config: {e}")
            raise

    def save_premium_config(self):
        """Save premium configuration with error handling."""
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.premium_file), exist_ok=True)
            
            # Write to temporary file first
            temp_file = f"{self.premium_file}.tmp"
            with open(temp_file, "w", encoding='utf-8') as f:
                json.dump(self.premium_config, f, indent=4, ensure_ascii=False)
            
            # Replace original file with temp file (atomic operation on most systems)
            os.replace(temp_file, self.premium_file)
            logger.info(f"Premium config saved successfully to {self.premium_file}")
            
        except PermissionError as e:
            logger.error(f"Permission denied when saving to {self.premium_file}: {e}")
            logger.error("Check file/directory permissions")
        except OSError as e:
            logger.error(f"OS error when saving premium config: {e}")
            logger.error("Check disk space and file system permissions")
        except Exception as e:
            logger.error(f"Unexpected error saving premium config: {e}")
            # Clean up temp file if it exists
            temp_file = f"{self.premium_file}.tmp"
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except:
                    pass

    async def create_stripe_checkout_session(self, user_id, server_id, guild_name, channel_id):
        """Create a Stripe checkout session for 5."""
        try:
            session = self.stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=[{
                    'price_data': {
                        'currency': 'gbp',
                        'product_data': {
                            'name': f'Premium Bot Features for {guild_name}',
                        },
                        'unit_amount': STRIPE_PAYMENT_AMOUNT,
                    },
                    'quantity': 1,
                }],
                mode='payment',
                success_url=f'https://9bbc-2a02-6ea0-c041-2254-00-12.ngrok-free.app/success?session_id={{CHECKOUT_SESSION_ID}}',
                cancel_url=f'https://9bbc-2a02-6ea0-c041-2254-00-12.ngrok-free.app/cancel?session_id={{CHECKOUT_SESSION_ID}}',
                metadata={
                    'user_id': str(user_id),
                    'server_id': str(server_id),
                    'guild_name': guild_name,
                    'channel_id': str(channel_id),
                }
            )
            
            # Save pending payment with error handling
            try:
                self.premium_config["pending_payments"][session.id] = {
                    "user_id": user_id,
                    "server_id": server_id,
                    "guild_name": guild_name,
                    "amount": STRIPE_PAYMENT_AMOUNT / 100,  # GBP (The Price Go up if it USD 🔥)
                    "channel_id": channel_id
                }
                self.save_premium_config()
                logger.info(f"Saved pending payment for session {session.id}")
            except Exception as save_error:
                logger.error(f"Failed to save pending payment: {save_error}")
                # Still return the URL, but log the error
            
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
        try:
            metadata = session.get('metadata', {})
            server_id = int(metadata.get('server_id'))
            user_id = int(metadata.get('user_id'))
            guild_name = metadata.get('guild_name', 'Unknown Server')
            session_id = session['id']  # Stripe session object always has 'id'

            # Add to paid_servers if not already present
            if server_id not in self.premium_config["paid_servers"]:
                self.premium_config["paid_servers"].append(server_id)
                logger.info(f"Added server {server_id} to paid_servers")

            # Remove from pending_payments if present
            if session_id in self.premium_config["pending_payments"]:
                del self.premium_config["pending_payments"][session_id]
                logger.info(f"Removed session {session_id} from pending_payments")

            # Save changes
            self.save_premium_config()

            # Notify user
            user = await self.bot.fetch_user(user_id)
            if user:
                try:
                    await user.send(
                        f"🎉 Payment confirmed! Premium features unlocked for {guild_name}!\n"
                        "You can now use `/premium set_nickname` and `/premium set_pfp`."
                    )
                    logger.info(f"Notified user {user_id} of successful payment")
                except discord.Forbidden:
                    logger.warning(f"Could not DM user {user_id}")
        except Exception as e:
            logger.error(f"Error processing payment success: {e}")

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

    @premium.command(name="set_pfp", description="Set the bot's global profile picture")
    @is_server_owner()
    @is_premium_server()
    async def set_pfp(self, interaction: discord.Interaction, attachment: discord.Attachment):
        """
        Set the bot's global profile picture (premium only).
        Note: Discord doesn't support server-specific avatars, this changes the bot's global avatar.
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
            # Change the bot's global avatar (not server-specific)
            await self.bot.user.edit(avatar=image_bytes)
            await interaction.followup.send(
                "Bot profile picture updated globally. Note: This affects the bot across all servers.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Failed to set profile picture. The bot may not have permission or you may be rate limited.", ephemeral=True
            )
        except discord.HTTPException as e:
            if e.status == 429:  # Rate limited
                await interaction.followup.send(
                    "Rate limited! You can only change the bot's avatar twice per hour. Please try again later.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    "Failed to process the image. Ensure it's a valid PNG or JPEG under 8MB.", ephemeral=True
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

        try:
            self.premium_config["applications"].append({
                "server_id": server_id,
                "user_id": interaction.user.id,
                "description": description
            })
            self.save_premium_config()
            await interaction.followup.send(
                f"Application submitted for server {guild.name}. You will be notified once reviewed.", ephemeral=True
            )
            logger.info(f"Application submitted for server {server_id} by user {interaction.user.id}")
        except Exception as e:
            logger.error(f"Failed to save application: {e}")
            await interaction.followup.send(
                "Failed to submit application. Please try again or contact an admin.", ephemeral=True
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

        try:
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
            logger.info(f"Application for server {server_id} {action}ed by admin {interaction.user.id}")
        except Exception as e:
            logger.error(f"Failed to process application review: {e}")
            await interaction.followup.send(
                "Failed to process application review. Please try again.", ephemeral=True
            )

    @premium.command(name="get_premium", description="Get premium features for a server")
    @is_server_owner()
    async def get_premium(self, interaction: discord.Interaction, server_id: str):
        """
        Get premium features by donating £5.
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
            f"Please complete the £5 payment to unlock premium features for {guild.name}:\n{payment_url}\n"
            "You will be notified once the payment is confirmed.",
            ephemeral=True
        )

    async def handle_success(self, request):
        session_id = request.query.get('session_id')
        if not session_id:
            return web.Response(text="Missing session_id", status=400)
        pending = self.premium_config["pending_payments"].get(session_id)
        if not pending:
            return web.Response(text="Session not found or already processed.", status=404)
        user_id = pending["user_id"]
        channel_id = pending.get("channel_id")
        await self.notify_user(user_id, channel_id, True, pending["guild_name"])
        # CLEANUP: Remove from pending_payments and save
        try:
            del self.premium_config["pending_payments"][session_id]
            self.save_premium_config()
        except Exception as e:
            logger.error(f"Failed to cleanup pending payment: {e}")
        return web.Response(text="Payment successful! You will be notified in Discord.")

    async def handle_cancel(self, request):
        session_id = request.query.get('session_id')
        if not session_id:
            return web.Response(text="Missing session_id", status=400)
        pending = self.premium_config["pending_payments"].get(session_id)
        if not pending:
            return web.Response(text="Session not found or already processed.", status=404)
        user_id = pending["user_id"]
        channel_id = pending.get("channel_id")
        await self.notify_user(user_id, channel_id, False, pending["guild_name"])
        # CLEANUP: Remove from pending_payments and save
        try:
            del self.premium_config["pending_payments"][session_id]
            self.save_premium_config()
        except Exception as e:
            logger.error(f"Failed to cleanup pending payment: {e}")
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

    @premium.command(name="debug_payment", description="Debug a pending payment (Admin only)")
    @is_admin_user()
    async def debug_payment(self, interaction: discord.Interaction, session_id: str):
        """
        Manually process a pending payment for debugging.
        :param session_id: The Stripe session ID from pending_payments
        """
        await interaction.response.defer(ephemeral=True)
        
        if session_id not in self.premium_config["pending_payments"]:
            await interaction.followup.send(
                f"Session ID `{session_id}` not found in pending payments.", ephemeral=True
            )
            return
        
        pending = self.premium_config["pending_payments"][session_id]
        server_id = pending["server_id"]
        user_id = pending["user_id"]
        guild_name = pending["guild_name"]
        
        # Simulate successful payment processing
        try:
            # Add to paid_servers if not already present
            if server_id not in self.premium_config["paid_servers"]:
                self.premium_config["paid_servers"].append(server_id)
                logger.info(f"DEBUG: Added server {server_id} to paid_servers")

            # Remove from pending_payments
            del self.premium_config["pending_payments"][session_id]
            logger.info(f"DEBUG: Removed session {session_id} from pending_payments")

            # Save changes
            self.save_premium_config()
            logger.info("DEBUG: Config saved successfully")

            # Notify user
            user = await self.bot.fetch_user(user_id)
            if user:
                try:
                    await user.send(
                        f"🎉 Payment manually processed! Premium features unlocked for {guild_name}!\n"
                        "You can now use `/premium set_nickname` and `/premium set_pfp`."
                    )
                    logger.info(f"DEBUG: Notified user {user_id}")
                except discord.Forbidden:
                    logger.warning(f"DEBUG: Could not DM user {user_id}")

            await interaction.followup.send(
                f"✅ Successfully processed payment for {guild_name} (Server ID: {server_id})", 
                ephemeral=True
            )
            
        except Exception as e:
            logger.error(f"DEBUG: Error processing payment: {e}")
            await interaction.followup.send(
                f"❌ Error processing payment: {str(e)}", ephemeral=True
            )

    @premium.command(name="check_webhook", description="Check webhook server status (Admin only)")
    @is_admin_user()
    async def check_webhook(self, interaction: discord.Interaction):
        """Check if the webhook server is running and accessible."""
        await interaction.response.defer(ephemeral=True)
        
        import aiohttp
        webhook_url = "https://9bbc-2a02-6ea0-c041-2254-00-12.ngrok-free.app/stripe-webhook"
        
        try:
            async with aiohttp.ClientSession() as session:
                # Try to access the webhook URL (this will fail but shows if it's reachable)
                async with session.get(webhook_url.replace('/stripe-webhook', '/success')) as response:
                    status = response.status
                    await interaction.followup.send(
                        f"✅ Webhook server is reachable. Status: {status}", ephemeral=True
                    )
        except Exception as e:
            await interaction.followup.send(
                f"❌ Webhook server not reachable: {str(e)}\n"
                f"URL: {webhook_url}", ephemeral=True
            )

    @premium.command(name="list_pending", description="List all pending payments (Admin only)")
    @is_admin_user()
    async def list_pending(self, interaction: discord.Interaction):
        """List all pending payments for debugging."""
        await interaction.response.defer(ephemeral=True)
        
        pending = self.premium_config["pending_payments"]
        if not pending:
            await interaction.followup.send("No pending payments.", ephemeral=True)
            return
        
        message = "**Pending Payments:**\n"
        for session_id, data in pending.items():
            guild = self.bot.get_guild(data["server_id"])
            guild_name = guild.name if guild else data["guild_name"]
            message += f"• `{session_id[:20]}...` - {guild_name} (Server: {data['server_id']}) - User: <@{data['user_id']}>\n"
        
        await interaction.followup.send(message, ephemeral=True)

    def cog_unload(self):
        # Note: http_session is not defined in your original code
        # Remove this line or define http_session if needed
        pass

async def setup(bot):
    premium_cog = PremiumCog(bot)
    await bot.add_cog(premium_cog)
    await premium_cog.start_webhook()