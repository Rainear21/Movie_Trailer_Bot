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

`docker-compose.yml` builds the bot straight from this GitHub repo, so the server
only needs two files: `docker-compose.yml` and your `.env`.

```bash
mkdir movie-trailer-bot && cd movie-trailer-bot
curl -O https://raw.githubusercontent.com/Rainear21/Movie_Trailer_Bot/main/docker-compose.yml
curl -o .env https://raw.githubusercontent.com/Rainear21/Movie_Trailer_Bot/main/.env.example
nano .env               # fill in the Discord settings and PUBLIC_URL
docker compose up -d
docker compose logs -f
```

**To update:** run `docker compose up -d` again. Each time, it downloads the latest
code from the `main` branch and rebuilds. Settings are kept in the `trailerbot-data` volume.

The dashboard is served on port **8080**. Open that port on the server's firewall and
set `PUBLIC_URL` to the address people will use (for example `http://your-server-ip:8080`),
then add `<PUBLIC_URL>/callback` under OAuth2 → Redirects in the Discord Developer Portal.

Building from GitHub only works while the repo is public. For a private repo,
clone it and change `context:` in `docker-compose.yml` to `.`.

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
| `/trailer <movie>` | Everyone | Find any movie's official trailer (suggests titles as you type; needs TMDB) |
| `/trailers dashboard` | Everyone | Link to this server's dashboard page |
| `/trailers setchannel [channel]` | Manage Server | Quick setup without the dashboard |
| `/trailers stop` | Manage Server | Pause posting |
| `/trailers check` | Manage Server | Check for new trailers now |
| `/trailers latest [count]` | Everyone | Recently posted trailers |

The first time the bot starts, slash commands can take a few minutes to show up in Discord.

## Movies only

Studio channels like Netflix also post TV series trailers. With **Movies only** turned on
in the dashboard (the default), the bot looks up each new YouTube trailer on TMDB and skips
it if it's a series. Without a TMDB key, it only catches titles that mention a season or series.

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
