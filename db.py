"""SQLite storage: per-server settings, discovered trailers, posts, login sessions."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS guilds (
    guild_id         INTEGER PRIMARY KEY,
    channel_id       INTEGER,
    enabled          INTEGER NOT NULL DEFAULT 1,
    ping_role_id     INTEGER,
    youtube_channels TEXT NOT NULL,
    include_keywords TEXT NOT NULL,
    exclude_keywords TEXT NOT NULL,
    skip_shorts      INTEGER NOT NULL DEFAULT 1,
    use_tmdb         INTEGER NOT NULL DEFAULT 1,
    movies_only      INTEGER NOT NULL DEFAULT 1,
    configured_at    TEXT,
    last_error       TEXT
);
CREATE TABLE IF NOT EXISTS videos (
    video_id      TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    source_name   TEXT NOT NULL,
    sources       TEXT NOT NULL,
    published     TEXT,
    discovered_at TEXT NOT NULL,
    seeded        INTEGER NOT NULL,
    release_date  TEXT,
    media_type    TEXT
);
CREATE INDEX IF NOT EXISTS videos_discovered ON videos(discovered_at);
CREATE TABLE IF NOT EXISTS posts (
    guild_id  INTEGER NOT NULL,
    video_id  TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    ok        INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (guild_id, video_id)
);
CREATE TABLE IF NOT EXISTS sources (
    source_key     TEXT PRIMARY KEY,
    initialized_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handles (
    handle     TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    sid        TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    username   TEXT NOT NULL,
    avatar     TEXT,
    guilds     TEXT NOT NULL,
    csrf       TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


def now_iso(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path, defaults: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.defaults = defaults

    def _migrate(self) -> None:
        """Add columns introduced after the first release to existing databases."""
        added = {
            "guilds": [("movies_only", "INTEGER NOT NULL DEFAULT 1")],
            "videos": [("media_type", "TEXT")],
        }
        for table, columns in added.items():
            existing = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self.conn.commit()

    # ------------------------------------------------------------------ guild settings
    def _guild_row_to_dict(self, row) -> dict:
        d = dict(row)
        for key in ("youtube_channels", "include_keywords", "exclude_keywords"):
            d[key] = json.loads(d[key])
        for key in ("enabled", "skip_shorts", "use_tmdb", "movies_only"):
            d[key] = bool(d[key])
        return d

    def get_guild(self, guild_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM guilds WHERE guild_id=?", (guild_id,)).fetchone()
        if row:
            return self._guild_row_to_dict(row)
        return {
            "guild_id": guild_id, "channel_id": None, "enabled": True, "ping_role_id": None,
            "youtube_channels": list(self.defaults["youtube_channels"]),
            "include_keywords": list(self.defaults["include_keywords"]),
            "exclude_keywords": list(self.defaults["exclude_keywords"]),
            "skip_shorts": bool(self.defaults.get("skip_shorts", True)),
            "use_tmdb": True, "movies_only": True, "configured_at": None, "last_error": None,
        }

    def save_guild(self, settings: dict) -> None:
        old = self.get_guild(settings["guild_id"])
        # "configured_at" marks the start of what this server should receive, so a
        # newly set (or re-enabled) channel only gets trailers from now on.
        if (settings["channel_id"] != old["channel_id"]
                or (settings["enabled"] and not old["enabled"])
                or not old["configured_at"]):
            settings["configured_at"] = now_iso()
        else:
            settings["configured_at"] = old["configured_at"]
        self.conn.execute(
            """INSERT INTO guilds (guild_id, channel_id, enabled, ping_role_id, youtube_channels,
                   include_keywords, exclude_keywords, skip_shorts, use_tmdb, movies_only,
                   configured_at, last_error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)
               ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id,
                   enabled=excluded.enabled, ping_role_id=excluded.ping_role_id,
                   youtube_channels=excluded.youtube_channels, include_keywords=excluded.include_keywords,
                   exclude_keywords=excluded.exclude_keywords, skip_shorts=excluded.skip_shorts,
                   use_tmdb=excluded.use_tmdb, movies_only=excluded.movies_only,
                   configured_at=excluded.configured_at, last_error=NULL""",
            (settings["guild_id"], settings["channel_id"], int(settings["enabled"]),
             settings["ping_role_id"], json.dumps(settings["youtube_channels"]),
             json.dumps(settings["include_keywords"]), json.dumps(settings["exclude_keywords"]),
             int(settings["skip_shorts"]), int(settings["use_tmdb"]), int(settings["movies_only"]),
             settings["configured_at"]))
        self.conn.commit()

    def set_guild_error(self, guild_id: int, error: str | None) -> None:
        self.conn.execute("UPDATE guilds SET last_error=? WHERE guild_id=?", (error, guild_id))
        self.conn.commit()

    def active_guilds(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM guilds WHERE enabled=1 AND channel_id IS NOT NULL").fetchall()
        return [self._guild_row_to_dict(r) for r in rows]

    def all_guilds(self) -> list[dict]:
        return [self._guild_row_to_dict(r) for r in self.conn.execute("SELECT * FROM guilds")]

    # ------------------------------------------------------------------ handles
    def handle_cache(self) -> dict:
        return {r["handle"]: r["channel_id"] for r in self.conn.execute("SELECT * FROM handles")}

    def save_handle_cache(self, cache: dict) -> None:
        self.conn.executemany("INSERT OR REPLACE INTO handles VALUES (?,?)", cache.items())
        self.conn.commit()

    # ------------------------------------------------------------------ discovery
    def record_trailers(self, trailers, max_age_days: int) -> list[dict]:
        """Store newly found trailers. Returns the new, postable ones."""
        initialized = {r[0] for r in self.conn.execute("SELECT source_key FROM sources")}
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        found_at = now_iso()
        fresh: list[dict] = []
        for t in trailers:
            row = self.conn.execute("SELECT sources FROM videos WHERE video_id=?",
                                    (t.video_id,)).fetchone()
            if row:
                srcs = set(json.loads(row["sources"]))
                if t.source_key not in srcs:
                    srcs.add(t.source_key)
                    self.conn.execute("UPDATE videos SET sources=? WHERE video_id=?",
                                      (json.dumps(sorted(srcs)), t.video_id))
                    if t.source_key == "tmdb":  # TMDB only lists movies
                        self.conn.execute("UPDATE videos SET media_type='movie' WHERE video_id=?",
                                          (t.video_id,))
                continue
            # First time we see a source, its back catalogue is recorded but never posted.
            seeded = t.source_key not in initialized or (t.published is not None and t.published < cutoff)
            media_type = "movie" if t.source_key == "tmdb" else None
            self.conn.execute(
                "INSERT INTO videos (video_id, title, url, source_name, sources, published, discovered_at,"
                " seeded, release_date, media_type) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (t.video_id, t.title, t.url, t.source_name, json.dumps([t.source_key]),
                 t.published.isoformat(timespec="seconds") if t.published else None,
                 found_at, int(seeded), t.extra.get("release_date"), media_type))
            if not seeded:
                fresh.append({"video_id": t.video_id, "title": t.title, "media_type": media_type})
        for key in {t.source_key for t in trailers} - initialized:
            self.conn.execute("INSERT OR IGNORE INTO sources VALUES (?,?)", (key, found_at))
        self.conn.commit()
        return fresh

    def set_media_type(self, video_id: str, media_type: str) -> None:
        self.conn.execute("UPDATE videos SET media_type=? WHERE video_id=?", (media_type, video_id))
        self.conn.commit()

    def candidates_for(self, guild: dict, window_hours: int = 48) -> list[dict]:
        since = max(guild["configured_at"] or now_iso(), now_iso(-timedelta(hours=window_hours)))
        rows = self.conn.execute(
            """SELECT v.* FROM videos v
               WHERE v.seeded=0 AND v.discovered_at > ?
                 AND NOT EXISTS (SELECT 1 FROM posts p WHERE p.guild_id=? AND p.video_id=v.video_id)
               ORDER BY COALESCE(v.published, v.discovered_at)""",
            (since, guild["guild_id"])).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["sources"] = set(json.loads(d["sources"]))
            out.append(d)
        return out

    def record_post(self, guild_id: int, video_id: str, ok: bool) -> None:
        self.conn.execute("INSERT OR REPLACE INTO posts VALUES (?,?,?,?)",
                          (guild_id, video_id, now_iso(), int(ok)))
        self.conn.commit()

    def recent_posts(self, guild_id: int, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            """SELECT v.title, v.url, v.source_name, p.posted_at FROM posts p
               JOIN videos v ON v.video_id=p.video_id
               WHERE p.guild_id=? AND p.ok=1 ORDER BY p.posted_at DESC LIMIT ?""",
            (guild_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def prune(self, days: int = 120) -> None:
        cutoff = now_iso(-timedelta(days=days))
        self.conn.execute("DELETE FROM posts WHERE posted_at < ?", (cutoff,))
        self.conn.execute("DELETE FROM videos WHERE discovered_at < ?", (cutoff,))
        self.conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))
        self.conn.commit()

    # ------------------------------------------------------------------ web sessions
    def create_session(self, sid, user, guilds, csrf, days: int = 7) -> None:
        self.conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                          (sid, int(user["id"]), user.get("global_name") or user["username"],
                           user.get("avatar"), json.dumps(guilds), csrf, now_iso(timedelta(days=days))))
        self.conn.commit()

    def get_session(self, sid: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE sid=? AND expires_at > ?",
                                (sid, now_iso())).fetchone()
        if not row:
            return None
        d = dict(row)
        d["guilds"] = json.loads(d["guilds"])
        return d

    def delete_session(self, sid: str) -> None:
        self.conn.execute("DELETE FROM sessions WHERE sid=?", (sid,))
        self.conn.commit()
