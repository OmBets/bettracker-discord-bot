"""
BetTracker Discord Bot
======================
Run on Render.com (Web Service, Python 3.11+).
ENV VARS (set in Render → Environment):
  DISCORD_BOT_TOKEN   - Bot token from https://discord.com/developers/applications
  DISCORD_INGEST_KEY  - Same value as the DISCORD_INGEST_KEY secret in Lovable Cloud
  SUPABASE_URL        - https://rvvuremsxjlayplgnzkm.supabase.co
  PORT                - Render injects this automatically
The bot:
  • Exposes GET /health on $PORT so the BetTracker app can show online/offline status.
  • Handles `/link kode:<CODE>` slash command to link a Discord user to a BetTracker account.
  • Listens to messages with image attachments. For each image it forwards to the
    `discord-ingest` edge function ONLY IF the author has a linked BetTracker
    account AND the message is in one of the user's allowed channel IDs.
    The edge function double-checks both before doing anything.
"""
import os
import asyncio
import aiohttp
from aiohttp import web
import discord
from discord import app_commands
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_INGEST_KEY = os.environ["DISCORD_INGEST_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
PORT = int(os.environ.get("PORT", "10000"))
INGEST_URL = f"{SUPABASE_URL}/functions/v1/discord-ingest"
LINK_URL = f"{SUPABASE_URL}/functions/v1/discord-link"
RESULT_URL = f"{SUPABASE_URL}/functions/v1/discord-result"
intents = discord.Intents.default()
intents.message_content = True
intents.guild_messages = True
intents.dm_messages = True
intents.guilds = 
intents.guild_messages = True
intents.guild_reactions = True
class BetTrackerBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.http_session: aiohttp.ClientSession | None = None
    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession()
        await self.tree.sync()
bot = BetTrackerBot()
@bot.tree.command(name="link", description="Link din Discord-konto til BetTracker")
@app_commands.describe(kode="Engangskoden fra BetTracker → Indstillinger → Discord")
async def link_cmd(interaction: discord.Interaction, kode: str):
    await interaction.response.defer(ephemeral=True)
    try:
        async with bot.http_session.post(
            LINK_URL,
            headers={
                "Content-Type": "application/json",
                "x-ingest-key": DISCORD_INGEST_KEY,
            },
            json={
                "code": kode.strip().upper(),
                "discord_user_id": str(interaction.user.id),
                "discord_username": interaction.user.name,
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            data = await r.json()
            if r.status == 200 and data.get("ok"):
                await interaction.followup.send(
                    "✅ Din Discord-konto er nu linket til BetTracker. "
                    "Husk at tilføje denne kanals ID til 'Tilladte kanaler' i appen.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"❌ Kunne ikke linke: {data.get('error', 'ukendt fejl')}",
                    ephemeral=True,
                )
    except Exception as e:
        await interaction.followup.send(f"❌ Fejl: {e}", ephemeral=True)
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id {bot.user.id})")
@bot.event
async def on_message(message: discord.Message):
    # Ignore bots (incl. self) and DMs (only act in guild channels with explicit allowlist).
    if message.author.bot:
        return
    if message.guild is None:
        return
    if not message.attachments:
        return
    # Only forward image attachments — the ingest function will determine if it's a bet slip.
    images = [
        a for a in message.attachments
        if (a.content_type and a.content_type.startswith("image/"))
        or a.filename.lower().endswith(IMAGE_EXTS)
    ]
    if not images:
        return
    for att in images:
        try:
            async with bot.http_session.post(
                INGEST_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-ingest-key": DISCORD_INGEST_KEY,
                },
                json={
                    "discord_user_id": str(message.author.id),
                    "image": att.url,
                    "channel_id": str(message.channel.id),
                },
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                data = await r.json()
                # Silently ignore expected rejections so we don't spam unrelated channels.
                if r.status == 404 and data.get("code") == "NOT_LINKED":
                    return
                if r.status == 403 and data.get("code") == "CHANNEL_NOT_ALLOWED":
                    return
                if r.status == 429 and data.get("code") == "LIMIT_REACHED":
                    try:
                        await message.add_reaction("⛔")
                    except Exception:
                        pass
                    return
                if r.status == 200 and data.get("ok") and not data.get("skipped"):
                    try:
                        await message.add_reaction("📲")
                    except Exception:
                        pass
        except Exception as e:
            print(f"ingest error: {e}")
@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return

    emoji = str(payload.emoji)

    if emoji == "✅":
        result = "won"
    elif emoji == "❌":
        result = "lost"
    elif emoji == "➖":
        result = "void"
    else:
        return

    try:
        async with bot.http_session.post(
            RESULT_URL,
            headers={
                "Content-Type": "application/json",
                "x-ingest-key": DISCORD_INGEST_KEY,
            },
            json={
                "message_id": str(payload.message_id),
                "result": result,
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:

            if r.status == 200:
                print(
                    f"Updated bet from Discord reaction: "
                    f"{payload.message_id} -> {result}"
                )
            else:
                print(
                    f"discord-result error: "
                    f"{r.status} {await r.text()}"
                )

    except Exception as e:
        print(f"reaction update failed: {e}")
# ---------------- HTTP /health endpoint (for Render + BetTracker status panel) ---------------- #
async def health(_request):
    ready = bot.is_ready()
    return web.json_response({
        "ok": ready,
        "bot_user": str(bot.user) if bot.user else None,
        "guilds": len(bot.guilds) if ready else 0,
        "latency_ms": round((bot.latency or 0) * 1000) if ready else None,
    }, headers={"Access-Control-Allow-Origin": "*"})
async def start_http():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Health endpoint listening on :{PORT}")
async def main():
    await start_http()
    await bot.start(DISCORD_BOT_TOKEN)
if __name__ == "__main__":
    asyncio.run(main())
