"""SQLite storage: per-server settings, trailers, posts, follows, logins and stats."""
from __future__ import annotations

import json
import re
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
    genres_include   TEXT NOT NULL DEFAULT '[]',
    genres_exclude   TEXT NOT NULL DEFAULT '[]',
    digest_enabled   INTEGER NOT NULL DEFAULT 0,
    digest_day       INTEGER NOT NULL DEFAULT 4,
    digest_hour      INTEGER NOT NULL DEFAULT 10,
    timezone         TEXT NOT NULL DEFAULT 'America/Los_Angeles',
    last_digest      TEXT,
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
    media_type    TEXT,
    genres        TEXT
);
CREATE INDEX IF NOT EXISTS videos_discovered ON videos(discovered_at);
CREATE TABLE IF NOT EXISTS posts (
    guild_id  INTEGER NOT NULL,
    video_id  TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    ok        INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (guild_id, video_id)
);
CREATE INDEX IF NOT EXISTS posts_time ON posts(posted_at);
CREATE TABLE IF NOT EXISTS follows (
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    keyword    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id, keyword)
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

# Columns added after the first release: (table, column, declaration).
MIGRATIONS = [
    ("guilds", "movies_only", "INTEGER NOT NULL DEFAULT 1"),
    ("guilds", "genres_include", "TEXT NOT NULL DEFAULT '[]'"),
    ("guilds", "genres_exclude", "TEXT NOT NULL DEFAULT '[]'"),
    ("guilds", "digest_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("guilds", "digest_day", "INTEGER NOT NULL DEFAULT 4"),
    ("guilds", "digest_hour", "INTEGER NOT NULL DEFAULT 10"),
    ("guilds", "timezone", "TEXT NOT NULL DEFAULT 'America/Los_Angeles'"),
    ("guilds", "last_digest", "TEXT"),
    ("videos", "media_type", "TEXT"),
    ("videos", "genres", "TEXT"),
]

# Settings a server admin can change: name -> kind.
SETTINGS = {
    "channel_id": "int", "enabled": "bool", "ping_role_id": "int",
    "youtube_channels": "json", "include_keywords": "json", "exclude_keywords": "json",
    "skip_shorts": "bool", "use_tmdb": "bool", "movies_only": "bool",
    "genres_include": "json", "genres_exclude": "json",
    "digest_enabled": "bool", "digest_day": "int", "digest_hour": "int", "timezone": "str",
}
MAX_FOLLOWS_PER_USER = 25


def now_iso(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds")


def follow_matches(keyword: str, text: str) -> bool:
    return re.search(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", text, re.I) is not None


class Database:
    def __init__(self, path: Path, defaults: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()
        self.conn.executescript(SCHEMA)
        self.defaults = defaults

    def _migrate(self) -> None:
        """Add columns introduced after the first release to existing databases."""
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, name, decl in MIGRATIONS:
            if table not in tables:
                continue  # brand-new database; SCHEMA creates it with every column
            existing = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if name not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self.conn.commit()

    def _count(self, sql: str, *args) -> int:
        return self.conn.execute(sql, args).fetchone()[0] or 0

    # ------------------------------------------------------------------ guild settings
    def _guild_row_to_dict(self, row) -> dict:
        d = dict(row)
        for key, kind in SETTINGS.items():
            if kind == "json":
                d[key] = json.loads(d[key])
            elif kind == "bool":
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
            "use_tmdb": True, "movies_only": True, "genres_include": [], "genres_exclude": [],
            "digest_enabled": False, "digest_day": 4, "digest_hour": 10,
            "timezone": self.defaults.get("default_timezone", "America/Los_Angeles"),
            "last_digest": None, "configured_at": None, "last_error": None,
        }

    def save_guild(self, settings: dict) -> None:
        old = self.get_guild(settings["guild_id"])
        # "configured_at" marks the start of what this server should receive, so a
        # newly set (or re-enabled) channel only gets trailers from now on.
        if (settings["channel_id"] != old["channel_id"]
                or (settings["enabled"] and not old["enabled"])
                or not old["configured_at"]):
            configured_at = now_iso()
        else:
            configured_at = old["configured_at"]

        values = []
        for key, kind in SETTINGS.items():
            v = settings[key]
            values.append(json.dumps(v) if kind == "json" else int(v) if kind == "bool" else v)
        cols = list(SETTINGS) + ["configured_at"]
        self.conn.execute(
            f"""INSERT INTO guilds (guild_id, {", ".join(cols)}, last_error)
                VALUES (?, {", ".join("?" * len(cols))}, NULL)
                ON CONFLICT(guild_id) DO UPDATE SET
                {", ".join(f"{c}=excluded.{c}" for c in cols)}, last_error=NULL""",
            (settings["guild_id"], *values, configured_at))
        self.conn.commit()

    def set_guild_error(self, guild_id: int, error: str | None) -> None:
        self.conn.execute("UPDATE guilds SET last_error=? WHERE guild_id=?", (error, guild_id))
        self.conn.commit()

    def set_last_digest(self, guild_id: int, local_date: str) -> None:
        self.conn.execute("UPDATE guilds SET last_digest=? WHERE guild_id=?", (local_date, guild_id))
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
                    if t.source_key == "tmdb":  # TMDB only lists movies, and knows the genres
                        self.conn.execute("UPDATE videos SET media_type='movie', genres=? WHERE video_id=?",
                                          (json.dumps(t.extra.get("genres", [])), t.video_id))
                continue
            # First time we see a source, its back catalogue is recorded but never posted.
            seeded = t.source_key not in initialized or (t.published is not None and t.published < cutoff)
            is_tmdb = t.source_key == "tmdb"
            self.conn.execute(
                "INSERT INTO videos (video_id, title, url, source_name, sources, published, discovered_at,"
                " seeded, release_date, media_type, genres) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (t.video_id, t.title, t.url, t.source_name, json.dumps([t.source_key]),
                 t.published.isoformat(timespec="seconds") if t.published else None,
                 found_at, int(seeded), t.extra.get("release_date"),
                 "movie" if is_tmdb else None,
                 json.dumps(t.extra.get("genres", [])) if is_tmdb else None))
            if not seeded:
                fresh.append({"video_id": t.video_id, "title": t.title,
                              "media_type": "movie" if is_tmdb else None})
        for key in {t.source_key for t in trailers} - initialized:
            self.conn.execute("INSERT OR IGNORE INTO sources VALUES (?,?)", (key, found_at))
        self.conn.commit()
        return fresh

    def set_classification(self, video_id: str, media_type: str, genres: list[int]) -> None:
        self.conn.execute("UPDATE videos SET media_type=?, genres=? WHERE video_id=?",
                          (media_type, json.dumps(genres), video_id))
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
            d["genres"] = json.loads(d["genres"]) if d.get("genres") else []
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

    # ------------------------------------------------------------------ follows
    def add_follow(self, guild_id: int, user_id: int, keyword: str) -> str:
        """Returns 'added', 'exists' or 'limit'."""
        if self.conn.execute("SELECT 1 FROM follows WHERE guild_id=? AND user_id=? AND keyword=?",
                             (guild_id, user_id, keyword)).fetchone():
            return "exists"
        if self._count("SELECT COUNT(*) FROM follows WHERE guild_id=? AND user_id=?",
                       guild_id, user_id) >= MAX_FOLLOWS_PER_USER:
            return "limit"
        self.conn.execute("INSERT INTO follows VALUES (?,?,?,?)", (guild_id, user_id, keyword, now_iso()))
        self.conn.commit()
        return "added"

    def remove_follow(self, guild_id: int, user_id: int, keyword: str) -> bool:
        cur = self.conn.execute("DELETE FROM follows WHERE guild_id=? AND user_id=? AND keyword=?",
                                (guild_id, user_id, keyword))
        self.conn.commit()
        return cur.rowcount > 0

    def user_follows(self, guild_id: int, user_id: int) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT keyword FROM follows WHERE guild_id=? AND user_id=? ORDER BY keyword",
            (guild_id, user_id))]

    def followers_matching(self, guild_id: int, text: str) -> dict[int, list[str]]:
        """user_id -> the keywords of theirs that match this text."""
        out: dict[int, list[str]] = {}
        for r in self.conn.execute("SELECT user_id, keyword FROM follows WHERE guild_id=?", (guild_id,)):
            if follow_matches(r["keyword"], text):
                out.setdefault(r["user_id"], []).append(r["keyword"])
        return out

    def follow_summary(self, guild_id: int, top: int = 8) -> dict:
        people = self._count("SELECT COUNT(DISTINCT user_id) FROM follows WHERE guild_id=?", guild_id)
        rows = self.conn.execute(
            """SELECT keyword, COUNT(*) n FROM follows WHERE guild_id=?
               GROUP BY keyword ORDER BY n DESC, keyword LIMIT ?""", (guild_id, top)).fetchall()
        return {"people": people, "top": [dict(r) for r in rows]}

    # ------------------------------------------------------------------ owner stats
    def stats(self) -> dict:
        def since(**kw):
            return now_iso(-timedelta(**kw))
        return {
            "configured": self._count("SELECT COUNT(*) FROM guilds WHERE channel_id IS NOT NULL"),
            "active": self._count("SELECT COUNT(*) FROM guilds WHERE enabled=1 AND channel_id IS NOT NULL"),
            "digest": self._count("SELECT COUNT(*) FROM guilds WHERE digest_enabled=1 AND enabled=1"
                                  " AND channel_id IS NOT NULL"),
            "posts_24h": self._count("SELECT COUNT(*) FROM posts WHERE ok=1 AND posted_at > ?", since(days=1)),
            "posts_7d": self._count("SELECT COUNT(*) FROM posts WHERE ok=1 AND posted_at > ?", since(days=7)),
            "posts_30d": self._count("SELECT COUNT(*) FROM posts WHERE ok=1 AND posted_at > ?", since(days=30)),
            "failed_7d": self._count("SELECT COUNT(*) FROM posts WHERE ok=0 AND posted_at > ?", since(days=7)),
            "trailers_7d": self._count("SELECT COUNT(*) FROM videos WHERE seeded=0 AND discovered_at > ?",
                                       since(days=7)),
            "follows": self._count("SELECT COUNT(*) FROM follows"),
            "followers": self._count("SELECT COUNT(DISTINCT user_id) FROM follows"),
            "sessions": self._count("SELECT COUNT(*) FROM sessions WHERE expires_at > ?", now_iso()),
            "top_sources": [dict(r) for r in self.conn.execute(
                """SELECT v.source_name, COUNT(*) n FROM posts p JOIN videos v ON v.video_id=p.video_id
                   WHERE p.ok=1 AND p.posted_at > ? GROUP BY v.source_name ORDER BY n DESC LIMIT 8""",
                (since(days=30),))],
            "recent_trailers": [dict(r) for r in self.conn.execute(
                """SELECT title, url, source_name, discovered_at, media_type,
                          (SELECT COUNT(*) FROM posts p WHERE p.video_id=v.video_id AND p.ok=1) AS servers
                   FROM videos v WHERE seeded=0 ORDER BY discovered_at DESC LIMIT 12""")],
            "posts_by_guild": {r[0]: r[1] for r in self.conn.execute(
                "SELECT guild_id, COUNT(*) FROM posts WHERE ok=1 AND posted_at > ? GROUP BY guild_id",
                (since(days=30),))},
            "follows_by_guild": {r[0]: r[1] for r in self.conn.execute(
                "SELECT guild_id, COUNT(*) FROM follows GROUP BY guild_id")},
        }

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
