"""
BetTracker Discord Bot
======================
Lytter til billeder på din Discord-server og sender dem til BetTracker.
Botten kalder en AI-detektor: kun rigtige bet slips bliver gemt.

Setup: se bettracker-discord-bot-README.md
"""
import os
import io
import base64
import asyncio
import aiohttp
import discord
from discord import app_commands

DISCORD_TOKEN     = os.environ["DISCORD_BOT_TOKEN"]
INGEST_KEY        = os.environ["DISCORD_INGEST_KEY"]
SUPABASE_URL      = os.environ.get("SUPABASE_URL", "https://rvvuremsxjlayplgnzkm.supabase.co")
INGEST_ENDPOINT   = f"{SUPABASE_URL}/functions/v1/discord-ingest"
LINK_ENDPOINT     = f"{SUPABASE_URL}/functions/v1/discord-link"

# Optional: only listen in these channel IDs (comma-separated env var). Empty = all channels.
ALLOWED_CHANNELS = {
    int(x) for x in os.environ.get("DISCORD_ALLOWED_CHANNELS", "").split(",") if x.strip()
}

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")
MAX_BYTES = 8 * 1024 * 1024  # 8 MB safety cap

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


async def post_json(session: aiohttp.ClientSession, url: str, payload: dict) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json", "x-ingest-key": INGEST_KEY}
    async with session.post(url, json=payload, headers=headers, timeout=60) as r:
        try:
            data = await r.json()
        except Exception:
            data = {"error": await r.text()}
        return r.status, data


@tree.command(name="link", description="Forbind din Discord-konto til BetTracker")
@app_commands.describe(kode="Engangskoden du fik på BetTracker -> Indstillinger")
async def link_command(interaction: discord.Interaction, kode: str):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as s:
        status, data = await post_json(s, LINK_ENDPOINT, {
            "code": kode.strip().upper(),
            "discord_user_id": str(interaction.user.id),
            "discord_username": interaction.user.name,
        })
    if status == 200 and data.get("ok"):
        await interaction.followup.send("✅ Din Discord-konto er nu forbundet til BetTracker!", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Kunne ikke koble: {data.get('error', 'ukendt fejl')}", ephemeral=True)


async def handle_attachment(message: discord.Message, attachment: discord.Attachment):
    if attachment.size > MAX_BYTES:
        return
    if not attachment.filename.lower().endswith(IMAGE_EXT):
        return

    raw = await attachment.read()
    mime = "image/png"
    fn = attachment.filename.lower()
    if fn.endswith((".jpg", ".jpeg")): mime = "image/jpeg"
    elif fn.endswith(".webp"): mime = "image/webp"
    elif fn.endswith(".gif"): mime = "image/gif"
    data_url = f"data:{mime};base64,{base64.b64encode(raw).decode()}"

    async with aiohttp.ClientSession() as s:
        status, data = await post_json(s, INGEST_ENDPOINT, {
            "discord_user_id": str(message.author.id),
            "image": data_url,
        })

    if status == 404 and data.get("code") == "NOT_LINKED":
        try:
            await message.author.send(
                "Hej! Du har sendt et billede i en kanal hvor BetTracker-botten lytter, "
                "men din Discord-konto er ikke forbundet til BetTracker endnu.\n"
                "Gå til BetTracker -> Indstillinger -> Discord-integration og generér en kode, "
                "og skriv så `/link kode:DIN-KODE` til mig."
            )
        except discord.Forbidden:
            pass
        return

    if status == 200 and data.get("ok"):
        if data.get("skipped"):
            return  # not a bet slip — silent
        try:
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass
    else:
        print(f"[ingest error] {status} {data}")


@client.event
async def on_ready():
    print(f"BetTracker bot logget ind som {client.user}")
    try:
        await tree.sync()
        print("Slash commands synkroniseret")
    except Exception as e:
        print(f"Sync fejl: {e}")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if ALLOWED_CHANNELS and message.channel.id not in ALLOWED_CHANNELS:
        return
    if not message.attachments:
        return

    # Process attachments concurrently
    await asyncio.gather(*(handle_attachment(message, a) for a in message.attachments))


if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
