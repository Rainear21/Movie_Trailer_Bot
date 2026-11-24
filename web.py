"""Web dashboard: Discord login, server picker and per-server settings."""
from __future__ import annotations

import hmac
import logging
import secrets
from pathlib import Path
from urllib.parse import urlencode

import discord
from aiohttp import web
from jinja2 import Environment, FileSystemLoader, select_autoescape

from sources import normalize_channel_ref

BASE = Path(__file__).resolve().parent
DISCORD_API = "https://discord.com/api/v10"
MANAGE_GUILD, ADMINISTRATOR = 0x20, 0x8
SESSION_DAYS = 7
log = logging.getLogger("trailerbot.web")

jinja = Environment(loader=FileSystemLoader(BASE / "templates"),
                    autoescape=select_autoescape(["html"]))


def render(request: web.Request, template: str, status: int = 200, **ctx) -> web.Response:
    ctx.setdefault("user", request.get("user"))
    ctx.setdefault("public_url", request.app["bot"].public_url)
    ctx.setdefault("bot_name", request.app["bot"].user.name if request.app["bot"].user else "Trailer Bot")
    html = jinja.get_template(template).render(**ctx)
    return web.Response(text=html, content_type="text/html", status=status)


def message_page(request, title: str, text: str, status: int = 400):
    return render(request, "message.html", status=status, title=title, text=text)


# ---------------------------------------------------------------------- middleware
@web.middleware
async def session_middleware(request: web.Request, handler):
    sid = request.cookies.get("sid")
    request["user"] = request.app["bot"].db.get_session(sid) if sid else None
    response = await handler(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' https://cdn.discordapp.com https://i.ytimg.com data:; "
        "style-src 'self'; script-src 'self'; form-action 'self' https://discord.com; frame-ancestors 'none'")
    return response


def require_login(handler):
    async def wrapper(request: web.Request):
        if not request["user"]:
            raise web.HTTPFound("/login?" + urlencode({"next": request.path_qs}))
        return await handler(request)
    return wrapper


def check_csrf(request: web.Request, form) -> None:
    if not hmac.compare_digest(form.get("csrf", ""), request["user"]["csrf"]):
        raise web.HTTPForbidden(text="Invalid form token - reload the page and try again.")


def safe_next(value: str | None) -> str:
    return value if value and value.startswith("/") and not value.startswith("//") else "/dashboard"


# ---------------------------------------------------------------------- auth
async def index(request: web.Request):
    bot = request.app["bot"]
    return render(request, "index.html", invite_url=bot.invite_url(),
                  server_count=len(bot.guilds), login_enabled=bool(bot.client_secret))


async def login(request: web.Request):
    bot = request.app["bot"]
    if not bot.client_secret:
        return message_page(request, "Login isn't set up",
                            "The bot owner needs to set DISCORD_CLIENT_SECRET to enable dashboard login.", 503)
    state = secrets.token_urlsafe(24)
    params = {"client_id": bot.client_id, "response_type": "code", "scope": "identify guilds",
              "redirect_uri": f"{bot.public_url}/callback", "state": state, "prompt": "none"}
    resp = web.HTTPFound("https://discord.com/oauth2/authorize?" + urlencode(params))
    secure = bot.public_url.startswith("https://")
    resp.set_cookie("oauth_state", state, max_age=600, httponly=True, samesite="Lax", secure=secure)
    resp.set_cookie("oauth_next", safe_next(request.query.get("next")), max_age=600,
                    httponly=True, samesite="Lax", secure=secure)
    return resp


async def callback(request: web.Request):
    bot = request.app["bot"]
    code, state = request.query.get("code"), request.query.get("state", "")
    expected = request.cookies.get("oauth_state", "")
    if not code or not expected or not hmac.compare_digest(state, expected):
        return message_page(request, "Login failed", "That login link expired. Please try again.")

    async with bot.session.post(f"{DISCORD_API}/oauth2/token", data={
        "client_id": bot.client_id, "client_secret": bot.client_secret,
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": f"{bot.public_url}/callback",
    }) as resp:
        if resp.status != 200:
            log.warning("OAuth token exchange failed: %s %s", resp.status, await resp.text())
            return message_page(request, "Login failed", "Discord didn't accept the login. Please try again.")
        token = (await resp.json())["access_token"]

    headers = {"Authorization": f"Bearer {token}"}
    async with bot.session.get(f"{DISCORD_API}/users/@me", headers=headers) as resp:
        user = await resp.json()
    async with bot.session.get(f"{DISCORD_API}/users/@me/guilds", headers=headers) as resp:
        guilds = await resp.json() if resp.status == 200 else []

    manageable = [
        {"id": g["id"], "name": g["name"], "icon": g.get("icon")}
        for g in guilds
        if g.get("owner") or int(g.get("permissions", 0)) & (MANAGE_GUILD | ADMINISTRATOR)
    ]
    sid = secrets.token_urlsafe(32)
    bot.db.create_session(sid, user, manageable, secrets.token_urlsafe(24), SESSION_DAYS)

    resp = web.HTTPFound(safe_next(request.cookies.get("oauth_next")))
    resp.set_cookie("sid", sid, max_age=SESSION_DAYS * 86400, httponly=True, samesite="Lax",
                    secure=bot.public_url.startswith("https://"))
    resp.del_cookie("oauth_state")
    resp.del_cookie("oauth_next")
    return resp


async def logout(request: web.Request):
    sid = request.cookies.get("sid")
    if sid:
        request.app["bot"].db.delete_session(sid)
    resp = web.HTTPFound("/")
    resp.del_cookie("sid")
    return resp


# ---------------------------------------------------------------------- dashboard
@require_login
async def servers(request: web.Request):
    bot = request.app["bot"]
    cards = []
    for g in request["user"]["guilds"]:
        guild = bot.get_guild(int(g["id"]))
        settings = bot.db.get_guild(int(g["id"])) if guild else None
        channel = guild.get_channel(settings["channel_id"]) if guild and settings["channel_id"] else None
        cards.append({
            **g, "in_server": guild is not None,
            "status": ("Paused" if settings and not settings["enabled"] else
                       f"Posting in #{channel.name}" if channel else
                       "Not set up yet") if guild else "Bot not added",
            "invite_url": bot.invite_url(int(g["id"])),
        })
    cards.sort(key=lambda c: (not c["in_server"], c["name"].lower()))
    return render(request, "servers.html", servers=cards)


async def authorize_guild(request: web.Request) -> discord.Guild:
    """Make sure the logged-in user can manage this server right now."""
    bot = request.app["bot"]
    try:
        guild_id = int(request.match_info["guild_id"])
    except ValueError:
        raise web.HTTPNotFound()
    if str(guild_id) not in {g["id"] for g in request["user"]["guilds"]}:
        raise web.HTTPForbidden(text="You don't have Manage Server in that server.")
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise web.HTTPFound(bot.invite_url(guild_id))
    member = guild.get_member(request["user"]["user_id"])
    if member is None:
        try:
            member = await guild.fetch_member(request["user"]["user_id"])
        except discord.HTTPException:
            raise web.HTTPForbidden(text="You're not a member of that server.")
    if not member.guild_permissions.manage_guild:
        raise web.HTTPForbidden(text="You no longer have Manage Server in that server.")
    return guild


def postable_channels(guild: discord.Guild):
    out = []
    for ch in sorted(guild.text_channels, key=lambda c: (c.category.position if c.category else -1, c.position)):
        perms = ch.permissions_for(guild.me)
        out.append({"id": ch.id, "name": ch.name,
                    "category": ch.category.name if ch.category else "",
                    "ok": perms.view_channel and perms.send_messages and perms.embed_links})
    return out


def pingable_roles(guild: discord.Guild):
    return [{"id": r.id, "name": r.name} for r in reversed(guild.roles)
            if not r.is_default() and not r.managed]


@require_login
async def guild_page(request: web.Request):
    bot = request.app["bot"]
    guild = await authorize_guild(request)
    settings = bot.db.get_guild(guild.id)
    cache = bot.db.handle_cache()
    sources = []
    for ref in settings["youtube_channels"]:
        channel_id, handle = normalize_channel_ref(ref)
        sources.append({"ref": ref, "resolved": bool(channel_id or cache.get(handle.lower()))})
    return render(request, "guild.html", guild=guild, settings=settings,
                  channels=postable_channels(guild), roles=pingable_roles(guild),
                  sources=sources, recent=bot.db.recent_posts(guild.id, 10),
                  tmdb_available=bot.tmdb_on, csrf=request["user"]["csrf"],
                  defaults=bot.config, notice=request.query.get("notice"),
                  error=request.query.get("error"))


def parse_keywords(raw: str) -> list[str]:
    words = [w.strip().lower() for w in raw.replace("\n", ",").split(",")]
    return [w[:40] for w in words if w][:30]


@require_login
async def guild_save(request: web.Request):
    bot = request.app["bot"]
    guild = await authorize_guild(request)
    form = await request.post()
    check_csrf(request, form)
    back = f"/dashboard/{guild.id}"

    channel_id = form.get("channel_id", "")
    channel = guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
    if channel_id and not isinstance(channel, discord.TextChannel):
        raise web.HTTPFound(back + "?error=channel")
    role_id = form.get("ping_role_id", "")
    role = guild.get_role(int(role_id)) if role_id.isdigit() else None

    refs = []
    for line in form.get("youtube_channels", "").splitlines():
        line = line.strip()[:100]
        if line and line.lower() not in (r.lower() for r in refs):
            refs.append(line)
    if len(refs) > 25:
        raise web.HTTPFound(back + "?error=too_many")

    settings = bot.db.get_guild(guild.id)
    settings.update(
        channel_id=channel.id if channel else None,
        enabled=form.get("enabled") == "on",
        ping_role_id=role.id if role else None,
        youtube_channels=refs,
        include_keywords=parse_keywords(form.get("include_keywords", "")),
        exclude_keywords=parse_keywords(form.get("exclude_keywords", "")),
        skip_shorts=form.get("skip_shorts") == "on",
        use_tmdb=form.get("use_tmdb") == "on",
        movies_only=form.get("movies_only") == "on",
    )
    bot.db.save_guild(settings)
    raise web.HTTPFound(back + "?notice=saved")


@require_login
async def guild_reset_sources(request: web.Request):
    bot = request.app["bot"]
    guild = await authorize_guild(request)
    form = await request.post()
    check_csrf(request, form)
    settings = bot.db.get_guild(guild.id)
    settings["youtube_channels"] = list(bot.config.get("youtube_channels", []))
    bot.db.save_guild(settings)
    raise web.HTTPFound(f"/dashboard/{guild.id}?notice=reset")


@require_login
async def guild_test(request: web.Request):
    bot = request.app["bot"]
    guild = await authorize_guild(request)
    form = await request.post()
    check_csrf(request, form)
    settings = bot.db.get_guild(guild.id)
    channel = guild.get_channel(settings["channel_id"]) if settings["channel_id"] else None
    back = f"/dashboard/{guild.id}"
    if channel is None:
        raise web.HTTPFound(back + "?error=no_channel")
    try:
        await channel.send("✅ Test post from the trailer bot - new movie trailers will show up here.")
    except discord.Forbidden:
        raise web.HTTPFound(back + "?error=forbidden")
    except discord.HTTPException:
        raise web.HTTPFound(back + "?error=send_failed")
    raise web.HTTPFound(back + "?notice=tested")


async def health(request: web.Request):
    bot = request.app["bot"]
    return web.json_response({"ok": True, "discord_ready": bot.is_ready(), "servers": len(bot.guilds)})


def create_app(bot) -> web.Application:
    app = web.Application(middlewares=[session_middleware], client_max_size=64 * 1024)
    app["bot"] = bot
    app.router.add_get("/", index)
    app.router.add_get("/login", login)
    app.router.add_get("/callback", callback)
    app.router.add_get("/logout", logout)
    app.router.add_get("/dashboard", servers)
    app.router.add_get("/dashboard/{guild_id}", guild_page)
    app.router.add_post("/dashboard/{guild_id}", guild_save)
    app.router.add_post("/dashboard/{guild_id}/test", guild_test)
    app.router.add_post("/dashboard/{guild_id}/reset-sources", guild_reset_sources)
    app.router.add_get("/healthz", health)
    app.router.add_static("/static", BASE / "static")
    return app
