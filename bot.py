"""Lordnine: Infinite Class — Field Boss Timer bot.

Two kinds of bosses (hardcoded from Fieldboss_Timer.md, permanent data):
  * Interval bosses  — respawn a fixed number of hours after they are killed.
                       Record kills with `!killed <boss> [time]`.
  * Scheduled bosses — spawn at fixed weekly day/time slots.

The bot posts a notification in the configured channel (`!setchannel`)
10 minutes before every known spawn.

Built to run on Render's free tier:
  * All times use the BOT_TZ timezone (default Asia/Manila), no matter
    where the server is located.
  * supervisor.py binds $PORT so an uptime pinger can keep the free
    instance awake. It lives there, not here, so a bot that cannot reach
    Discord no longer leaves the port dead and the service asleep.
  * State (channel + kill times) is mirrored into a pinned Discord message,
    so it survives Render wiping the disk on every restart/redeploy. That
    message lives in the notification channel by default; `!setstoragechannel`
    moves it to a private channel to keep the timer channel clean.
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# The guild's timezone, and the anchor for every naive datetime here.
#
# This used to be enforced process-wide with os.environ["TZ"] plus
# time.tzset(). tzset is Unix-only -- it does not exist on Windows -- so
# on a PC there was no process zone to set and every naive <-> epoch
# conversion silently fell back to whatever the machine was set to. For
# `deaths` that is not cosmetic: state written on one host and read on
# another moved every boss kill time by the offset between them.
#
# ZoneInfo works everywhere, so BOT_TZ is now applied at each conversion
# (local_now(), _epoch()) instead of globally. tzset is still called where it
# exists, so log and file timestamps keep matching the bots' own clock.
BOT_TZ = os.getenv("BOT_TZ", os.environ.get("TZ", "Asia/Manila"))
_TZ = ZoneInfo(BOT_TZ)
if hasattr(time, "tzset"):
    os.environ["TZ"] = BOT_TZ
    time.tzset()


def local_now() -> datetime:
    """The current time in BOT_TZ, naive.

    Named local_now rather than now so it cannot be shadowed: `now` is
    the obvious name for a local holding the current time, and this file
    already uses it that way twice. Written `now = now()`, the assignment
    binds `now` locally for the whole function, so the call on its own
    right-hand side raises UnboundLocalError -- which it did, on every
    tick of the watcher and every !killed, until the name changed.

    Naive rather than aware because every comparison in this module --
    spawn schedules, kill times, the state file -- is against naive
    datetimes, and Python refuses to compare the two.
    """
    return datetime.now(_TZ).replace(tzinfo=None)


def _epoch(dt: datetime) -> int:
    """A naive BOT_TZ datetime as a Unix timestamp, on any host."""
    return int(dt.replace(tzinfo=_TZ).timestamp())

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

import channel_guard
import discord_login

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

DATA_FILE = Path(__file__).with_name("data.json")
NOTIFY_BEFORE = timedelta(minutes=10)
# How long after the spawn moment the "has spawned!" message may still be
# sent (covers the watcher's 30s cadence and short outages/restarts).
SPAWN_ANNOUNCE_WINDOW = timedelta(minutes=10)
STATE_MARKER = "FBTIMER_STATE_V1"

# ---------------------------------------------------------------------------
# Permanent boss data (from Fieldboss_Timer.md)
# ---------------------------------------------------------------------------

# Boss -> respawn time in hours after its death.
INTERVAL_BOSSES = {
    "Venatus": 10,
    "Viorent": 10,
    "Ego": 21,
    "Livera": 24,
    "Araneo": 24,
    "Undo": 24,
    "Dalia": 18,
    "General": 29,
    "Amentis": 29,
    "Baron": 32,
    "Wannitas": 48,
    "Metus": 48,
    "Duplican": 48,
    "Shuliar": 35,
    "Gareth": 32,
    "Titore": 37,
    "Larba": 35,
    "Catena": 35,
    "Secreta": 62,
    "Ordo": 62,
    "Asta": 62,
    "Supore": 62
}

# Boss -> list of (weekday, "HH:MM") weekly spawn slots. Monday = 0.
SCHEDULED_BOSSES = {
    "Clemantis": [(0, "11:30"), (3, "19:00")],
    "Saphirus": [(6, "17:00"), (1, "11:30")],
    "Neutro": [(1, "19:00"), (3, "11:30")],
    "Thymele": [(0, "19:00"), (2, "11:30")],
    "Milavy": [(5, "15:00")],
    "Ringor": [(5, "17:00")],
    "Roderick": [(4, "19:00")],
    "Auraq": [(4, "22:00"), (2, "21:00")],
    "Chaiflock": [(6, "15:00")],
    "Benji": [(6, "21:00")],
    "Libitina": [(0, "21:00"), (5, "21:00")],
    "Rakajeth": [(1, "22:00"), (6, "19:00")],
    "Icaruthia": [(1, "21:00"), (4, "21:00")],
    "Motti": [(2, "19:00"), (5, "19:00")],
    "Camalia": [(3, "21:00")],
    "Nevaeh": [(6, "22:00")],
    "Tumier": [(6, "19:00")],
    "Lucus": [(5, "22:00")],
}

ALL_BOSSES = {name.lower(): name for name in (*INTERVAL_BOSSES, *SCHEDULED_BOSSES)}
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# ---------------------------------------------------------------------------
# State: notification channel, recorded death times, sent notifications.
# Kept in memory, mirrored to data.json (local cache) and to a pinned
# Discord message (survives Render's ephemeral disk).
# ---------------------------------------------------------------------------


def load_local() -> dict:
    if DATA_FILE.exists():
        with DATA_FILE.open() as f:
            return json.load(f)
    return {
        "channel_id": None,
        "storage_channel_id": None,
        "tod_channel_id": None,
        "deaths": {},
        "notified": {},
        "spawned": {},
    }


def save_local() -> None:
    with DATA_FILE.open("w") as f:
        json.dump(data, f, indent=2)


data = load_local()
data.setdefault("spawned", {})
data.setdefault("storage_channel_id", None)
# Absent from data.json and from pinned state written before the channel
# guard existed; None keeps the guard inert until !settodchannel is run.
data.setdefault("tod_channel_id", None)
state_msg: discord.Message | None = None
state_pinned = False
# How many history messages restore_state() scans per channel, and how deep
# the unpinned storage message may sink before persist() reposts it.
RESTORE_SCAN_LIMIT = 100
REPOST_DEPTH = 25


def prune_state(now: datetime) -> None:
    """Drop markers that can no longer matter."""
    for boss, iso in list(data["notified"].items()):
        if datetime.fromisoformat(iso) < now:
            del data["notified"][boss]
    for boss, iso in list(data["spawned"].items()):
        if datetime.fromisoformat(iso) < now - timedelta(hours=1):
            del data["spawned"][boss]


def _unix_map(section: str) -> dict:
    return {b: _epoch(datetime.fromisoformat(v))
            for b, v in data[section].items()}


def storage_channel_id() -> int | None:
    """Where the storage message lives: the dedicated channel if one was set
    with `!setstoragechannel`, otherwise the notification channel."""
    return data.get("storage_channel_id") or data.get("channel_id")


def encode_state() -> str:
    payload = {
        "channel_id": data["channel_id"],
        "storage_channel_id": data.get("storage_channel_id"),
        "tod_channel_id": data.get("tod_channel_id"),
        "deaths": _unix_map("deaths"),
        "notified": _unix_map("notified"),
        "spawned": _unix_map("spawned"),
    }
    content = (
        f"{STATE_MARKER} — bot storage, please don't delete this message.\n"
        f"```json\n{json.dumps(payload)}\n```"
    )
    if len(content) > 1990:  # never exceed Discord's 2000-char message limit;
        # keep the critical data (channel + kill times) and drop the rest
        payload["notified"] = {}
        payload["spawned"] = {}
        content = (
            f"{STATE_MARKER} — bot storage, please don't delete this message.\n"
            f"```json\n{json.dumps(payload)}\n```"
        )
    return content


def decode_state(content: str) -> dict | None:
    def from_unix(section: dict) -> dict:
        return {b: datetime.fromtimestamp(t, _TZ).replace(tzinfo=None).isoformat()
                for b, t in section.items() if b.lower() in ALL_BOSSES}

    try:
        raw = content.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
        payload = json.loads(raw)
        return {
            "channel_id": payload["channel_id"],
            # Absent in messages written before storage channels existed.
            "storage_channel_id": payload.get("storage_channel_id"),
            # Absent in messages written before the channel guard existed.
            "tod_channel_id": payload.get("tod_channel_id"),
            "deaths": from_unix(payload["deaths"]),
            "notified": from_unix(payload["notified"]),
            "spawned": from_unix(payload.get("spawned", {})),
        }
    except (IndexError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None


async def try_pin_state_msg() -> None:
    """Pin the storage message if possible (needs Manage Messages)."""
    global state_pinned
    try:
        await state_msg.pin()
        state_pinned = True
    except discord.HTTPException:
        # Without the pin the storage message must stay within the history
        # window restore_state() scans; persist() reposts it to ensure that.
        state_pinned = False
        print("Could not pin the storage message (missing Manage Messages?).")


async def state_msg_is_recent() -> bool:
    """Is the storage message shallow enough for restore_state() to find?"""
    channel = state_msg.channel
    if channel.last_message_id == state_msg.id:
        return True
    try:
        async for msg in channel.history(limit=REPOST_DEPTH):
            if msg.id == state_msg.id:
                return True
    except discord.HTTPException:
        return True  # can't check — keep editing rather than churn
    return False


async def persist() -> None:
    """Save state locally and mirror it into a pinned/recent Discord message."""
    global state_msg
    prune_state(local_now())
    save_local()
    target_id = storage_channel_id()
    if not target_id:
        return
    content = encode_state()
    try:
        if (
            state_msg is not None
            and state_msg.channel.id == target_id
            and (state_pinned or await state_msg_is_recent())
        ):
            await state_msg.edit(content=content)
            return

        # Either there is no storage message yet, it sits in a channel we no
        # longer store in, or it is unpinned and has sunk below the window
        # restore_state() scans — losing it there silently disables every
        # spawn notification. Post the replacement before deleting the old
        # one so a failure here can't leave Discord with no copy at all.
        channel = bot.get_channel(target_id)
        if channel is None:
            return
        old = state_msg
        state_msg = await channel.send(content)
        if old is not None:
            try:
                await old.delete()
            except discord.HTTPException:
                pass
        await try_pin_state_msg()
    except discord.HTTPException as exc:
        print(f"Failed to mirror state to Discord: {exc}")


async def restore_state() -> bool:
    """Find the storage message after a restart and reload state from it."""
    global state_msg, state_pinned
    candidates: list[discord.Message] = []
    for guild in bot.guilds:
        for channel in guild.text_channels:
            perms = channel.permissions_for(guild.me)
            if not (perms.view_channel and perms.read_message_history):
                continue
            seen: set[int] = set()
            try:
                for messages in (
                    channel.pins(limit=50),
                    channel.history(limit=RESTORE_SCAN_LIMIT),
                ):
                    async for msg in messages:
                        if (
                            msg.author.id == bot.user.id
                            and msg.content.startswith(STATE_MARKER)
                            and msg.id not in seen
                        ):
                            seen.add(msg.id)
                            candidates.append(msg)
            except discord.HTTPException:
                continue

    # Newest edit wins; anything older is a stale leftover.
    candidates.sort(key=lambda m: m.edited_at or m.created_at, reverse=True)
    for msg in candidates:
        decoded = decode_state(msg.content)
        if decoded is None:
            continue
        data.update(decoded)
        state_msg = msg
        state_pinned = msg.pinned
        save_local()
        for stale in candidates:
            if stale.id != msg.id:
                try:
                    await stale.delete()
                except discord.HTTPException:
                    pass
        return True
    return False


# ---------------------------------------------------------------------------
# Spawn-time helpers (naive local time in BOT_TZ; shown as Discord timestamps)
# ---------------------------------------------------------------------------


def next_scheduled_spawn(boss: str, now: datetime) -> datetime:
    """Earliest upcoming slot for a fixed-schedule boss."""
    candidates = []
    for weekday, hhmm in SCHEDULED_BOSSES[boss]:
        hour, minute = map(int, hhmm.split(":"))
        days_ahead = (weekday - now.weekday()) % 7
        candidate = (now + timedelta(days=days_ahead)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate <= now:
            candidate += timedelta(days=7)
        candidates.append(candidate)
    return min(candidates)


def last_scheduled_spawn(boss: str, now: datetime) -> datetime:
    """Most recent slot of a fixed-schedule boss that is already past."""
    candidates = []
    for weekday, hhmm in SCHEDULED_BOSSES[boss]:
        hour, minute = map(int, hhmm.split(":"))
        days_back = (now.weekday() - weekday) % 7
        candidate = (now - timedelta(days=days_back)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate > now:
            candidate -= timedelta(days=7)
        candidates.append(candidate)
    return max(candidates)


def interval_spawn(boss: str) -> datetime | None:
    """Spawn time for an interval boss, or None if no kill is recorded."""
    death_iso = data["deaths"].get(boss)
    if not death_iso:
        return None
    death = datetime.fromisoformat(death_iso)
    return death + timedelta(hours=INTERVAL_BOSSES[boss])


def next_spawn(boss: str, now: datetime) -> datetime | None:
    if boss in SCHEDULED_BOSSES:
        return next_scheduled_spawn(boss, now)
    return interval_spawn(boss)


def resolve_boss(name: str) -> str | None:
    """Case-insensitive exact or unique-prefix match on a boss name."""
    key = name.lower()
    if key in ALL_BOSSES:
        return ALL_BOSSES[key]
    matches = [full for low, full in ALL_BOSSES.items() if low.startswith(key)]
    return matches[0] if len(matches) == 1 else None


def parse_death_time(text: str, now: datetime) -> datetime | None:
    """Parse '9PM', '9:30 pm', '21:00' or '2026-07-20 21:00'. None = invalid."""
    raw = text.strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M")
    except ValueError:
        pass

    compact = raw.replace(" ", "").upper()
    for fmt in ("%H:%M", "%I%p", "%I:%M%p"):
        try:
            t = datetime.strptime(compact, fmt)
        except ValueError:
            continue
        death = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if death > now:  # a future time today means it happened yesterday
            death -= timedelta(days=1)
        return death
    return None


def ts(dt: datetime, style: str = "f") -> str:
    """Discord timestamp markup — renders in each viewer's own timezone."""
    return f"<t:{_epoch(dt)}:{style}>"


# ---------------------------------------------------------------------------
# Shared embed look — every message the bot sends uses the same design
# as `!bosses`: orange embed, emoji title, boss fields, footer for hints.
# ---------------------------------------------------------------------------

EMBED_COLOR = discord.Color.orange()


def make_embed(
    title: str, description: str | None = None, footer: str | None = None
) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=EMBED_COLOR)
    if footer:
        embed.set_footer(text=footer)
    return embed


def boss_embed(
    title: str, boss: str, value: str, footer: str | None = None
) -> discord.Embed:
    """One-boss embed shaped like a single `!bosses` grid cell."""
    embed = make_embed(title, footer=footer)
    embed.add_field(name=boss, value=value, inline=True)
    return embed


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
_started = False


@bot.event
async def on_ready():
    global _started
    print(f"Logged in as {bot.user} (ID: {bot.user.id}).")
    if _started:
        return
    _started = True
    try:
        restored = await restore_state()
    except Exception as exc:  # never let a restore failure kill notifications
        restored = False
        print(f"State restore crashed: {exc!r}")
    print(f"State restored from Discord: {restored}")
    spawn_watcher.start()


@tasks.loop(seconds=30)
async def spawn_watcher():
    """Post a heads-up 10 minutes before each spawn, and again at spawn time."""
    channel_id = data.get("channel_id")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        return

    now = local_now()
    changed = False
    for boss in ALL_BOSSES.values():
        # 10-minute warning for the upcoming spawn.
        spawn = next_spawn(boss, now)
        if spawn is not None and timedelta(0) <= spawn - now <= NOTIFY_BEFORE:
            spawn_iso = spawn.isoformat()
            if data["notified"].get(boss) != spawn_iso:
                try:
                    await channel.send(
                        embed=boss_embed(
                            "🔔 Spawning Soon",
                            boss,
                            f"🔔 Spawns {ts(spawn, 'R')}\n{ts(spawn, 'F')}",
                        )
                    )
                    data["notified"][boss] = spawn_iso
                    changed = True
                except discord.HTTPException as exc:
                    print(f"Failed to send warning for {boss}: {exc}")
                    await _QUIET.note(exc)

        # "Has spawned!" message right when the spawn moment passes.
        if boss in SCHEDULED_BOSSES:
            spawned_at = last_scheduled_spawn(boss, now)
        else:
            spawned_at = interval_spawn(boss)
        if spawned_at is None or not (
            timedelta(0) <= now - spawned_at <= SPAWN_ANNOUNCE_WINDOW
        ):
            continue
        spawned_iso = spawned_at.isoformat()
        if data["spawned"].get(boss) == spawned_iso:
            continue
        embed = boss_embed(
            "⚔️ Boss Spawned",
            boss,
            f"⚔️ Has spawned!\n{ts(spawned_at, 'F')}",
            footer=(
                f"After the kill, log it with !killed {boss}"
                if boss in INTERVAL_BOSSES
                else None
            ),
        )
        try:
            await channel.send(embed=embed)
            data["spawned"][boss] = spawned_iso
            changed = True
        except discord.HTTPException as exc:
            print(f"Failed to send spawn message for {boss}: {exc}")
            await _QUIET.note(exc)
    if changed:
        await persist()


@spawn_watcher.error
async def spawn_watcher_error(exc: BaseException) -> None:
    # An unhandled exception stops a tasks.loop for good — restart it so one
    # bad tick can't silently end all future notifications.
    print(f"spawn_watcher crashed: {exc!r}; restarting")
    spawn_watcher.restart()


# The setup commands must work anywhere -- they are how a channel gets
# chosen. Everything else is confined to the notification channel and the
# TOD log. The storage channel is deliberately excluded: it exists only to
# hold the pinned state message.
_EXEMPT_COMMANDS = frozenset({
    "setchannel", "setstoragechannel", "clearstoragechannel", "settodchannel",
})


def command_channels(ctx):
    """The channel ids ctx.command may run in, or EXEMPT."""
    if ctx.command.name in _EXEMPT_COMMANDS:
        return channel_guard.EXEMPT
    return (data.get("channel_id"), data.get("tod_channel_id"))


bot.add_check(channel_guard.make_check(command_channels))


# Closes this bot when 429s keep arriving after login, so the supervisor
# can hold every child off an address Discord is refusing. discord_login
# only catches the login-time case.
_QUIET = discord_login.QuietOnBlock(bot, "[timer]")


@bot.event
async def on_command_error(ctx, error):
    """Keep the log readable; this bot has no user-facing error replies.

    CommandNotFound is swallowed because all three bots share the "!"
    prefix, so most commands reaching this one belong to another bot --
    it already fills the logs with tracebacks today. WrongChannel is
    swallowed silently on purpose: replying would post into the very
    channel the guard is keeping quiet.
    """
    if isinstance(error, (commands.CommandNotFound, channel_guard.WrongChannel)):
        return
    print(f"Command {ctx.command} failed: {error!r}", file=sys.stderr, flush=True)
    await _QUIET.note(error)


@bot.command(name="setchannel")
async def setchannel(ctx: commands.Context):
    """Use spawn notifications in this channel: !setchannel"""
    data["channel_id"] = ctx.channel.id
    await persist()
    if data.get("storage_channel_id"):
        footer = "Timers are stored in the storage channel you set earlier."
    else:
        footer = (
            "I keep a pinned storage message here so timers survive restarts — "
            "please don't delete it. Use !setstoragechannel elsewhere to hide it."
        )
    await ctx.send(
        embed=make_embed(
            "✅ Notification Channel Set",
            f"Spawn notifications will be posted in {ctx.channel.mention}.",
            footer=footer,
        )
    )


@bot.command(name="setstoragechannel")
async def setstoragechannel(ctx: commands.Context):
    """Keep the storage message in this channel: !setstoragechannel"""
    data["storage_channel_id"] = ctx.channel.id
    # Moves the existing message here, kill times and all, and deletes the old
    # copy — nothing has to be re-entered.
    await persist()
    await ctx.send(
        embed=make_embed(
            "✅ Storage Channel Set",
            f"Timers are now stored in {ctx.channel.mention}, so the "
            "notification channel stays clean.",
            footer=(
                "Existing timers moved across automatically — please don't "
                "delete the storage message or remove my access here."
            ),
        )
    )


@bot.command(name="clearstoragechannel")
async def clearstoragechannel(ctx: commands.Context):
    """Store the timers in the notification channel again: !clearstoragechannel"""
    data["storage_channel_id"] = None
    await persist()
    await ctx.send(
        embed=make_embed(
            "✅ Storage Channel Cleared",
            "Timers are stored in the notification channel again.",
        )
    )


@bot.command(name="settodchannel")
@commands.has_permissions(administrator=True)
async def settodchannel(ctx: commands.Context):
    """Also take commands in this channel: !settodchannel"""
    data["tod_channel_id"] = ctx.channel.id
    await persist()
    await ctx.send(
        embed=make_embed(
            "✅ TOD Log Channel Set",
            f"I'll accept commands in {ctx.channel.mention} as well as the "
            "notification channel, and ignore them everywhere else.",
            footer="Run this in a different channel to move it.",
        )
    )


@bot.command(name="killed")
async def killed(ctx: commands.Context, boss_name: str, *, when: str = ""):
    """Record a boss kill: !killed <boss> [time]  (e.g. !killed Supore 9PM)"""
    boss = resolve_boss(boss_name)
    if boss is None:
        await ctx.send(embed=unknown_boss_embed(boss_name))
        return
    now = local_now()
    if boss in SCHEDULED_BOSSES:
        slots = ", ".join(
            f"{WEEKDAYS[d]} {t}" for d, t in SCHEDULED_BOSSES[boss]
        )
        await ctx.send(
            embed=boss_embed(
                "📋 Boss Timer",
                boss,
                boss_field(boss, now),
                footer=f"Fixed weekly schedule ({slots}) — no kill time needed",
            )
        )
        return

    if when:
        death = parse_death_time(when, now)
        if death is None:
            await ctx.send(
                embed=make_embed(
                    "❓ Couldn't Read That Time",
                    "Use e.g. `9PM`, `21:00` or `2026-07-20 21:00` — "
                    "or leave the time out for right now.",
                )
            )
            return
    else:
        death = now

    data["deaths"][boss] = death.isoformat()
    data["notified"].pop(boss, None)
    await persist()

    hours = INTERVAL_BOSSES[boss]
    spawn = death + timedelta(hours=hours)
    await ctx.send(
        embed=boss_embed(
            "💀 Kill Recorded",
            boss,
            f"💀 Killed {ts(death, 'F')}\n"
            f"⏰ Respawns ({hours}H)\n{ts(spawn, 'F')} — {ts(spawn, 'R')}",
        )
    )


def unknown_boss_embed(boss_name: str) -> discord.Embed:
    return make_embed(
        "❓ Unknown Boss",
        f"`{boss_name}` is not a known boss.",
        footer="Use !bosses for the full list",
    )


@bot.command(name="boss")
async def boss_info(ctx: commands.Context, boss_name: str):
    """Show one boss's timer: !boss <name>"""
    boss = resolve_boss(boss_name)
    if boss is None:
        await ctx.send(embed=unknown_boss_embed(boss_name))
        return
    footer = None
    if boss in SCHEDULED_BOSSES:
        slots = ", ".join(f"{WEEKDAYS[d]} {t}" for d, t in SCHEDULED_BOSSES[boss])
        footer = f"Fixed weekly schedule: {slots}"
    await ctx.send(
        embed=boss_embed(
            "📋 Boss Timer", boss, boss_field(boss, local_now()), footer=footer
        )
    )


def boss_field(boss: str, now: datetime) -> str:
    """Embed field body for one boss: status line + spawn time."""
    if boss in SCHEDULED_BOSSES:
        spawn = next_scheduled_spawn(boss, now)
        return f"⏰ Cooling down (weekly)\n{ts(spawn, 'F')}"

    hours = INTERVAL_BOSSES[boss]
    spawn = interval_spawn(boss)
    if spawn is None:
        return f"❓ No kill recorded\nUse `!killed {boss}`"
    if spawn <= now:
        return f"⚔️ Should be up!\nSpawned {ts(spawn, 'R')}"
    return f"⏰ Cooling down ({hours}H)\n{ts(spawn, 'F')}"


def build_boss_embeds(now: datetime, limit: int | None = None) -> list[discord.Embed]:
    def sort_key(boss: str):
        spawn = next_spawn(boss, now)
        # Unknown/overdue timers sink to the bottom of the list.
        if spawn is None:
            return (2, datetime.max)
        if spawn <= now:
            return (1, spawn)
        return (0, spawn)

    ordered = sorted(ALL_BOSSES.values(), key=sort_key)
    total = len(ordered)
    if limit:
        ordered = ordered[:limit]

    embeds = []
    embed = discord.Embed(title="📋 All Boss Timers", color=EMBED_COLOR)
    for i, boss in enumerate(ordered, 1):
        if len(embed.fields) == 25:  # Discord's per-embed field limit
            embeds.append(embed)
            embed = discord.Embed(color=EMBED_COLOR)
        embed.add_field(
            name=f"{i}. {boss}", value=boss_field(boss, now), inline=True
        )
    if len(ordered) < total:
        embed.set_footer(
            text=f"Showing the next {len(ordered)} of {total} — use !bosses all for everything"
        )
    embeds.append(embed)
    return embeds


@bot.command(name="bosses")
async def bosses(ctx: commands.Context, scope: str = ""):
    """Next 20 spawns: !bosses — or the full list: !bosses all"""
    limit = None if scope.lower() == "all" else 20
    await ctx.send(embeds=build_boss_embeds(local_now(), limit=limit))


@killed.error
@boss_info.error
async def boss_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(
            embed=make_embed(
                "❓ Missing Boss Name",
                "Usage: `!killed <boss> [time]` or `!boss <name>`",
                footer="e.g. !killed Supore 9PM",
            )
        )
    else:
        raise error


@bot.command(name="timer")
async def timer(ctx: commands.Context, seconds: str):
    """Simple live countdown: !timer <seconds>"""
    try:
        remaining = int(seconds)
    except ValueError:
        await ctx.send(
            embed=make_embed(
                "❓ Invalid Timer",
                "Please provide a positive whole number of seconds.",
                footer="e.g. !timer 300",
            )
        )
        return
    if remaining <= 0:
        await ctx.send(
            embed=make_embed(
                "❓ Invalid Timer", "The number of seconds must be greater than zero."
            )
        )
        return
    if remaining > 3600:
        await ctx.send(
            embed=make_embed(
                "❓ Invalid Timer", "Timers are capped at 3600 seconds (1 hour)."
            )
        )
        return

    ends_at = local_now() + timedelta(seconds=remaining)
    msg = await ctx.send(
        embed=make_embed("⏳ Timer", f"Time remaining: {ts(ends_at, 'R')}")
    )
    try:
        # One sleep and one edit, where this used to PATCH the message every
        # second for up to an hour -- 3600 requests, silent on success, and
        # by far the heaviest thing this instance asked of Discord. The <t:R>
        # markup above is retimed by every Discord client on its own, so the
        # visible countdown now costs nothing and the only reason left to
        # touch the message is the ping at the end.
        await asyncio.sleep(remaining)
        await msg.edit(
            embed=make_embed("⏰ Time's Up!", f"Your timer is done, {ctx.author.mention}!")
        )
    except discord.NotFound:
        pass
    except discord.HTTPException as exc:
        print(f"Timer for {ctx.author} aborted: {exc}")


# Must match supervisor.EXIT_NOT_CONFIGURED. The timer's ChildSpec sets
# no_restart_codes=frozenset() so an ordinary bot.run() return is always
# relaunched -- which makes a plain sys.exit(message), status 1, a
# permanent crash-loop when the token is missing: it can never start, so
# the supervisor respawns it forever and buries the log. 78 is the one
# code that policy still honours as "stopped on purpose".
EXIT_NOT_CONFIGURED = 78


if __name__ == "__main__":
    if not TOKEN or TOKEN == "your-bot-token-here":
        print(
            "DISCORD_TOKEN is not set. Put your bot token in the .env file "
            "(DISCORD_TOKEN=...) — see README.md.",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(EXIT_NOT_CONFIGURED)
    discord_login.run(bot, TOKEN)
    sys.exit(_QUIET.exit_code)
