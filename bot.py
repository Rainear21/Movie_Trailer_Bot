"""Discord bot that posts new movie trailers, with a web dashboard for setup.

Runs the Discord bot and the dashboard website in one process. Each server
picks its channel, studios and filters on the dashboard (or with slash commands).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from db import Database
from sources import fetch_tmdb, fetch_youtube, normalize_channel_ref

BASE = Path(__file__).resolve().parent
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("trailerbot")

BOT_PERMISSIONS = 1024 | 2048 | 16384  # View Channel, Send Messages, Embed Links


def load_config() -> dict:
    try:
        return json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit("config.json is missing.")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"config.json is not valid JSON: {exc}")


def guild_source_keys(guild: dict, handle_cache: dict, tmdb_on: bool) -> set[str]:
    keys = set()
    for ref in guild["youtube_channels"]:
        channel_id, handle = normalize_channel_ref(ref)
        channel_id = channel_id or handle_cache.get(handle.lower())
        if channel_id:
            keys.add(f"yt:{channel_id}")
    if tmdb_on and guild["use_tmdb"]:
        keys.add("tmdb")
    return keys


def video_matches(guild: dict, video: dict, keys: set[str]) -> bool:
    via = video["sources"] & keys
    if not via:
        return False
    if guild["skip_shorts"] and "/shorts/" in video["url"]:
        return False
    title = video["title"].lower()
    if any(w in title for w in guild["exclude_keywords"]):
        return False
    if "tmdb" in via:  # TMDB already tells us it's a trailer or teaser
        return True
    return not guild["include_keywords"] or any(w in title for w in guild["include_keywords"])


def format_post(video: dict, ping_role_id: int | None) -> str:
    lines = [f"<@&{ping_role_id}>"] if ping_role_id else []
    lines.append(f"🎬 **{video['title']}**")
    details = [f"via {video['source_name']}"]
    if video.get("release_date"):
        details.append(f"in theaters {video['release_date']}")
    lines.append(" · ".join(details))
    lines.append(video["url"])  # Discord turns this into a playable YouTube embed
    return "\n".join(lines)


class TrailerBot(discord.Client):
    def __init__(self, config: dict):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.config = config
        self.tmdb_key = os.getenv("TMDB_API_KEY", "").strip() or None
        self.client_id = os.getenv("DISCORD_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("DISCORD_CLIENT_SECRET", "").strip()
        self.port = int(os.getenv("PORT", "8080"))
        self.public_url = (os.getenv("PUBLIC_URL") or f"http://localhost:{self.port}").rstrip("/")
        data_dir = Path(os.getenv("DATA_DIR") or BASE / "data")
        self.db = Database(data_dir / "trailerbot.db", config)
        self.session: aiohttp.ClientSession | None = None
        self.web_runner: web.AppRunner | None = None
        self.check_lock = asyncio.Lock()
        self.last_manual_check = 0.0

    @property
    def tmdb_on(self) -> bool:
        return bool(self.tmdb_key) and self.config.get("tmdb", {}).get("enabled", True)

    def invite_url(self, guild_id: int | None = None) -> str:
        url = (f"https://discord.com/oauth2/authorize?client_id={self.client_id}"
               f"&scope=bot%20applications.commands&permissions={BOT_PERMISSIONS}")
        if guild_id:
            url += f"&guild_id={guild_id}&disable_guild_select=true"
        return url

    # ------------------------------------------------------------------ lifecycle
    async def setup_hook(self):
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DiscordTrailerBot/2.0",
                     "Accept-Language": "en-US,en;q=0.9"},
            timeout=aiohttp.ClientTimeout(total=30),
        )
        if not self.client_id:
            self.client_id = str((await self.application_info()).id)
        register_commands(self)

        from web import create_app
        self.web_runner = web.AppRunner(create_app(self))
        await self.web_runner.setup()
        await web.TCPSite(self.web_runner, "0.0.0.0", self.port).start()
        log.info("Dashboard running at %s", self.public_url)
        log.info("Discord OAuth2 redirect must be exactly: %s/callback  (client ID %s)",
                 self.public_url, self.client_id)
        if not self.client_secret:
            log.warning("DISCORD_CLIENT_SECRET not set - dashboard login is disabled.")

        self.poll.change_interval(minutes=max(5, int(self.config.get("poll_minutes", 15))))
        self.poll.start()

    async def close(self):
        if self.web_runner:
            await self.web_runner.cleanup()
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self):
        log.info("Logged in as %s in %d server(s)", self.user, len(self.guilds))
        try:
            await self.tree.sync()  # global; can take a few minutes to appear the first time
        except discord.HTTPException as exc:
            log.warning("Couldn't sync slash commands: %s", exc)

    # ------------------------------------------------------------------ polling
    @tasks.loop(minutes=15)
    async def poll(self):
        try:
            await self.run_check()
        except Exception:
            log.exception("Trailer check failed")

    @poll.before_loop
    async def before_poll(self):
        await self.wait_until_ready()

    async def run_check(self) -> int:
        async with self.check_lock:
            guilds = self.db.active_guilds()
            refs: list[str] = []
            for g in guilds or [{"youtube_channels": self.config.get("youtube_channels", [])}]:
                for ref in g["youtube_channels"]:
                    if ref.lower() not in (r.lower() for r in refs):
                        refs.append(ref)

            cache = self.db.handle_cache()
            found = await fetch_youtube(self.session, refs, cache, [], [], False)
            self.db.save_handle_cache(cache)
            if self.tmdb_on and (not guilds or any(g["use_tmdb"] for g in guilds)):
                try:
                    found += await fetch_tmdb(self.session, self.tmdb_key, self.config.get("tmdb", {}))
                except Exception as exc:
                    log.warning("TMDB check failed: %s", exc)

            fresh = self.db.record_trailers(found, int(self.config.get("max_age_days", 7)))
            log.info("Checked %d sources: %d videos, %d new trailers", len(refs), len(found), fresh)

            posted = 0
            for g in guilds:
                if self.get_guild(g["guild_id"]) is None:
                    continue  # bot was removed from this server
                posted += await self.deliver(g, cache)
            self.db.prune()
            return posted

    async def deliver(self, guild: dict, cache: dict) -> int:
        candidates = self.db.candidates_for(guild)
        if not candidates:
            return 0
        channel = self.get_channel(guild["channel_id"])
        if channel is None:
            self.db.set_guild_error(guild["guild_id"], "The trailer channel no longer exists. Pick a new one.")
            return 0
        keys = guild_source_keys(guild, cache, self.tmdb_on)
        limit = int(self.config.get("max_posts_per_check", 10))
        role = guild["ping_role_id"]
        mentions = discord.AllowedMentions(everyone=False, users=False,
                                           roles=[discord.Object(role)] if role else False)
        posted = 0
        for video in candidates:
            if posted >= limit:
                break
            if not video_matches(guild, video, keys):
                continue
            try:
                await channel.send(format_post(video, role), allowed_mentions=mentions)
            except discord.Forbidden:
                self.db.record_post(guild["guild_id"], video["video_id"], ok=False)
                self.db.set_guild_error(guild["guild_id"],
                                        f"Missing permission to post in #{channel.name}. "
                                        "Give the bot View Channel, Send Messages and Embed Links there.")
                return posted
            except discord.HTTPException as exc:
                log.warning("Post to guild %s failed: %s", guild["guild_id"], exc)
                continue
            self.db.record_post(guild["guild_id"], video["video_id"], ok=True)
            posted += 1
            await asyncio.sleep(1)
        if posted:
            self.db.set_guild_error(guild["guild_id"], None)
        return posted


# ---------------------------------------------------------------------- slash commands
def register_commands(bot: TrailerBot):
    group = app_commands.Group(name="trailers", description="Movie trailer feed", guild_only=True)

    @group.command(name="dashboard", description="Get the link to set up the bot for this server")
    async def dashboard(interaction: discord.Interaction):
        await interaction.response.send_message(
            f"Set up trailers for this server here: {bot.public_url}/dashboard/{interaction.guild_id}",
            ephemeral=True)

    @group.command(name="setchannel", description="Post new trailers in this channel (or the one you pick)")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        channel = channel or interaction.channel
        settings = bot.db.get_guild(interaction.guild_id)
        settings.update(channel_id=channel.id, enabled=True)
        bot.db.save_guild(settings)
        await interaction.response.send_message(
            f"New trailers will be posted in {channel.mention}. "
            f"Fine-tune studios and filters at {bot.public_url}/dashboard/{interaction.guild_id}",
            ephemeral=True)

    @group.command(name="stop", description="Pause trailer posts in this server")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def stop(interaction: discord.Interaction):
        settings = bot.db.get_guild(interaction.guild_id)
        settings["enabled"] = False
        bot.db.save_guild(settings)
        await interaction.response.send_message("Trailer posts paused. Turn them back on in the dashboard "
                                                "or with /trailers setchannel.", ephemeral=True)

    @group.command(name="check", description="Check for new trailers right now")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def check(interaction: discord.Interaction):
        if time.monotonic() - bot.last_manual_check < 120:
            await interaction.response.send_message("A check just ran - try again in a couple of minutes.",
                                                    ephemeral=True)
            return
        bot.last_manual_check = time.monotonic()
        await interaction.response.defer(ephemeral=True, thinking=True)
        await bot.run_check()
        await interaction.followup.send("Done - any new trailers have been posted.", ephemeral=True)

    @group.command(name="latest", description="Show the most recently posted trailers")
    async def latest(interaction: discord.Interaction, count: app_commands.Range[int, 1, 10] = 5):
        recent = bot.db.recent_posts(interaction.guild_id, count)
        if not recent:
            await interaction.response.send_message("No trailers posted here yet.", ephemeral=True)
            return
        text = "\n".join(f"• [{r['title']}](<{r['url']}>) - {r['source_name']}" for r in recent)
        await interaction.response.send_message(text, ephemeral=True)

    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "You need the **Manage Server** permission for that."
        else:
            log.exception("Command error", exc_info=error)
            msg = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    bot.tree.add_command(group)


def main():
    load_dotenv(BASE / ".env")
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is missing. Copy .env.example to .env and fill it in.")
    TrailerBot(load_config()).run(token, log_handler=None)


if __name__ == "__main__":
    main()
