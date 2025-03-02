import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime
import datetime as dt
import requests
import json
import os
from dotenv import load_dotenv



intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents)

# Load environment variables from the .env file
load_dotenv()

# Access the token from the environment variable
TOKEN = os.getenv("BOT_TOKEN")

# Load blacklists from JSON files
def load_blacklist(filename):
    try:
        with open(filename, 'r') as f:
            return set(json.load(f))
    except FileNotFoundError:
        print(f"Warning: {filename} not found. Creating an empty file.")
        with open(filename, 'w') as f:
            json.dump([], f)
        return set()

BLACKLISTED_USERS = load_blacklist('blacklisted_users.json')
BLACKLISTED_CHANNELS = load_blacklist('blacklisted_channels.json')

async def load_cogs():
    for root, dirs, files in os.walk("./cogs"):  # Recursively walks through the cogs directory
        for file in files:
            if file.endswith(".py") and file != "__init__.py":
                # Create a module path, replacing the slashes with dots
                cog_path = os.path.join(root, file).replace("./", "").replace("\\", ".").replace("/", ".")
                cog_path = cog_path[:-3]  # Remove the .py extension
                
                try:
                    await bot.load_extension(cog_path)
                    print(f"Loaded {cog_path}")
                except Exception as e:
                    print(f"Failed to load {cog_path}: {e}")

# When bot starts
@bot.event
async def on_ready():
    print(f'We have logged in as {bot.user}')
    
    await load_cogs()
 
    print(f"Views have been registered for {len(bot.guilds)} guilds.")

    try:
        #BLACKLISTED_USERS = load_blacklist('blacklisted_users.json')
        #BLACKLISTED_CHANNELS = load_blacklist('blacklisted_channels.json')
        synced = await bot.tree.sync()

        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(e)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    try:
        if isinstance(error, app_commands.errors.TransformerError):
            await interaction.response.send_message("The provided channel is not a forum channel. Please select a valid forum channel.", ephemeral=True)
        else:
            await interaction.response.send_message(f"An error occurred while processing the command: {error}", ephemeral=True)
    except discord.errors.InteractionResponded:
        # If the interaction has already been responded to, use followup instead
        await interaction.followup.send(f"An error occurred while processing the command: {error}", ephemeral=True)
    
    # Log the error for debugging
    print(f"Error in {interaction.command.name}: {error}")


# Get log channels
def get_log_channel(guild):
    """Find the appropriate logging channel in the given guild."""
    log_channel_names = ["moderator-only", "logs"]
    for channel_name in log_channel_names:
        channel = discord.utils.get(guild.channels, name=channel_name)
        if channel:
            return channel
    return None

def should_log(message):
    """Check if the message should be logged based on blacklists."""
    if str(message.author.id) in BLACKLISTED_USERS:
        return False
    if str(message.channel.id) in BLACKLISTED_CHANNELS:
        return False
    return True

# Log on delete
@bot.event
async def on_message_delete(message):
    # Ignore DMs
    if not message.guild:
        return

    # Check if the message should be logged
    if not should_log(message):
        return

    log_channel = get_log_channel(message.guild)
    
    # If no suitable channel is found, we can't log the deletion
    if not log_channel:
        return

    embed = discord.Embed(title=f"{message.author}'s Message Was Deleted", 
                          description=f"Deleted Message: {message.content}\nAuthor: {message.author.mention}\nLocation: {message.channel.mention}", 
                          timestamp=datetime.now(), 
                          color=discord.Color.red())

    channel2 = bot.get_channel(1260856171905159190)
    if channel2:
        embed2 = discord.Embed(title=f"{message_before.author}'s Message Was Edited", description=f"Message: {message_before.content}\nAfter: {message_after.content}\nAuthor: {message_before.author.mention}\nLocation: {message_before.channel.mention}", timestamp=datetime.now(), color=1)
        await channel2.send(embed=embed2)
    else:
        print(f"Error: Could not find channel with ID 1260856171905159190")
    embed2 = discord.Embed(title = f"{message.author}'s Message Was Deleted",description = f"Deleted Message: {message.content}\nAuthor: {message.author.mention}\nLocation: {message.channel.mention}", timestamp = datetime.now(), color = 5)
    await channel2.send(embed = embed2)

    await log_channel.send(embed=embed)

# Log on edit
@bot.event
async def on_message_edit(message_before, message_after):
    # Ignore DMs
    if not message_before.guild:
        return

    # Check if the message should be logged
    if not should_log(message_before):
        return

    log_channel = get_log_channel(message_before.guild)
    
    # If no suitable channel is found, we can't log the edit
    if not log_channel:
        return

    embed = discord.Embed(title=f"{message_before.author}'s Message Was Edited", 
                          description=f"Before: {message_before.content}\nAfter: {message_after.content}\nAuthor: {message_before.author.mention}\nLocation: {message_before.channel.mention}", 
                          timestamp=datetime.now(), 
                          color=discord.Color.blue())
    
    channel2 = bot.get_channel(1260856171905159190)
    if channel2:
        embed2 = discord.Embed(title=f"{message_before.author}'s Message Was Edited", description=f"Message: {message_before.content}\nAfter: {message_after.content}\nAuthor: {message_before.author.mention}\nLocation: {message_before.channel.mention}", timestamp=datetime.now(), color=1)
        await channel2.send(embed=embed2)
    else:
        print(f"Error: Could not find channel with ID 1260856171905159190")
    embed2 = discord.Embed(title = f"{message_before.author}'s Message Was Edited", description = f"Message: {message_before.content}\nAfter: {message_after.content}\nAuthor: {message_before.author.mention}\nLocation: {message_before.channel.mention}", timestamp = datetime.now(), color = 1)
    await channel2.send(embed = embed2)


@bot.tree.command(name="embed", description="Create an embed message")
@app_commands.describe(title="Embed title", description="Embed description", color="Embed color (hex)")
async def embed(interaction: discord.Interaction, title: str, description: str, color: str):
    embed = discord.Embed(title=title, description=description, color=int(color, 16))
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="editembed", description="Edit embed messages by its id")
@app_commands.describe(message_id="Message ID", title="Embed title", description="Embed description", color="Embed color (hex)")
async def editembed(ctx, message_id: str, title: str, description: str, color: str):
    message = await ctx.channel.fetch_message(int(message_id))  # Fetch the message by ID

    new_embed = discord.Embed(
        title=title,
        description=description,
        color=int(color, 16),
    )
    await message.edit(embed=new_embed)
    await ctx.response.send_message("Successfully Edited Embed", ephemeral=False)

@bot.tree.command(name="github", description="Get information about a GitHub repository")
@app_commands.describe(username="GitHub username", repository="Repository name")
async def github(interaction: discord.Interaction, username: str, repository: str):
    url = f'https://api.github.com/repos/{username}/{repository}'
    response = requests.get(url)
    data = json.loads(response.text)

    if response.status_code != 200:
        await interaction.response.send_message(f"Error: {data.get('message', 'Unknown error occurred')}", ephemeral=True)
        return

    embed = discord.Embed(title=data['name'], description=data['description'], color=0x00ff00)
    embed.add_field(name='Stars', value=data['stargazers_count'])
    embed.add_field(name='Forks', value=data['forks_count'])
    embed.add_field(name='Watchers', value=data['watchers_count'])
    embed.set_footer(text=f'Created at {data["created_at"]}')
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="reload_blacklists", description="Reloads Blacklists")
@app_commands.checks.has_permissions(administrator=True)
async def reload_blacklists(interaction: discord.Interaction):
    global BLACKLISTED_USERS, BLACKLISTED_CHANNELS
    BLACKLISTED_USERS = load_blacklist('blacklisted_users.json')
    BLACKLISTED_CHANNELS = load_blacklist('blacklisted_channels.json')
    await interaction.response.send_message("Blacklists reloaded.")

@reload_blacklists.error
async def reload_blacklists_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.errors.MissingPermissions):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
    else:
        await interaction.response.send_message("An error occurred while executing the command.", ephemeral=True)

bot.run(TOKEN)
