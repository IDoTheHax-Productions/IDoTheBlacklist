import discord
from discord import app_commands, ui
from discord.ext import commands
import aiohttp
import asyncio
import re
import os
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class BlacklistModal(ui.Modal, title='Blacklist Confirmation'):
    request_id = ui.TextInput(label='Request ID', placeholder='Enter the Request ID to confirm', required=True)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)  # Acknowledge immediately

        try:
            request_id = int(self.request_id.value)
            await self.cog.submit_blacklist(interaction, request_id) # Passing the request_id
        except ValueError:
            await interaction.followup.send("Invalid Request ID. Please enter a valid integer.", ephemeral=True)
        except Exception as e:
            logging.exception("Error in BlacklistModal.on_submit")
            await interaction.followup.send(f"An error occurred: {e}", ephemeral=True)

class Blacklist(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.AUTHORIZED_USERS = [int(user_id) for user_id in os.getenv("AUTHORIZED_USER_IDS", "").split(",")] # Load from environment variables

        if not self.AUTHORIZED_USERS:
            logging.warning("No authorized users found.  Please set the AUTHORIZED_USER_IDS environment variable.")

    async def cog_load(self):
        logging.info(f"{self.__class__.__name__} cog loaded")

    async def cog_unload(self):
        logging.info(f"{self.__class__.__name__} cog unloaded")

    async def submit_blacklist(self, interaction: discord.Interaction, request_id: int):
        """This method calls the API and blacklists a user based on request ID."""
        try:
            auth_id = int(os.getenv("AUTHORIZED_USER_ID"))  # From ENV
            if interaction.user.id not in self.AUTHORIZED_USERS:
                await interaction.followup.send("You are not authorized to use this command.", ephemeral=True)
                return

            payload = {
                "request_id": int(request_id),
                "auth_id": auth_id
            }

            async with aiohttp.ClientSession() as session:
                async with session.post('http://localhost:5000/process_blacklist', json=payload) as response:
                    if response.status == 200:
                        await interaction.followup.send(f"Blacklist request {request_id} processed successfully.", ephemeral=True)
                    else:
                        await interaction.followup.send(f"Failed to process blacklist request {request_id}. Status code: {response.status}", ephemeral=True)
                        logging.error(f"API request failed with status code: {response.status} and payload: {payload}")

        except ValueError:
            await interaction.followup.send("Invalid Request ID. Please enter a valid integer.", ephemeral=True)
        except aiohttp.ClientError as e:
            logging.exception(f"AIOHTTP error during blacklist processing: {e}")
            await interaction.followup.send(f"A network error occurred: {e}", ephemeral=True)
        except Exception as e:
            logging.exception("Error in submit_blacklist")
            await interaction.followup.send(f"An unexpected error occurred: {e}", ephemeral=True)

    @commands.command()
    async def blacklist(self, ctx: commands.Context):
        """Opens a modal to enter the Blacklist Request ID."""
        if ctx.author.id not in self.AUTHORIZED_USERS:
            await ctx.send("You are not authorized to use this command.")
            return

        modal = BlacklistModal(self)
        await ctx.interaction.response.send_modal(modal)


async def setup(bot):
    await bot.add_cog(Blacklist(bot))
