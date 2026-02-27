import os
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Tuple, Dict, Any

import discord
from discord import app_commands
import psycopg2
import psycopg2.extras


# =============================
# ENV
# =============================
TOKEN = os.getenv("DISCORD_TOKEN")

DATABASE_URL = (
    os.getenv("DATABASE_URL")
    or os.getenv("DATABASE_PRIVATE_URL")
    or os.getenv("PGDATABASE_URL")
)

# Optional: instant command sync in a single server
GUILD_ID = os.getenv("GUILD_ID")  # server id as string; optional

PGSSLMODE = os.getenv("PGSSLMODE", "prefer")


# =============================
# REL TYPES
# =============================
REL_TYPES = ("romantic", "platonic", "familial")


def normalize_rel_type(t: Optional[str]) -> str:
    if not t:
        return "platonic"
    t = t.strip().lower()
    if t not in REL_TYPES:
        raise ValueError(f"Invalid relationship type: {t}. Must be one of {REL_TYPES}.")
    return t


REL_TYPE_CHOICES = [
    app_commands.Choice(name="romantic", value="romantic"),
    app_commands.Choice(name="platonic", value="platonic"),
    app_commands.Choice(name="familial", value="familial"),
]

REL_TYPE_PLUS_ALL_CHOICES = [
    app_commands.Choice(name="all", value="all"),
    *REL_TYPE_CHOICES
]


# =============================
# TIME / DB HELPERS
# =============================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat(timespec="seconds")


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Python can parse Z? usually no; Railway gives +00:00
        return datetime.fromisoformat(s)
    except Exception:
        return None


def clamp_score(score: int) -> int:
    return max(-100, min(100, int(score)))


def normalize_pair(name1: str, name2: str) -> Tuple[str, str]:
    n1, n2 = name1.strip(), name2.strip()
    return (n1, n2) if n1.casefold() < n2.casefold() else (n2, n1)


def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("Missing DATABASE_URL. Add a Postgres database and set DATABASE_URL.")
    return psycopg2.connect(DATABASE_URL, sslmode=PGSSLMODE)


# =============================
# DB INIT + MIGRATIONS
# =============================
def db_init() -> None:
    con = db_connect()
    cur = con.cursor()

    # Characters
    cur.execute("""
    CREATE TABLE IF NOT EXISTS characters (
        id SERIAL PRIMARY KEY,
        guild_id TEXT NOT NULL,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """)

    # Relationships
    # NOTE: last_player_update_at is used for decay cooldown (decay never changes it)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS relationships (
        id SERIAL PRIMARY KEY,
        guild_id TEXT NOT NULL,
        a_name TEXT NOT NULL,
        b_name TEXT NOT NULL,
        rel_type TEXT,
        score INTEGER NOT NULL,
        updated_by TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_player_update_at TEXT,
        note TEXT
    );
    """)

    # History
    cur.execute("""
    CREATE TABLE IF NOT EXISTS rel_history (
        id SERIAL PRIMARY KEY,
        guild_id TEXT NOT NULL,
        a_name TEXT NOT NULL,
        b_name TEXT NOT NULL,
        rel_type TEXT,
        delta INTEGER NOT NULL,
        new_score INTEGER NOT NULL,
        updated_by TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        reason TEXT
    );
    """)

    # Server settings (milestone log channel)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id TEXT PRIMARY KEY,
        log_channel_id TEXT
    );
    """)

    # Decay settings (per guild + rel_type)
    # paused_until applies server-wide (we store it redundantly per rel_type for simplicity)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS rel_decay_settings (
        guild_id TEXT NOT NULL,
        rel_type TEXT NOT NULL,
        enabled BOOLEAN NOT NULL DEFAULT FALSE,
        decay_amount INTEGER NOT NULL DEFAULT 1,
        interval_minutes INTEGER NOT NULL DEFAULT 360,
        cooldown_minutes INTEGER NOT NULL DEFAULT 720,
        paused_until TEXT,
        freeze_familial BOOLEAN NOT NULL DEFAULT FALSE,
        PRIMARY KEY (guild_id, rel_type)
    );
    """)

    # Per-relationship freeze (no decay until freeze_until)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS rel_freeze (
        id SERIAL PRIMARY KEY,
        guild_id TEXT NOT NULL,
        a_name TEXT NOT NULL,
        b_name TEXT NOT NULL,
        rel_type TEXT NOT NULL,
        freeze_until TEXT NOT NULL,
        reason TEXT,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """)

    # --- Migrations: ensure columns exist and are NOT NULL where needed ---
    cur.execute("ALTER TABLE relationships ADD COLUMN IF NOT EXISTS rel_type TEXT;")
    cur.execute("ALTER TABLE rel_history ADD COLUMN IF NOT EXISTS rel_type TEXT;")
    cur.execute("ALTER TABLE relationships ADD COLUMN IF NOT EXISTS last_player_update_at TEXT;")
    cur.execute("ALTER TABLE relationships ADD COLUMN IF NOT EXISTS note TEXT;")

    # Backfill existing rows
    cur.execute("UPDATE relationships SET rel_type='platonic' WHERE rel_type IS NULL;")
    cur.execute("UPDATE rel_history SET rel_type='platonic' WHERE rel_type IS NULL;")

    # Enforce NOT NULL for rel_type (new + old rows now filled)
    cur.execute("ALTER TABLE relationships ALTER COLUMN rel_type SET NOT NULL;")
    cur.execute("ALTER TABLE rel_history ALTER COLUMN rel_type SET NOT NULL;")

    # Unique indexes (case-insensitive)
    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS ux_characters_guild_lower_name
    ON characters (guild_id, lower(name));
    """)

    # Relationships unique per type
    cur.execute("DROP INDEX IF EXISTS ux_relationships_guild_lower_pair;")
    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS ux_relationships_guild_lower_pair_type
    ON relationships (guild_id, lower(a_name), lower(b_name), rel_type);
    """)

    # Freeze uniqueness (one active row per pair/type at a time is not enforced strictly,
    # but we keep lookup fast)
    cur.execute("""
    CREATE INDEX IF NOT EXISTS ix_rel_freeze_lookup
    ON rel_freeze (guild_id, lower(a_name), lower(b_name), rel_type);
    """)

    con.commit()
    cur.close()
    con.close()


# =============================
# DB: CHARACTERS
# =============================
def character_exists(guild_id: str, name: str) -> bool:
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        "SELECT 1 FROM characters WHERE guild_id=%s AND lower(name)=lower(%s) LIMIT 1",
        (guild_id, name),
    )
    ok = cur.fetchone() is not None
    cur.close()
    con.close()
    return ok


def add_character(guild_id: str, name: str) -> None:
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO characters (guild_id, name, created_at) VALUES (%s, %s, %s)",
        (guild_id, name.strip(), now_iso()),
    )
    con.commit()
    cur.close()
    con.close()


def remove_character(guild_id: str, name: str) -> int:
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        "SELECT name FROM characters WHERE guild_id=%s AND lower(name)=lower(%s)",
        (guild_id, name),
    )
    row = cur.fetchone()
    if not row:
        cur.close()
        con.close()
        return 0

    stored = row[0]
    cur.execute("DELETE FROM characters WHERE guild_id=%s AND lower(name)=lower(%s)", (guild_id, stored))
    cur.execute(
        "DELETE FROM relationships WHERE guild_id=%s AND (lower(a_name)=lower(%s) OR lower(b_name)=lower(%s))",
        (guild_id, stored, stored),
    )
    cur.execute(
        "DELETE FROM rel_history WHERE guild_id=%s AND (lower(a_name)=lower(%s) OR lower(b_name)=lower(%s))",
        (guild_id, stored, stored),
    )
    cur.execute(
        "DELETE FROM rel_freeze WHERE guild_id=%s AND (lower(a_name)=lower(%s) OR lower(b_name)=lower(%s))",
        (guild_id, stored, stored),
    )
    con.commit()
    cur.close()
    con.close()
    return 1


def list_characters(guild_id: str) -> List[str]:
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        "SELECT name FROM characters WHERE guild_id=%s ORDER BY lower(name) ASC",
        (guild_id,),
    )
    rows = cur.fetchall()
    cur.close()
    con.close()
    return [r[0] for r in rows]


# =============================
# DB: RELATIONSHIPS
# =============================
def get_relationship(guild_id: str, name1: str, name2: str, rel_type: str):
    rel_type = normalize_rel_type(rel_type)
    a, b = normalize_pair(name1, name2)
    con = db_connect()
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT *
        FROM relationships
        WHERE guild_id=%s
          AND lower(a_name)=lower(%s)
          AND lower(b_name)=lower(%s)
          AND rel_type=%s
        """,
        (guild_id, a, b, rel_type),
    )
    row = cur.fetchone()
    cur.close()
    con.close()
    return row


def upsert_relationship(
    guild_id: str,
    name1: str,
    name2: str,
    rel_type: str,
    new_score: int,
    updated_by: str,
    note: Optional[str],
    delta_for_history: int,
    reason: Optional[str],
    is_player_action: bool = True,
) -> int:
    rel_type = normalize_rel_type(rel_type)
    a, b = normalize_pair(name1, name2)
    new_score = clamp_score(new_score)
    ts = now_iso()

    con = db_connect()
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        """
        SELECT score
        FROM relationships
        WHERE guild_id=%s
          AND lower(a_name)=lower(%s)
          AND lower(b_name)=lower(%s)
          AND rel_type=%s
        """,
        (guild_id, a, b, rel_type),
    )
    existing = cur.fetchone()

    last_player_update = ts if is_player_action else None

    if existing is None:
        cur.execute(
            """
            INSERT INTO relationships (guild_id, a_name, b_name, rel_type, score, updated_by, updated_at, last_player_update_at, note)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (guild_id, a, b, rel_type, new_score, updated_by, ts, last_player_update, note),
        )
    else:
        # Only update last_player_update_at if this is a player action
        cur.execute(
            """
            UPDATE relationships
            SET score=%s,
                updated_by=%s,
                updated_at=%s,
                last_player_update_at = CASE WHEN %s IS NULL THEN last_player_update_at ELSE %s END,
                note=COALESCE(%s, note)
            WHERE guild_id=%s
              AND lower(a_name)=lower(%s)
              AND lower(b_name)=lower(%s)
              AND rel_type=%s
            """,
            (new_score, updated_by, ts, last_player_update, last_player_update, note, guild_id, a, b, rel_type),
        )

    # History row
    cur.execute(
        """
        INSERT INTO rel_history (guild_id, a_name, b_name, rel_type, delta, new_score, updated_by, updated_at, reason)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (guild_id, a, b, rel_type, int(delta_for_history), new_score, updated_by, ts, reason),
    )

    con.commit()
    cur.close()
    con.close()
    return new_score


def add_to_relationship(
    guild_id: str,
    name1: str,
    name2: str,
    rel_type: str,
    delta: int,
    updated_by: str,
    reason: Optional[str],
) -> int:
    row = get_relationship(guild_id, name1, name2, rel_type)
    old = int(row["score"]) if row else 0
    new = clamp_score(old + int(delta))
    return upsert_relationship(
        guild_id=guild_id,
        name1=name1,
        name2=name2,
        rel_type=rel_type,
        new_score=new,
        updated_by=updated_by,
        note=None,
        delta_for_history=int(delta),
        reason=reason,
        is_player_action=True,
    )


def fetch_history(guild_id: str, name1: str, name2: str, rel_type: str, limit: int = 10):
    rel_type = normalize_rel_type(rel_type)
    a, b = normalize_pair(name1, name2)
    con = db_connect()
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT *
        FROM rel_history
        WHERE guild_id=%s
          AND lower(a_name)=lower(%s)
          AND lower(b_name)=lower(%s)
          AND rel_type=%s
        ORDER BY id DESC
        LIMIT %s
        """,
        (guild_id, a, b, rel_type, limit),
    )
    rows = cur.fetchall()
    cur.close()
    con.close()
    return rows


def top_relationships_for(guild_id: str, name: str, rel_type: Optional[str] = None, limit: int = 10):
    con = db_connect()
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if rel_type:
        rel_type = normalize_rel_type(rel_type)
        cur.execute(
            """
            SELECT *,
                   CASE WHEN lower(a_name)=lower(%s) THEN b_name ELSE a_name END AS other
            FROM relationships
            WHERE guild_id=%s
              AND (lower(a_name)=lower(%s) OR lower(b_name)=lower(%s))
              AND rel_type=%s
            ORDER BY score DESC
            LIMIT %s
            """,
            (name, guild_id, name, name, rel_type, limit),
        )
    else:
        cur.execute(
            """
            SELECT *,
                   CASE WHEN lower(a_name)=lower(%s) THEN b_name ELSE a_name END AS other
            FROM relationships
            WHERE guild_id=%s
              AND (lower(a_name)=lower(%s) OR lower(b_name)=lower(%s))
            ORDER BY score DESC
            LIMIT %s
            """,
            (name, guild_id, name, name, limit),
        )
    rows = cur.fetchall()
    cur.close()
    con.close()
    return rows


# =============================
# DB: GUILD SETTINGS (LOG CHANNEL)
# =============================
def get_log_channel_id(guild_id: str) -> Optional[int]:
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT log_channel_id FROM guild_settings WHERE guild_id=%s", (guild_id,))
    row = cur.fetchone()
    cur.close()
    con.close()
    if not row or not row[0]:
        return None
    try:
        return int(row[0])
    except ValueError:
        return None


def set_log_channel_id(guild_id: str, channel_id: int) -> None:
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO guild_settings (guild_id, log_channel_id)
        VALUES (%s, %s)
        ON CONFLICT (guild_id) DO UPDATE SET log_channel_id = EXCLUDED.log_channel_id
        """,
        (guild_id, str(channel_id)),
    )
    con.commit()
    cur.close()
    con.close()


def clear_log_channel_id(guild_id: str) -> None:
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO guild_settings (guild_id, log_channel_id)
        VALUES (%s, NULL)
        ON CONFLICT (guild_id) DO UPDATE SET log_channel_id = NULL
        """,
        (guild_id,),
    )
    con.commit()
    cur.close()
    con.close()


# =============================
# DB: DECAY SETTINGS + FREEZE
# =============================
def ensure_decay_rows(guild_id: str) -> None:
    con = db_connect()
    cur = con.cursor()
    for rt in REL_TYPES:
        cur.execute(
            """
            INSERT INTO rel_decay_settings (guild_id, rel_type)
            VALUES (%s, %s)
            ON CONFLICT (guild_id, rel_type) DO NOTHING
            """,
            (guild_id, rt),
        )
    con.commit()
    cur.close()
    con.close()


def get_decay_settings_rows(guild_id: str) -> List[Dict[str, Any]]:
    ensure_decay_rows(guild_id)
    con = db_connect()
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM rel_decay_settings WHERE guild_id=%s ORDER BY rel_type ASC",
        (guild_id,),
    )
    rows = cur.fetchall()
    cur.close()
    con.close()
    return rows


def set_decay_settings(
    guild_id: str,
    rel_type: str,
    enabled: Optional[bool] = None,
    decay_amount: Optional[int] = None,
    interval_minutes: Optional[int] = None,
    cooldown_minutes: Optional[int] = None,
    paused_until: Optional[str] = None,
    freeze_familial: Optional[bool] = None,
) -> None:
    rel_type = normalize_rel_type(rel_type)
    ensure_decay_rows(guild_id)

    con = db_connect()
    cur = con.cursor()
    cur.execute(
        """
        UPDATE rel_decay_settings
        SET enabled = COALESCE(%s, enabled),
            decay_amount = COALESCE(%s, decay_amount),
            interval_minutes = COALESCE(%s, interval_minutes),
            cooldown_minutes = COALESCE(%s, cooldown_minutes),
            paused_until = COALESCE(%s, paused_until),
            freeze_familial = COALESCE(%s, freeze_familial)
        WHERE guild_id=%s AND rel_type=%s
        """,
        (enabled, decay_amount, interval_minutes, cooldown_minutes, paused_until, freeze_familial, guild_id, rel_type),
    )
    con.commit()
    cur.close()
    con.close()


def set_pause_all_decay(guild_id: str, until_iso: Optional[str]) -> None:
    ensure_decay_rows(guild_id)
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        """
        UPDATE rel_decay_settings
        SET paused_until=%s
        WHERE guild_id=%s
        """,
        (until_iso, guild_id),
    )
    con.commit()
    cur.close()
    con.close()


def upsert_freeze(
    guild_id: str,
    name1: str,
    name2: str,
    rel_type: str,
    freeze_until_iso: str,
    reason: Optional[str],
    created_by: str,
) -> None:
    rel_type = normalize_rel_type(rel_type)
    a, b = normalize_pair(name1, name2)
    con = db_connect()
    cur = con.cursor()
    # We "upsert" by deleting existing and inserting one new row (simple + reliable).
    cur.execute(
        """
        DELETE FROM rel_freeze
        WHERE guild_id=%s AND lower(a_name)=lower(%s) AND lower(b_name)=lower(%s) AND rel_type=%s
        """,
        (guild_id, a, b, rel_type),
    )
    cur.execute(
        """
        INSERT INTO rel_freeze (guild_id, a_name, b_name, rel_type, freeze_until, reason, created_by, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (guild_id, a, b, rel_type, freeze_until_iso, reason, created_by, now_iso()),
    )
    con.commit()
    cur.close()
    con.close()


def clear_freeze(guild_id: str, name1: str, name2: str, rel_type: str) -> int:
    rel_type = normalize_rel_type(rel_type)
    a, b = normalize_pair(name1, name2)
    con = db_connect()
    cur = con.cursor()
    cur.execute(
        """
        DELETE FROM rel_freeze
        WHERE guild_id=%s AND lower(a_name)=lower(%s) AND lower(b_name)=lower(%s) AND rel_type=%s
        """,
        (guild_id, a, b, rel_type),
    )
    deleted = cur.rowcount
    con.commit()
    cur.close()
    con.close()
    return deleted


def list_freezes(guild_id: str) -> List[Dict[str, Any]]:
    con = db_connect()
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT *
        FROM rel_freeze
        WHERE guild_id=%s
        ORDER BY rel_type ASC, lower(a_name) ASC, lower(b_name) ASC
        """,
        (guild_id,),
    )
    rows = cur.fetchall()
    cur.close()
    con.close()
    return rows


# =============================
# BLACKTHORN COSMETICS
# =============================
BLACKTHORN_STAGES = {
    "romantic": [
        (-85, "Blood in the Water"),
        (-60, "Cutthroat"),
        (-30, "Bad History"),
        ( 10, "Playing It Cool"),
        ( 35, "Slowburn"),
        ( 65, "Back in the Saddle"),
        (100, "Endgame"),
    ],
    "platonic": [
        (-85, "Kill-on-Sight"),
        (-60, "No-Contact"),
        (-30, "Thin Ice"),
        ( 20, "Town-Polite"),
        ( 50, "Good Company"),
        ( 80, "Ride-or-Die"),
        (100, "Chosen Family"),
    ],
    "familial": [
        (-90, "Scorched Earth"),
        (-70, "Cut Off"),
        (-40, "Bad Blood"),
        ( 15, "Holding Pattern"),
        ( 45, "Mending Fences"),
        ( 75, "Blood & Bone"),
        (100, "Unbreakable"),
    ],
}

REL_TYPE_META = {
    "romantic": {"emoji": "💘", "title": "Romantic"},
    "platonic": {"emoji": "🤝", "title": "Platonic"},
    "familial": {"emoji": "🧬", "title": "Familial"},
}

# Simple color palette by "heat"
def heat_color(score: int) -> int:
    score = clamp_score(score)
    if score <= -85: return 0x8B0000  # dark red
    if score <= -60: return 0xC0392B
    if score <= -30: return 0xE67E22
    if score <=  20: return 0xF1C40F
    if score <=  50: return 0x2ECC71
    if score <=  80: return 0x3498DB
    return 0x9B59B6


def stage_label(score: int, rel_type: str = "platonic") -> str:
    rel_type = normalize_rel_type(rel_type)
    score = clamp_score(score)
    for upper, label in BLACKTHORN_STAGES[rel_type]:
        if score <= upper:
            return label
    return BLACKTHORN_STAGES[rel_type][-1][1]


def mood_line(score: int, rel_type: str) -> str:
    rel_type = normalize_rel_type(rel_type)
    score = clamp_score(score)

    if rel_type == "romantic":
        if score <= -85: return "Spite with a pulse. Somebody’s gonna bleed first."
        if score <= -60: return "Every look is a dare. Every word lands like a hook."
        if score <= -30: return "Chemistry they refuse to name. History they can’t outrun."
        if score <=  10: return "Careful distance. Watching for weakness. Wanting anyway."
        if score <=  35: return "Soft spots showing. Small mercies. Dangerous tenderness."
        if score <=  65: return "They keep finding their way back. Even when it’s stupid."
        return "It’s settled. This is the person they pick—again and again."

    if rel_type == "familial":
        if score <= -90: return "The kind of feud that poisons holidays."
        if score <= -70: return "Doors closed. Names not spoken."
        if score <= -40: return "Love is there—under the anger."
        if score <=  15: return "Quiet tension. Things left unsaid on purpose."
        if score <=  45: return "Trying. Showing up. Mending what can be mended."
        if score <=  75: return "Loyalty that hurts. Pride that runs deep."
        return "No matter what—blood shows up."

    # platonic
    if score <= -85: return "They’d cross the street rather than share air."
    if score <= -60: return "Bad for business. Worse for the heart."
    if score <= -30: return "One wrong move and it turns ugly."
    if score <=  20: return "Civil. Not close. Not cruel."
    if score <=  50: return "Easy laughs. Mutual respect. Same side, mostly."
    if score <=  80: return "If it goes down, they’re in it together."
    return "Family by choice. The real kind."


def milestone_message(old_score: int, new_score: int, rel_type: str) -> Optional[str]:
    rel_type = normalize_rel_type(rel_type)
    old_stage = stage_label(old_score, rel_type)
    new_stage = stage_label(new_score, rel_type)
    if old_stage == new_stage:
        return None
    return f"🏁 **Milestone:** *{old_stage}* → **{new_stage}**"


def heat_emoji(score: int) -> str:
    score = clamp_score(score)
    if score <= -85: return "🟥"
    if score <= -60: return "🔴"
    if score <= -30: return "🟠"
    if score <=  20: return "🟡"
    if score <=  50: return "🟢"
    if score <=  80: return "🔵"
    return "🟣"


def meter_bar(score: int, width: int = 20) -> str:
    score = clamp_score(score)
    filled = int(round(((score + 100) / 200) * width))
    filled = max(0, min(width, filled))
    bar = "█" * filled + "░" * (width - filled)
    mid = width // 2
    bar_list = list(bar)
    bar_list[mid] = "┃"
    return "".join(bar_list)


def ensure_guild(interaction: discord.Interaction) -> Optional[str]:
    return str(interaction.guild.id) if interaction.guild else None


def rel_type_title(rt: str) -> str:
    return normalize_rel_type(rt).capitalize()


def build_rel_embed(rel_type: str, a: str, b: str, score: int, note: Optional[str] = None, extra: Optional[str] = None) -> discord.Embed:
    rel_type = normalize_rel_type(rel_type)
    meta = REL_TYPE_META.get(rel_type, {"emoji": "🔗", "title": rel_type_title(rel_type)})
    status = stage_label(score, rel_type)
    mood = mood_line(score, rel_type)
    heat = heat_emoji(score)

    embed = discord.Embed(
        title=f"{meta['emoji']} {meta['title']}: {a} ↔ {b}",
        description=f"{heat} **{score}** • **{status}**\n`{meter_bar(score)}`\n*{mood}*",
        color=heat_color(score),
    )
    if note:
        embed.add_field(name="Note", value=note, inline=False)
    if extra:
        embed.add_field(name="Update", value=extra, inline=False)
    embed.set_footer(text="Blackthorn Relationship System")
    return embed


async def post_milestone_log(
    interaction: discord.Interaction,
    rel_type: str,
    a: str,
    b: str,
    old_score: int,
    new_score: int,
    delta: Optional[int],
    reason: Optional[str],
):
    """Logs ONLY milestones to the configured log channel (if set)."""
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return

    milestone = milestone_message(old_score, new_score, rel_type)
    if not milestone:
        return

    chan_id = get_log_channel_id(guild_id)
    if not chan_id:
        return

    channel = interaction.client.get_channel(chan_id)
    if channel is None:
        try:
            channel = await interaction.client.fetch_channel(chan_id)
        except Exception:
            return

    rel_type = normalize_rel_type(rel_type)
    meta = REL_TYPE_META.get(rel_type, {"emoji": "🔗", "title": rel_type_title(rel_type)})

    status = stage_label(new_score, rel_type)
    mood = mood_line(new_score, rel_type)
    heat = heat_emoji(new_score)

    delta_part = f" `{delta:+d}`" if delta is not None else ""
    reason_part = f"\n**Reason:** {reason}" if reason else ""

    msg = (
        f"{meta['emoji']} **{meta['title']} Milestone** — **{a} ↔ {b}**\n"
        f"{milestone}\n"
        f"{heat} **Score:** `{old_score}` → `{new_score}`{delta_part}  |  **Now:** **{status}**\n"
        f"*{mood}*"
        f"{reason_part}\n"
        f"**By:** {interaction.user.mention}"
    )

    try:
        await channel.send(msg)
    except Exception:
        return


# =============================
# DECAY ENGINE (WITH HISTORY)
# =============================
def apply_decay_tick_with_history(
    guild_id: str,
    rel_type: str,
    decay_amount: int,
    cooldown_minutes: int,
) -> int:
    """
    Apply a decay tick for one guild+type:
    - move score toward 0 by decay_amount
    - skip if last_player_update_at is within cooldown
    - skip if relationship is frozen (freeze_until > now)
    - write a rel_history row for each updated relationship (reason='DECAY', updated_by='DECAY')
    Returns number of relationships updated.
    """
    rel_type = normalize_rel_type(rel_type)
    decay_amount = max(1, int(decay_amount))
    cooldown_minutes = max(0, int(cooldown_minutes))

    ts = now_iso()
    cutoff = (now_utc() - timedelta(minutes=cooldown_minutes)).isoformat(timespec="seconds")

    con = db_connect()
    cur = con.cursor()

    # CTE: choose candidates, update, then insert history rows with delta.
    cur.execute(
        """
        WITH candidates AS (
            SELECT r.id, r.guild_id, r.a_name, r.b_name, r.rel_type, r.score AS old_score
            FROM relationships r
            WHERE r.guild_id=%s
              AND r.rel_type=%s
              AND r.score <> 0
              AND (r.last_player_update_at IS NULL OR r.last_player_update_at <= %s)
              AND NOT EXISTS (
                SELECT 1
                FROM rel_freeze f
                WHERE f.guild_id = r.guild_id
                  AND f.rel_type = r.rel_type
                  AND lower(f.a_name) = lower(r.a_name)
                  AND lower(f.b_name) = lower(r.b_name)
                  AND f.freeze_until > %s
              )
        ),
        updated AS (
            UPDATE relationships r
            SET score = CASE
                WHEN r.score > 0 THEN GREATEST(0, r.score - %s)
                WHEN r.score < 0 THEN LEAST(0, r.score + %s)
                ELSE 0
            END,
            updated_by = 'DECAY',
            updated_at = %s
            FROM candidates c
            WHERE r.id = c.id
            RETURNING r.guild_id, r.a_name, r.b_name, r.rel_type, c.old_score, r.score AS new_score
        )
        INSERT INTO rel_history (guild_id, a_name, b_name, rel_type, delta, new_score, updated_by, updated_at, reason)
        SELECT guild_id,
               a_name,
               b_name,
               rel_type,
               (new_score - old_score) AS delta,
               new_score,
               'DECAY',
               %s,
               'DECAY'
        FROM updated
        """,
        (guild_id, rel_type, cutoff, ts, decay_amount, decay_amount, ts, ts),
    )

    # rowcount here is inserted history count (same as updated count)
    updated_count = cur.rowcount

    con.commit()
    cur.close()
    con.close()
    return max(0, updated_count)


# =============================
# DISCORD BOT
# =============================
intents = discord.Intents.default()
intents.guilds = True  # ensure guild info is available for app commands


class RPBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.decay_task: Optional[asyncio.Task] = None

    async def setup_hook(self) -> None:
        # Register command groups BEFORE syncing
        self.tree.add_command(char_group)
        self.tree.add_command(settings_group)
        self.tree.add_command(rel_group)
        self.tree.add_command(decay_group)

        # Sync commands
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            await self.tree.sync(guild=guild)
            print(f"[sync] Synced commands to guild {GUILD_ID}")
        else:
            await self.tree.sync()
            print("[sync] Synced global commands (may take time to propagate)")

        # Start decay background loop
        if self.decay_task is None:
            self.decay_task = asyncio.create_task(self._decay_loop())

    async def _decay_loop(self):
        await self.wait_until_ready()
        last_run: Dict[Tuple[str, str], datetime] = {}

        while not self.is_closed():
            try:
                for g in self.guilds:
                    guild_id = str(g.id)
                    rows = get_decay_settings_rows(guild_id)

                    # Evaluate paused_until once (server-wide; stored in each row)
                    paused_until_iso = rows[0].get("paused_until") if rows else None
                    paused_until_dt = parse_iso(paused_until_iso)
                    if paused_until_dt and now_utc() < paused_until_dt:
                        continue

                    for s in rows:
                        rt = s["rel_type"]
                        enabled = bool(s["enabled"])
                        decay_amount = int(s["decay_amount"])
                        interval_minutes = int(s["interval_minutes"])
                        cooldown_minutes = int(s["cooldown_minutes"])
                        freeze_familial = bool(s.get("freeze_familial", False))

                        if not enabled:
                            continue
                        if freeze_familial and rt == "familial":
                            continue

                        key = (guild_id, rt)
                        now = now_utc()
                        last = last_run.get(key)

                        if last is None:
                            last_run[key] = now
                            continue

                        elapsed_min = (now - last).total_seconds() / 60.0
                        if elapsed_min >= max(1, interval_minutes):
                            apply_decay_tick_with_history(
                                guild_id=guild_id,
                                rel_type=rt,
                                decay_amount=decay_amount,
                                cooldown_minutes=cooldown_minutes,
                            )
                            last_run[key] = now
            except Exception:
                # keep bot alive
                pass

            await asyncio.sleep(60)


client = RPBot()


# =============================
# AUTOCOMPLETE
# =============================
async def character_autocomplete(interaction: discord.Interaction, current: str):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return []
    chars = list_characters(guild_id)
    cur = current.casefold().strip()
    filtered = [c for c in chars if cur in c.casefold()]
    return [app_commands.Choice(name=c, value=c) for c in filtered[:25]]


# =============================
# /char GROUP
# =============================
char_group = app_commands.Group(name="char", description="Manage RP characters (per server).")


@char_group.command(name="add", description="Add a character to this server.")
@app_commands.describe(name="Character name (e.g., Riley Kaplan)")
async def char_add(interaction: discord.Interaction, name: str):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("This command only works in a server.", ephemeral=True)

    name = name.strip()
    if not name:
        return await interaction.response.send_message("Name can’t be empty.", ephemeral=True)

    try:
        add_character(guild_id, name)
    except Exception:
        return await interaction.response.send_message(f"Character **{name}** already exists.", ephemeral=True)

    await interaction.response.send_message(f"Added character **{name}** ✅")


@char_group.command(name="list", description="List all characters in this server.")
async def char_list(interaction: discord.Interaction):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("This command only works in a server.", ephemeral=True)

    chars = list_characters(guild_id)
    if not chars:
        return await interaction.response.send_message("No characters yet. Add one with `/char add`.")

    text = "\n".join(f"• {c}" for c in chars[:100])
    if len(chars) > 100:
        text += f"\n… and {len(chars)-100} more."

    embed = discord.Embed(title="Characters", description=text, color=0x95A5A6)
    await interaction.response.send_message(embed=embed)


@char_group.command(name="remove", description="Remove a character (and their relationships) from this server.")
@app_commands.autocomplete(name=character_autocomplete)
async def char_remove(interaction: discord.Interaction, name: str):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("This command only works in a server.", ephemeral=True)

    deleted = remove_character(guild_id, name.strip())
    if deleted == 0:
        return await interaction.response.send_message(f"I can’t find **{name}**.", ephemeral=True)

    await interaction.response.send_message(f"Removed **{name}** (and any linked relationships). 🗑️")


# =============================
# /settings GROUP (LOG CHANNEL)
# =============================
settings_group = app_commands.Group(name="settings", description="Server settings for the RP bot.")


@settings_group.command(name="set-log-channel", description="Set a channel to receive milestone logs.")
@app_commands.describe(channel="The channel to post milestone logs into")
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("This command only works in a server.", ephemeral=True)

    set_log_channel_id(guild_id, channel.id)
    await interaction.response.send_message(f"✅ Milestone log channel set to {channel.mention}")


@settings_group.command(name="clear-log-channel", description="Disable milestone logging.")
async def clear_log_channel(interaction: discord.Interaction):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("This command only works in a server.", ephemeral=True)

    clear_log_channel_id(guild_id)
    await interaction.response.send_message("✅ Milestone logging disabled.")


@settings_group.command(name="show", description="Show current server settings.")
async def show_settings(interaction: discord.Interaction):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("This command only works in a server.", ephemeral=True)

    chan_id = get_log_channel_id(guild_id)
    if chan_id:
        await interaction.response.send_message(f"📌 Milestone log channel: <#{chan_id}>", ephemeral=True)
    else:
        await interaction.response.send_message("📌 Milestone log channel: (not set)", ephemeral=True)


# =============================
# /rel GROUP
# =============================
rel_group = app_commands.Group(name="rel", description="Track relationship meters between characters.")


@rel_group.command(name="top", description="Show strongest relationships for a character (optionally filter by type).")
@app_commands.describe(type="Optional: romantic / platonic / familial / all")
@app_commands.choices(type=REL_TYPE_PLUS_ALL_CHOICES)
@app_commands.autocomplete(name=character_autocomplete)
async def rel_top(
    interaction: discord.Interaction,
    name: str,
    type: app_commands.Choice[str],
):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("This command only works in a server.", ephemeral=True)

    name = name.strip()
    chosen = (type.value or "all").strip().lower()

    rel_type = None if chosen == "all" else normalize_rel_type(chosen)

    rows = top_relationships_for(guild_id, name, rel_type=rel_type, limit=10)
    if not rows:
        return await interaction.response.send_message(f"No relationships tracked yet for **{name}**.", ephemeral=True)

    lines = []
    for r in rows:
        score = int(r["score"])
        rt = r["rel_type"]
        lines.append(f"• **{r['other']}** — `{score}` ({stage_label(score, rt)})  ·  *{rt}*")

    title = f"Top relationships for {name}" + (f" ({rel_type_title(rel_type)})" if rel_type else "")
    embed = discord.Embed(title=title, description="\n".join(lines), color=0x34495E)
    await interaction.response.send_message(embed=embed)


@rel_group.command(name="view", description="View relationship meter between two characters.")
@app_commands.choices(type=REL_TYPE_CHOICES)
@app_commands.autocomplete(a=character_autocomplete, b=character_autocomplete)
async def rel_view(
    interaction: discord.Interaction,
    type: app_commands.Choice[str],
    a: str,
    b: str
):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("This command only works in a server.", ephemeral=True)

    a, b = a.strip(), b.strip()
    if a.casefold() == b.casefold():
        return await interaction.response.send_message("Pick two different characters.", ephemeral=True)

    rel_type = type.value
    row = get_relationship(guild_id, a, b, rel_type)
    score = int(row["score"]) if row else 0
    note = row.get("note") if row else None

    embed = build_rel_embed(rel_type, a, b, score, note=note)
    await interaction.response.send_message(embed=embed)


@rel_group.command(name="set", description="Set relationship score (-100 to +100).")
@app_commands.choices(type=REL_TYPE_CHOICES)
@app_commands.autocomplete(a=character_autocomplete, b=character_autocomplete)
@app_commands.describe(score="Integer from -100 to 100", note="Optional note")
async def rel_set(
    interaction: discord.Interaction,
    type: app_commands.Choice[str],
    a: str,
    b: str,
    score: int,
    note: Optional[str] = None
):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("This command only works in a server.", ephemeral=True)

    a, b = a.strip(), b.strip()
    if a.casefold() == b.casefold():
        return await interaction.response.send_message("Pick two different characters.", ephemeral=True)

    rel_type = type.value

    # Auto-create characters
    if not character_exists(guild_id, a):
        add_character(guild_id, a)
    if not character_exists(guild_id, b):
        add_character(guild_id, b)

    prev = get_relationship(guild_id, a, b, rel_type)
    old = int(prev["score"]) if prev else 0

    final = upsert_relationship(
        guild_id=guild_id,
        name1=a,
        name2=b,
        rel_type=rel_type,
        new_score=score,
        updated_by=interaction.user.display_name,
        note=note,
        delta_for_history=(clamp_score(score) - old),
        reason="SET",
        is_player_action=True,
    )

    milestone = milestone_message(old, final, rel_type)
    extra = None
    if milestone:
        extra = milestone

    embed = build_rel_embed(rel_type, a, b, final, note=note, extra=extra)
    await interaction.response.send_message(embed=embed)

    await post_milestone_log(
        interaction=interaction,
        rel_type=rel_type,
        a=a,
        b=b,
        old_score=old,
        new_score=final,
        delta=(final - old),
        reason="SET",
    )


@rel_group.command(name="add", description="Adjust relationship score by a delta (e.g., -10 or +25).")
@app_commands.choices(type=REL_TYPE_CHOICES)
@app_commands.autocomplete(a=character_autocomplete, b=character_autocomplete)
@app_commands.describe(delta="Change amount (e.g., -10 or +15)", reason="Optional reason")
async def rel_add(
    interaction: discord.Interaction,
    type: app_commands.Choice[str],
    a: str,
    b: str,
    delta: int,
    reason: Optional[str] = None
):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("This command only works in a server.", ephemeral=True)

    a, b = a.strip(), b.strip()
    if a.casefold() == b.casefold():
        return await interaction.response.send_message("Pick two different characters.", ephemeral=True)

    rel_type = type.value

    if not character_exists(guild_id, a):
        add_character(guild_id, a)
    if not character_exists(guild_id, b):
        add_character(guild_id, b)

    prev = get_relationship(guild_id, a, b, rel_type)
    old_score = int(prev["score"]) if prev else 0

    final = add_to_relationship(
        guild_id=guild_id,
        name1=a,
        name2=b,
        rel_type=rel_type,
        delta=delta,
        updated_by=interaction.user.display_name,
        reason=reason
    )

    milestone = milestone_message(old_score, final, rel_type)
    extra = f"**Delta:** `{delta:+d}`"
    if reason:
        extra += f"\n**Reason:** {reason}"
    if milestone:
        extra += f"\n\n{milestone}"

    embed = build_rel_embed(rel_type, a, b, final, extra=extra)
    await interaction.response.send_message(embed=embed)

    await post_milestone_log(
        interaction=interaction,
        rel_type=rel_type,
        a=a,
        b=b,
        old_score=old_score,
        new_score=final,
        delta=delta,
        reason=reason,
    )


@rel_group.command(name="history", description="Show recent changes for a relationship meter.")
@app_commands.choices(type=REL_TYPE_CHOICES)
@app_commands.autocomplete(a=character_autocomplete, b=character_autocomplete)
@app_commands.describe(limit="How many entries (max 15)")
async def rel_history_cmd(
    interaction: discord.Interaction,
    type: app_commands.Choice[str],
    a: str,
    b: str,
    limit: int = 10
):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("This command only works in a server.", ephemeral=True)

    limit = max(1, min(15, int(limit)))
    rel_type = type.value

    rows = fetch_history(guild_id, a, b, rel_type, limit=limit)
    if not rows:
        return await interaction.response.send_message("No history yet for that pair/type.", ephemeral=True)

    lines = []
    for r in rows:
        delta_i = int(r["delta"])
        new_i = int(r["new_score"])
        by = r["updated_by"]
        at = r["updated_at"]
        why = r.get("reason") or ""
        why_part = f" — {why}" if why else ""
        lines.append(f"• `{delta_i:+d}` → `{new_i}` by **{by}** ({at}){why_part}")

    embed = discord.Embed(
        title=f"History ({rel_type_title(rel_type)}): {a} ↔ {b}",
        description="\n".join(lines),
        color=0x7F8C8D
    )
    await interaction.response.send_message(embed=embed)


# =============================
# /decay GROUP (DECAY + FREEZE CONTROLS)
# =============================
decay_group = app_commands.Group(name="decay", description="Decay + freeze controls (server-wide & per relationship).")


@decay_group.command(name="show", description="Show current decay settings for this server.")
async def decay_show(interaction: discord.Interaction):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("Server only.", ephemeral=True)

    rows = get_decay_settings_rows(guild_id)
    paused_until_iso = rows[0].get("paused_until") if rows else None

    lines = []
    if paused_until_iso:
        pu = parse_iso(paused_until_iso)
        if pu and now_utc() < pu:
            lines.append(f"⏸️ **Decay paused until:** `{paused_until_iso}`")
        else:
            lines.append("⏸️ **Decay pause:** (not active)")

    for r in rows:
        lines.append(
            f"• **{r['rel_type']}** — "
            f"{'✅ ON' if r['enabled'] else '❌ OFF'} | "
            f"amount `{r['decay_amount']}` | every `{r['interval_minutes']}m` | "
            f"cooldown `{r['cooldown_minutes']}m`"
            + (f" | freeze_familial `{r['freeze_familial']}`" if r["rel_type"] == "romantic" else "")
        )

    await interaction.response.send_message("\n".join(lines) if lines else "No decay settings found.", ephemeral=True)


@decay_group.command(name="enable", description="Enable decay for a relationship type.")
@app_commands.choices(type=REL_TYPE_CHOICES)
async def decay_enable(interaction: discord.Interaction, type: app_commands.Choice[str]):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("Server only.", ephemeral=True)

    set_decay_settings(guild_id, type.value, enabled=True)
    await interaction.response.send_message(f"✅ Decay enabled for **{type.value}**.", ephemeral=True)


@decay_group.command(name="disable", description="Disable decay for a relationship type.")
@app_commands.choices(type=REL_TYPE_CHOICES)
async def decay_disable(interaction: discord.Interaction, type: app_commands.Choice[str]):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("Server only.", ephemeral=True)

    set_decay_settings(guild_id, type.value, enabled=False)
    await interaction.response.send_message(f"✅ Decay disabled for **{type.value}**.", ephemeral=True)


@decay_group.command(name="configure", description="Configure decay: amount, interval, cooldown (per type).")
@app_commands.choices(type=REL_TYPE_CHOICES)
@app_commands.describe(
    amount="Points per tick (1-10 recommended)",
    interval_minutes="Minutes per tick (10..10080)",
    cooldown_minutes="No decay for this many minutes after last player update (0..43200)"
)
async def decay_configure(
    interaction: discord.Interaction,
    type: app_commands.Choice[str],
    amount: int,
    interval_minutes: int,
    cooldown_minutes: int,
):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("Server only.", ephemeral=True)

    amount = max(1, min(10, int(amount)))
    interval_minutes = max(10, min(10080, int(interval_minutes)))  # 10m .. 7d
    cooldown_minutes = max(0, min(43200, int(cooldown_minutes)))   # up to 30 days

    set_decay_settings(
        guild_id=guild_id,
        rel_type=type.value,
        decay_amount=amount,
        interval_minutes=interval_minutes,
        cooldown_minutes=cooldown_minutes
    )

    await interaction.response.send_message(
        f"✅ **{type.value}** decay updated: amount `{amount}`, interval `{interval_minutes}m`, cooldown `{cooldown_minutes}m`.",
        ephemeral=True
    )


@decay_group.command(name="pause", description="Pause ALL decay for this server for N hours.")
@app_commands.describe(hours="How long to pause decay (1..168)")
async def decay_pause(interaction: discord.Interaction, hours: int):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("Server only.", ephemeral=True)

    hours = max(1, min(168, int(hours)))  # up to 7 days
    until = now_utc() + timedelta(hours=hours)
    set_pause_all_decay(guild_id, until.isoformat(timespec="seconds"))
    await interaction.response.send_message(f"⏸️ Decay paused for `{hours}` hour(s).", ephemeral=True)


@decay_group.command(name="resume", description="Resume decay immediately (clear server pause).")
async def decay_resume(interaction: discord.Interaction):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    set_pause_all_decay(guild_id, None)
    await interaction.response.send_message("▶️ Decay resumed.", ephemeral=True)


@decay_group.command(name="freeze-familial", description="Freeze or allow familial decay (server-wide lever).")
@app_commands.describe(enabled="If true, familial relationships will not decay.")
async def decay_freeze_familial(interaction: discord.Interaction, enabled: bool):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("Server only.", ephemeral=True)

    # store on all rows (simple)
    for rt in REL_TYPES:
        set_decay_settings(guild_id, rt, freeze_familial=enabled)

    await interaction.response.send_message(
        f"✅ freeze_familial set to `{enabled}` (familial decay {'disabled' if enabled else 'enabled'}).",
        ephemeral=True
    )


@decay_group.command(name="freeze", description="Freeze decay for ONE relationship meter for N hours.")
@app_commands.choices(type=REL_TYPE_CHOICES)
@app_commands.autocomplete(a=character_autocomplete, b=character_autocomplete)
@app_commands.describe(hours="Freeze duration (1..720)", reason="Optional note why it’s frozen")
async def decay_freeze(
    interaction: discord.Interaction,
    type: app_commands.Choice[str],
    a: str,
    b: str,
    hours: int,
    reason: Optional[str] = None
):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("Server only.", ephemeral=True)

    hours = max(1, min(720, int(hours)))  # up to 30 days
    rel_type = type.value
    until = now_utc() + timedelta(hours=hours)

    upsert_freeze(
        guild_id=guild_id,
        name1=a,
        name2=b,
        rel_type=rel_type,
        freeze_until_iso=until.isoformat(timespec="seconds"),
        reason=reason,
        created_by=interaction.user.display_name
    )

    await interaction.response.send_message(
        f"🧊 Frozen **{rel_type}** decay for **{a} ↔ {b}** until `{until.isoformat(timespec='seconds')}`.",
        ephemeral=True
    )


@decay_group.command(name="unfreeze", description="Remove freeze from ONE relationship meter.")
@app_commands.choices(type=REL_TYPE_CHOICES)
@app_commands.autocomplete(a=character_autocomplete, b=character_autocomplete)
async def decay_unfreeze(
    interaction: discord.Interaction,
    type: app_commands.Choice[str],
    a: str,
    b: str,
):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("Server only.", ephemeral=True)

    rel_type = type.value
    deleted = clear_freeze(guild_id, a, b, rel_type)
    if deleted <= 0:
        return await interaction.response.send_message("No freeze found for that pair/type.", ephemeral=True)

    await interaction.response.send_message(f"✅ Unfroze **{rel_type}** decay for **{a} ↔ {b}**.", ephemeral=True)


@decay_group.command(name="frozen", description="List all currently frozen relationships in this server.")
async def decay_frozen_list(interaction: discord.Interaction):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("Server only.", ephemeral=True)

    rows = list_freezes(guild_id)
    if not rows:
        return await interaction.response.send_message("No frozen relationships.", ephemeral=True)

    now = now_utc()
    lines = []
    for r in rows[:50]:
        until_dt = parse_iso(r["freeze_until"])
        if until_dt and until_dt <= now:
            continue
        lines.append(
            f"• **{r['rel_type']}** — **{r['a_name']} ↔ {r['b_name']}** until `{r['freeze_until']}`"
            + (f" — {r['reason']}" if r.get("reason") else "")
        )

    if not lines:
        return await interaction.response.send_message("No currently active freezes (some may have expired).", ephemeral=True)

    if len(rows) > 50:
        lines.append(f"… and {len(rows) - 50} more (showing first 50).")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# =============================
# RUN
# =============================
def main():
    if not TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN env var.")
    db_init()
    client.run(TOKEN)


if __name__ == "__main__":
    main()
