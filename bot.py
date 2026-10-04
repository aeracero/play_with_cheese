import asyncio
import os

import discord
from discord.ext import commands

intents = discord.Intents.default()
intents.message_content = True  # 文字数制限に必要（Developer Portal でも有効化）
bot = commands.Bot(command_prefix="!", intents=intents)

GUILD_ID = os.getenv("GUILD_ID")  # 設定すると、そのサーバーにコマンドが即時反映されます


@bot.event
async def setup_hook():
    await bot.load_extension("message_limit")
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()


async def main():
    async with bot:
        await bot.start(os.environ["DISCORD_TOKEN"])


if __name__ == "__main__":
    asyncio.run(main())
