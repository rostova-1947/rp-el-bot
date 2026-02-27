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

# This is the change you asked for:
# users can force the roll to be Positive or Negative (or let it be Mixed)
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

    # --- Base tables ---
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

    # --- Per-guild settings (milestone log channel) ---
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS guild_settings (
        guild_id TEXT PRIMARY KEY,
        log_channel_id TEXT
    );
    """
    )

    # --- Migration: ensure rel_type exists + backfill ---
    cur.execute("ALTER TABLE relationships ADD COLUMN IF NOT EXISTS rel_type TEXT;")
    cur.execute("ALTER TABLE rel_history ADD COLUMN IF NOT EXISTS rel_type TEXT;")
    cur.execute("UPDATE relationships SET rel_type='platonic' WHERE rel_type IS NULL;")
    cur.execute("UPDATE rel_history SET rel_type='platonic' WHERE rel_type IS NULL;")
    cur.execute("ALTER TABLE relationships ALTER COLUMN rel_type SET NOT NULL;")
    cur.execute("ALTER TABLE rel_history ALTER COLUMN rel_type SET NOT NULL;")

    # --- Unique indexes ---
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
# GUILD SETTINGS (MILESTONE LOG CHANNEL)
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
# BLACKTHORN DISPLAY HELPERS + MILESTONES
# ============================================================
BLACKTHORN_STAGES = {
    "romantic": [
        (-85, "Blood in the Water"),
        (-60, "Cutthroat"),
        (-30, "Bad History"),
        (10, "Playing It Cool"),
        (35, "Slowburn"),
        (65, "Back in the Saddle"),
        (100, "Endgame"),
    ],
    "platonic": [
        (-85, "Kill-on-Sight"),
        (-60, "No-Contact"),
        (-30, "Thin Ice"),
        (20, "Town-Polite"),
        (50, "Good Company"),
        (80, "Ride-or-Die"),
        (100, "Chosen Family"),
    ],
    "familial": [
        (-90, "Scorched Earth"),
        (-70, "Cut Off"),
        (-40, "Bad Blood"),
        (15, "Holding Pattern"),
        (45, "Mending Fences"),
        (75, "Blood & Bone"),
        (100, "Unbreakable"),
    ],
}

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
        if score <= -85:
            return "Spite with a pulse. Somebody’s gonna bleed first."
        if score <= -60:
            return "Every look is a dare. Every word lands like a hook."
        if score <= -30:
            return "Chemistry they refuse to name. History they can’t outrun."
        if score <= 10:
            return "Careful distance. Watching for weakness. Wanting anyway."
        if score <= 35:
            return "Soft spots showing. Small mercies. Dangerous tenderness."
        if score <= 65:
            return "They keep finding their way back. Even when it’s stupid."
        return "It’s settled. This is the person they pick—again and again."

    if rel_type == "familial":
        if score <= -90:
            return "The kind of feud that poisons holidays."
        if score <= -70:
            return "Doors closed. Names not spoken."
        if score <= -40:
            return "Love is there—under the anger."
        if score <= 15:
            return "Quiet tension. Things left unsaid on purpose."
        if score <= 45:
            return "Trying. Showing up. Mending what can be mended."
        if score <= 75:
            return "Loyalty that hurts. Pride that runs deep."
        return "No matter what—blood shows up."

    # platonic
    if score <= -85:
        return "They’d cross the street rather than share air."
    if score <= -60:
        return "Bad for business. Worse for the heart."
    if score <= -30:
        return "One wrong move and it turns ugly."
    if score <= 20:
        return "Civil. Not close. Not cruel."
    if score <= 50:
        return "Easy laughs. Mutual respect. Same side, mostly."
    if score <= 80:
        return "If it goes down, they’re in it together."
    return "Family by choice. The real kind."


def milestone_message(old_score: int, new_score: int, rel_type: str) -> Optional[str]:
    rel_type = normalize_rel_type(rel_type)
    old_stage = stage_label(old_score, rel_type)
    new_stage = stage_label(new_score, rel_type)
    if old_stage == new_stage:
        return None
    return f"🏁 **Milestone:** *{old_stage}* → **{new_stage}**"


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


# ============================================================
# EVENT TABLE → POSITIVE OR NEGATIVE FILTER
# ============================================================
@dataclass(frozen=True)
class EventDef:
    title: str
    description: str
    delta: int  # positive or negative


EVENT_TABLE: Dict[str, Dict[str, List[EventDef]]] = {
    "romantic": {
        "low": [
            EventDef("Arena Lights", "Caught staring too long under the arena lights. Neither looks away first.", +5),
            EventDef("Shared Cigarette", "A cigarette passed between them like a truce no one agreed to out loud.", +3),
            EventDef("Sharp Words, Soft Eyes", "They snap, but the way they watch each other gives it away.", +2),
            EventDef("Bad Timing", "They reach for each other—then remember they’re not supposed to.", -3),
            EventDef("Old Wound (Mentioned)", "Somebody brings up Teddy. The air goes cold. Old pain resurfaces.", -5),
            EventDef("Diner Humiliation", "A public comment lands wrong at the diner. Everyone hears it.", -6),
        ],
        "med": [
            EventDef("Porch Patch-Up", "A patch-up on the porch at 2am. Quiet voices. No witnesses.", +10),
            EventDef("Jealousy Spike", "Someone laughs too close to one of them. The other goes rigid.", +6),
            EventDef("Truck Bed Confession", "In the truck bed, under stars, the truth leaks out in pieces.", +8),
            EventDef("Public Humiliation", "A public humiliation at the diner. Pride gets dragged through gravel.", -12),
            EventDef("Old Wound Reopened", "The Teddy subject isn’t avoided this time. It cracks something open.", -10),
            EventDef("Fistfight Energy", "It’s not a fight… but it feels like one. One wrong word and it’ll swing.", -8),
        ],
        "high": [
            EventDef("End of the Leash", "They finally say what they’ve been circling for months. It’s ugly and honest.", +15),
            EventDef("Aftercare", "Bandages, water, steady hands. The kind of care that changes things.", +12),
            EventDef("Kiss Like a Threat", "It’s not tender. It’s a decision. A line crossed on purpose.", +14),
            EventDef("Burned Bridge", "A betrayal—real or perceived. Something breaks in a way that echoes.", -18),
            EventDef("Ranch War", "The families get involved. They choose sides. The choosing hurts.", -15),
            EventDef("Cruel in Public", "They go cruel in public to protect a private softness. The damage is real.", -16),
        ],
    },
    "platonic": {
        "low": [
            EventDef("Shared Work", "Fences, feed, and silence. Respect earned the old way.", +4),
            EventDef("Inside Joke", "A small joke lands. For a second, it feels easy.", +3),
            EventDef("Town-Polite", "A nod in passing. Civil. Not warm—managed.", +2),
            EventDef("Thin Ice", "A harmless comment hits a nerve. The mood shifts fast.", -3),
            EventDef("Shoulder Check", "A deliberate shoulder check—petty, but pointed.", -2),
            EventDef("Bad Rumor", "They hear something said behind their back. They don’t forget.", -4),
        ],
        "med": [
            EventDef("Backed Up", "When it counted, they showed up. No questions. No hesitation.", +10),
            EventDef("Ride Together", "Same truck, same road, same problem. Suddenly they’re a team.", +8),
            EventDef("Saved Face", "One of them saves the other’s dignity in front of the town.", +7),
            EventDef("Public Argument", "Voices raised where everyone can hear. Pride takes the wheel.", -9),
            EventDef("Crossed Line", "Someone crosses a boundary. The apology comes late.", -10),
            EventDef("No Contact", "They shut the door. Not dramatic—final.", -12),
        ],
        "high": [
            EventDef("Ride-or-Die", "They go down together before they go down alone.", +15),
            EventDef("Chosen Family", "Not blood. Better. They claim each other anyway.", +16),
            EventDef("Kept the Secret", "A secret held, a risk taken. Loyalty proven.", +14),
            EventDef("Betrayal", "They sell each other out—on purpose or by accident. Either way: bitter.", -16),
            EventDef("Hands Thrown", "It goes physical. Not playful. Nobody wins.", -18),
            EventDef("Cut Deep", "Words said that can’t be unsaid. The town will repeat them for weeks.", -15),
        ],
    },
    "familial": {
        "low": [
            EventDef("Showed Up", "They show up anyway. That’s the whole love language.", +4),
            EventDef("Kitchen Truce", "Coffee poured. Plates set. A truce made with chores and silence.", +3),
            EventDef("Small Mercy", "An apology in actions, not words.", +2),
            EventDef("Old Grudge", "An old grudge slips out mid-sentence. Everybody tenses.", -4),
            EventDef("Cold Shoulder", "They freeze each other out like it’s second nature.", -3),
            EventDef("Bad Blood", "Someone brings up the past like it’s a weapon.", -5),
        ],
        "med": [
            EventDef("Mending Fences", "Actual fences, and the other kind. They work without speaking much.", +10),
            EventDef("Protective Instinct", "Family closes ranks. Somebody gets protected whether they deserve it or not.", +8),
            EventDef("Blood & Bone", "They take the hit for each other. Without asking.", +12),
            EventDef("Cut Off", "They cut contact. Holiday-level consequences.", -12),
            EventDef("Blowup", "It’s a blowup that rattles the whole house.", -10),
            EventDef("Line in the Sand", "A line gets drawn. Somebody stands on the wrong side.", -11),
        ],
        "high": [
            EventDef("Unbreakable", "They prove it: no matter what, they show up.", +16),
            EventDef("Shared Grief", "Grief softens what anger couldn’t. They let each other in.", +14),
            EventDef("Protect the Name", "They defend the family name like it’s sacred.", +12),
            EventDef("Scorched Earth", "They go scorched earth. Thanksgivings will remember.", -18),
            EventDef("Inheritance Fight", "Money, land, legacy—every old wound shows its teeth.", -16),
            EventDef("Public Shame", "Family business becomes town business. Shame spreads fast.", -15),
        ],
    },
}


def _polarity_filter(events: List[EventDef], polarity: str) -> List[EventDef]:
    p = (polarity or "mixed").strip().lower()
    if p == "positive":
        return [e for e in events if e.delta > 0] or events
    if p == "negative":
        return [e for e in events if e.delta < 0] or events
    return events


def roll_event(rel_type: str, intensity: str, polarity: str, seed: int) -> EventDef:
    rel_type = normalize_rel_type(rel_type)
    intensity = (intensity or "med").strip().lower()
    if intensity not in ("low", "med", "high"):
        intensity = "med"

    base = EVENT_TABLE[rel_type][intensity]
    pool = _polarity_filter(base, polarity)

    rng = random.Random(seed)  # deterministic per interaction.id
    return rng.choice(pool)


# ============================================================
# DISCORD BOT (SLASH COMMANDS)
# ============================================================
intents = discord.Intents.default()


class RPBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        # Add groups BEFORE syncing (fixes "commands not appearing")
        self.tree.add_command(char_group)
        self.tree.add_command(rel_group)
        self.tree.add_command(settings_group)
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
# /CHAR GROUP
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
# /SETTINGS GROUP (LOG CHANNEL)
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
# /REL GROUP
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
    note = row.get("note") if row else None

    meta = REL_TYPE_META.get(rt, {"emoji": "🔗", "title": rel_type_title(rt)})
    status = stage_label(score, rt)
    mood = mood_line(score, rt)
    heat = heat_emoji(score)

    embed = discord.Embed(
        title=f"{meta['emoji']} {meta['title']}: {a} ↔ {b}",
        description=f"{heat} **{score}** • **{status}**\n`{meter_bar(score)}`\n*{mood}*",
    )
    if note:
        embed.add_field(name="Note", value=note, inline=False)

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

    milestone = milestone_message(old, final, rt)

    meta = REL_TYPE_META.get(rt, {"emoji": "🔗", "title": rel_type_title(rt)})
    status = stage_label(final, rt)
    mood = mood_line(final, rt)
    heat = heat_emoji(final)

    desc = f"{heat} **{final}** • **{status}**\n`{meter_bar(final)}`\n*{mood}*"
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
        rel_type=rt,
        a=a,
        b=b,
        old_score=old,
        new_score=final,
        delta=(final - old),
        reason="SET",
    )


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

    prev = get_relationship(guild_id, a, b, rt)
    old_score = int(prev["score"]) if prev else 0

    final = add_to_relationship(
        guild_id=guild_id,
        name1=a,
        name2=b,
        rel_type=rt,
        delta=delta,
        updated_by=interaction.user.display_name,
        reason=reason,
    )

    milestone = milestone_message(old_score, final, rt)

    meta = REL_TYPE_META.get(rt, {"emoji": "🔗", "title": rel_type_title(rt)})
    status = stage_label(final, rt)
    mood = mood_line(final, rt)
    heat = heat_emoji(final)

    desc = f"{heat} **{final}** • **{status}**\n`{meter_bar(final)}`\n*{mood}*\n\n**Delta:** `{delta:+d}`"
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
        rel_type=rt,
        a=a,
        b=b,
        old_score=old_score,
        new_score=final,
        delta=delta,
        reason=reason,
    )


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

    embed = discord.Embed(title=f"History ({rel_type_title(rt)}): {a} ↔ {b}", description="\n".join(lines))
    await interaction.response.send_message(embed=embed)


# ============================================================
# /EVENT GROUP  (ROLL POSITIVE / NEGATIVE / MIXED EVENTS)
# ============================================================
event_group = app_commands.Group(name="event", description="Roll Blackthorn events that change relationship meters.")


@event_group.command(name="roll", description="Roll a Blackthorn event and apply its delta.")
@app_commands.choices(rel_type=REL_TYPE_CHOICES, intensity=INTENSITY_CHOICES, polarity=POLARITY_CHOICES)
@app_commands.autocomplete(a=character_autocomplete, b=character_autocomplete)
@app_commands.describe(
    rel_type="Which meter type to affect",
    a="Character A",
    b="Character B",
    intensity="How big the story beat is",
    polarity="Force a positive or negative beat (or mixed)",
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

    # Seed for deterministic roll per command
    ev = roll_event(rt, inten, pol, seed=int(interaction.id))
    delta = int(ev.delta)

    final = add_to_relationship(
        guild_id=guild_id,
        name1=a,
        name2=b,
        rel_type=rt,
        delta=delta,
        updated_by=interaction.user.display_name,
        reason=f"EVENT ROLL [{inten.upper()}/{pol.upper()}]: {ev.title}",
    )

    milestone = milestone_message(old_score, final, rt)

    meta = REL_TYPE_META.get(rt, {"emoji": "🎲", "title": rel_type_title(rt)})
    status = stage_label(final, rt)
    mood = mood_line(final, rt)
    heat = heat_emoji(final)

    sign = "✅" if delta > 0 else ("⚠️" if delta < 0 else "➖")
    desc = (
        f"**{ev.title}** {sign}\n"
        f"*{ev.description}*\n\n"
        f"**Impact:** `{delta:+d}`  |  **Score:** `{old_score}` → `{final}`\n"
        f"{heat} **{status}**\n"
        f"`{meter_bar(final)}`\n"
        f"*{mood}*"
    )
    if milestone:
        desc += f"\n\n{milestone}"

    embed = discord.Embed(
        title=f"{meta['emoji']} Event Roll — {meta['title']}: {a} ↔ {b}",
        description=desc,
    )
    embed.set_footer(text=f"Intensity: {inten} • Polarity: {pol} • Seed: {interaction.id}")

    await interaction.response.send_message(embed=embed)

    await post_milestone_log(
        interaction=interaction,
        rel_type=rt,
        a=a,
        b=b,
        old_score=old_score,
        new_score=final,
        delta=delta,
        reason=f"EVENT ROLL [{inten.upper()}/{pol.upper()}]: {ev.title}",
    )


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
