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
  * A tiny web server binds $PORT so an uptime pinger can keep the free
    instance awake.
  * State (channel + kill times) is mirrored into a pinned Discord message,
    so it survives Render wiping the disk on every restart/redeploy.
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

os.environ["TZ"] = os.getenv("BOT_TZ", os.environ.get("TZ", "Asia/Manila"))
time.tzset()

import discord
from aiohttp import web
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

DATA_FILE = Path(__file__).with_name("data.json")
NOTIFY_BEFORE = timedelta(minutes=10)
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
    "Amentos": 29,
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
    "Supore": 62,
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
    return {"channel_id": None, "deaths": {}, "notified": {}}


def save_local() -> None:
    with DATA_FILE.open("w") as f:
        json.dump(data, f, indent=2)


data = load_local()
state_msg: discord.Message | None = None


def prune_notified(now: datetime) -> None:
    """Drop notification markers for spawns that already happened."""
    for boss, iso in list(data["notified"].items()):
        if datetime.fromisoformat(iso) < now:
            del data["notified"][boss]


def encode_state() -> str:
    payload = {
        "channel_id": data["channel_id"],
        "deaths": {b: int(datetime.fromisoformat(v).timestamp())
                   for b, v in data["deaths"].items()},
        "notified": {b: int(datetime.fromisoformat(v).timestamp())
                     for b, v in data["notified"].items()},
    }
    return (
        f"{STATE_MARKER} — bot storage, please don't delete this message.\n"
        f"```json\n{json.dumps(payload)}\n```"
    )


def decode_state(content: str) -> dict | None:
    try:
        raw = content.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
        payload = json.loads(raw)
        return {
            "channel_id": payload["channel_id"],
            "deaths": {b: datetime.fromtimestamp(t).isoformat()
                       for b, t in payload["deaths"].items() if b.lower() in ALL_BOSSES},
            "notified": {b: datetime.fromtimestamp(t).isoformat()
                         for b, t in payload["notified"].items() if b.lower() in ALL_BOSSES},
        }
    except (IndexError, KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None


async def persist() -> None:
    """Save state locally and mirror it into the pinned Discord message."""
    global state_msg
    prune_notified(datetime.now())
    save_local()
    if not data["channel_id"]:
        return
    content = encode_state()
    try:
        if state_msg is not None and state_msg.channel.id != data["channel_id"]:
            # Notification channel moved: retire the old storage message.
            try:
                await state_msg.delete()
            except discord.HTTPException:
                pass
            state_msg = None

        if state_msg is None:
            channel = bot.get_channel(data["channel_id"])
            if channel is None:
                return
            state_msg = await channel.send(content)
            try:
                await state_msg.pin()
            except discord.HTTPException:
                # Pinning needs Manage Messages; the fallback history scan
                # in restore_state() still finds an unpinned storage message.
                print("Could not pin the storage message (missing Manage Messages?).")
        else:
            await state_msg.edit(content=content)
    except discord.HTTPException as exc:
        print(f"Failed to mirror state to Discord: {exc}")


async def restore_state() -> bool:
    """Find the storage message after a restart and reload state from it."""
    global state_msg
    for scan_history in (False, True):
        for guild in bot.guilds:
            for channel in guild.text_channels:
                perms = channel.permissions_for(guild.me)
                if not (perms.view_channel and perms.read_message_history):
                    continue
                try:
                    if scan_history:
                        messages = channel.history(limit=25)
                    else:
                        messages = channel.pins(limit=50)
                    async for msg in messages:
                        if (
                            msg.author.id == bot.user.id
                            and msg.content.startswith(STATE_MARKER)
                        ):
                            decoded = decode_state(msg.content)
                            if decoded:
                                data.update(decoded)
                                state_msg = msg
                                save_local()
                                return True
                except discord.HTTPException:
                    continue
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
    return f"<t:{int(dt.timestamp())}:{style}>"


# ---------------------------------------------------------------------------
# Keep-alive web server so an uptime pinger can stop Render from sleeping
# ---------------------------------------------------------------------------


async def start_keepalive() -> None:
    async def alive(_request):
        return web.Response(text="FB Timer bot is alive")

    app = web.Application()
    app.router.add_get("/", alive)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    try:
        await web.TCPSite(runner, "0.0.0.0", port).start()
        print(f"Keep-alive server listening on port {port}.")
    except OSError as exc:
        print(f"Keep-alive server not started (port {port}): {exc}")


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
_started = False


@bot.event
async def setup_hook():
    await start_keepalive()


@bot.event
async def on_ready():
    global _started
    print(f"Logged in as {bot.user} (ID: {bot.user.id}).")
    if _started:
        return
    _started = True
    restored = await restore_state()
    print(f"State restored from Discord: {restored}")
    spawn_watcher.start()


@tasks.loop(seconds=30)
async def spawn_watcher():
    """Post a heads-up 10 minutes before each known spawn."""
    channel_id = data.get("channel_id")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        return

    now = datetime.now()
    changed = False
    for boss in ALL_BOSSES.values():
        spawn = next_spawn(boss, now)
        if spawn is None or not (timedelta(0) <= spawn - now <= NOTIFY_BEFORE):
            continue
        spawn_iso = spawn.isoformat()
        if data["notified"].get(boss) == spawn_iso:
            continue
        try:
            await channel.send(
                f"🔔 **{boss}** spawns {ts(spawn, 'R')} — at {ts(spawn, 't')}!"
            )
        except discord.HTTPException as exc:
            print(f"Failed to send notification for {boss}: {exc}")
            continue
        data["notified"][boss] = spawn_iso
        changed = True
    if changed:
        await persist()


@bot.command(name="setchannel")
async def setchannel(ctx: commands.Context):
    """Use spawn notifications in this channel: !setchannel"""
    data["channel_id"] = ctx.channel.id
    await persist()
    await ctx.send(
        f"✅ Spawn notifications will be posted in {ctx.channel.mention}.\n"
        "I keep a pinned storage message here so timers survive restarts — "
        "please don't delete it."
    )


@bot.command(name="killed")
async def killed(ctx: commands.Context, boss_name: str, *, when: str = ""):
    """Record a boss kill: !killed <boss> [time]  (e.g. !killed Supore 9PM)"""
    boss = resolve_boss(boss_name)
    if boss is None:
        await ctx.send(f"Unknown boss `{boss_name}`. Check `!bosses` for the list.")
        return
    if boss in SCHEDULED_BOSSES:
        slots = ", ".join(
            f"{WEEKDAYS[d]} {t}" for d, t in SCHEDULED_BOSSES[boss]
        )
        await ctx.send(
            f"**{boss}** is on a fixed schedule ({slots}) — "
            "no death time needed."
        )
        return

    now = datetime.now()
    if when:
        death = parse_death_time(when, now)
        if death is None:
            await ctx.send(
                "Couldn't read that time. Use e.g. `9PM`, `21:00` or "
                "`2026-07-20 21:00` (or leave it out for right now)."
            )
            return
    else:
        death = now

    data["deaths"][boss] = death.isoformat()
    data["notified"].pop(boss, None)
    await persist()

    spawn = death + timedelta(hours=INTERVAL_BOSSES[boss])
    await ctx.send(
        f"💀 **{boss}** killed at {ts(death)}.\n"
        f"Next spawn ({INTERVAL_BOSSES[boss]}h later): {ts(spawn)} — {ts(spawn, 'R')}."
    )


@bot.command(name="boss")
async def boss_info(ctx: commands.Context, boss_name: str):
    """Show one boss's timer: !boss <name>"""
    boss = resolve_boss(boss_name)
    if boss is None:
        await ctx.send(f"Unknown boss `{boss_name}`. Check `!bosses` for the list.")
        return
    await ctx.send(boss_line(boss, datetime.now()))


def boss_line(boss: str, now: datetime) -> str:
    if boss in SCHEDULED_BOSSES:
        slots = ", ".join(f"{WEEKDAYS[d]} {t}" for d, t in SCHEDULED_BOSSES[boss])
        spawn = next_scheduled_spawn(boss, now)
        return f"**{boss}** ({slots}) — next: {ts(spawn)} {ts(spawn, 'R')}"

    hours = INTERVAL_BOSSES[boss]
    spawn = interval_spawn(boss)
    if spawn is None:
        return f"**{boss}** ({hours}h) — no kill recorded, use `!killed {boss}`"
    if spawn <= now:
        return (
            f"**{boss}** ({hours}h) — should have spawned {ts(spawn, 'R')}; "
            f"record the new kill with `!killed {boss}`"
        )
    return f"**{boss}** ({hours}h) — next: {ts(spawn)} {ts(spawn, 'R')}"


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
    embed = discord.Embed(title="📋 All Boss Timers", color=discord.Color.orange())
    for i, boss in enumerate(ordered, 1):
        if len(embed.fields) == 25:  # Discord's per-embed field limit
            embeds.append(embed)
            embed = discord.Embed(color=discord.Color.orange())
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
    await ctx.send(embeds=build_boss_embeds(datetime.now(), limit=limit))


@killed.error
@boss_info.error
async def boss_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(
            "Usage: `!killed <boss> [time]` or `!boss <name>` — e.g. "
            "`!killed Supore 9PM`."
        )
    else:
        raise error


@bot.command(name="timer")
async def timer(ctx: commands.Context, seconds: str):
    """Simple live countdown: !timer <seconds>"""
    try:
        remaining = int(seconds)
    except ValueError:
        await ctx.send("Please provide a positive whole number of seconds.")
        return
    if remaining <= 0:
        await ctx.send("The number of seconds must be greater than zero.")
        return
    if remaining > 3600:
        await ctx.send("Timers are capped at 3600 seconds (1 hour).")
        return

    msg = await ctx.send(f"⏳ Time remaining: **{remaining}s**")
    try:
        while remaining > 0:
            await asyncio.sleep(1)
            remaining -= 1
            if remaining > 0:
                await msg.edit(content=f"⏳ Time remaining: **{remaining}s**")
        await msg.edit(content=f"⏰ Time's up! {ctx.author.mention}")
    except discord.NotFound:
        pass
    except discord.HTTPException as exc:
        print(f"Timer for {ctx.author} aborted: {exc}")


if __name__ == "__main__":
    if not TOKEN or TOKEN == "your-bot-token-here":
        sys.exit(
            "DISCORD_TOKEN is not set. Put your bot token in the .env file "
            "(DISCORD_TOKEN=...) — see README.md."
        )
    bot.run(TOKEN)
