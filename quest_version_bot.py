"""
Quest App Version Discord Bot
------------------------------
Checks the live/dev binary versions of a Quest (Oculus) app via Meta's
internal GraphQL store endpoint, on-demand via a Discord command.

Persistence: rather than a local file, the bot stores its last-known
state as a small JSON payload inside a message it posts to a dedicated
"storage" channel. On each check, it reads that channel's most recent
bot message, compares versions, reports any change to the user, then
posts an updated JSON message with the new state.

Setup
-----
1. pip install discord.py aiohttp
2. Fill in the CONFIG section below (or use environment variables).
3. Create/pick a private channel in your server for storage and put its
   ID in STORAGE_CHANNEL_ID. The bot needs Read/Send/Read History there.
4. Run: python quest_version_bot.py

Usage
-----
    !version        -> fetches current live/dev version, compares
                        against last stored value, reports the result.
"""

import asyncio
import json
import os
from typing import Any, Optional

import aiohttp
import discord
from discord.ext import commands


# ----------------------------- CONFIG ------------------------------------

# Prefer environment variables in production; hardcoded values are here
# only as a fallback for quick local testing.
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "YOUR_DISCORD_BOT_TOKEN")
COMMAND_PREFIX = os.environ.get("COMMAND_PREFIX", "!")

# Channel where the bot reads/writes its JSON state message.
# Right-click a channel in Discord (dev mode enabled) -> Copy Channel ID.
STORAGE_CHANNEL_ID = int(os.environ.get("STORAGE_CHANNEL_ID", "0"))

# Oculus GraphQL access — same as the original script.
OCULUS_ACCESS_TOKEN = os.environ.get("OCULUS_ACCESS_TOKEN", "OC|752908224809889|")
OCULUS_APP_ID = int(os.environ.get("OCULUS_APP_ID", "7190422614401072"))
OCULUS_DOC_ID = int(os.environ.get("OCULUS_DOC_ID", "6771539532935162"))

STATE_MARKER = "QUEST_VERSION_BOT_STATE"  # tag so we can find our own JSON msgs

# ---------------------------------------------------------------------------


class GraphQLClient:
    """Rate-limited async client for the Oculus GraphQL store endpoint."""

    def __init__(
        self,
        url: str = "https://graph.oculus.com/graphql",
        max_requests: int = 5,
        per_seconds: float = 5.0,
    ) -> None:
        self.url = url
        self.max_requests = max_requests
        self.per_seconds = per_seconds
        self._timestamps: list[float] = []
        self._session: Optional[aiohttp.ClientSession] = None
        self._timeout = aiohttp.ClientTimeout(total=15)

    async def _acquire_slot(self) -> None:
        now = asyncio.get_running_loop().time()
        self._timestamps = [t for t in self._timestamps if now - t < self.per_seconds]

        if len(self._timestamps) >= self.max_requests:
            delay = self.per_seconds - (now - self._timestamps[0])
            if delay > 0:
                await asyncio.sleep(delay)

        self._timestamps.append(asyncio.get_running_loop().time())

    async def post(self, payload: dict) -> Optional[dict]:
        await self._acquire_slot()

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)

        try:
            async with self._session.post(self.url, data=payload) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except Exception as e:
            print(f"GraphQL error: {type(e).__name__}: {e}")

            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None
            return None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


def _payload() -> dict:
    return {
        "access_token": OCULUS_ACCESS_TOKEN,
        "variables": json.dumps({"applicationID": str(OCULUS_APP_ID)}),
        "doc_id": str(OCULUS_DOC_ID),
    }


graphql_client = GraphQLClient()


async def fetch_store_metadata() -> Optional[dict]:
    data = await graphql_client.post(_payload())
    return data if isinstance(data, dict) else None


def _extract_live_version(meta: dict) -> Optional[str]:
    nodes = meta.get("data", {}).get("node", {}).get("liveChannel", {}).get("nodes", [])
    if not nodes:
        return None
    return nodes[0].get("latest_supported_binary", {}).get("version")


def _extract_dev_version(meta: dict) -> Optional[str]:
    nodes = meta.get("data", {}).get("node", {}).get("primary_binaries", {}).get("nodes", [])
    if not nodes:
        return None
    return nodes[0].get("version")


async def get_current_versions() -> tuple[Optional[str], Optional[str]]:
    meta = await fetch_store_metadata()
    if not isinstance(meta, dict):
        return None, None
    return _extract_live_version(meta), _extract_dev_version(meta)


# --------------------------- Discord state I/O ----------------------------

async def read_last_state(channel: discord.abc.Messageable) -> Optional[dict]:
    """Scan recent history in the storage channel for our last JSON state."""
    async for msg in channel.history(limit=50):
        if msg.author.bot and STATE_MARKER in msg.content:
            try:
                start = msg.content.index("{")
                payload = json.loads(msg.content[start:])
                return payload
            except (ValueError, json.JSONDecodeError):
                continue
    return None


async def write_state(channel: discord.abc.Messageable, live: Optional[str], dev: Optional[str]) -> None:
    payload = {"live": live, "dev": dev}
    content = f"{STATE_MARKER}\n```json\n{json.dumps(payload)}\n```"
    await channel.send(content)


# --------------------------------- Bot -------------------------------------

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} (id={bot.user.id})")
    if STORAGE_CHANNEL_ID == 0:
        print("WARNING: STORAGE_CHANNEL_ID is not set. Persistence will fail.")


@bot.command(name="version")
async def version_cmd(ctx: commands.Context) -> None:
    """Check current live/dev version and compare against last known state."""
    async with ctx.typing():
        live, dev = await get_current_versions()

        if live is None and dev is None:
            await ctx.send("Couldn't fetch version info right now — the API call failed or returned nothing.")
            return

        storage_channel = bot.get_channel(STORAGE_CHANNEL_ID)
        if storage_channel is None:
            await ctx.send(
                f"Live: `{live}`\nDev: `{dev}`\n"
                "(Note: storage channel not configured/found, so I can't compare to previous values.)"
            )
            return

        previous = await read_last_state(storage_channel)

        lines = [f"**Live:** `{live}`", f"**Dev:** `{dev}`"]

        if previous is not None:
            prev_live = previous.get("live")
            prev_dev = previous.get("dev")

            if prev_live != live:
                lines.append(f"🔄 Live version changed: `{prev_live}` → `{live}`")
            if prev_dev != dev:
                lines.append(f"🔄 Dev version changed: `{prev_dev}` → `{dev}`")
            if prev_live == live and prev_dev == dev:
                lines.append("No change since last check.")
        else:
            lines.append("(No previous record found — this is the first check.)")

        await ctx.send("\n".join(lines))
        await write_state(storage_channel, live, dev)


@bot.event
async def on_disconnect() -> None:
    await graphql_client.close()


if __name__ == "__main__":
    if DISCORD_TOKEN == "YOUR_DISCORD_BOT_TOKEN":
        raise SystemExit(
            "Set DISCORD_TOKEN (env var or in CONFIG) before running the bot."
        )
    bot.run(DISCORD_TOKEN)
