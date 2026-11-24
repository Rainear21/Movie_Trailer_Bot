"""Discord bot that posts new movie trailers, with a web dashboard for setup.

Runs the Discord bot and the dashboard website in one process. Each server
picks its channel, studios and filters on the dashboard (or with slash commands).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from db import Database
from sources import (fetch_tmdb, fetch_youtube, guess_title, normalize_channel_ref,
                     tmdb_classify, tmdb_movie_trailer, tmdb_releases_between, tmdb_search_movies)

BASE = Path(__file__).resolve().parent
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("trailerbot")

BOT_PERMISSIONS = 1024 | 2048 | 16384  # View Channel, Send Messages, Embed Links
BRAND_COLOR = 0xF2B544
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MAX_FOLLOWER_PINGS = 40


def load_config() -> dict:
    try:
        return json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit("config.json is missing.")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"config.json is not valid JSON: {exc}")


def guild_tz(guild: dict) -> ZoneInfo:
    try:
        return ZoneInfo(guild.get("timezone") or "UTC")
    except Exception:
        return ZoneInfo("UTC")


# ---------------------------------------------------------------------- matching
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


def genres_ok(guild: dict, genres: list[int]) -> bool:
    include, exclude = set(guild.get("genres_include") or []), set(guild.get("genres_exclude") or [])
    if include and not include & set(genres):
        return False  # also skips trailers whose genre is unknown
    return not exclude & set(genres)


def video_matches(guild: dict, video: dict, keys: set[str]) -> bool:
    via = video["sources"] & keys
    if not via:
        return False
    if guild["skip_shorts"] and "/shorts/" in video["url"]:
        return False
    if guild.get("movies_only", True) and video.get("media_type") == "tv":
        return False
    if not genres_ok(guild, video.get("genres") or []):
        return False
    title = video["title"].lower()
    if any(w in title for w in guild["exclude_keywords"]):
        return False
    if "tmdb" in via:  # TMDB already tells us it's a trailer or teaser
        return True
    return not guild["include_keywords"] or any(w in title for w in guild["include_keywords"])


# ---------------------------------------------------------------------- formatting
def short_date(d: date) -> str:
    return f"{d.strftime('%b')} {d.day}"


def format_release(release_date: str | None) -> str | None:
    """'2026-12-18' -> 'Releases Dec 18, 2026' (or 'Released ...' if it's in the past)."""
    try:
        d = date.fromisoformat(release_date or "")
    except ValueError:
        return None
    verb = "Releases" if d >= date.today() else "Released"
    return f"{verb} {short_date(d)}, {d.year}"


def format_post(video: dict, ping_role_id: int | None, follower_ids: list[int] = ()) -> str:
    lines = [f"<@&{ping_role_id}>"] if ping_role_id else []
    lines.append(f"🎬 **{video['title']}**")
    details = [f"via {video['source_name']}"]
    release = format_release(video.get("release_date"))
    if release:
        details.append(release)
    lines.append(" · ".join(details))
    lines.append(video["url"])  # Discord turns this into a playable YouTube embed
    if follower_ids:
        lines.append("🔔 " + " ".join(f"<@{uid}>" for uid in follower_ids))
    return "\n".join(lines)


def digest_embed(movies: list[dict], start: date, end: date, region: str) -> discord.Embed:
    embed = discord.Embed(title=f"🍿 Out in theaters this week · {short_date(start)} – {short_date(end)}",
                          color=BRAND_COLOR)
    if movies:
        lines = []
        for m in movies:
            line = f"**{m['title']}** — {m['date'].strftime('%a')}, {short_date(m['date'])}"
            if m["trailer"]:
                line += f" · [Trailer]({m['trailer']})"
            lines.append(line)
        embed.description = "\n".join(lines)[:4000]
        poster = next((m["poster"] for m in movies if m["poster"]), None)
        if poster:
            embed.set_thumbnail(url=poster)
    else:
        embed.description = "No new theatrical releases this week."
    embed.set_footer(text=f"Theatrical release dates for {region} · Data from TMDB")
    return embed


def digest_due(guild: dict, local_now: datetime) -> bool:
    """Is it time for this server's weekly post? (local_now is in the server's time zone)"""
    return (guild["digest_enabled"] and local_now.weekday() == guild["digest_day"]
            and local_now.hour >= guild["digest_hour"]
            and guild["last_digest"] != local_now.date().isoformat())


def clean_keyword(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()[:50]


# ---------------------------------------------------------------------- the bot
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
        self.owner_ids = {int(x) for x in re.findall(r"\d+", os.getenv("OWNER_IDS", ""))}
        data_dir = Path(os.getenv("DATA_DIR") or BASE / "data")
        self.db = Database(data_dir / "trailerbot.db", config)
        self.session: aiohttp.ClientSession | None = None
        self.web_runner: web.AppRunner | None = None
        self.check_lock = asyncio.Lock()
        self.last_manual_check = 0.0
        self.started_at = datetime.now(timezone.utc)
        self.last_check: dict | None = None
        self.last_ok_check: datetime | None = None
        self._releases_cache: dict[tuple, tuple[float, list]] = {}

    @property
    def tmdb_on(self) -> bool:
        return bool(self.tmdb_key) and self.config.get("tmdb", {}).get("enabled", True)

    @property
    def poll_minutes(self) -> int:
        return max(5, int(self.config.get("poll_minutes", 15)))

    @property
    def region(self) -> str:
        return self.config.get("tmdb", {}).get("region", "US")

    def invite_url(self, guild_id: int | None = None) -> str:
        url = (f"https://discord.com/oauth2/authorize?client_id={self.client_id}"
               f"&scope=bot%20applications.commands&permissions={BOT_PERMISSIONS}")
        if guild_id:
            url += f"&guild_id={guild_id}&disable_guild_select=true"
        return url

    def is_healthy(self) -> bool:
        if not self.is_ready():
            return False
        window = timedelta(minutes=max(3 * self.poll_minutes, 30))
        reference = self.last_ok_check or self.started_at
        return datetime.now(timezone.utc) - reference < window

    # ------------------------------------------------------------------ lifecycle
    async def setup_hook(self):
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DiscordTrailerBot/2.0",
                     "Accept-Language": "en-US,en;q=0.9"},
            timeout=aiohttp.ClientTimeout(total=30),
        )
        info = await self.application_info()
        self.client_id = self.client_id or str(info.id)
        if info.team:
            self.owner_ids |= {m.id for m in info.team.members}
        elif info.owner:
            self.owner_ids.add(info.owner.id)
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

        self.poll.change_interval(minutes=self.poll_minutes)
        self.poll.start()
        self.digest_loop.start()

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

    # ------------------------------------------------------------------ trailer checks
    @tasks.loop(minutes=15)
    async def poll(self):
        try:
            await self.run_check()
        except Exception as exc:
            log.exception("Trailer check failed")
            self.last_check = {"at": datetime.now(timezone.utc), "ok": False, "error": str(exc)}

    @poll.before_loop
    async def before_poll(self):
        await self.wait_until_ready()

    async def run_check(self) -> int:
        async with self.check_lock:
            started = time.monotonic()
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
            await self.classify(fresh)
            log.info("Checked %d sources: %d videos, %d new trailers", len(refs), len(found), len(fresh))

            posted = 0
            for g in guilds:
                if self.get_guild(g["guild_id"]) is None:
                    continue  # bot was removed from this server
                posted += await self.deliver(g, cache)
            self.db.prune()

            self.last_ok_check = datetime.now(timezone.utc)
            self.last_check = {"at": self.last_ok_check, "ok": True, "error": None,
                               "seconds": round(time.monotonic() - started, 1), "sources": len(refs),
                               "videos": len(found), "new": len(fresh), "posted": posted}
            return posted

    async def classify(self, videos: list[dict]) -> None:
        """Label new YouTube trailers as movie or series, with genres, for the server filters."""
        seen: dict[str, tuple[str, list[int]]] = {}
        for v in videos:
            if v["media_type"]:
                continue
            key = guess_title(v["title"]).lower()
            if key not in seen:
                try:
                    seen[key] = await tmdb_classify(self.session, self.tmdb_key, v["title"])
                except Exception as exc:
                    log.debug("Couldn't classify %r: %s", v["title"], exc)
                    seen[key] = ("unknown", [])
            media_type, genres = seen[key]
            self.db.set_classification(v["video_id"], media_type, genres)
            if media_type == "tv":
                log.info("Labelled as a series: %s", v["title"])

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
        posted = 0
        for video in candidates:
            if posted >= limit:
                break
            if not video_matches(guild, video, keys):
                continue
            followers = list(self.db.followers_matching(
                guild["guild_id"], f"{video['title']} {video['source_name']}"))[:MAX_FOLLOWER_PINGS]
            mentions = discord.AllowedMentions(
                everyone=False, roles=[discord.Object(role)] if role else False,
                users=[discord.Object(u) for u in followers] if followers else False)
            try:
                await channel.send(format_post(video, role, followers), allowed_mentions=mentions)
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

    # ------------------------------------------------------------------ weekly releases
    async def releases(self, start: date, end: date) -> list[dict]:
        key = (self.region, start, end)
        cached = self._releases_cache.get(key)
        if cached and time.monotonic() - cached[0] < 6 * 3600:
            return cached[1]
        movies = await tmdb_releases_between(self.session, self.tmdb_key, self.region, start, end)
        self._releases_cache = {k: v for k, v in self._releases_cache.items()
                                if time.monotonic() - v[0] < 6 * 3600}
        self._releases_cache[key] = (time.monotonic(), movies)
        return movies

    async def week_embed(self, guild: dict | None, start: date) -> discord.Embed:
        end = start + timedelta(days=6)
        movies = await self.releases(start, end)
        if guild:
            movies = [m for m in movies if genres_ok(guild, m["genres"])]
        return digest_embed(movies[:12], start, end, self.region)

    async def send_digest(self, guild: dict) -> str:
        """Post this week's releases to the server's channel. Returns 'ok' or an error code."""
        if not self.tmdb_key:
            return "no_tmdb"
        channel = self.get_channel(guild["channel_id"]) if guild["channel_id"] else None
        if channel is None:
            return "no_channel"
        today = datetime.now(guild_tz(guild)).date()
        try:
            embed = await self.week_embed(guild, today)
        except Exception as exc:
            log.warning("Weekly releases lookup failed: %s", exc)
            return "tmdb_failed"
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            self.db.set_guild_error(guild["guild_id"], f"Missing permission to post in #{channel.name}.")
            return "forbidden"
        except discord.HTTPException:
            return "send_failed"
        return "ok"

    @tasks.loop(minutes=10)
    async def digest_loop(self):
        if not self.tmdb_key:
            return
        for guild in self.db.active_guilds():
            if self.get_guild(guild["guild_id"]) is None:
                continue
            local = datetime.now(guild_tz(guild))
            if not digest_due(guild, local):
                continue
            today = local.date().isoformat()
            result = await self.send_digest(guild)
            if result != "tmdb_failed":  # TMDB hiccups are retried on the next loop
                self.db.set_last_digest(guild["guild_id"], today)
            log.info("Weekly releases for guild %s: %s", guild["guild_id"], result)

    @digest_loop.before_loop
    async def before_digest(self):
        await self.wait_until_ready()


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

    @group.command(name="upcoming", description="Movies coming out in theaters this week")
    @app_commands.checks.cooldown(1, 10)
    async def upcoming(interaction: discord.Interaction):
        if not bot.tmdb_key:
            await interaction.response.send_message(
                "This needs a TMDB API key, which isn't set up on this bot yet.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        guild = bot.db.get_guild(interaction.guild_id)
        try:
            embed = await bot.week_embed(guild, datetime.now(guild_tz(guild)).date())
        except Exception as exc:
            log.warning("/trailers upcoming failed: %s", exc)
            await interaction.followup.send("Couldn't load this week's releases. Try again in a minute.")
            return
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------ /trailer search
    async def movie_autocomplete(interaction: discord.Interaction, current: str):
        if not bot.tmdb_key or len(current.strip()) < 2:
            return []
        try:
            movies = await tmdb_search_movies(bot.session, bot.tmdb_key, current.strip(), limit=10)
        except Exception:
            return []
        choices = []
        for m in movies:
            year = (m.get("release_date") or "")[:4]
            name = f"{m.get('title', '?')} ({year})" if year else m.get("title", "?")
            choices.append(app_commands.Choice(name=name[:100], value=str(m["id"])))
        return choices

    @bot.tree.command(name="trailer", description="Find the official trailer for a movie")
    @app_commands.describe(movie="Start typing a movie title")
    @app_commands.autocomplete(movie=movie_autocomplete)
    @app_commands.checks.cooldown(1, 5)
    async def trailer(interaction: discord.Interaction, movie: str):
        if not bot.tmdb_key:
            await interaction.response.send_message(
                "Movie search isn't set up on this bot yet (it needs a TMDB API key).", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            movie_id = int(movie) if movie.isdigit() else None
            if movie_id is None:  # typed a title without picking a suggestion
                results = await tmdb_search_movies(bot.session, bot.tmdb_key, movie, limit=1)
                movie_id = results[0]["id"] if results else None
            if movie_id is None:
                await interaction.followup.send(f"Couldn't find a movie called **{movie[:100]}**.")
                return
            details, video = await tmdb_movie_trailer(bot.session, bot.tmdb_key, movie_id)
        except Exception as exc:
            log.warning("/trailer lookup failed: %s", exc)
            await interaction.followup.send("Movie search is having trouble right now. Try again in a minute.")
            return

        year = (details.get("release_date") or "")[:4]
        title = f"{details.get('title', 'Unknown')}" + (f" ({year})" if year else "")
        if not video:
            await interaction.followup.send(f"**{title}** doesn't have a trailer on YouTube yet.")
            return
        lines = [f"🎬 **{title}** - {video.get('name') or video.get('type')}"]
        release = format_release(details.get("release_date"))
        if release:
            lines.append(release)
        lines.append(f"https://www.youtube.com/watch?v={video['key']}")
        await interaction.followup.send("\n".join(lines), allowed_mentions=discord.AllowedMentions.none())

    # ------------------------------------------------------------------ follows
    @bot.tree.command(name="follow", description="Get pinged when a trailer for a movie or franchise is posted")
    @app_commands.describe(name="A movie, franchise or studio, e.g. Dune, Star Wars, Marvel, A24")
    @app_commands.guild_only()
    async def follow(interaction: discord.Interaction, name: str):
        keyword = clean_keyword(name)
        if len(keyword) < 2:
            await interaction.response.send_message("That's too short - try a movie or franchise name.",
                                                    ephemeral=True)
            return
        result = bot.db.add_follow(interaction.guild_id, interaction.user.id, keyword)
        settings = bot.db.get_guild(interaction.guild_id)
        if result == "limit":
            msg = "You're following the maximum of 25 things here. Use /unfollow to make room."
        elif result == "exists":
            msg = f"You're already following **{keyword}**."
        else:
            msg = f"Done! You'll be pinged when a **{keyword}** trailer is posted here."
            if not settings["channel_id"] or not settings["enabled"]:
                msg += " (Heads up: this server hasn't turned trailer posts on yet.)"
        await interaction.response.send_message(msg, ephemeral=True)

    async def my_follows_autocomplete(interaction: discord.Interaction, current: str):
        follows = bot.db.user_follows(interaction.guild_id, interaction.user.id)
        return [app_commands.Choice(name=f, value=f) for f in follows if current.lower() in f][:25]

    @bot.tree.command(name="unfollow", description="Stop getting pinged for something you follow")
    @app_commands.autocomplete(name=my_follows_autocomplete)
    @app_commands.guild_only()
    async def unfollow(interaction: discord.Interaction, name: str):
        keyword = clean_keyword(name)
        if bot.db.remove_follow(interaction.guild_id, interaction.user.id, keyword):
            msg = f"Unfollowed **{keyword}**."
        else:
            msg = f"You weren't following **{keyword}**. Use /following to see your list."
        await interaction.response.send_message(msg, ephemeral=True)

    @bot.tree.command(name="following", description="See the movies and franchises you follow")
    @app_commands.guild_only()
    async def following(interaction: discord.Interaction):
        follows = bot.db.user_follows(interaction.guild_id, interaction.user.id)
        if not follows:
            msg = "You're not following anything yet. Try `/follow Dune`."
        else:
            msg = "You'll be pinged for trailers matching:\n" + "\n".join(f"• {f}" for f in follows)
        await interaction.response.send_message(msg, ephemeral=True)

    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "You need the **Manage Server** permission for that."
        elif isinstance(error, app_commands.CommandOnCooldown):
            msg = f"Slow down a little - try again in {error.retry_after:.0f}s."
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
