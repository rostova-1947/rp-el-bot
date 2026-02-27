import os
import random
from datetime import datetime, timezone
from typing import Optional, List, Tuple, Dict, Any

import discord
from discord import app_commands
import psycopg2
import psycopg2.extras

# ============================================================
# Env
# ============================================================
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = (
    os.getenv("DATABASE_URL")
    or os.getenv("DATABASE_PRIVATE_URL")
    or os.getenv("PGDATABASE_URL")
)

# Optional: fast slash-command sync to one server (recommended while developing)
# Put your server ID here in Railway Variables
GUILD_ID = os.getenv("GUILD_ID")  # optional, e.g. "123456789012345678"

# SSL mode for Railway/hosted Postgres
PGSSLMODE = os.getenv("PGSSLMODE", "prefer")


# ============================================================
# Relationship Types
# ============================================================
REL_TYPES = ("romantic", "platonic", "familial")

def normalize_rel_type(t: Optional[str]) -> str:
    if not t:
        return "platonic"
    t = t.strip().lower()
    if t not in REL_TYPES:
        raise ValueError(f"Invalid relationship type: {t}. Must be one of {REL_TYPES}.")
    return t


# ============================================================
# DB layer (Postgres)
# ============================================================
def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("Missing DATABASE_URL. Add a Postgres database and set DATABASE_URL.")
    return psycopg2.connect(DATABASE_URL, sslmode=PGSSLMODE)

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def clamp_score(score: int) -> int:
    return max(-100, min(100, int(score)))

def normalize_pair(name1: str, name2: str) -> Tuple[str, str]:
    n1, n2 = name1.strip(), name2.strip()
    return (n1, n2) if n1.casefold() < n2.casefold() else (n2, n1)

def db_init() -> None:
    con = db_connect()
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS characters (
        id SERIAL PRIMARY KEY,
        guild_id TEXT NOT NULL,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """)

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
        note TEXT
    );
    """)

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

    # Per-guild settings (milestone log channel)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id TEXT PRIMARY KEY,
        log_channel_id TEXT
    );
    """)

    # Migration: ensure rel_type exists + backfill
    cur.execute("ALTER TABLE relationships ADD COLUMN IF NOT EXISTS rel_type TEXT;")
    cur.execute("ALTER TABLE rel_history ADD COLUMN IF NOT EXISTS rel_type TEXT;")
    cur.execute("UPDATE relationships SET rel_type='platonic' WHERE rel_type IS NULL;")
    cur.execute("UPDATE rel_history SET rel_type='platonic' WHERE rel_type IS NULL;")
    cur.execute("ALTER TABLE relationships ALTER COLUMN rel_type SET NOT NULL;")
    cur.execute("ALTER TABLE rel_history ALTER COLUMN rel_type SET NOT NULL;")

    # Unique indexes (case-insensitive)
    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS ux_characters_guild_lower_name
    ON characters (guild_id, lower(name));
    """)

    cur.execute("DROP INDEX IF EXISTS ux_relationships_guild_lower_pair;")
    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS ux_relationships_guild_lower_pair_type
    ON relationships (guild_id, lower(a_name), lower(b_name), rel_type);
    """)

    con.commit()
    cur.close()
    con.close()

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

def get_relationship(guild_id: str, name1: str, name2: str, rel_type: Optional[str] = None):
    rel_type = normalize_rel_type(rel_type)
    a, b = normalize_pair(name1, name2)
    con = db_connect()
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT * FROM relationships
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
) -> int:
    rel_type = normalize_rel_type(rel_type)
    a, b = normalize_pair(name1, name2)
    new_score = clamp_score(new_score)
    ts = now_iso()

    con = db_connect()
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        """
        SELECT score FROM relationships
        WHERE guild_id=%s
          AND lower(a_name)=lower(%s)
          AND lower(b_name)=lower(%s)
          AND rel_type=%s
        """,
        (guild_id, a, b, rel_type),
    )
    existing = cur.fetchone()

    if existing is None:
        cur.execute(
            """
            INSERT INTO relationships (guild_id, a_name, b_name, rel_type, score, updated_by, updated_at, note)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (guild_id, a, b, rel_type, new_score, updated_by, ts, note),
        )
    else:
        cur.execute(
            """
            UPDATE relationships
            SET score=%s, updated_by=%s, updated_at=%s, note=COALESCE(%s, note)
            WHERE guild_id=%s
              AND lower(a_name)=lower(%s)
              AND lower(b_name)=lower(%s)
              AND rel_type=%s
            """,
            (new_score, updated_by, ts, note, guild_id, a, b, rel_type),
        )

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
    )

def fetch_history(guild_id: str, name1: str, name2: str, rel_type: str, limit: int = 10):
    rel_type = normalize_rel_type(rel_type)
    a, b = normalize_pair(name1, name2)
    con = db_connect()
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT * FROM rel_history
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
            CASE
                WHEN lower(a_name)=lower(%s) THEN b_name
                ELSE a_name
            END AS other
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
            CASE
                WHEN lower(a_name)=lower(%s) THEN b_name
                ELSE a_name
            END AS other
            FROM relationships
            WHERE guild_id=%s
              AND (lower(a_name)=lower(%s) OR lower(b_name)=lower(%s))
              AND (lower(b_name)=lower(%s) OR lower(a_name)=lower(%s))
            ORDER BY score DESC
            LIMIT %s
            """,
            (name, guild_id, name, name, name, name, limit),
        )
    rows = cur.fetchall()
    cur.close()
    con.close()
    return rows


# ============================================================
# Guild settings: milestone log channel
# ============================================================
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


# ============================================================
# Display + Flavor (general, not setting-specific)
# ============================================================
REL_TYPE_META = {
    "romantic": {"emoji": "💘", "title": "Romantic"},
    "platonic": {"emoji": "🤝", "title": "Platonic"},
    "familial": {"emoji": "🧬", "title": "Familial"},
}

def ensure_guild(interaction: discord.Interaction) -> Optional[str]:
    return str(interaction.guild.id) if interaction.guild else None

def rel_type_title(rt: str) -> str:
    rt = normalize_rel_type(rt)
    return rt.capitalize()

def meter_bar(score: int, width: int = 20) -> str:
    score = clamp_score(score)
    filled = int(round(((score + 100) / 200) * width))
    filled = max(0, min(width, filled))
    bar = "█" * filled + "░" * (width - filled)
    mid = width // 2
    bar_list = list(bar)
    bar_list[mid] = "┃"
    return "".join(bar_list)

def heat_emoji(score: int) -> str:
    score = clamp_score(score)
    if score <= -85: return "🟥"
    if score <= -60: return "🔴"
    if score <= -30: return "🟠"
    if score <= 20:  return "🟡"
    if score <= 50:  return "🟢"
    if score <= 80:  return "🔵"
    return "🟣"

def vibe_tag(score: int) -> str:
    score = clamp_score(score)
    if score <= -85: return "Hostile"
    if score <= -60: return "Volatile"
    if score <= -30: return "Strained"
    if score <= 20:  return "Neutral"
    if score <= 50:  return "Warm"
    if score <= 80:  return "Close"
    return "Bonded"

# Simple milestones (generic)
MILESTONE_BANDS = [(-85, "Hostile"), (-60, "Volatile"), (-30, "Strained"), (20, "Neutral"), (50, "Warm"), (80, "Close"), (100, "Bonded")]
def milestone_message(old_score: int, new_score: int) -> Optional[str]:
    old_tag = vibe_tag(old_score)
    new_tag = vibe_tag(new_score)
    if old_tag == new_tag:
        return None
    return f"🏁 **Milestone:** *{old_tag}* → **{new_tag}**"

FLAVOR_LINES = {
    "positive": {
        "low":  ["A small win. The edge softens.", "A good moment—quietly earned.", "Something goes right for once."],
        "med":  ["Something shifts. They’re easier with each other.", "Trust builds, not loudly—but clearly.", "They find a rhythm that works."],
        "high": ["That changes things. Permanently.", "A turning point. Neither of them forgets it.", "They cross a line they can’t uncross."],
    },
    "negative": {
        "low":  ["A small cut. It stings anyway.", "Tension flickers. Not gone.", "It’s minor—still leaves a mark."],
        "med":  ["Tension climbs. Words land wrong.", "Old patterns snap back into place.", "It escalates faster than anyone wanted."],
        "high": ["It blows up. Everybody feels it.", "Damage done. Now they have to live with it.", "No clean landing. Just fallout."],
    },
    "mixed": {
        "low":  ["A moment—hard to read.", "Unclear. But it lingers.", "It could mean nothing. It doesn’t."],
        "med":  ["Complicated. It could’ve gone either way.", "Soft and sharp in the same breath.", "They circle the truth and miss it."],
        "high": ["Messy. Intense. Not resolved.", "Big feelings. No clean landing.", "They leave it raw on the table."],
    },
}

def pick_flavor(intensity: str, polarity: str, seed: int) -> str:
    inten = (intensity or "med").strip().lower()
    pol = (polarity or "mixed").strip().lower()
    if inten not in ("low", "med", "high"):
        inten = "med"
    if pol not in ("positive", "negative", "mixed"):
        pol = "mixed"
    rng = random.Random(seed + 1337)
    return rng.choice(FLAVOR_LINES[pol][inten])

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
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return

    milestone = milestone_message(old_score, new_score)
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
    heat = heat_emoji(new_score)
    tag = vibe_tag(new_score)

    delta_part = f" `{delta:+d}`" if delta is not None else ""
    reason_part = f"\n**Reason:** {reason}" if reason else ""

    msg = (
        f"{meta['emoji']} **{meta['title']} Milestone** — **{a} ↔ {b}**\n"
        f"{milestone}\n"
        f"{heat} **Score:** `{old_score}` → `{new_score}`{delta_part}  |  **Now:** **{tag}**\n"
        f"**By:** {interaction.user.mention}"
        f"{reason_part}"
    )

    try:
        await channel.send(msg)
    except Exception:
        return


REL_TYPE_CHOICES = [
    app_commands.Choice(name="romantic", value="romantic"),
    app_commands.Choice(name="platonic", value="platonic"),
    app_commands.Choice(name="familial", value="familial"),
]


# ============================================================
# Discord bot
# ============================================================
intents = discord.Intents.default()

class RPBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        # Sync commands
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

client = RPBot()


async def character_autocomplete(interaction: discord.Interaction, current: str):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return []
    chars = list_characters(guild_id)
    cur = current.casefold().strip()
    filtered = [c for c in chars if cur in c.casefold()]
    return [app_commands.Choice(name=c, value=c) for c in filtered[:25]]


# ============================================================
# /char
# ============================================================
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

    embed = discord.Embed(title="Characters", description=text)
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


# ============================================================
# /settings
# ============================================================
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
        await interaction.response.send_message(f"📌 Milestone log channel: <#{chan_id}>")
    else:
        await interaction.response.send_message("📌 Milestone log channel: (not set)")


# ============================================================
# /rel
# ============================================================
rel_group = app_commands.Group(name="rel", description="Track relationship meters between characters.")

@rel_group.command(name="top", description="Show strongest relationships for a character (optionally filter by type).")
@app_commands.describe(type="Optional: romantic / platonic / familial / all")
@app_commands.choices(type=[
    app_commands.Choice(name="all", value="all"),
    app_commands.Choice(name="romantic", value="romantic"),
    app_commands.Choice(name="platonic", value="platonic"),
    app_commands.Choice(name="familial", value="familial"),
])
@app_commands.autocomplete(name=character_autocomplete)
async def rel_top(
    interaction: discord.Interaction,
    name: str,
    type: Optional[str] = "all",
):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("This command only works in a server.", ephemeral=True)

    name = name.strip()
    chosen = (type or "all").strip().lower()
    rel_type = None if chosen == "all" else chosen
    if rel_type is not None:
        rel_type = normalize_rel_type(rel_type)

    rows = top_relationships_for(guild_id, name, rel_type=rel_type, limit=10)
    if not rows:
        return await interaction.response.send_message(f"No relationships tracked yet for **{name}**.")

    lines = []
    for r in rows:
        score = int(r["score"])
        rt = r["rel_type"]
        lines.append(f"• **{r['other']}** — `{score}`  {heat_emoji(score)}  **{vibe_tag(score)}**  ·  *{rt}*")

    title = f"Top relationships for {name}"
    if rel_type:
        title += f" ({rel_type_title(rel_type)})"

    embed = discord.Embed(title=title, description="\n".join(lines))
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

    meta = REL_TYPE_META.get(rel_type, {"emoji": "🔗", "title": rel_type_title(rel_type)})
    heat = heat_emoji(score)
    tag = vibe_tag(score)

    embed = discord.Embed(
        title=f"{meta['emoji']} {meta['title']}: {a} ↔ {b}",
        description=f"{heat} **{score}** • **{tag}**\n`{meter_bar(score)}`",
    )
    if note:
        embed.add_field(name="Note", value=note, inline=False)

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
    )

    milestone = milestone_message(old, final)

    meta = REL_TYPE_META.get(rel_type, {"emoji": "🔗", "title": rel_type_title(rel_type)})
    heat = heat_emoji(final)
    tag = vibe_tag(final)

    # Flavor: infer polarity from delta
    delta_val = final - old
    if delta_val > 0:
        pol, inten = "positive", "high" if abs(delta_val) >= 13 else ("med" if abs(delta_val) >= 6 else "low")
    elif delta_val < 0:
        pol, inten = "negative", "high" if abs(delta_val) >= 13 else ("med" if abs(delta_val) >= 6 else "low")
    else:
        pol, inten = "mixed", "low"
    flavor = pick_flavor(inten, pol, seed=int(interaction.id))

    desc = (
        f"{heat} **{final}** • **{tag}**\n"
        f"`{meter_bar(final)}`\n"
        f"*{flavor}*"
    )
    if milestone:
        desc += f"\n\n{milestone}"

    embed = discord.Embed(
        title=f"{meta['emoji']} Set {meta['title']}: {a} ↔ {b}",
        description=desc,
    )
    if note:
        embed.add_field(name="Note", value=note, inline=False)

    await interaction.response.send_message(embed=embed)

    await post_milestone_log(
        interaction=interaction,
        rel_type=rel_type,
        a=a,
        b=b,
        old_score=old,
        new_score=final,
        delta=delta_val,
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

    milestone = milestone_message(old_score, final)

    meta = REL_TYPE_META.get(rel_type, {"emoji": "🔗", "title": rel_type_title(rel_type)})
    heat = heat_emoji(final)
    tag = vibe_tag(final)

    pol = "positive" if delta > 0 else ("negative" if delta < 0 else "mixed")
    inten = "high" if abs(delta) >= 13 else ("med" if abs(delta) >= 6 else "low")
    flavor = pick_flavor(inten, pol, seed=int(interaction.id))

    desc = (
        f"{heat} **{final}** • **{tag}**\n"
        f"`{meter_bar(final)}`\n"
        f"*{flavor}*\n\n"
        f"**Delta:** `{delta:+d}`"
    )
    if reason:
        desc += f"\n**Reason:** {reason}"
    if milestone:
        desc += f"\n\n{milestone}"

    embed = discord.Embed(
        title=f"{meta['emoji']} Updated {meta['title']}: {a} ↔ {b}",
        description=desc,
    )

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
async def rel_history(
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
        return await interaction.response.send_message("No history yet for that pair/type.")

    lines = []
    for r in rows:
        lines.append(
            f"• `{int(r['delta']):+d}` → `{int(r['new_score'])}`  {heat_emoji(int(r['new_score']))}  **{vibe_tag(int(r['new_score']))}**"
            f" — **{r['updated_by']}** ({r['updated_at']})"
            + (f" — {r['reason']}" if r.get("reason") else "")
        )

    embed = discord.Embed(
        title=f"History ({rel_type_title(rel_type)}): {a} ↔ {b}",
        description="\n".join(lines)
    )
    await interaction.response.send_message(embed=embed)


# ============================================================
# /event (random delta + general flavor)
# ============================================================
event_group = app_commands.Group(name="event", description="Random relationship changes (no custom event text).")

POLARITY_CHOICES = [
    app_commands.Choice(name="positive", value="positive"),
    app_commands.Choice(name="negative", value="negative"),
    app_commands.Choice(name="mixed", value="mixed"),
]

INTENSITY_CHOICES = [
    app_commands.Choice(name="low", value="low"),
    app_commands.Choice(name="med", value="med"),
    app_commands.Choice(name="high", value="high"),
]

def roll_delta(polarity: str, intensity: str, seed: int) -> int:
    rng = random.Random(seed)
    inten = intensity.lower()
    pol = polarity.lower()

    ranges = {
        "low": (1, 5),
        "med": (6, 12),
        "high": (13, 25),
    }
    lo, hi = ranges.get(inten, (6, 12))
    mag = rng.randint(lo, hi)

    if pol == "positive":
        return mag
    if pol == "negative":
        return -mag
    # mixed: coinflip sign
    return mag if rng.random() >= 0.5 else -mag

@event_group.command(name="roll", description="Roll a random delta and apply it to the relationship meter.")
@app_commands.choices(type=REL_TYPE_CHOICES, polarity=POLARITY_CHOICES, intensity=INTENSITY_CHOICES)
@app_commands.autocomplete(a=character_autocomplete, b=character_autocomplete)
@app_commands.describe(
    type="romantic / platonic / familial",
    a="Character A",
    b="Character B",
    polarity="positive / negative / mixed",
    intensity="low / med / high",
    reason="Optional label stored in history (e.g. 'arena', 'argument', 'good day')"
)
async def event_roll(
    interaction: discord.Interaction,
    type: app_commands.Choice[str],
    a: str,
    b: str,
    polarity: app_commands.Choice[str],
    intensity: app_commands.Choice[str],
    reason: Optional[str] = None
):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("This command only works in a server.", ephemeral=True)

    a, b = a.strip(), b.strip()
    if a.casefold() == b.casefold():
        return await interaction.response.send_message("Pick two different characters.", ephemeral=True)

    rel_type = type.value
    pol = polarity.value
    inten = intensity.value

    if not character_exists(guild_id, a):
        add_character(guild_id, a)
    if not character_exists(guild_id, b):
        add_character(guild_id, b)

    prev = get_relationship(guild_id, a, b, rel_type)
    old_score = int(prev["score"]) if prev else 0

    delta = roll_delta(pol, inten, seed=int(interaction.id))
    final = add_to_relationship(
        guild_id=guild_id,
        name1=a,
        name2=b,
        rel_type=rel_type,
        delta=delta,
        updated_by=interaction.user.display_name,
        reason=reason or f"EVENT({pol}/{inten})"
    )

    milestone = milestone_message(old_score, final)
    meta = REL_TYPE_META.get(rel_type, {"emoji": "🔗", "title": rel_type_title(rel_type)})

    heat = heat_emoji(final)
    tag = vibe_tag(final)
    flavor = pick_flavor(inten, pol, seed=int(interaction.id))

    desc = (
        f"🎲 **Roll:** `{delta:+d}` ({pol}/{inten})\n"
        f"{heat} **{final}** • **{tag}**\n"
        f"`{meter_bar(final)}`\n"
        f"*{flavor}*"
    )
    if reason:
        desc += f"\n\n**Label:** {reason}"
    if milestone:
        desc += f"\n\n{milestone}"

    embed = discord.Embed(
        title=f"{meta['emoji']} Event ({meta['title']}): {a} ↔ {b}",
        description=desc,
    )
    await interaction.response.send_message(embed=embed)

    await post_milestone_log(
        interaction=interaction,
        rel_type=rel_type,
        a=a,
        b=b,
        old_score=old_score,
        new_score=final,
        delta=delta,
        reason=reason or f"EVENT({pol}/{inten})",
    )


# ============================================================
# Register groups (IMPORTANT for slash commands)
# ============================================================
client.tree.add_command(char_group)
client.tree.add_command(settings_group)
client.tree.add_command(rel_group)
client.tree.add_command(event_group)


# ============================================================
# Run
# ============================================================
def main():
    if not TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN env var.")
    db_init()
    client.run(TOKEN)

if __name__ == "__main__":
    main()
