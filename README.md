# Discord Movie Trailer Bot

Posts new movie trailers to Discord servers as soon as they appear on official
studio YouTube channels and TMDB. Server admins set it up on a **web dashboard**:
they log in with Discord, pick a server, and choose the channel, a role to ping,
the studios to watch and the filters. Each server has its own settings.

The bot and the dashboard run together in one process (`python bot.py`).

```
bot.py        Discord bot, trailer checks, slash commands; starts the web server
web.py        Dashboard: Discord login, server list, settings pages
db.py         SQLite storage (settings, trailers seen, posts, logins)
sources.py    YouTube RSS + TMDB fetching
config.json   Check interval, limits, and the default settings for new servers
templates/    Dashboard HTML      static/   Dashboard CSS
```

## 1. Create the Discord application

1. Go to https://discord.com/developers/applications → **New Application**.
2. **Bot** tab → **Reset Token** → copy it (`DISCORD_TOKEN`).
3. **OAuth2** tab → copy the **Client ID** and **Reset Secret** (`DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`).
4. Still on **OAuth2** → **Redirects** → add `<PUBLIC_URL>/callback`, for example
   `http://localhost:8080/callback` for testing and `https://your-app.example.com/callback` once deployed.
   You can add both.
5. (Recommended) Get a free TMDB key at https://www.themoviedb.org/settings/api (`TMDB_API_KEY`).

## 2. Run it on your own computer (to try it out)

Copy `.env.example` to `.env`, fill it in, then:

- **Windows:** double-click `run.bat`
- **Linux / macOS:** `./run.sh`

Open http://localhost:8080, click **Add to Discord**, then **Open dashboard**.

## 3. Deploy it

You need a host that keeps a process running all the time and has **persistent
storage** for `/data`, where the database lives. Set the variables from `.env.example`
as environment variables on the host, and set `PUBLIC_URL` to the host's https address.

### Option A: Any Linux server with Docker (VPS, home server, Raspberry Pi)

`docker-compose.prod.yml` runs the bot behind [Caddy](https://caddyserver.com), which
serves the dashboard over HTTPS and renews the certificate on its own.

1. Point your domain (or a subdomain like `trailers.example.com`) at the server with a DNS **A record**.
2. Open ports **80** and **443** on the server's firewall.
3. Set up the config and start it:

```bash
git clone https://github.com/Rainear21/Movie_Trailer_Bot.git
cd Movie_Trailer_Bot
cp .env.example .env    # fill in the Discord settings and DOMAIN
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml logs -f
```

4. In the Discord Developer Portal, add `https://<your domain>/callback` under OAuth2 → Redirects.

To update later: `git pull && docker compose -f docker-compose.prod.yml up -d --build`.

The plain `docker-compose.yml` runs without HTTPS on port 8080, for testing or for
use behind a proxy you already have.

### Option B: Railway / Render / Fly.io

1. Push this folder to a GitHub repo. `.env` is git-ignored, so the secrets stay out.
2. Create a new service from the repo. These hosts detect the `Dockerfile` automatically.
3. Add the environment variables. `PORT` is usually provided by the host.
4. **Attach a volume mounted at `/data`.** Without it, settings reset on every deploy.
5. Copy the service's public URL into `PUBLIC_URL`, and add `<that URL>/callback`
   to the Discord redirects.

The service needs to stay running all the time, so avoid free tiers that sleep when idle.
The health check endpoint is `/healthz`.

## Slash commands

| Command | Who | What it does |
|---|---|---|
| `/trailers dashboard` | Everyone | Link to this server's dashboard page |
| `/trailers setchannel [channel]` | Manage Server | Quick setup without the dashboard |
| `/trailers stop` | Manage Server | Pause posting |
| `/trailers check` | Manage Server | Check for new trailers now |
| `/trailers latest [count]` | Everyone | Recently posted trailers |

The first time the bot starts, slash commands can take a few minutes to show up in Discord.

## How it avoids spam

- When a studio channel is watched for the first time, the videos already on it
  are recorded but never posted.
- A server only gets trailers found **after** it picked its channel.
- Each trailer is posted at most once per server, even if YouTube and TMDB both list it.
- Trailers older than `max_age_days` (in `config.json`) are ignored.

## Security notes

- Only people with **Manage Server** can change a server's settings. This is checked
  live against Discord on every page load and save, so removing someone's permission
  takes effect right away.
- Logins are stored server-side and last 7 days. Forms are protected against CSRF.
- Keep `.env` and `DISCORD_CLIENT_SECRET` private. If they leak, reset them in the Developer Portal.
