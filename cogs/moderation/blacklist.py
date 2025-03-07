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

class ConfirmButton(ui.View):
    def __init__(self, cog, blacklist_data):
        super().__init__()
        self.cog = cog
        self.blacklist_data = blacklist_data

    @ui.button(label='Confirm Blacklist', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)

        if interaction.user.id not in self.cog.AUTHORIZED_USERS:
            await interaction.followup.send("You are not authorized to confirm blacklist requests.", ephemeral=True)
            return

        user_id = self.blacklist_data['discord_user_id']
        username = self.blacklist_data['discord_username']
        reason = self.blacklist_data['reason']

        payload = {
            'auth_id': str(interaction.user.id),
            'user_id': user_id,
            'display_name': username,
            'reason': reason
        }

        mc_info = {}
        if self.blacklist_data.get('minecraft_username'):
            mc_info['minecraft_username'] = self.blacklist_data['minecraft_username']
            if not self.blacklist_data.get('minecraft_uuid'):
                self.blacklist_data['minecraft_uuid'] = await self.cog.fetch_minecraft_uuid(mc_info['minecraft_username'])
            mc_info['minecraft_uuid'] = self.blacklist_data.get('minecraft_uuid', '')
            payload['mc_info'] = mc_info

        print(f"Sending blacklist payload: {payload}")

        try:
            async with aiohttp.ClientSession() as session:
                headers = {"X-API-Key": getattr(self.cog, 'api_key', 'unset')}
                async with session.post('http://localhost:5000/blacklist', json=payload, headers=headers) as response:
                    if response.status != 200:
                        response_text = await response.text()
                        print(f"API Error: {response.status} - {response_text}")
                        await interaction.followup.send(f"Failed to blacklist user. API returned: {response.status}", ephemeral=True)
                        return
                    print("Blacklist API request successful")
        except Exception as e:
            print(f"API request error: {e}")
            await interaction.followup.send(f"Failed to connect to blacklist API: {str(e)}", ephemeral=True)
            return

        kicked_servers = []
        mutual_servers = []

        try:
            user = await self.cog.bot.fetch_user(int(user_id))
            for guild in self.cog.bot.guilds:
                member = guild.get_member(int(user_id))
                if member:
                    mutual_servers.append(guild.name)
                    try:
                        await member.kick(reason=f"Blacklisted: {reason}")
                        kicked_servers.append(guild.name)
                    except discord.Forbidden:
                        print(f"Missing permissions to kick from {guild.name}")
                    except Exception as e:
                        print(f"Error kicking from {guild.name}: {e}")

            if mutual_servers:
                dm_message = f"Hello {user.display_name},\n\nYou have been blacklisted for the following reason: {reason}\n\n"
                dm_message += "You were a member of the following servers:\n"
                dm_message += "\n".join(mutual_servers)
                try:
                    await user.send(dm_message)
                    print(f"Successfully sent DM to {user.display_name}")
                except discord.Forbidden:
                    print(f"User {user.display_name} has DMs disabled")
                except Exception as e:
                    print(f"Error sending DM: {e}")
        except Exception as e:
            print(f"Error processing user actions: {e}")

        if kicked_servers:
            kick_message = f"User {username} ({user_id}) has been blacklisted and kicked from:\n" + "\n".join(kicked_servers)
        else:
            kick_message = f"User {username} ({user_id}) has been blacklisted, but couldn't be kicked from any servers."

        if self.blacklist_data.get('minecraft_username'):
            kick_message += f"\nMinecraft Username: {self.blacklist_data.get('minecraft_username')}"
        if self.blacklist_data.get('minecraft_uuid'):
            kick_message += f"\nMinecraft UUID: {self.blacklist_data.get('minecraft_uuid')}"

        try:
            await interaction.message.edit(content=kick_message, embed=None, view=None)
        except Exception as e:
            print(f"Error updating message: {e}")
            try:
                await interaction.followup.send(kick_message, ephemeral=False)
            except Exception as e2:
                print(f"Error sending followup message: {e2}")

        await interaction.followup.send("Blacklist operation completed successfully.", ephemeral=True)
        self.stop()

    @ui.button(label='Cancel', style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content="Blacklist action cancelled.", view=None)
        self.stop()

class Blacklist(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.AUTHORIZED_USERS = [987323487343493191, 1088268266499231764, 726721909374320640, 710863981039845467, 1151136371164065904]
        # Load the API key from the environment variable
        self.api_key = os.getenv("API_KEY", "unset")

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
                    if data:  # Check if data indicates blacklisted (assuming non-empty dict means blacklisted)
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
                await thread.send(embed=embed, view=view)
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

async def setup(bot):
    await bot.add_cog(Blacklist(bot))
