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

bot.run(TOKEN)
