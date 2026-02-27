import os
from datetime import datetime, timezone
from typing import Optional, List, Tuple

import discord
from discord import app_commands
import psycopg2
import psycopg2.extras

# -----------------------------
# Env
# -----------------------------
TOKEN = os.getenv("DISCORD_TOKEN")
# Railway usually provides DATABASE_URL for managed Postgres when referenced correctly.
DATABASE_URL = (
    os.getenv("DATABASE_URL")
    or os.getenv("DATABASE_PRIVATE_URL")
    or os.getenv("PGDATABASE_URL")
)

# Optional: make slash commands appear instantly in a single server
# Set this to your server ID in Railway Variables, e.g. 123456789012345678
GUILD_ID = os.getenv("GUILD_ID")  # optional

# SSL: prefer is usually safest across hosted environments
PGSSLMODE = os.getenv("PGSSLMODE", "prefer")  # try "require" or "disable" if needed


# -----------------------------
# Relationship Types
# -----------------------------
REL_TYPES = ("romantic", "platonic", "familial")

def normalize_rel_type(t: Optional[str]) -> str:
    if not t:
        return "platonic"
    t = t.strip().lower()
    if t not in REL_TYPES:
        raise ValueError(f"Invalid relationship type: {t}. Must be one of {REL_TYPES}.")
    return t


# -----------------------------
# DB layer (Postgres)
# -----------------------------
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

    # --- Base tables ---
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
        rel_type TEXT,  -- will be made NOT NULL via migration below
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
        rel_type TEXT,  -- will be made NOT NULL via migration below
        delta INTEGER NOT NULL,
        new_score INTEGER NOT NULL,
        updated_by TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        reason TEXT
    );
    """)

    # --- Migration: ensure rel_type exists and is populated ---
    cur.execute("ALTER TABLE relationships ADD COLUMN IF NOT EXISTS rel_type TEXT;")
    cur.execute("ALTER TABLE rel_history ADD COLUMN IF NOT EXISTS rel_type TEXT;")

    # Backfill existing rows to 'platonic'
    cur.execute("UPDATE relationships SET rel_type='platonic' WHERE rel_type IS NULL;")
    cur.execute("UPDATE rel_history SET rel_type='platonic' WHERE rel_type IS NULL;")

    # Make required going forward
    cur.execute("ALTER TABLE relationships ALTER COLUMN rel_type SET NOT NULL;")
    cur.execute("ALTER TABLE rel_history ALTER COLUMN rel_type SET NOT NULL;")

    # --- Unique indexes for case-insensitive uniqueness ---
    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS ux_characters_guild_lower_name
    ON characters (guild_id, lower(name));
    """)

    # Old relationship index may exist
    cur.execute("DROP INDEX IF EXISTS ux_relationships_guild_lower_pair;")

    # New index includes rel_type so meters coexist
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
    # If rel_type is None, return all types; otherwise filter
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


# -----------------------------
# Display helpers
# -----------------------------
def stage_label(score: int) -> str:
    if score <= -80: return "Nemeses"
    if score <= -50: return "Enemies"
    if score <= -20: return "Rivals"
    if score <= 20:  return "Neutral"
    if score <= 50:  return "Friends"
    if score <= 80:  return "Close"
    return "Soulmates"


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
    rt = normalize_rel_type(rt)
    return rt.capitalize()


REL_TYPE_CHOICES = [
    app_commands.Choice(name="romantic", value="romantic"),
    app_commands.Choice(name="platonic", value="platonic"),
    app_commands.Choice(name="familial", value="familial"),
]


# -----------------------------
# Discord bot (slash commands)
# -----------------------------
intents = discord.Intents.default()

class RPBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        # If GUILD_ID is set, sync instantly to that server; otherwise global sync.
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


# -----------------------------
# /char group
# -----------------------------
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

client.tree.add_command(char_group)


# -----------------------------
# /rel group
# -----------------------------
# -----------------------------
# /rel group
# -----------------------------
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
        lines.append(f"• **{r['other']}** — `{score}` ({stage_label(score)})  ·  *{r['rel_type']}*")

    title = f"Top relationships for {name}"
    if rel_type:
        title += f" ({rel_type_title(rel_type)})"

    embed = discord.Embed(title=title, description="\n".join(lines))
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

    if not character_exists(guild_id, a): add_character(guild_id, a)
    if not character_exists(guild_id, b): add_character(guild_id, b)

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

    embed = discord.Embed(
        title=f"Set {rel_type_title(rel_type)}: {a} ↔ {b}",
        description=f"`{meter_bar(final)}`\n**Score:** `{final}` • **Status:** **{stage_label(final)}**",
    )
    if note:
        embed.add_field(name="Note", value=note, inline=False)

    await interaction.response.send_message(embed=embed)


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

    if not character_exists(guild_id, a): add_character(guild_id, a)
    if not character_exists(guild_id, b): add_character(guild_id, b)

    final = add_to_relationship(
        guild_id=guild_id,
        name1=a,
        name2=b,
        rel_type=rel_type,
        delta=delta,
        updated_by=interaction.user.display_name,
        reason=reason
    )

    embed = discord.Embed(
        title=f"Updated {rel_type_title(rel_type)}: {a} ↔ {b}",
        description=f"`{meter_bar(final)}`\n**Delta:** `{delta:+d}` → **Score:** `{final}` • **Status:** **{stage_label(final)}**",
    )
    if reason:
        embed.add_field(name="Reason", value=reason, inline=False)

    await interaction.response.send_message(embed=embed)


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
            f"• `{int(r['delta']):+d}` → `{int(r['new_score'])}` by **{r['updated_by']}** ({r['updated_at']})"
            + (f" — {r['reason']}" if r.get("reason") else "")
        )

    embed = discord.Embed(
        title=f"History ({rel_type_title(rel_type)}): {a} ↔ {b}",
        description="\n".join(lines)
    )
    await interaction.response.send_message(embed=embed)


client.tree.add_command(rel_group)

# -----------------------------
# Run
# -----------------------------
def main():
    if not TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN env var.")
    db_init()
    client.run(TOKEN)

if __name__ == "__main__":
    main()



