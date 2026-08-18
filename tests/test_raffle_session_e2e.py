"""A full raffle sitting, driven end to end without a live server.

The unit tests in test_items_bot.py monkeypatch `commit_approval` and
`poll_voters` away, so the code that actually ticks a checkbox and the
code that turns a Discord voter into a roster row never run there. This
module exists to run them.

Only the two TRANSPORTS are faked: the Discord HTTP layer, and gspread
(via the shared fakes in conftest, which reproduce the Sheets API's
habit of omitting trailing empty cells). Everything between them is the
shipped code -- build_poll, poll_voters including the guild-nickname
lookup and tag stripping, poll_is_open, the pool freeze, render_pool,
won_cmd, and items_sheet.commit_approval writing into a real grid. The
restart is a real encode_state/decode_shards round trip.

What this canNOT prove, and no local test can:
  * that Discord's live Poll object behaves as ClosedPoll does,
  * that Sheets really renders a ticked checkbox as "TRUE" when read
    back (items_sheet.CHECKED_VALUES depends on it -- though that path
    is unchanged code already running in production),
  * that the Server Members Intent is enabled on the live application.

The nickname lookup is load-bearing here rather than decorative: every
voter is supplied as a User whose global name ("jjew_global") appears
nowhere in the roster, so the pool can only come out right if the guild
fetch and the tag stripping both really ran.
"""

import asyncio

import gspread
import pytest
from conftest import FakeSpreadsheet, FakeWorksheet

import items_bot
import items_rules
import items_sheet
import items_state
from test_items_bot import FakeChannel, FakeCtx, FakeMember, FakeRole, _register_channel


ROSTER = ["Jjew", "Kobe", "chinchong ni Mumu", "wile-KAMOTE"]
SPECIALS = ["Asta's Heart", "Amentis Foot"]


class WritingWorksheet(FakeWorksheet):
    """conftest's worksheet, but batch_update actually lands in the grid.

    The shared fake only records the batches, which is all its callers
    assert on. Here the write has to be readable afterwards, because the
    point is to prove the checkbox really ends up ticked.
    """

    def batch_update(self, data):
        super().batch_update(data)
        for update in data:
            row, column = gspread.utils.a1_to_rowcol(update["range"])
            while len(self._rows) < row:
                self._rows.append([])
            line = self._rows[row - 1]
            while len(line) < column:
                line.append("")
            value = update["values"][0][0]
            # Sheets reads a ticked checkbox back as TRUE, which is what
            # items_sheet.CHECKED_VALUES matches on.
            line[column - 1] = "TRUE" if value is True else str(value)


def build_spreadsheet(already_holds=()):
    """A Special Logs tab shaped like the real one, plus the ledger."""
    rows = [["Player Name", *SPECIALS]]
    for player in ROSTER:
        rows.append(
            [
                player,
                *[
                    "TRUE" if (player, item) in already_holds else "FALSE"
                    for item in SPECIALS
                ],
            ]
        )
    return FakeSpreadsheet(
        {
            items_sheet.SPECIAL_TAB: WritingWorksheet(
                rows, title=items_sheet.SPECIAL_TAB
            ),
            items_sheet.LEDGER_TAB: WritingWorksheet(
                [list(items_sheet.LEDGER_HEADER)], title=items_sheet.LEDGER_TAB
            ),
        }
    )


class FakeUser:
    """A poll voter as Discord returns one when the guild is not cached.

    Carries only the GLOBAL name, so resolving it to a roster row has to
    go through guild.fetch_member -- the production path.
    """

    def __init__(self, user_id, global_name):
        self.id = user_id
        self.display_name = global_name


class FakeGuildMember:
    def __init__(self, user_id, nickname):
        self.id = user_id
        self.display_name = nickname


class FakeGuild:
    def __init__(self, nicknames):
        self._nicknames = nicknames

    async def fetch_member(self, user_id):
        nickname = self._nicknames.get(user_id)
        if nickname is None:
            raise LookupError("not a member of this guild")
        return FakeGuildMember(user_id, nickname)


class VotedAnswer:
    def __init__(self, text, voters):
        self.text = text
        self._voters = list(voters)

    def voters(self, **kwargs):
        async def _iterator():
            for voter in self._voters:
                yield voter

        return _iterator()


class ClosedPoll:
    """A poll Discord has finalised, with no expires_at (older payload)."""

    def __init__(self, answers, finalised=True):
        self.answers = answers
        self._finalised = finalised

    def is_finalised(self):
        return self._finalised


def cast_votes(message, voters, finalised=True):
    """Replace the posted poll with one carrying votes, as Discord would.

    The answer text comes from items_bot.POLL_ANSWER rather than a
    literal, so poll_voters' find-the-answer-by-text step is genuinely
    exercised instead of mirrored.
    """
    message.poll = ClosedPoll(
        [VotedAnswer(items_bot.POLL_ANSWER, voters)], finalised=finalised
    )


def close_raffle(item, ends_at="2020-01-01 00:00:00"):
    """Move a raffle's stored end time into the past."""
    raffle = items_state.find_raffle(items_bot._STATE, item)
    items_state.replace_raffle(items_bot._STATE, raffle, ends_at=ends_at)


def posted_polls(channel):
    return [m for m in channel.sent if getattr(m, "poll", None) is not None]


def last_embed(ctx):
    return ctx.sent[-1]["embed"]


def numbered_lines(description):
    """The "1. Name" entries -- the players actually drawable."""
    return [line for line in description.splitlines() if line[:1].isdigit()]


@pytest.fixture(autouse=True)
def reset_bot():
    items_bot._STATE = items_state.State()
    items_bot._STATE_MESSAGES = []
    yield
    items_bot._STATE = items_state.State()
    items_bot._STATE_MESSAGES = []


@pytest.fixture
def world(monkeypatch):
    """A configured bot, a raffle channel, and a live spreadsheet."""
    spreadsheet = build_spreadsheet(already_holds={("wile-KAMOTE", "Asta's Heart")})
    monkeypatch.setattr(items_bot, "_SPREADSHEET", spreadsheet)

    officer_channel = _register_channel(FakeChannel(1))
    raffle_channel = _register_channel(FakeChannel(42))
    channels = {1: officer_channel, 42: raffle_channel}
    monkeypatch.setattr(
        items_bot.bot, "get_channel", lambda cid: channels.get(cid, officer_channel)
    )

    items_bot._STATE.officer_channel_id = 1
    items_bot._STATE.raffle_channel_id = 42
    items_bot._STATE.raffle_role_ids = [10]

    ctx = FakeCtx(raffle_channel)
    ctx.author = FakeMember(user_id=99, roles=[FakeRole(10)], display_name="Officer")

    guild = FakeGuild(
        {
            1: "BK | Jjew",
            2: "M2 - Kobe",
            3: "chinchong ni Mumu",
            4: "wile-KAMOTE",
        }
    )
    raffle_channel.guild = guild
    return ctx, raffle_channel, spreadsheet, guild


def all_four_voters():
    return [
        FakeUser(1, "jjew_global"),
        FakeUser(2, "kobe_global"),
        FakeUser(3, "chinchong_global"),
        FakeUser(4, "kamote_global"),
    ]


def open_two_polls(ctx, channel, guild):
    """!poll both logs, cast votes on both, and close them."""
    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart --hours 1"))
    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Amentis Foot --hours 1"))

    polls = posted_polls(channel)
    assert len(polls) == 2, f"both polls should have been posted: {len(polls)}"
    for message in polls:
        message.guild = guild
        cast_votes(message, all_four_voters())

    close_raffle("Asta's Heart")
    close_raffle("Amentis Foot")
    return polls


def test_poll_posts_a_real_discord_poll_with_the_expected_answer(world):
    ctx, channel, _, _ = world

    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart --hours 1"))

    polls = posted_polls(channel)
    assert len(polls) == 1
    # build_poll's real output: one answer, carrying the text
    # poll_voters later searches for.
    assert [answer.text for answer in polls[0].poll.answers] == [items_bot.POLL_ANSWER]
    raffle = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert raffle is not None and raffle.listed is False


def test_a_sitting_runs_end_to_end_and_nobody_wins_twice(world):
    ctx, channel, spreadsheet, guild = world
    open_two_polls(ctx, channel, guild)

    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    session = items_bot._STATE.raffle_session
    assert session is not None
    assert session.items == ("Asta's Heart", "Amentis Foot"), (
        "both closed polls, in the order they were opened"
    )

    pool_one = last_embed(ctx)
    assert "Asta's Heart" in pool_one.title
    for ign in ("Jjew", "Kobe", "chinchong ni Mumu"):
        assert ign in pool_one.description, f"{ign} missing from the pool"
    # wile-KAMOTE's checkbox for this log is already ticked.
    assert "Already has it" in pool_one.description
    frozen = items_state.find_raffle(items_bot._STATE, "Asta's Heart")
    assert frozen.listed is True
    assert "wile-KAMOTE" not in frozen.eligible

    # The officer draws Kobe by hand and records it.
    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Kobe"))

    special = spreadsheet.worksheet(items_sheet.SPECIAL_TAB).get_all_values()
    kobe_row = next(row for row in special if row[0] == "Kobe")
    assert kobe_row[1 + SPECIALS.index("Asta's Heart")] == "TRUE"
    ledger = spreadsheet.worksheet(items_sheet.LEDGER_TAB).get_all_values()
    assert len(ledger) == 2, "one header row plus one distribution row"
    assert ledger[1][1] == "Kobe" and ledger[1][2] == "Asta's Heart"
    assert ledger[1][3] == items_rules.SPECIAL

    # Poll 2 posts by itself, and Kobe is not drawable in it.
    pool_two = last_embed(ctx)
    assert "Amentis Foot" in pool_two.title
    assert "Won earlier this session" in pool_two.description
    drawable = numbered_lines(pool_two.description)
    assert not any("Kobe" in line for line in drawable), (
        f"Kobe must not be drawable again: {drawable}"
    )
    assert any("Jjew" in line for line in drawable)

    # And he is refused if the officer types him anyway.
    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Kobe"))

    assert "already won earlier in this session" in last_embed(ctx).description
    ledger = spreadsheet.worksheet(items_sheet.LEDGER_TAB).get_all_values()
    assert len(ledger) == 2, "the refusal must not have written anything"

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Jjew"))

    assert items_bot._STATE.raffle_session is None, "the sitting is over"
    summary = last_embed(ctx)
    assert "Asta's Heart" in summary.description and "Kobe" in summary.description
    assert "Amentis Foot" in summary.description and "Jjew" in summary.description
    assert len(spreadsheet.worksheet(items_sheet.LEDGER_TAB).get_all_values()) == 3


def test_the_sitting_survives_a_restart_between_two_polls(world):
    """Render's free tier restarts. The winner list must come back."""
    ctx, channel, spreadsheet, guild = world
    open_two_polls(ctx, channel, guild)

    asyncio.run(items_bot.startraffle_cmd.callback(ctx))
    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Kobe"))

    shards = items_state.encode_state(items_bot._STATE)
    assert len(shards) <= items_state.MAX_SHARDS
    restored = items_state.decode_shards(shards)
    assert restored is not None
    items_bot._STATE = restored

    session = items_bot._STATE.raffle_session
    assert session is not None, "the sitting must come back from the pin"
    assert session.current_item == "Amentis Foot"
    assert session.winners == ("Kobe",)

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Kobe"))
    assert "already won earlier in this session" in last_embed(ctx).description

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Jjew"))
    assert items_bot._STATE.raffle_session is None
    ledger = spreadsheet.worksheet(items_sheet.LEDGER_TAB).get_all_values()
    assert [row[1] for row in ledger[1:]] == ["Kobe", "Jjew"]


def test_an_unknown_voter_holds_the_sitting_until_an_officer_names_them(world):
    ctx, channel, spreadsheet, guild = world
    asyncio.run(items_bot.poll_cmd.callback(ctx, argument="Asta's Heart --hours 1"))
    message = posted_polls(channel)[0]
    message.guild = guild
    # User 7 is in no roster row and the guild knows no nickname for them.
    cast_votes(message, [FakeUser(1, "jjew_global"), FakeUser(7, "xXshadowXx")])
    close_raffle("Asta's Heart")

    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    held = last_embed(ctx)
    assert held.title == "❌ Pool not frozen"
    assert "<@7>" in held.description
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").listed is False
    assert items_bot._STATE.raffle_session is not None, "the sitting waits"

    # The officer names them; the held poll must re-post by itself.
    stranger = FakeMember(user_id=7, display_name="xXshadowXx")
    asyncio.run(
        items_bot.bind_cmd.callback(ctx, stranger, argument="chinchong ni Mumu")
    )

    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").listed is True
    reposted = last_embed(ctx)
    assert "Asta's Heart" in reposted.title
    assert "chinchong ni Mumu" in reposted.description

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="chinchong ni Mumu"))
    ledger = spreadsheet.worksheet(items_sheet.LEDGER_TAB).get_all_values()
    assert ledger[1][1] == "chinchong ni Mumu"


def test_a_skipped_log_is_offered_again_by_the_next_sitting(world):
    ctx, channel, spreadsheet, guild = world
    open_two_polls(ctx, channel, guild)

    asyncio.run(items_bot.startraffle_cmd.callback(ctx))
    asyncio.run(items_bot.skipraffle_cmd.callback(ctx))
    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Kobe"))

    assert items_bot._STATE.raffle_session is None
    assert "skipped" in last_embed(ctx).description.lower()
    assert items_state.find_raffle(items_bot._STATE, "Asta's Heart").drawn is False

    # A second sitting picks the skipped log back up, and Kobe -- who won
    # in the PREVIOUS sitting -- is eligible again: the one-win rule is
    # per sitting, not forever.
    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    assert items_bot._STATE.raffle_session.items == ("Asta's Heart",)
    assert "Kobe" in last_embed(ctx).description


def test_a_sheet_failure_part_way_leaves_the_poll_current(world, monkeypatch):
    """The officer must be able to finish the draw, not lose it."""
    ctx, channel, spreadsheet, guild = world
    open_two_polls(ctx, channel, guild)
    asyncio.run(items_bot.startraffle_cmd.callback(ctx))

    real_record = items_sheet.record_special
    calls = {"n": 0}

    def flaky(sheet, ign, item):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("Sheets is down")
        return real_record(sheet, ign, item)

    monkeypatch.setattr(items_sheet, "record_special", flaky)

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Jjew - Kobe"))

    session = items_bot._STATE.raffle_session
    assert session.position == 0, "the poll stays current after a failure"
    assert session.results == ()
    failed = last_embed(ctx)
    assert "Kobe" in failed.description and "!won" in failed.description

    # The retry completes the draw, and BOTH names count against the
    # sitting -- the one written before the failure must not be forgotten.
    monkeypatch.setattr(items_sheet, "record_special", real_record)
    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Kobe"))

    session = items_bot._STATE.raffle_session
    assert session.results == (("Asta's Heart", ("Jjew", "Kobe")),)
    assert set(session.winners) == {"Jjew", "Kobe"}
    drawable = numbered_lines(last_embed(ctx).description)
    assert not any("Jjew" in line or "Kobe" in line for line in drawable), (
        f"neither winner may be drawable in poll 2: {drawable}"
    )


def test_a_winner_is_still_excluded_after_the_first_raffle_sheds_its_pool(world):
    """The exclusion must not depend on the entry list we now discard.

    A sitting bars a previous winner using RaffleSession.results, not the
    earlier raffle's pool -- so dropping the pool from a drawn raffle
    cannot let them back in. Asserted end to end, through a real
    encode/decode round trip, because that is where the pool actually
    disappears.
    """
    ctx, channel, spreadsheet, guild = world
    open_two_polls(ctx, channel, guild)

    asyncio.run(items_bot.startraffle_cmd.callback(ctx))
    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Jjew"))

    # The first raffle is drawn, so the pin no longer carries its pool.
    restored = items_state.decode_shards(
        items_state.encode_state(items_bot._STATE)
    )
    first = items_state.find_raffle(restored, "Asta's Heart")
    assert first.drawn is True
    assert first.eligible == (), "a drawn raffle should shed its pool"
    items_bot._STATE = restored

    # The sitting moved to the second log; Jjew must not be drawable again.
    session = items_bot._STATE.raffle_session
    assert session.current_item == "Amentis Foot"
    assert session.winners == ("Jjew",)

    asyncio.run(items_bot.won_cmd.callback(ctx, argument="Jjew"))
    assert "already won earlier in this session" in last_embed(ctx).description
