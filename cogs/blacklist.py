import discord
from discord import app_commands, ui
from discord.ext import commands
import aiohttp
import asyncio
import re
import json
import os
from dotenv import load_dotenv

load_dotenv()

# File to store pending blacklist requests
PENDING_FILE = "data/pending_blacklists.json"

# File to store the announcement channel ID
ANNOUNCEMENT_CHANNEL_FILE = "data/announcement_channel.json"

class ConfirmButton(ui.View):
    def __init__(self, cog, blacklist_data, message_id=None):
        super().__init__(timeout=None)  # No timeout for persistent views
        self.cog = cog
        self.blacklist_data = blacklist_data
        self.message_id = message_id  # Track the message this view is tied to

    @ui.button(label='Confirm Blacklist', style=discord.ButtonStyle.danger, custom_id="confirm_blacklist")
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)

        if interaction.user.id not in self.cog.AUTHORIZED_USERS:
            await interaction.followup.send("You are not authorized to confirm blacklist requests.", ephemeral=True)
            return

        user_id = self.blacklist_data['discord_user_id']
        username = self.blacklist_data['discord_username']
        reason = self.blacklist_data['reason']

        # First, send an initial status message
        await interaction.followup.send(f"Processing blacklist for {username} ({user_id})...", ephemeral=True)

        kicked_servers = []
        mutual_servers = []

        try:
            # Fetch the user object
            user = await self.cog.bot.fetch_user(int(user_id))

            # Log for debugging
            print(f"Processing blacklist for user {username} ({user_id})")

            # First gather all mutual servers to avoid race conditions
            # Force the bot to fetch members for all guilds to avoid caching issues
            for guild in self.cog.bot.guilds:
                try:
                    # Try to get the member from cache first
                    member = guild.get_member(int(user_id))

                    if not member:
                        # If not in cache, force fetch the member
                        try:
                            member = await guild.fetch_member(int(user_id))
                        except discord.NotFound:
                            # User is not in this guild
                            member = None
                        except discord.HTTPException as e:
                            print(f"HTTP error when fetching member in {guild.name}: {e}")

                    if member:
                        mutual_servers.append(guild)
                        print(f"Found mutual server: {guild.name}")

                except Exception as e:
                    print(f"Error checking membership in {guild.name}: {e}")

            # DM the owner
            for guild in mutual_servers:
                try:
                    member = guild.get_member(int(user_id))
                    if not member:
                        try:
                            member = await guild.fetch_member(int(user_id))
                        except discord.NotFound:
                            print(f"User {username} not found in {guild.name} (even after fetch), skipping")
                            continue
                        except Exception as e:
                            print(f"Error fetching member {username} in {guild.name}: {e}")
                            continue

                    if not member:
                        print(f"User {username} not found in {guild.name}, skipping")
                        continue

                    print(f"Processing guild: {guild}")

                    try:
                        owner_id = guild.owner_id
                        owner = await guild.fetch_member(owner_id)  # get guild owner
                        if owner is None:
                            print(f"ERROR: Owner is None for guild {guild.name} (ID: {guild.id})")
                            continue
                        print(f"Owner found: Name={owner.name}, ID={owner.id}")
                        dm_message = (
                            f"Hello {owner.display_name}, \n\n"
                            f"This user `{username}` (ID: {user_id}) has been blacklisted for the following reason: {reason}.\n"
                            f"Do you approve kicking them from your server `{guild.name}`?\n\n"
                            "Please reply with 'yes' or 'no'. You will be reminded within 24 hours, reminders will be sent."
                        )
                    except Exception as e:
                        print(f"Error getting owner or creating DM for guild {guild.name}: {e}")
                        continue
                    
                    try:
                        await owner.send(dm_message)

                        response = None
                        for _ in range(24):
                            def check(msg):
                                return (
                                    msg.author == owner
                                    and msg.channel.type == discord.ChannelType.private
                                    and msg.content.lower() in ['yes', 'no']
                                )

                            try:
                                response = await self.cog.bot.wait_for('message', timeout=3600, check=check)
                                break
                            except asyncio.TimeoutError:
                                await owner.send(
                                    f"Reminder: Please respond to the blacklist request for `{username}` in your server `{guild.name}`."
                                )

                        if not response:
                            # Timeout after 24 hours
                            await owner.send(f"No response received within 24 hours. `{username}` has not been kicked")
                        elif response.content.lower() == 'yes':
                            await owner.send(f"User `{username}` has been kicked from `{guild.name}`.")
                            await member.kick(reason=f"Blacklisted: {reason}")
                            kicked_servers.append(guild.name)
                        else:
                            await owner.send(f"User `{username}` will not be kicked from `{guild.name}`.")

                    except discord.Forbidden:
                        print(f"Missing permissions to DM owner in {guild.name}")
                    except Exception as e:
                        print(f"Error sending DM or waiting for response in {guild.name}: {e}")
                except Exception as e:
                    print(f"Error processing guild {guild.name}: {e}")
                                
            # Update the local blacklist database through the API
            try:
                async with aiohttp.ClientSession() as session:
                    headers = {"X-API-Key": self.cog.api_key}
                    payload = {
                        "discord_user_id": user_id,
                        "discord_username": username,
                        "reason": reason
                    }

                    if self.blacklist_data.get('minecraft_username'):
                        payload["minecraft_username"] = self.blacklist_data.get('minecraft_username')
                    if self.blacklist_data.get('minecraft_uuid'):
                        payload["minecraft_uuid"] = self.blacklist_data.get('minecraft_uuid')

                    async with session.post('http://localhost:5000/blacklist', json=payload, headers=headers) as response:
                        if response.status == 200:
                            print(f"Successfully added {username} to API blacklist")
                        else:
                            print(f"Failed to add to API blacklist: {response.status}")
            except Exception as e:
                print(f"API blacklist error: {e}")

            # Send announcement to the announcement channel
            announcement_channel_id = self.cog.get_announcement_channel()
            if announcement_channel_id:
                print("before try fetch")
                try:
                    channel = await self.cog.bot.fetch_channel(announcement_channel_id)  # Use fetch_channel to avoid cache issues
                    if channel:
                        print("after try fetch")
                        
                        # Create the embed using the new format
                        post_link = f"https://discord.com/channels/{self.cog.bot.guild.id}/{announcement_channel_id}/{self.message_id}"
                        embed = BlacklistEmbed.create_embed(user=username, reason=reason, banned_servers=kicked_servers, post_link=post_link)
                        view = BlacklistEmbed.create_view(post_link)

                        # Send the embed and view
                        await channel.send(embed=embed, view=view)
                        print(f"Sent blacklist announcement to channel {channel.name} (ID: {announcement_channel_id})")
                    else:
                        print(f"Error: Announcement channel with ID {announcement_channel_id} not found.")
                except discord.Forbidden:
                    print(f"Error: Bot lacks permission to send messages in announcement channel (ID: {announcement_channel_id})")
                except discord.NotFound:
                    print(f"Error: Announcement channel with ID {announcement_channel_id} does not exist.")
                except Exception as e:
                    print(f"Error sending announcement to channel ID {announcement_channel_id}: {e}")
            else:
                print("Error: Announcement channel not set.")
                
            # Notify the blacklisted user
            if mutual_servers:
                user_dm_message = f"Hello {user.display_name},\n\nYou have been blacklisted for the following reason: {reason}\n\n"
                if kicked_servers:
                    user_dm_message += "You have been kicked from the following servers:\n"
                    user_dm_message += "\n".join(kicked_servers)
                else:
                    user_dm_message += "The server owners have been notified of your blacklist status."

                try:
                    await user.send(user_dm_message)
                    print(f"Successfully sent DM to {user.display_name}")
                except discord.Forbidden:
                    print(f"User {user.display_name} has DMs disabled")
                except Exception as e:
                    print(f"Error sending DM: {e}")

        except Exception as e:
            print(f"Error processing user actions: {e}")
            await interaction.followup.send(f"Error processing blacklist: {str(e)}", ephemeral=True)
            return

        # Construct the updated message content
        kick_message = f"User {username} ({user_id}) has been blacklisted."
        if kicked_servers:
            kick_message += f"\n\nKicked from servers:\n" + "\n".join(kicked_servers)
        else:
            kick_message += f"\n\nNot kicked from any servers."

        # Update the original message
        try:
            await interaction.message.edit(content=kick_message, embed=None, view=None)
            self.cog.remove_pending_blacklist(self.message_id)
        except discord.NotFound:
            print(f"Original message {self.message_id} not found for editing.")
            await interaction.followup.send(kick_message, ephemeral=False)
        except discord.Forbidden:
            print(f"Bot lacks permission to edit message {self.message_id}.")
            await interaction.followup.send(kick_message, ephemeral=False)
        except Exception as e:
            print(f"Error updating message {self.message_id}: {e}")
            await interaction.followup.send(kick_message, ephemeral=False)

        await interaction.followup.send("Blacklist operation completed successfully.", ephemeral=True)
        self.stop()

    @ui.button(label='Cancel', style=discord.ButtonStyle.secondary, custom_id="cancel_blacklist")
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content="Blacklist action cancelled.", view=None)
        self.cog.remove_pending_blacklist(self.message_id)  # Remove from pending on cancel
        self.stop()

class BlacklistEmbed:
    @staticmethod
    def create_embed(user, reason, banned_servers, post_link):
        """
        Creates a Discord embed that matches the given design.

        Args:
            user (discord.User or str): The user being blacklisted. Can be a discord.User or a string (username).
            reason (str): The reason for the blacklist.
            banned_servers (list): List of banned servers.
            post_link (str): Link to the original post/thread.

        Returns:
            discord.Embed: The embed object.
        """
        embed = discord.Embed(
            title="Blacklist Accepted", 
            color=discord.Color.red()
        )

        # Handle if `user` is a string or `discord.User`
        if isinstance(user, discord.User):
            username = user.display_name
            user_id = user.id
        else:
            username = user  # Assume `user` is a string (username)
            user_id = "Unknown ID"

        # Add user and ID
        embed.add_field(
            name=f"{username}",
            value=f"`{user_id}`",
            inline=False
        )

        # Add Post link
        embed.add_field(
            name="Post:",
            value=f"[{username}](<{post_link}>)",
            inline=False
        )

        # Add Ban Reason
        embed.add_field(
            name="Ban Reason:",
            value=reason,
            inline=False
        )

        # Add Ban Check
        banned_servers_str = "\n".join([f"✅ {server}" for server in banned_servers])
        embed.add_field(
            name="Ban Check:",
            value=banned_servers_str,
            inline=False
        )

        return embed

    @staticmethod
    def create_view(post_link):
        """
        Creates a View with a button linking to the post.

        Args:
            post_link (str): Link to the original post/thread.

        Returns:
            discord.ui.View: A view containing buttons.
        """
        button = Button(
            label="Go To Post", 
            url=post_link, 
            style=discord.ButtonStyle.link
        )

        view = View()
        view.add_item(button)

        return view
class Blacklist(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.AUTHORIZED_USERS = [1362041490779672576, 1088268266499231764, 726721909374320640, 710863981039845467, 1151136371164065904]
        # Load the API key from the environment variable
        self.api_key = os.getenv("API_KEY", "unset")
        self.bot.add_view(ConfirmButton(self, {}, None))
        self.load_pending_blacklists()
        self.announcement_channel_id = self.load_announcement_channel()

        # Create data directory if it doesn't exist
        os.makedirs("data", exist_ok=True)

    def load_announcement_channel(self):
        """Load the announcement channel ID from file."""
        if os.path.exists(ANNOUNCEMENT_CHANNEL_FILE):
            try:
                with open(ANNOUNCEMENT_CHANNEL_FILE, 'r') as f:
                    data = json.load(f)
                    channel_id = data.get('channel_id')
                    print(f"Loaded announcement channel ID: {channel_id}")
                    return channel_id
            except (json.JSONDecodeError, FileNotFoundError) as e:
                print(f"Error loading announcement channel: {e}")
                return None
        print(f"Announcement channel file not found: {ANNOUNCEMENT_CHANNEL_FILE}")
        return None

    def save_announcement_channel(self, channel_id):
        """Save the announcement channel ID to file."""
        try:
            with open(ANNOUNCEMENT_CHANNEL_FILE, 'w') as f:
                json.dump({'channel_id': channel_id}, f)
            self.announcement_channel_id = channel_id
        except Exception as e:
            print(f"Error saving announcement channel: {e}")

    def get_announcement_channel(self):
        """Get the announcement channel ID."""
        return self.announcement_channel_id

    def load_pending_blacklists(self):
        """Load pending blacklist requests from file."""
        if os.path.exists(PENDING_FILE):
            try:
                with open(PENDING_FILE, 'r') as f:
                    pending = json.load(f)
                    for message_id, data in pending.items():
                        view = ConfirmButton(self, data, message_id)
                        self.bot.add_view(view)  # Reattach view for each pending request
                print(f"Loaded {len(pending)} pending blacklist requests.")
            except (json.JSONDecodeError, FileNotFoundError):
                print("Error loading pending blacklists: Invalid JSON or file not found")
                return
        else:
            print(f"Pending blacklist file not found: {PENDING_FILE}")

    def save_pending_blacklist(self, message_id, blacklist_data):
        """Save a pending blacklist request to file."""
        try:
            with open(PENDING_FILE, 'r') as f:
                pending = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            pending = {}
        pending[message_id] = blacklist_data
        try:
            with open(PENDING_FILE, 'w') as f:
                json.dump(pending, f)
        except Exception as e:
            print(f"Error saving pending blacklist: {e}")

    def remove_pending_blacklist(self, message_id):
        """Remove a pending blacklist request from file."""
        try:
            with open(PENDING_FILE, 'r') as f:
                pending = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return
        if message_id in pending:
            del pending[message_id]
            try:
                with open(PENDING_FILE, 'w') as f:
                    json.dump(pending, f)
            except Exception as e:
                print(f"Error removing pending blacklist: {e}")

    async def fetch_minecraft_uuid(self, username):
        url = f'https://api.mojang.com/users/profiles/minecraft/{username}'
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get('id')
                return None

    def get_correct_format_embed(self):
        embed = discord.Embed(title="Correct Blacklist Request Format", color=discord.Color.blue())
        embed.description = "Please use the following format in your thread description:"
        format_text = """
Discord username:
Discord user ID:
Minecraft username (if applicable):
Minecraft UUID (if applicable):
Reason:"""
        embed.add_field(name="Format", value=f"```" + format_text + "```", inline=False)
        example = """
Discord username: JohnDoe#1234
Discord user ID: 123456789012345678
Minecraft username: JohnDoe123
Minecraft UUID: 550e8400-e29b-41d4-a716-446655440000
Reason: Griefing and using hacks"""
        embed.add_field(name="Example", value=f"```" + example + "```", inline=False)
        return embed

    @commands.Cog.listener()
    async def on_member_join(self, member):
        async with aiohttp.ClientSession() as session:
            headers = {"X-API-Key": getattr(self, 'api_key', 'unset')}
            async with session.get(f'http://localhost:5000/check_blacklist/{member.id}', headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if data:
                        reason = data.get('reason', 'No reason provided')
                        await member.ban(reason=f"Blacklisted: {reason}")
                else:
                    print(f"Failed to check blacklist for {member.id}: {response.status}")

    @commands.Cog.listener()
    async def on_thread_create(self, thread):
        if isinstance(thread.parent, discord.ForumChannel):
            await asyncio.sleep(1)
            try:
                starter_message = await thread.fetch_message(thread.id)
                blacklist_channel_ids = [1345490362981945376, 1343190729597653023]
                if thread.parent.id not in blacklist_channel_ids:
                    return

                blacklist_data = await self.parse_blacklist_request(starter_message.content)
                if not blacklist_data:
                    correct_format_embed = self.get_correct_format_embed()
                    await thread.send(embed=correct_format_embed)
                    return

                embed = discord.Embed(title="Blacklist Application", color=discord.Color.orange())
                embed.add_field(name="Discord Username", value=blacklist_data['discord_username'], inline=False)
                embed.add_field(name="Discord User ID", value=blacklist_data['discord_user_id'], inline=False)
                embed.add_field(name="Reason", value=blacklist_data['reason'], inline=False)
                if blacklist_data.get('minecraft_username'):
                    embed.add_field(name="Minecraft Username", value=blacklist_data['minecraft_username'], inline=False)
                if blacklist_data.get('minecraft_uuid'):
                    embed.add_field(name="Minecraft UUID", value=blacklist_data['minecraft_uuid'], inline=False)

                view = ConfirmButton(self, blacklist_data)
                message = await thread.send(embed=embed, view=view)
                self.save_pending_blacklist(str(message.id), blacklist_data)  # Save pending request
                view.message_id = str(message.id)  # Assign message ID to view
            except discord.NotFound:
                print(f"Could not find starter message for thread {thread.id}")
            except Exception as e:
                print(f"Error processing thread {thread.id}: {e}")

    async def parse_blacklist_request(self, content):
        data = {}
        discord_username_pattern = r"Discord username:\s*([^\n]+)"
        discord_id_pattern = r"Discord user ID:\s*(\d+)"
        minecraft_username_pattern = r"Minecraft username(?:\s*\(if applicable\))?:\s*([^\n]+)"
        minecraft_uuid_pattern = r"Minecraft UUID(?:\s*\(if applicable\))?:\s*([^\n]+)"
        reason_pattern = r"Reason:\s*([\s\S]+)$"

        username_match = re.search(discord_username_pattern, content, re.IGNORECASE)
        id_match = re.search(discord_id_pattern, content, re.IGNORECASE)
        mc_username_match = re.search(minecraft_username_pattern, content, re.IGNORECASE)
        mc_uuid_match = re.search(minecraft_uuid_pattern, content, re.IGNORECASE)
        reason_match = re.search(reason_pattern, content, re.IGNORECASE)

        if username_match:
            data['discord_username'] = username_match.group(1).strip()
        if id_match:
            data['discord_user_id'] = id_match.group(1).strip()
        if mc_username_match:
            data['minecraft_username'] = mc_username_match.group(1).strip()
        if mc_uuid_match:
            data['minecraft_uuid'] = mc_uuid_match.group(1).strip()
        if reason_match:
            data['reason'] = reason_match.group(1).strip()

        if 'minecraft_username' in data and 'minecraft_uuid' not in data:
            minecraft_username = data['minecraft_username']
            minecraft_uuid = await self.fetch_minecraft_uuid(minecraft_username)
            if minecraft_uuid:
                data['minecraft_uuid'] = minecraft_uuid

        if 'discord_username' in data and 'discord_user_id' in data and 'reason' in data:
            return data
        return None

    @app_commands.command(name="set_api_key", description="Set the API key for blacklist operations (owner only)")
    @commands.is_owner()
    async def set_api_key(self, interaction: discord.Interaction, key: str):
        await interaction.response.defer(ephemeral=True)
        # Update the environment variable (optional, only if you want to persist it this way)
        os.environ["API_KEY"] = key
        self.api_key = key  # Update the cog's instance variable
        await interaction.followup.send("API key updated successfully.", ephemeral=True)

    @app_commands.command(name="test_blacklist_api", description="Test the blacklist API connection")
    @commands.check(lambda ctx: ctx.author.id in [987323487343493191, 726721909374320640])
    async def test_blacklist_api(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        # Log the API key being used
        print(f"API Key being used: {getattr(self, 'api_key', 'unset')}")
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"X-API-Key": getattr(self, 'api_key', 'unset')}
                async with session.get('http://localhost:5000/check_blacklist/test', headers=headers) as response:
                    status = response.status
                    response_text = await response.text()
                    await interaction.followup.send(f"API connection test:\nStatus: {status}\nResponse: {response_text[:1000]}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"API connection test failed: {str(e)}", ephemeral=True)

    @app_commands.command(name="sync_commands", description="Sync bot commands with Discord (owner only)")
    @commands.is_owner()
    async def sync_commands(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.bot.tree.sync()
            await interaction.followup.send("Commands synced successfully.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed to sync commands: {str(e)}", ephemeral=True)

    @app_commands.command(name="test_announcment_channel", description="Test announcement")
    async def test_channel(self, ctx):
        """
        A command to test if the announcement channel is set up correctly
        and the bot can send messages to it.
        """
        channel_id = self.load_announcement_channel()
        if channel_id:
            channel = self.bot.get_channel(channel_id)
            if channel:
                try:
                    await channel.send("Test message: Bot is working and can send messages to this channel!")
                    await ctx.send(f"Test message sent to channel {channel.mention}!")
                except discord.Forbidden:
                    await ctx.send("I do not have permissions to send messages to the announcement channel.")
                except Exception as e:
                    await ctx.send(f"An error occurred: {e}")
            else:
                await ctx.send("Could not find the announcement channel. Please check the ID.")
        else:
            await ctx.send("Announcement channel is not set. Please set it using the set_channel command.")

    @app_commands.command(name="remove_from_blacklist", description="Remove a user from the blacklist by a specific field")
    @commands.check(lambda ctx: ctx.author.id in [987323487343493191, 1088268266499231764, 726721909374320640, 710863981039845467, 1151136371164065904])
    async def remove_from_blacklist(self, interaction: discord.Interaction, identifier: str, field: str = "user_id"):
        await interaction.response.defer(ephemeral=True)

        if field not in ["user_id", "minecraft_uuid"]:
            await interaction.followup.send("Invalid field. Use 'user_id' or 'minecraft_uuid'.", ephemeral=True)
            return

        payload = {
            "identifier": identifier,
            "field": field
        }

        print(f"Sending remove blacklist payload: {payload}")

        try:
            async with aiohttp.ClientSession() as session:
                headers = {"X-API-Key": getattr(self, 'api_key', 'unset')}
                async with session.post('http://localhost:5000/blacklist/remove', json=payload, headers=headers) as response:
                    response_text = await response.text()
                    if response.status == 200:
                        await interaction.followup.send(f"Successfully removed user with {field}={identifier} from blacklist.", ephemeral=True)
                    else:
                        print(f"API Error: {response.status} - {response_text}")
                        await interaction.followup.send(f"Failed to remove from blacklist. API returned: {response.status} - {response_text}", ephemeral=True)
        except Exception as e:
            print(f"API request error: {e}")
            await interaction.followup.send(f"Failed to connect to blacklist API: {str(e)}", ephemeral=True)

    @app_commands.command(name="set_announcement_channel", description="Set the channel for blacklist announcements (admin only)")
    @app_commands.default_permissions(administrator=True)
    async def set_announcement_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        try:
            # Test sending a message to verify permissions
            test_message = await channel.send("Testing announcement channel permissions...")
            await test_message.delete()  # Delete the test message
            self.save_announcement_channel(channel.id)
            await interaction.followup.send(f"Blacklist announcements will be sent to {channel.mention}", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send(f"Cannot set {channel.mention} as announcement channel: Bot lacks permission to send messages.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Error setting announcement channel: {str(e)}", ephemeral=True)

    @commands.command(name="test_blacklist_announcement")
    async def test_blacklist_announcement(self, ctx, user_id: int):
        """
        Test the blacklist announcement functionality.

        Parameters:
        - user_id (int): The ID of the blacklisted user to test.
        """
        # Fetch blacklisted user data
        try:
            user = await self.bot.fetch_user(user_id)
            if not user:
                await ctx.send(f"User with ID {user_id} could not be found.")
                return
        except Exception as e:
            await ctx.send(f"Error fetching user: {e}")
            return

        # Example data (replace with actual log retrieval)
        reason = (
            "Stealing Plugins and trading the stolen plugins for other plugins\n\n"
            "He used to be known as Dex/D3xyt but he rebranded to a new name which is his old friend's name\n\n"
            "Saying the n word"
        )
        kicked_servers = ["Immortal SMP", "Blitz SMP", "Marine SMP"]
        post_link = f"https://discord.com/channels/{ctx.guild.id}/{ctx.channel.id}/123456789012345678"  # Replace with actual message ID

        # Prepare and send the announcement
        announcement_channel_id = self.get_announcement_channel()
        if not announcement_channel_id:
            await ctx.send("Announcement channel is not set. Please set it before testing.")
            return

        try:
            channel = await self.bot.fetch_channel(announcement_channel_id)  # Fetch the announcement channel
            if channel:
                embed = BlacklistEmbed.create_embed(user=user, reason=reason, banned_servers=kicked_servers, post_link=post_link)
                view = BlacklistEmbed.create_view(post_link)
                await channel.send(embed=embed, view=view)
                await ctx.send(f"Test announcement sent to {channel.mention}.")
            else:
                await ctx.send(f"Announcement channel with ID {announcement_channel_id} not found.")
        except discord.Forbidden:
            await ctx.send(f"Bot lacks permission to send messages in the announcement channel.")
        except Exception as e:
            await ctx.send(f"Error sending test announcement: {e}")

async def setup(bot):
    await bot.add_cog(Blacklist(bot))
