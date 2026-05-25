import asyncio
import logging
import os
from dotenv import load_dotenv

import discord
from discord.ext import commands

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
MY_GUILD = discord.Object(id=GUILD_ID)


class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=".", intents=intents)

    async def setup_hook(self):
        logger.info("Cog 로딩 중: yt_music_play")
        await self.load_extension("yt_music_play")
        logger.info("Cog 로딩 완료")

        logger.info("슬래시 커맨드 동기화 중 (guild_id=%s)", GUILD_ID)
        self.tree.copy_global_to(guild=MY_GUILD)
        await self.tree.sync(guild=MY_GUILD)
        logger.info("슬래시 커맨드 동기화 완료")

    async def on_ready(self):
        logger.info("봇 준비 완료: %s (id=%s)", self.user, self.user.id)


async def main():
    async with MyBot() as bot:
        await bot.start(TOKEN)


asyncio.run(main())
