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
        owner_responses = {}  # To track the responses from owners

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
            
            # Continue even if no mutual servers are found
            if not mutual_servers:
                await interaction.followup.send(f"Warning: User {username} ({user_id}) could not be found in any mutual servers. Continuing with blacklist process anyway.", ephemeral=True)
            
            for guild in mutual_servers:
                member = guild.get_member(int(user_id))
                if not member:
                    continue  # Skip if user is no longer in the server
                
                owner = guild.owner  # Get the guild owner
                if not owner:
                    print(f"Could not find owner for guild {guild.name}")
                    continue
                
                # DM the guild owner for approval
                dm_message = (
                    f"Hello {owner.display_name},\n\n"
                    f"The user `{username}` (ID: {user_id}) has been flagged for blacklisting for the following reason:\n"
                    f"`{reason}`\n\n"
                    f"Do you approve kicking them from your server `{guild.name}`?\n\n"
                    "**Please reply with 'yes' or 'no'.**"
                )

                try:
                    # Create a custom button view for the DM
                    class DmResponseView(ui.View):
                        def __init__(self):
                            super().__init__(timeout=86400)  # 24 hour timeout
                            self.response = None
                            
                        @ui.button(label='Yes, kick user', style=discord.ButtonStyle.danger)
                        async def yes_button(self, dm_interaction: discord.Interaction, dm_button: ui.Button):
                            self.response = "yes"
                            await dm_interaction.response.edit_message(content=f"Thank you for your response. User `{username}` will be kicked from `{guild.name}`.")
                            self.stop()
                            
                        @ui.button(label='No, do not kick', style=discord.ButtonStyle.secondary)
                        async def no_button(self, dm_interaction: discord.Interaction, dm_button: ui.Button):
                            self.response = "no"
                            await dm_interaction.response.edit_message(content=f"Thank you for your response. User `{username}` will **not** be kicked from `{guild.name}`.")
                            self.stop()
                    
                    # Create the view
                    view = DmResponseView()
                    
                    # Send initial DM
                    dm = await owner.send(dm_message, view=view)
                    
                    # Wait for button press
                    await view.wait()
                    if view.response:
                        response = view.response
                    else:
                        # If timed out, send a follow-up message
                        await owner.send(f"No response received within 24 hours. `{username}` has not been kicked from `{guild.name}`.")
                        owner_responses[guild.id] = "timeout"
                        continue
                    
                    # Process response
                    if response == "yes":
                        try:
                            # Attempt to kick the user
                            await member.kick(reason=f"Blacklisted: {reason}")
                            kicked_servers.append(guild.name)
                            await owner.send(f"User `{username}` has been successfully kicked from `{guild.name}`.")
                            owner_responses[guild.id] = "approved"
                        except discord.Forbidden:
                            await owner.send(f"I don't have permission to kick `{username}` from `{guild.name}`. Please kick them manually.")
                            owner_responses[guild.id] = "permission_error"
                        except Exception as e:
                            await owner.send(f"Error kicking `{username}` from `{guild.name}`: {e}")
                            owner_responses[guild.id] = "error"
                    else:
                        await owner.send(f"User `{username}` will not be kicked from `{guild.name}`.")
                        owner_responses[guild.id] = "denied"

                except discord.Forbidden:
                    print(f"Could not DM the owner of {guild.name}.")
                    owner_responses[guild.id] = "dm_blocked"
                except Exception as e:
                    print(f"Error sending DM to owner of {guild.name}: {e}")
                    owner_responses[guild.id] = f"error: {str(e)}"

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
                        
                    async with session.post('http://localhost:5000/blacklist/add', json=payload, headers=headers) as response:
                        if response.status == 200:
                            print(f"Successfully added {username} to API blacklist")
                        else:
                            print(f"Failed to add to API blacklist: {response.status}")
            except Exception as e:
                print(f"API blacklist error: {e}")

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

        if mutual_servers:
            kick_message += f"\n\nMutual servers: {len(mutual_servers)}"
        
        # Add owner response summary
        if owner_responses:
            kick_message += "\n\nServer owner responses:"
            for guild_id, response in owner_responses.items():
                guild = self.cog.bot.get_guild(guild_id)
                if guild:
                    kick_message += f"\n- {guild.name}: {response}"

        if self.blacklist_data.get('minecraft_username'):
            kick_message += f"\nMinecraft Username: {self.blacklist_data.get('minecraft_username')}"
        if self.blacklist_data.get('minecraft_uuid'):
            kick_message += f"\nMinecraft UUID: {self.blacklist_data.get('minecraft_uuid')}"

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

class Blacklist(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.AUTHORIZED_USERS = [1362041490779672576, 1088268266499231764, 726721909374320640, 710863981039845467, 1151136371164065904]
        # Load the API key from the environment variable
        self.api_key = os.getenv("API_KEY", "unset")
        self.bot.add_view(ConfirmButton(self, {}, None))
        self.load_pending_blacklists()
        
        # Create data directory if it doesn't exist
        os.makedirs("data", exist_ok=True)

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
            except json.JSONDecodeError:
                print("Error loading pending blacklists: Invalid JSON")
                with open(PENDING_FILE, 'w') as f:
                    json.dump({}, f)
            except Exception as e:
                print(f"Error loading pending blacklists: {e}")
                with open(PENDING_FILE, 'w') as f:
                    json.dump({}, f)
        else:
            with open(PENDING_FILE, 'w') as f:
                json.dump({}, f)
    
    def save_pending_blacklist(self, message_id, blacklist_data):
        """Save a pending blacklist request to file."""
        try:
            with open(PENDING_FILE, 'r') as f:
                pending = json.load(f)
            pending[message_id] = blacklist_data
            with open(PENDING_FILE, 'w') as f:
                json.dump(pending, f)
        except Exception as e:
            print(f"Error saving pending blacklist: {e}")
    
    def remove_pending_blacklist(self, message_id):
        """Remove a pending blacklist request from file."""
        try:
            with open(PENDING_FILE, 'r') as f:
                pending = json.load(f)
            if message_id in pending:
                del pending[message_id]
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
        # Print log message for debugging
        print(f"New member joined: {member.name} ({member.id}) in server {member.guild.name}")
        
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"X-API-Key": getattr(self, 'api_key', 'unset')}
                api_url = f'http://localhost:5000/check_blacklist/{member.id}'
                
                # Debug log
                print(f"Checking blacklist API: {api_url}")
                
                async with session.get(api_url, headers=headers) as response:
                    print(f"API response status: {response.status}")
                    
                    if response.status == 200:
                        response_text = await response.text()
                        print(f"API response: {response_text}")
                        
                        try:
                            data = await response.json()
                            if data:
                                reason = data.get('reason', 'No reason provided')
                                print(f"User {member.name} is blacklisted for: {reason}")
                                
                                try:
                                    await member.ban(reason=f"Blacklisted: {reason}")
                                    print(f"Banned blacklisted user {member.name} from {member.guild.name}")
                                    
                                    # Log to a logging channel if one is set
                                    log_channel_id = getattr(self, 'log_channel_id', None)
                                    if log_channel_id:
                                        log_channel = self.bot.get_channel(log_channel_id)
                                        if log_channel:
                                            await log_channel.send(f"📢 Banned blacklisted user {member.mention} ({member.id}) from {member.guild.name} for: {reason}")
                                except discord.Forbidden:
                                    print(f"No permission to ban {member.name} from {member.guild.name}")
                                    
                                    # Try to kick if ban fails
                                    try:
                                        await member.kick(reason=f"Blacklisted: {reason}")
                                        print(f"Kicked blacklisted user {member.name} from {member.guild.name}")
                                    except discord.Forbidden:
                                        print(f"No permission to kick {member.name} from {member.guild.name}")
                                    except Exception as e:
                                        print(f"Error kicking {member.name}: {e}")
                                except Exception as e:
                                    print(f"Error banning {member.name}: {e}")
                        except json.JSONDecodeError:
                            print(f"Failed to parse API response: {response_text}")
                    else:
                        print(f"Failed to check blacklist for {member.id}: {response.status}")
        except Exception as e:
            print(f"Error checking blacklist for new member {member.id}: {e}")

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
    @app_commands.default_permissions(administrator=True)
    async def set_api_key(self, interaction: discord.Interaction, key: str):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id not in self.AUTHORIZED_USERS:
            await interaction.followup.send("You are not authorized to use this command.", ephemeral=True)
            return
            
        # Update the environment variable (optional, only if you want to persist it this way)
        os.environ["API_KEY"] = key
        self.api_key = key  # Update the cog's instance variable
        await interaction.followup.send("API key updated successfully.", ephemeral=True)

    @app_commands.command(name="set_log_channel", description="Set the channel for blacklist logs")
    @app_commands.default_permissions(administrator=True)
    async def set_log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id not in self.AUTHORIZED_USERS:
            await interaction.followup.send("You are not authorized to use this command.", ephemeral=True)
            return
            
        self.log_channel_id = channel.id
        await interaction.followup.send(f"Log channel set to {channel.mention}", ephemeral=True)

    @app_commands.command(name="test_blacklist_api", description="Test the blacklist API connection")
    @app_commands.default_permissions(administrator=True)
    async def test_blacklist_api(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id not in self.AUTHORIZED_USERS:
            await interaction.followup.send("You are not authorized to use this command.", ephemeral=True)
            return
            
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
    @app_commands.default_permissions(administrator=True)
    async def sync_commands(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id not in self.AUTHORIZED_USERS:
            await interaction.followup.send("You are not authorized to use this command.", ephemeral=True)
            return
            
        try:
            await self.bot.tree.sync()
            await interaction.followup.send("Commands synced successfully.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Failed to sync commands: {str(e)}", ephemeral=True)

    @app_commands.command(name="remove_from_blacklist", description="Remove a user from the blacklist by a specific field")
    @app_commands.default_permissions(administrator=True)
    async def remove_from_blacklist(self, interaction: discord.Interaction, identifier: str, field: str = "user_id"):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id not in self.AUTHORIZED_USERS:
            await interaction.followup.send("You are not authorized to use this command.", ephemeral=True)
            return

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

    @app_commands.command(name="force_blacklist", description="Immediately blacklist a user without server owner approval")
    @app_commands.default_permissions(administrator=True)
    async def force_blacklist(self, interaction: discord.Interaction, user_id: str, reason: str, kick_from_servers: bool = True):
        await interaction.response.defer(ephemeral=True)
        if interaction.user.id not in self.AUTHORIZED_USERS:
            await interaction.followup.send("You are not authorized to use this command.", ephemeral=True)
            return
            
        try:
            # Validate user_id format
            if not user_id.isdigit():
                await interaction.followup.send("User ID must be numeric.", ephemeral=True)
                return
                
            user_id = int(user_id)
            
            # Try to fetch user
            try:
                user = await self.bot.fetch_user(user_id)
                username = user.name
            except discord.NotFound:
                username = f"Unknown User {user_id}"
            except Exception as e:
                await interaction.followup.send(f"Error fetching user: {e}", ephemeral=True)
                return
                
            # Add to API blacklist
            try:
                async with aiohttp.ClientSession() as session:
                    headers = {"X-API-Key": getattr(self, 'api_key', 'unset')}
                    payload = {
                        "discord_user_id": str(user_id),
                        "discord_username": username,
                        "reason": reason
                    }
                    
                    async with session.post('http://localhost:5000/blacklist/add', json=payload, headers=headers) as response:
                        if response.status != 200:
                            await interaction.followup.send(f"Warning: Failed to add to API blacklist database. Status: {response.status}", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"Warning: Failed to connect to blacklist API: {e}", ephemeral=True)
                
            # Kick from servers if requested
            kicked_servers = []
            if kick_from_servers:
                for guild in self.bot.guilds:
                    try:
                        # First try to get from cache
                        member = guild.get_member(user_id)
                        
                        if not member:
                            # If not in cache, try to fetch the member
                            try:
                                member = await guild.fetch_member(user_id)
                            except discord.NotFound:
                                # User is not in this guild
                                member = None
                            except discord.HTTPException:
                                # API error, skip this guild
                                continue
                        
                        if member:
                            try:
                                await member.kick(reason=f"Force blacklisted: {reason}")
                                kicked_servers.append(guild.name)
                                print(f"Kicked {username} from {guild.name}")
                            except discord.Forbidden:
                                print(f"No permission to kick {username} from {guild.name}")
                            except Exception as e:
                                print(f"Error kicking {username} from {guild.name}: {e}")
                    except Exception as e:
                        print(f"Error processing guild {guild.name}: {e}")
            
            # Prepare response message
            response = f"User {username} ({user_id}) has been blacklisted for: {reason}"
            if kicked_servers:
                response += f"\n\nKicked from servers:\n" + "\n".join(kicked_servers)
            else:
                response += "\n\nNot kicked from any servers."
                
            await interaction.followup.send(response, ephemeral=True)
            
        except Exception as e:
            await interaction.followup.send(f"Error processing force blacklist: {e}", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Blacklist(bot))