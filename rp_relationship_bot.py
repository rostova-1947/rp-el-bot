import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List, Tuple, Dict

import discord
from discord import app_commands
import psycopg2
import psycopg2.extras

# ============================================================
# ENV
# ============================================================
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = (
    os.getenv("DATABASE_URL")
    or os.getenv("DATABASE_PRIVATE_URL")
    or os.getenv("PGDATABASE_URL")
)

# If set, slash commands sync instantly to that server (recommended while developing)
GUILD_ID = os.getenv("GUILD_ID")  # optional

# SSL (Railway Postgres usually works with prefer/require)
PGSSLMODE = os.getenv("PGSSLMODE", "prefer")  # try "require" if needed


# ============================================================
# REL TYPES
# ============================================================
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

INTENSITY_CHOICES = [
    app_commands.Choice(name="low", value="low"),
    app_commands.Choice(name="med", value="med"),
    app_commands.Choice(name="high", value="high"),
]

POLARITY_CHOICES = [
    app_commands.Choice(name="mixed", value="mixed"),
    app_commands.Choice(name="positive", value="positive"),
    app_commands.Choice(name="negative", value="negative"),
]


# ============================================================
# DB LAYER (POSTGRES)
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

    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS characters (
        id SERIAL PRIMARY KEY,
        guild_id TEXT NOT NULL,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """
    )

    cur.execute(
        """
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
    """
    )

    cur.execute(
        """
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
    """
    )

    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id TEXT PRIMARY KEY,
        log_channel_id TEXT
    );
    """
    )

    # Migration: rel_type backfill
    cur.execute("ALTER TABLE relationships ADD COLUMN IF NOT EXISTS rel_type TEXT;")
    cur.execute("ALTER TABLE rel_history ADD COLUMN IF NOT EXISTS rel_type TEXT;")
    cur.execute("UPDATE relationships SET rel_type='platonic' WHERE rel_type IS NULL;")
    cur.execute("UPDATE rel_history SET rel_type='platonic' WHERE rel_type IS NULL;")
    cur.execute("ALTER TABLE relationships ALTER COLUMN rel_type SET NOT NULL;")
    cur.execute("ALTER TABLE rel_history ALTER COLUMN rel_type SET NOT NULL;")

    # Uniques
    cur.execute(
        """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_characters_guild_lower_name
    ON characters (guild_id, lower(name));
    """
    )
    cur.execute("DROP INDEX IF EXISTS ux_relationships_guild_lower_pair;")
    cur.execute(
        """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_relationships_guild_lower_pair_type
    ON relationships (guild_id, lower(a_name), lower(b_name), rel_type);
    """
    )

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
    cur.execute(
        "DELETE FROM characters WHERE guild_id=%s AND lower(name)=lower(%s)",
        (guild_id, stored),
    )
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
            ORDER BY score DESC
            LIMIT %s
            """,
            (name, guild_id, name, name, limit),
        )
    rows = cur.fetchall()
    cur.close()
    con.close()
    return rows


# ============================================================
# DISPLAY (COMPACT, NO FLAVOR)
# ============================================================
def ensure_guild(interaction: discord.Interaction) -> Optional[str]:
    return str(interaction.guild.id) if interaction.guild else None


def meter_bar(score: int, width: int = 22) -> str:
    score = clamp_score(score)
    filled = int(round(((score + 100) / 200) * width))
    filled = max(0, min(width, filled))
    bar = "█" * filled + "░" * (width - filled)
    mid = width // 2
    bl = list(bar)
    bl[mid] = "┃"
    return "".join(bl)


def heat_emoji(score: int) -> str:
    score = clamp_score(score)
    if score <= -85:
        return "🟥"
    if score <= -60:
        return "🔴"
    if score <= -30:
        return "🟠"
    if score <= 20:
        return "🟡"
    if score <= 50:
        return "🟢"
    if score <= 80:
        return "🔵"
    return "🟣"


# ============================================================
# RANDOM DELTA RANGES (INTENSITY × POLARITY)
# ============================================================
# You asked: "general values with a range for low/med/high polarity with randomness"
# Here "intensity" controls magnitude; "polarity" controls sign bias.
DELTA_RANGES: Dict[str, Dict[str, Tuple[int, int]]] = {
    "low": {
        "positive": (1, 6),
        "negative": (-6, -1),
        "mixed": (-3, 3),
    },
    "med": {
        "positive": (5, 12),
        "negative": (-12, -5),
        "mixed": (-8, 8),
    },
    "high": {
        "positive": (10, 20),
        "negative": (-20, -10),
        "mixed": (-15, 15),
    },
}


def roll_delta(intensity: str, polarity: str, seed: int) -> int:
    inten = (intensity or "med").strip().lower()
    pol = (polarity or "mixed").strip().lower()
    if inten not in DELTA_RANGES:
        inten = "med"
    if pol not in DELTA_RANGES[inten]:
        pol = "mixed"

    lo, hi = DELTA_RANGES[inten][pol]
    rng = random.Random(seed)
    return int(rng.randint(lo, hi))


# ============================================================
# DISCORD BOT (SLASH COMMANDS)
# ============================================================
intents = discord.Intents.default()


class RPBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        # IMPORTANT: add commands before sync (fixes "slash commands not working/appearing")
        self.tree.add_command(char_group)
        self.tree.add_command(rel_group)
        self.tree.add_command(event_group)

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
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
# /CHAR
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
        text += f"\n… and {len(chars) - 100} more."

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
# /REL
# ============================================================
rel_group = app_commands.Group(name="rel", description="Track relationship meters between characters.")


@rel_group.command(name="view", description="View relationship meter between two characters.")
@app_commands.choices(rel_type=REL_TYPE_CHOICES)
@app_commands.autocomplete(a=character_autocomplete, b=character_autocomplete)
async def rel_view(
    interaction: discord.Interaction,
    rel_type: app_commands.Choice[str],
    a: str,
    b: str,
):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("This command only works in a server.", ephemeral=True)

    a, b = a.strip(), b.strip()
    if a.casefold() == b.casefold():
        return await interaction.response.send_message("Pick two different characters.", ephemeral=True)

    rt = rel_type.value
    row = get_relationship(guild_id, a, b, rt)
    score = int(row["score"]) if row else 0

    heat = heat_emoji(score)
    embed = discord.Embed(
        title=f"{rt.capitalize()}: {a} ↔ {b}",
        description=f"{heat} **{score}**\n`{meter_bar(score)}`",
    )
    await interaction.response.send_message(embed=embed)


@rel_group.command(name="set", description="Set relationship score (-100 to +100).")
@app_commands.choices(rel_type=REL_TYPE_CHOICES)
@app_commands.autocomplete(a=character_autocomplete, b=character_autocomplete)
@app_commands.describe(score="Integer from -100 to 100", note="Optional note")
async def rel_set(
    interaction: discord.Interaction,
    rel_type: app_commands.Choice[str],
    a: str,
    b: str,
    score: int,
    note: Optional[str] = None,
):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("This command only works in a server.", ephemeral=True)

    a, b = a.strip(), b.strip()
    if a.casefold() == b.casefold():
        return await interaction.response.send_message("Pick two different characters.", ephemeral=True)

    rt = rel_type.value

    if not character_exists(guild_id, a):
        add_character(guild_id, a)
    if not character_exists(guild_id, b):
        add_character(guild_id, b)

    prev = get_relationship(guild_id, a, b, rt)
    old = int(prev["score"]) if prev else 0

    final = upsert_relationship(
        guild_id=guild_id,
        name1=a,
        name2=b,
        rel_type=rt,
        new_score=score,
        updated_by=interaction.user.display_name,
        note=note,
        delta_for_history=(clamp_score(score) - old),
        reason="SET",
    )

    heat = heat_emoji(final)
    embed = discord.Embed(
        title=f"Set {rt.capitalize()}: {a} ↔ {b}",
        description=f"{heat} **{final}**\n`{meter_bar(final)}`",
    )
    await interaction.response.send_message(embed=embed)


@rel_group.command(name="add", description="Adjust relationship score by a delta (e.g., -10 or +25).")
@app_commands.choices(rel_type=REL_TYPE_CHOICES)
@app_commands.autocomplete(a=character_autocomplete, b=character_autocomplete)
@app_commands.describe(delta="Change amount (e.g., -10 or +15)", reason="Optional reason")
async def rel_add(
    interaction: discord.Interaction,
    rel_type: app_commands.Choice[str],
    a: str,
    b: str,
    delta: int,
    reason: Optional[str] = None,
):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("This command only works in a server.", ephemeral=True)

    a, b = a.strip(), b.strip()
    if a.casefold() == b.casefold():
        return await interaction.response.send_message("Pick two different characters.", ephemeral=True)

    rt = rel_type.value

    if not character_exists(guild_id, a):
        add_character(guild_id, a)
    if not character_exists(guild_id, b):
        add_character(guild_id, b)

    final = add_to_relationship(
        guild_id=guild_id,
        name1=a,
        name2=b,
        rel_type=rt,
        delta=delta,
        updated_by=interaction.user.display_name,
        reason=reason,
    )

    heat = heat_emoji(final)
    embed = discord.Embed(
        title=f"Updated {rt.capitalize()}: {a} ↔ {b}",
        description=f"{heat} **{final}**  (Δ {delta:+d})\n`{meter_bar(final)}`",
    )
    await interaction.response.send_message(embed=embed)


@rel_group.command(name="history", description="Show recent changes for a relationship meter.")
@app_commands.choices(rel_type=REL_TYPE_CHOICES)
@app_commands.autocomplete(a=character_autocomplete, b=character_autocomplete)
@app_commands.describe(limit="How many entries (max 15)")
async def rel_history(
    interaction: discord.Interaction,
    rel_type: app_commands.Choice[str],
    a: str,
    b: str,
    limit: int = 10,
):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("This command only works in a server.", ephemeral=True)

    limit = max(1, min(15, int(limit)))
    rt = rel_type.value

    rows = fetch_history(guild_id, a, b, rt, limit=limit)
    if not rows:
        return await interaction.response.send_message("No history yet for that pair/type.")

    lines = []
    for r in rows:
        lines.append(
            f"• `{int(r['delta']):+d}` → `{int(r['new_score'])}` by **{r['updated_by']}** ({r['updated_at']})"
            + (f" — {r['reason']}" if r.get("reason") else "")
        )

    embed = discord.Embed(title=f"History ({rt.capitalize()}): {a} ↔ {b}", description="\n".join(lines))
    await interaction.response.send_message(embed=embed)


# ============================================================
# /EVENT (RANDOM RANGE ROLL)
# ============================================================
event_group = app_commands.Group(name="event", description="Roll random deltas and apply them to a relationship meter.")


@event_group.command(name="roll", description="Roll a random delta (range by intensity/polarity) and apply it.")
@app_commands.choices(rel_type=REL_TYPE_CHOICES, intensity=INTENSITY_CHOICES, polarity=POLARITY_CHOICES)
@app_commands.autocomplete(a=character_autocomplete, b=character_autocomplete)
@app_commands.describe(
    rel_type="Which meter type to affect",
    a="Character A",
    b="Character B",
    intensity="Magnitude range: low/med/high",
    polarity="Bias: positive/negative/mixed",
)
async def event_roll(
    interaction: discord.Interaction,
    rel_type: app_commands.Choice[str],
    a: str,
    b: str,
    intensity: app_commands.Choice[str],
    polarity: app_commands.Choice[str],
):
    guild_id = ensure_guild(interaction)
    if not guild_id:
        return await interaction.response.send_message("This command only works in a server.", ephemeral=True)

    a, b = a.strip(), b.strip()
    if a.casefold() == b.casefold():
        return await interaction.response.send_message("Pick two different characters.", ephemeral=True)

    rt = rel_type.value
    inten = intensity.value
    pol = polarity.value if polarity else "mixed"

    if not character_exists(guild_id, a):
        add_character(guild_id, a)
    if not character_exists(guild_id, b):
        add_character(guild_id, b)

    prev = get_relationship(guild_id, a, b, rt)
    old_score = int(prev["score"]) if prev else 0

    # deterministic per interaction id (so discord retries don't change it)
    delta = roll_delta(inten, pol, seed=int(interaction.id))

    final = add_to_relationship(
        guild_id=guild_id,
        name1=a,
        name2=b,
        rel_type=rt,
        delta=delta,
        updated_by=interaction.user.display_name,
        reason=f"EVENT ROLL [{inten.upper()}/{pol.upper()}]",
    )

    heat = heat_emoji(final)
    embed = discord.Embed(
        title=f"Event Roll ({inten}/{pol}) — {rt.capitalize()}: {a} ↔ {b}",
        description=(
            f"**Delta:** `{delta:+d}`\n"
            f"**Score:** `{old_score}` → `{final}`\n\n"
            f"{heat} **{final}**\n`{meter_bar(final)}`"
        ),
    )
    embed.set_footer(text=f"Seed: {interaction.id}")

    await interaction.response.send_message(embed=embed)


# ============================================================
# RUN
# ============================================================
def main():
    if not TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN env var.")
    db_init()
    client.run(TOKEN)


if __name__ == "__main__":
    main()
