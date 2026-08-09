"""Discord bot for guild item requests and officer distribution.

A third process alongside the timer and the attendance bot, with its own
Discord token and its own spreadsheet, so nothing it does can affect
either of them. See supervisor.py for why all three share one Render
service.

Authorization is the private officer channel itself: !distribute is
accepted only there, and a button attached to a message in that channel
can only be pressed by someone Discord already lets see the channel.
There is no role configuration to drift.
"""

import asyncio
import os
import sys

import discord
from discord.ext import commands
from dotenv import load_dotenv

import items_rules
import items_board
import items_sheet
import items_state

load_dotenv()

EXIT_NOT_CONFIGURED = 78

REQUIRED_ENV = ("ITEMS_DISCORD_TOKEN", "ITEMS_SHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON")

# How long a !distribute panel accepts clicks. The queue outlives the
# panel; expiry only means the officer re-runs the command.
PANEL_TIMEOUT = 900  # 15 minutes

# Serializes every read-then-write pair. Two officers approving at once
# would otherwise both read "2 used today" and both write, yielding 4.
_SHEET_LOCK = asyncio.Lock()

_STATE = items_state.State()

# The pinned messages holding _STATE, cached in shard order so save_state
# edits them instead of posting a new copy on every change.
_STATE_MESSAGES: list[discord.Message] = []

_SPREADSHEET = None

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


def missing_credentials(env: dict) -> list[str]:
    return [name for name in REQUIRED_ENV if not env.get(name)]


def today_pht() -> str:
    """Today's date in Manila, as the ledger writes it."""
    return items_rules.pht_day(items_rules.format_timestamp(items_rules.now_pht()))


def gear_cap() -> int:
    """The daily gear limit, from the environment.

    A malformed value falls back to the default rather than crashing the
    bot: a typo in a Render env var should not take the bot down.
    """
    try:
        return int(os.getenv("ITEMS_GEAR_DAILY_CAP", ""))
    except ValueError:
        return items_rules.DEFAULT_GEAR_DAILY_CAP


async def save_state(channel) -> None:
    """Write _STATE into its pinned message shards."""
    global _STATE_MESSAGES
    try:
        contents = items_state.encode_state(_STATE)
    except ValueError as exc:
        print(f"[items] could not save state: {exc!r}", file=sys.stderr, flush=True)
        await channel.send(
            embed=error_embed(
                "State could not be saved",
                "The request queue is too large to store safely. An officer must "
                "work the queue down before more requests can be accepted.",
            )
        )
        return

    messages: list[discord.Message] = []
    for index, content in enumerate(contents):
        if index < len(_STATE_MESSAGES):
            message = _STATE_MESSAGES[index]
            # A member request holds _SHEET_LOCK; rewriting every unchanged
            # shard here amplifies Discord rate limits and makes that path wait.
            if (
                message.content
                and message.content.startswith(items_state.STATE_MARKER)
                and message.content == content
            ):
                messages.append(message)
                continue
            try:
                await message.edit(content=content)
                messages.append(message)
                continue
            except discord.HTTPException:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass

        message = await channel.send(content)
        try:
            await message.pin()
        except discord.HTTPException:
            # Pinning needs Manage Messages. Without it the message still
            # works -- load_state scans history too -- so this is not fatal.
            pass
        messages.append(message)

    for message in _STATE_MESSAGES[len(contents) :]:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
    _STATE_MESSAGES = messages
    _STATE.missing_parts = ()


async def load_state(channel) -> bool:
    """Restore _STATE from the channel's pinned messages.

    Returns True if a state message was found.

    channel.pins() is an ASYNC ITERATOR in discord.py 2.7, not a
    coroutine returning a list -- `await channel.pins()` raises
    TypeError. It must be consumed with `async for`.
    """
    global _STATE, _STATE_MESSAGES
    candidates = [
        message
        async for message in channel.pins(limit=50)
        if message.author.bot
    ]
    candidates += [
        message
        async for message in channel.history(limit=100)
        if message.author.bot
    ]
    unique = {message.id: message for message in candidates}
    shard_messages = [
        (decoded, message)
        for message in unique.values()
        if (decoded := items_state.decode_state(message.content)) is not None
    ]
    shard_by_part: dict[int, tuple[items_state.Shard, discord.Message]] = {}
    stale_messages: list[discord.Message] = []
    for decoded, message in shard_messages:
        previous = shard_by_part.get(decoded.part)
        if previous is None or message.id > previous[1].id:
            if previous is not None:
                stale_messages.append(previous[1])
            # A replacement has a newer Discord snowflake, so it is the
            # authoritative shard when an old edit-failure orphan remains.
            shard_by_part[decoded.part] = (decoded, message)
        else:
            stale_messages.append(message)
    for message in stale_messages:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
    shard_messages = list(shard_by_part.values())
    part_zero = shard_by_part.get(0)
    if part_zero is not None:
        # Message index 0 is edited on every save and is only deleted as
        # surplus when the state has no shards, so its newest copy defines
        # the current generation.
        authoritative_total = part_zero[0].total
        obsolete_messages = [
            message
            for decoded, message in shard_messages
            if decoded.part >= authoritative_total
        ]
        for message in obsolete_messages:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
        shard_messages = [
            (decoded, message)
            for decoded, message in shard_messages
            if decoded.part < authoritative_total
        ]
    restored = items_state.decode_shards(
        [message.content for _, message in shard_messages]
    )
    if restored is None:
        return False

    _STATE = restored
    _STATE.officer_channel_id = _STATE.officer_channel_id or channel.id
    _STATE_MESSAGES = [
        message for _, message in sorted(shard_messages, key=lambda pair: pair[0].part)
    ]
    if _STATE.missing_parts:
        await channel.send(
            embed=error_embed(
                "State recovery incomplete",
                f"Recovered what could be read, but {len(_STATE.missing_parts)} "
                "state shard(s) are missing. Check this channel's pinned messages.",
            )
        )
    return True


def _embed(title: str, description: str, colour: int) -> discord.Embed:
    return discord.Embed(title=title, description=description, colour=colour)


def ok_embed(title: str, description: str) -> discord.Embed:
    return _embed(f"✅ {title}", description, 0x2ECC71)


def error_embed(title: str, description: str) -> discord.Embed:
    return _embed(f"❌ {title}", description, 0xE74C3C)


def is_officer_channel(channel_id: int) -> bool:
    return (
        _STATE.officer_channel_id is not None
        and channel_id == _STATE.officer_channel_id
    )


async def refresh_board() -> None:
    """Redraw the member-facing queue board when one is configured."""
    if _STATE.queue_channel_id is None:
        return
    try:
        channel = bot.get_channel(_STATE.queue_channel_id)
        if channel is None:
            raise LookupError("configured queue channel is unreachable")
        embed = _embed("📦 Queue Board", items_board.render_board(_STATE.queue), 0x3498DB)
        message = None
        if _STATE.board_message_id is not None:
            try:
                message = await channel.fetch_message(_STATE.board_message_id)
            except discord.NotFound:
                pass
        if message is not None:
            try:
                await message.edit(embed=embed)
                # The board content matters even when pinning is temporarily
                # unavailable, so retry the best-effort pin only after editing.
                if not message.pinned:
                    try:
                        await message.pin()
                    except Exception as exc:
                        print(
                            f"[items] could not pin queue board: {exc!r}",
                            file=sys.stderr,
                            flush=True,
                        )
                return
            except discord.NotFound:
                pass

        message = await channel.send(embed=embed)
        try:
            await message.pin()
        except Exception as exc:
            print(f"[items] could not pin queue board: {exc!r}", file=sys.stderr, flush=True)
        _STATE.board_message_id = message.id
        state_channel = (
            bot.get_channel(_STATE.officer_channel_id)
            if _STATE.officer_channel_id is not None
            else None
        )
        if state_channel is None:
            raise LookupError("configured officer channel is unreachable")
        # Saving happens only after a replacement has a real ID. save_state()
        # never refreshes the board, so this persistence cannot recurse.
        await save_state(state_channel)
    except Exception as exc:
        # The board is cosmetic: it must never turn a completed queue change
        # into a failed request or, worse, a seemingly failed sheet approval.
        print(f"[items] could not refresh queue board: {exc!r}", file=sys.stderr, flush=True)


@bot.command(name="setofficerchannel")
@commands.has_permissions(administrator=True)
async def setofficerchannel_cmd(ctx):
    """Record this channel as the officers' channel."""
    global _STATE_MESSAGES
    if _STATE.officer_channel_id != ctx.channel.id:
        _STATE_MESSAGES = []
    _STATE.officer_channel_id = ctx.channel.id
    await save_state(ctx.channel)
    await ctx.send(
        embed=ok_embed(
            "Officer channel set",
            f"`!distribute` now works in {ctx.channel.mention}, and the bot "
            "keeps its request queue in pinned messages here. A long queue "
            "needs several of them. Don't delete any.",
        )
    )


@bot.command(name="setqueuechannel")
@commands.has_permissions(administrator=True)
async def setqueuechannel_cmd(ctx):
    """Record this channel as the member-facing queue board."""
    previous_channel = (
        bot.get_channel(_STATE.queue_channel_id)
        if _STATE.queue_channel_id is not None
        else None
    )
    if previous_channel is not None and _STATE.board_message_id is not None:
        try:
            message = await previous_channel.fetch_message(_STATE.board_message_id)
            await message.delete()
        except Exception as exc:
            # Clearing the old board is tidiness, not correctness: moving the
            # board must succeed even when the previous one cannot be removed.
            print(f"[items] could not remove old queue board: {exc!r}", file=sys.stderr, flush=True)

    _STATE.queue_channel_id = ctx.channel.id
    _STATE.board_message_id = None
    state_channel = (
        bot.get_channel(_STATE.officer_channel_id)
        if _STATE.officer_channel_id is not None
        else None
    )
    if state_channel is not None:
        # Persist the destination before drawing: a board failure must not undo
        # an admin's configuration. refresh_board saves once more only for a
        # newly created message ID, and save_state never calls it back.
        await save_state(state_channel)
    await refresh_board()
    await ctx.send(
        embed=ok_embed(
            "Queue channel set",
            f"The member queue board is now in {ctx.channel.mention}. The bot "
            "will keep it updated and pinned here.",
        )
    )


from dataclasses import dataclass


@dataclass(frozen=True)
class RequestOutcome:
    accepted: bool
    message: str
    request: items_state.PendingRequest | None = None


def evaluate_request(
    argument: str,
    user_id: int,
    snapshot: items_sheet.Snapshot,
    state: items_state.State,
    *,
    cap: int,
    today: str,
) -> RequestOutcome:
    """Decide a !request without touching Discord or the network.

    Pure: the snapshot already carries the special-log checkbox grid, so
    every question this asks is answered from values passed in. That is
    what makes the whole request path testable without a network.
    """
    try:
        parsed = items_rules.parse_request(
            argument, snapshot.roster, snapshot.special_headers, snapshot.gear_headers
        )
    except (items_rules.RequestParseError, items_rules.ItemLookupError) as exc:
        return RequestOutcome(accepted=False, message=str(exc))

    # A member requesting under a different IGN than last time is NOT
    # refused -- requesting for an alt is legitimate. It is flagged for
    # the officer instead, who is the one with the standing to judge it.
    # Blocking here would punish the honest case to catch a typo that
    # the roster check has already largely prevented.
    note = ""
    remembered = state.igns.get(str(user_id))
    if remembered and items_rules.normalize(remembered) != items_rules.normalize(parsed.ign):
        note = f"previously requested as {remembered}"

    # Keyed on IGN, not on the requesting account: the same item must
    # not sit in the queue twice for one player, whoever asked.
    for queued in state.queue:
        if (
            items_rules.normalize(queued.ign) == items_rules.normalize(parsed.ign)
            and items_rules.normalize(queued.item) == items_rules.normalize(parsed.item.name)
        ):
            return RequestOutcome(
                accepted=False,
                message=f"**{parsed.item.name}** is already pending for **{parsed.ign}**.",
            )

    eligibility = items_rules.check_eligibility(
        parsed.item.type,
        parsed.ign,
        snapshot.ledger_rows,
        today,
        already_has_special=items_sheet.holds_special(
            snapshot, parsed.ign, parsed.item.name
        ),
        pending_gear=items_state.pending_gear_for(state, parsed.ign, today),
        cap=cap,
    )
    if not eligibility.allowed:
        return RequestOutcome(
            accepted=False, message=f"**{parsed.ign}** {eligibility.reason}."
        )

    return RequestOutcome(
        accepted=True,
        message=(
            f"Requested **{parsed.item.name}** ({parsed.item.type}) for "
            f"**{parsed.ign}**. An officer will review it."
        ),
        request=items_state.PendingRequest(
            id=items_state.new_request_id(),
            user_id=user_id,
            ign=parsed.ign,
            item=parsed.item.name,
            type=parsed.item.type,
            requested_at=items_rules.format_timestamp(items_rules.now_pht()),
            note=note,
        ),
    )


@bot.command(name="request")
async def request_cmd(ctx, *, argument: str = ""):
    """Ask an officer for an item."""
    if _STATE.officer_channel_id is None:
        await ctx.send(
            embed=error_embed(
                "Not set up yet",
                "An admin must run `!setofficerchannel` in the officers' "
                "channel before requests can be taken.",
            )
        )
        return

    async with _SHEET_LOCK:
        try:
            snapshot = await asyncio.to_thread(items_sheet.read_snapshot, _SPREADSHEET)
        except Exception as exc:
            await ctx.send(embed=error_embed("Sheet unreachable", str(exc)))
            return

        outcome = evaluate_request(
            argument,
            ctx.author.id,
            snapshot,
            _STATE,
            cap=gear_cap(),
            today=today_pht(),
        )

        if not outcome.accepted:
            await ctx.send(embed=error_embed("Request refused", outcome.message))
            return

        previous_ign = _STATE.igns.get(str(ctx.author.id))
        _STATE.queue.append(outcome.request)
        _STATE.igns[str(ctx.author.id)] = outcome.request.ign

        if not items_state.fits(_STATE):
            _STATE.queue.remove(outcome.request)
            if previous_ign is None:
                _STATE.igns.pop(str(ctx.author.id), None)
            else:
                _STATE.igns[str(ctx.author.id)] = previous_ign
            await ctx.send(
                embed=error_embed(
                    "Queue is full",
                    "The officers need to work the queue down before your request "
                    "can be recorded. Please try again shortly.",
                )
            )
            return

        channel = bot.get_channel(_STATE.officer_channel_id)
        if channel is None:
            _STATE.queue.remove(outcome.request)
            if previous_ign is None:
                _STATE.igns.pop(str(ctx.author.id), None)
            else:
                _STATE.igns[str(ctx.author.id)] = previous_ign
            await ctx.send(
                embed=error_embed(
                    "Officer channel unreachable",
                    "Your request was not recorded. Please try again after an "
                    "admin restores the officer channel.",
                )
            )
            return
        await save_state(channel)
        await refresh_board()

    await ctx.send(embed=ok_embed("Request queued", outcome.message))


# Discord allows at most 25 options in a select menu.
MAX_PANEL_OPTIONS = 25


def panel_lines(
    requests: list[items_state.PendingRequest],
    snapshot: items_sheet.Snapshot,
    cap: int,
    today: str,
    start: int = 1,
) -> list[str]:
    """One display line per pending request, with its current standing.

    The standing is recomputed at render time, not stored: an officer
    needs to see the position as it is now, which may differ from when
    the member requested.
    """
    lines = []
    for number, request in enumerate(requests, start=start):
        if request.type == items_rules.GEAR:
            used = items_rules.gear_used_today(snapshot.ledger_rows, request.ign, today)
            flag = "⚠️" if used >= cap else "✅"
            status = f"{flag} {used}/{cap} today"
        elif items_sheet.holds_special(snapshot, request.ign, request.item):
            status = "⚠️ already has it"
        else:
            status = "✅ eligible"
        line = (
            f"**{number}. {request.ign}** — {request.item}  "
            f"`[{request.type}]`  {status}"
        )
        if request.note:
            line += f"\n     ⚠️ {request.note}"
        lines.append(line)
    return lines


def build_panel_embed(
    requests: list[items_state.PendingRequest],
    snapshot: items_sheet.Snapshot,
    cap: int,
    today: str,
    start: int = 1,
) -> discord.Embed:
    if not requests:
        return _embed("📦 Pending Item Requests", "There are no pending requests.", 0x95A5A6)
    body = "\n".join(panel_lines(requests, snapshot, cap, today, start))
    return _embed("📦 Pending Item Requests", body, 0x3498DB)


async def deny(request_id: str) -> str:
    """Drop a request. Writes nothing to any tab."""
    async with _SHEET_LOCK:
        removed = items_state.remove_request(_STATE, request_id)
        if removed is None:
            return "That request was already handled by another officer."
        channel = bot.get_channel(_STATE.officer_channel_id) if _STATE.officer_channel_id else None
        if channel is not None:
            await save_state(channel)
        await refresh_board()
    return f"Denied **{removed.item}** for **{removed.ign}**. Nothing was written to the sheet."


async def approve(request_id: str, officer_name: str) -> str:
    """Write the item to the sheet and the ledger, then drop the request.

    The whole sequence -- re-read, re-check the cap, write -- happens
    under _SHEET_LOCK. Splitting it would let two officers both read
    "2 used today" and both write.
    """
    async with _SHEET_LOCK:
        request = items_state.find_request(_STATE, request_id)
        if request is None:
            return "That request was already handled by another officer."

        try:
            snapshot = await asyncio.to_thread(items_sheet.read_snapshot, _SPREADSHEET)
        except Exception as exc:
            return f"Could not read the sheet, so nothing was written: {exc}"

        if items_sheet.already_recorded(snapshot, request.id):
            items_state.remove_request(_STATE, request_id)
            channel = bot.get_channel(_STATE.officer_channel_id) if _STATE.officer_channel_id else None
            if channel is not None:
                await save_state(channel)
            await refresh_board()
            return (
                f"**{request.item}** for **{request.ign}** was already recorded. "
                "Nothing was written again."
            )

        eligibility = items_rules.check_eligibility(
            request.type,
            request.ign,
            snapshot.ledger_rows,
            today_pht(),
            already_has_special=items_sheet.holds_special(
                snapshot, request.ign, request.item
            ),
            cap=gear_cap(),
        )
        if not eligibility.allowed:
            return (
                f"Not approved: **{request.ign}** {eligibility.reason}. "
                "The request is still in the queue."
            )

        try:
            await asyncio.to_thread(
                lambda: items_sheet.commit_approval(
                    _SPREADSHEET,
                    ign=request.ign,
                    item=request.item,
                    item_type=request.type,
                    timestamp=items_rules.format_timestamp(items_rules.now_pht()),
                    officer=officer_name,
                    user_id=request.user_id,
                    request_id=request.id,
                )
            )
        except items_sheet.LedgerWriteError as exc:
            # The item cell IS written. Retrying would double-count a
            # gear increment and could never succeed for a special log,
            # so drop the request and hand the officers the exact row.
            items_state.remove_request(_STATE, request_id)
            channel = bot.get_channel(_STATE.officer_channel_id) if _STATE.officer_channel_id else None
            if channel is not None:
                await save_state(channel)
            await refresh_board()
            pasteable = " | ".join(exc.row)
            return (
                f"⚠️ **{request.item}** was given to **{request.ign}** "
                f"(cell {exc.address} is updated), but the Distribution Log "
                f"row could not be written: {exc}\n"
                f"Do NOT approve this again — add this row to "
                f"`{items_sheet.LEDGER_TAB}` by hand:\n```\n{pasteable}\n```"
            )
        except Exception as exc:
            return f"Sheet write failed, request kept in the queue: {exc}"

        items_state.remove_request(_STATE, request_id)
        channel = bot.get_channel(_STATE.officer_channel_id) if _STATE.officer_channel_id else None
        if channel is not None:
            await save_state(channel)
        await refresh_board()

    return f"Approved **{request.item}** for **{request.ign}**."


def page_count(requests: list[items_state.PendingRequest]) -> int:
    return max(1, (len(requests) + MAX_PANEL_OPTIONS - 1) // MAX_PANEL_OPTIONS)


class DistributePanel(discord.ui.View):
    """Select a request, then approve or deny it.

    A select plus two buttons rather than a pair of buttons per request:
    Discord allows five action rows of five components, which would cap
    the panel at five requests. A select handles 25.
    """

    def __init__(
        self,
        requests: list[items_state.PendingRequest],
        snapshot: items_sheet.Snapshot,
        *,
        cap: int,
        today: str,
        page: int = 0,
    ):
        super().__init__(timeout=PANEL_TIMEOUT)
        # A panel is shared by several officers at once; one officer's
        # dropdown choice must never become another officer's approval.
        self.selected: dict[int, str] = {}
        self.requests = list(requests)
        self.snapshot = snapshot
        self.cap = cap
        self.today = today
        self.total_pages = page_count(self.requests)
        self.page = min(max(page, 0), self.total_pages - 1)
        self.start = self.page * MAX_PANEL_OPTIONS + 1
        # Set by the sender so on_timeout can edit the panel.
        self.message: discord.Message | None = None

        page_requests = self.requests[
            self.page * MAX_PANEL_OPTIONS : (self.page + 1) * MAX_PANEL_OPTIONS
        ]
        options = [
            discord.SelectOption(
                label=f"{n}. {r.ign} — {r.item}"[:100],
                value=r.id,
                description=f"{r.type} · requested {r.requested_at}"[:100],
            )
            for n, r in enumerate(page_requests, start=self.start)
        ]
        self.picker = discord.ui.Select(
            placeholder="Choose a request…", options=options, row=0
        )
        self.picker.callback = self._on_pick
        self.add_item(self.picker)
        self._add_page_controls()

    def build_embed(self) -> discord.Embed:
        requests = self.requests[
            self.page * MAX_PANEL_OPTIONS : (self.page + 1) * MAX_PANEL_OPTIONS
        ]
        embed = build_panel_embed(
            requests, self.snapshot, self.cap, self.today, start=self.start
        )
        if self.total_pages > 1:
            embed.set_footer(text=f"Page {self.page + 1} of {self.total_pages}")
        return embed

    def _add_page_controls(self) -> None:
        if self.total_pages == 1:
            return

        if self.total_pages > 5:
            self._add_page_button("◀", self.page - 1, disabled=self.page == 0)
            if self.page == 0:
                numbers = range(0, 3)
            elif self.page == self.total_pages - 1:
                numbers = range(self.total_pages - 3, self.total_pages)
            else:
                numbers = range(self.page - 1, self.page + 2)
            for page in numbers:
                self._add_page_button(str(page + 1), page, disabled=page == self.page)
            self._add_page_button(
                "▶", self.page + 1, disabled=self.page == self.total_pages - 1
            )
            return

        for page in range(self.total_pages):
            self._add_page_button(str(page + 1), page, disabled=page == self.page)

    def _add_page_button(self, label: str, page: int, *, disabled: bool) -> None:
        button = discord.ui.Button(
            label=label,
            style=(
                discord.ButtonStyle.primary
                if page == self.page and label not in {"◀", "▶"}
                else discord.ButtonStyle.secondary
            ),
            disabled=disabled,
            row=2,
        )

        async def change_page(interaction: discord.Interaction):
            self.selected.clear()
            next_panel = DistributePanel(
                self.requests,
                self.snapshot,
                cap=self.cap,
                today=self.today,
                page=page,
            )
            next_panel.message = interaction.message
            await interaction.response.edit_message(
                embed=next_panel.build_embed(), view=next_panel
            )

        button.callback = change_page
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """The private channel is the authorization gate.

        Discord already stops anyone who cannot see the channel from
        clicking. This re-checks against the CURRENTLY recorded officer
        channel -- not the one captured when the panel was built -- so
        that moving the officer channel with !setofficerchannel
        immediately makes panels left behind in the old channel inert.
        """
        if not is_officer_channel(interaction.channel_id):
            await interaction.response.send_message(
                "This panel only works in the current officers' channel.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        """Say the panel expired instead of failing silently.

        Once the timeout elapses discord.py stops dispatching component
        interactions, so without this an officer clicking an old panel
        gets Discord's generic "interaction failed" and no explanation.
        The queue itself is untouched -- only this view is dead.
        """
        for child in self.children:
            child.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(
                content="⏳ This panel expired. Run `!distribute` for a fresh one.",
                view=self,
            )
        except discord.HTTPException:
            pass

    async def _on_pick(self, interaction: discord.Interaction):
        self.selected[interaction.user.id] = self.picker.values[0]
        await interaction.response.defer()

    async def _require_selection(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id not in self.selected:
            await interaction.response.send_message(
                "Pick a request from the dropdown first.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label="Approve", style=discord.ButtonStyle.success, emoji="✅", row=1
    )
    async def approve_button(self, interaction: discord.Interaction, _button):
        if not await self._require_selection(interaction):
            return
        await interaction.response.defer()
        result = await approve(self.selected[interaction.user.id], interaction.user.display_name)
        self.selected.pop(interaction.user.id, None)
        await interaction.followup.send(result)
        await refresh_panel(interaction, self.page)

    @discord.ui.button(
        label="Deny", style=discord.ButtonStyle.danger, emoji="❌", row=1
    )
    async def deny_button(self, interaction: discord.Interaction, _button):
        if not await self._require_selection(interaction):
            return
        await interaction.response.defer()
        result = await deny(self.selected[interaction.user.id])
        self.selected.pop(interaction.user.id, None)
        await interaction.followup.send(result)
        await refresh_panel(interaction, self.page)


async def refresh_panel(interaction: discord.Interaction, page: int) -> None:
    """Redraw one page of the queue in place after a request resolves.

    `page` is the page this panel is showing, so page 2 stays page 2
    rather than being replaced by page 1's contents. Other pages go
    stale, which is harmless: approve() and deny() both re-check the
    queue, so a click on a stale entry reports that it was already
    handled rather than acting twice.
    """
    try:
        snapshot = await asyncio.to_thread(items_sheet.read_snapshot, _SPREADSHEET)
    except Exception:
        return
    requests = list(_STATE.queue)
    page = min(page, page_count(requests) - 1)
    view = (
        DistributePanel(
            requests, snapshot, cap=gear_cap(), today=today_pht(), page=page
        )
        if requests
        else None
    )
    if view is None:
        embed = build_panel_embed([], snapshot, gear_cap(), today_pht())
    else:
        embed = view.build_embed()
    await interaction.message.edit(embed=embed, view=view)
    if view is not None:
        view.message = interaction.message


@bot.command(name="distribute")
async def distribute_cmd(ctx):
    """Show the pending requests with approve/deny controls."""
    if not is_officer_channel(ctx.channel.id):
        return  # silently ignored outside the officers' channel

    try:
        snapshot = await asyncio.to_thread(items_sheet.read_snapshot, _SPREADSHEET)
    except Exception as exc:
        await ctx.send(embed=error_embed("Sheet unreachable", str(exc)))
        return

    requests = list(_STATE.queue)
    view = (
        DistributePanel(requests, snapshot, cap=gear_cap(), today=today_pht())
        if requests
        else None
    )
    embed = (
        view.build_embed()
        if view is not None
        else build_panel_embed([], snapshot, gear_cap(), today_pht())
    )
    message = await ctx.send(embed=embed, view=view)
    if view is not None:
        view.message = message


def requests_for_user(state: items_state.State, user_id: int) -> list[items_state.PendingRequest]:
    return [r for r in state.queue if r.user_id == user_id]


def cancellable(
    state: items_state.State, user_id: int, item_query: str
) -> tuple[items_state.PendingRequest | None, str | None]:
    """Which of this member's pending requests !cancelrequest means.

    Returns (request, error_message); exactly one is None.
    """
    mine = requests_for_user(state, user_id)
    if not mine:
        return None, "You have no pending requests."

    query = item_query.strip()
    if not query:
        if len(mine) == 1:
            return mine[0], None
        names = ", ".join(f"`{r.item}`" for r in mine)
        return None, f"You have several pending: {names}. Say which: `!cancelrequest <item name>`"

    wanted = items_rules.normalize(query)
    for request in mine:
        if items_rules.normalize(request.item) == wanted:
            return request, None
    return None, f"You have no pending request for {query!r}."


@bot.command(name="cancelrequest")
async def cancelrequest_cmd(ctx, *, item_query: str = ""):
    """Withdraw your own pending request."""
    async with _SHEET_LOCK:
        request, error = cancellable(_STATE, ctx.author.id, item_query)
        if error is not None:
            await ctx.send(embed=error_embed("Nothing cancelled", error))
            return
        items_state.remove_request(_STATE, request.id)
        channel = bot.get_channel(_STATE.officer_channel_id) if _STATE.officer_channel_id else None
        if channel is not None:
            await save_state(channel)
        await refresh_board()
    await ctx.send(
        embed=ok_embed("Request cancelled", f"Withdrew **{request.item}** for **{request.ign}**.")
    )


@bot.command(name="myrequests")
async def myrequests_cmd(ctx):
    """List your pending requests."""
    mine = requests_for_user(_STATE, ctx.author.id)
    if not mine:
        await ctx.send(embed=ok_embed("Nothing pending", "You have no pending requests."))
        return
    body = "\n".join(f"• **{r.item}** for **{r.ign}** — requested {r.requested_at}" for r in mine)
    await ctx.send(embed=_embed("📋 Your Pending Requests", body, 0x3498DB))


@bot.command(name="itemhelp")
async def itemhelp_cmd(ctx):
    """Explain the commands and the rules."""
    await ctx.send(
        embed=_embed(
            "📦 Item Requests",
            "**`!request <item name> <IGN>`** — ask for an item. "
            "Example: `!request Asta's Heart Kobe`\n"
            "**`!myrequests`** — see what you have pending\n"
            "**`!cancelrequest [item name]`** — withdraw a request\n\n"
            "**Rules**\n"
            "• Special logs: one per player, ever.\n"
            f"• Gear logs: {gear_cap()} per player per day, resetting at "
            "midnight (Manila time).\n\n"
            "Your IGN must match your row in the Logs Tracker sheet.",
            0x3498DB,
        )
    )


@bot.event
async def on_ready():
    print(f"[items] logged in as {bot.user}", flush=True)
    if _STATE.officer_channel_id is None:
        # Nothing to restore from until an admin has named the channel.
        # Scan every readable text channel's pins once, so a redeploy
        # recovers without anyone re-running !setofficerchannel.
        for guild in bot.guilds:
            for channel in guild.text_channels:
                try:
                    if await load_state(channel):
                        print(f"[items] restored state from #{channel.name}", flush=True)
                        return
                except discord.HTTPException:
                    continue
        print("[items] no state found; run !setofficerchannel", flush=True)
        return

    channel = bot.get_channel(_STATE.officer_channel_id)
    if channel is not None:
        await load_state(channel)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(embed=error_embed("Not allowed", "That command is for administrators."))
        return
    print(f"[items] command error: {error!r}", flush=True)
    await ctx.send(embed=error_embed("Something went wrong", str(error)))


def main() -> None:
    global _SPREADSHEET
    missing = missing_credentials(os.environ)
    if missing:
        print(
            f"[items] not configured, missing: {', '.join(missing)}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(EXIT_NOT_CONFIGURED)

    _SPREADSHEET = items_sheet.open_logs_tracker(
        os.environ["ITEMS_SHEET_ID"], os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    )
    bot.run(os.environ["ITEMS_DISCORD_TOKEN"])


if __name__ == "__main__":
    main()
