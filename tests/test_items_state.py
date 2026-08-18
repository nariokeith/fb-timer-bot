"""Tests for the pinned-message state format and queue operations."""

import dataclasses

import items_state


def _request(request_id="aaa", ign="Kobe", item="Asta's Heart", type_="Special"):
    return items_state.PendingRequest(
        id=request_id,
        user_id=42,
        ign=ign,
        item=item,
        type=type_,
        requested_at="2026-08-07 09:00:00",
    )


def test_encode_decode_round_trip():
    state = items_state.State(
        officer_channel_id=99, queue=[_request()], igns={"42": "Kobe"}
    )
    contents = items_state.encode_state(state)
    assert len(contents) == 1

    restored = items_state.decode_shards(contents)
    assert restored.officer_channel_id == 99
    assert restored.igns == {"42": "Kobe"}
    assert restored.queue[0].ign == "Kobe"
    assert restored.queue[0].id == "aaa"


def test_queue_board_ids_round_trip():
    state = items_state.State(queue_channel_id=77, board_message_id=88)

    restored = items_state.decode_shards(items_state.encode_state(state))

    assert restored.queue_channel_id == 77
    assert restored.board_message_id == 88


def test_encoded_content_carries_the_marker():
    contents = items_state.encode_state(items_state.State())
    assert contents[0].startswith(items_state.STATE_MARKER)


def test_decoding_an_unrelated_message_returns_none():
    assert items_state.decode_state("just a normal chat message") is None


def test_decoding_a_corrupt_payload_returns_none():
    assert items_state.decode_state(f"{items_state.STATE_MARKER}\n```json\n{{oops\n```") is None


def test_decoding_a_non_numeric_channel_id_returns_none():
    content = f'{items_state.STATE_MARKER}\n```json\n{{"officer_channel_id":"bad"}}\n```'
    assert items_state.decode_state(content) is None


def test_a_fresh_state_has_no_officer_channel():
    assert items_state.State().officer_channel_id is None


def _remembered_igns(count):
    return {str(10**17 + n): "PlayerName%02d" % n for n in range(count)}


def test_200_remembered_igns_and_queue_round_trip_across_shards():
    state = items_state.State(
        officer_channel_id=10**17,
        igns=_remembered_igns(200),
        queue=[_request(request_id=f"id{n:03d}", ign=f"Member {n}") for n in range(5)],
    )

    contents = items_state.encode_state(state)
    restored = items_state.decode_shards(contents)

    assert restored.igns == state.igns
    assert restored.queue == state.queue
    assert all(len(content) <= items_state.MAX_CONTENT for content in contents)


def test_fits_when_igns_span_multiple_shards_within_the_limit():
    state = items_state.State(igns=_remembered_igns(200))

    assert len(items_state.encode_state(state)) > 1
    assert len(items_state.encode_state(state)) <= items_state.MAX_SHARDS
    assert items_state.fits(state)


def test_empty_queue_with_a_huge_ign_map_still_fits():
    state = items_state.State(igns=_remembered_igns(500))

    contents = items_state.encode_state(state)

    assert items_state.fits(state)
    assert all(len(content) <= items_state.MAX_CONTENT for content in contents)


def test_a_single_request_too_large_for_one_shard_still_raises_value_error():
    state = items_state.State(queue=[_request(item="X" * items_state.MAX_CONTENT)])

    try:
        items_state.encode_state(state)
    except ValueError as error:
        assert "pending request" in str(error)
    else:
        raise AssertionError("an unencodable request must be reported")


def test_large_queue_and_ign_map_round_trip_without_reordering_requests():
    state = items_state.State(
        officer_channel_id=99,
        queue=[_request(request_id=f"id{n:03d}", ign=f"Member {n}") for n in range(50)],
        igns={str(n): f"Member {n}" for n in range(50)},
    )

    restored = items_state.decode_shards(items_state.encode_state(state))

    assert restored.officer_channel_id == 99
    assert restored.igns == state.igns
    assert [request.id for request in restored.queue] == [request.id for request in state.queue]


def test_each_encoded_shard_respects_discords_content_limit():
    state = items_state.State(
        officer_channel_id=99,
        queue=[_request(request_id=f"id{n:03d}", item="A Very Long Item Name Indeed") for n in range(50)],
        igns={str(n): f"Member {n}" for n in range(50)},
    )

    assert all(len(content) <= items_state.MAX_CONTENT for content in items_state.encode_state(state))


def test_old_single_message_format_decodes_as_one_shard():
    content = """ITEMS_STATE_V1 -- bot storage, please don't delete this message.
```json
{"officer_channel_id":99,"queue":[{"id":"old1","user_id":42,"ign":"Kobe","item":"Asta's Heart","type":"Special","requested_at":"2026-08-07 09:00:00","note":""}],"igns":{"42":"Kobe"}}
```"""

    shard = items_state.decode_state(content)

    assert shard.part == 0
    assert shard.total == 1
    assert shard.state.officer_channel_id == 99
    assert shard.state.queue_channel_id is None
    assert shard.state.board_message_id is None
    assert [request.id for request in shard.state.queue] == ["old1"]


def test_decode_shards_reassembles_out_of_order_queue_slices():
    state = items_state.State(
        queue=[_request(request_id=f"id{n:03d}") for n in range(50)],
        igns={str(n): f"Member {n}" for n in range(50)},
    )
    contents = items_state.encode_state(state)
    assert len(contents) > 1

    restored = items_state.decode_shards(list(reversed(contents)))

    assert [request.id for request in restored.queue] == [request.id for request in state.queue]


def test_queue_board_ids_survive_multi_shard_encode():
    state = items_state.State(
        queue_channel_id=77,
        board_message_id=88,
        queue=[_request(request_id=f"id{n:03d}") for n in range(50)],
        igns={str(n): f"Member {n}" for n in range(50)},
    )

    contents = items_state.encode_state(state)
    restored = items_state.decode_shards(contents)
    shards = [items_state.decode_state(content) for content in contents]

    assert len(contents) > 1
    assert shards[0].state.queue_channel_id == 77
    assert shards[0].state.board_message_id == 88
    assert all(
        shard.state.queue_channel_id is None and shard.state.board_message_id is None
        for shard in shards[1:]
    )
    assert restored.queue_channel_id == 77
    assert restored.board_message_id == 88


def test_decode_shards_reports_missing_parts_while_restoring_available_requests():
    state = items_state.State(
        queue=[_request(request_id=f"id{n:03d}") for n in range(50)],
        igns={str(n): f"Member {n}" for n in range(50)},
    )
    contents = items_state.encode_state(state)
    assert len(contents) > 2

    restored = items_state.decode_shards(contents[:1] + contents[2:])

    assert restored is not None
    assert restored.missing_parts == (1,)
    assert [request.id for request in restored.queue] != [request.id for request in state.queue]


def test_fits_is_true_at_the_shard_limit_and_false_past_it():
    # Enough to overflow MAX_SHARDS with room to spare; a range that only
    # just reached the old ceiling would make this test fail as a
    # StopIteration the day the ceiling is raised.
    requests = [_request(request_id=f"id{n:03d}") for n in range(items_state.MAX_SHARDS * 20)]
    first_over_limit = next(
        count
        for count in range(len(requests) + 1)
        if len(items_state.encode_state(items_state.State(queue=requests[:count])))
        > items_state.MAX_SHARDS
    )
    at_limit = items_state.State(queue=requests[: first_over_limit - 1])
    past_limit = items_state.State(queue=requests[:first_over_limit])

    assert len(items_state.encode_state(at_limit)) == items_state.MAX_SHARDS
    assert items_state.fits(at_limit)
    assert not items_state.fits(past_limit)


def test_small_queue_uses_exactly_one_shard():
    contents = items_state.encode_state(items_state.State(queue=[_request()]))

    assert len(contents) == 1


def test_new_request_ids_are_unique():
    assert len({items_state.new_request_id() for _ in range(200)}) == 200


import items_rules


def test_pending_gear_counts_only_that_players_gear_requests():
    state = items_state.State(
        queue=[
            _request("a", ign="Kobe", type_=items_rules.GEAR),
            _request("b", ign="Kobe", type_=items_rules.GEAR),
            _request("c", ign="Kobe", type_=items_rules.SPECIAL),
            _request("d", ign="Dajz", type_=items_rules.GEAR),
        ]
    )
    assert items_state.pending_gear_for(state, "Kobe", "2026-08-07") == 2
    assert items_state.pending_gear_for(state, "kobe", "2026-08-07") == 2


def test_pending_gear_does_not_cross_the_pht_day_boundary():
    state = items_state.State(queue=[_request("a", ign="Kobe", type_=items_rules.GEAR)])
    assert items_state.pending_gear_for(state, "Kobe", "2026-08-08") == 0


def test_find_request_returns_none_when_absent():
    assert items_state.find_request(items_state.State(), "nope") is None


def test_remove_request_takes_it_out_of_the_queue():
    state = items_state.State(queue=[_request("a"), _request("b")])
    removed = items_state.remove_request(state, "a")
    assert removed.id == "a"
    assert [r.id for r in state.queue] == ["b"]


def test_removing_an_already_removed_request_returns_none():
    state = items_state.State(queue=[_request("a")])
    items_state.remove_request(state, "a")
    assert items_state.remove_request(state, "a") is None


def _raffle(item="Asta's Heart", created="2026-08-09 10:00:00", ends="2026-08-10 10:00:00", **kwargs):
    return items_state.Raffle(
        item=item,
        channel_id=555,
        message_id=777,
        created_at=created,
        ends_at=ends,
        **kwargs,
    )


def test_bindings_and_not_players_survive_a_round_trip():
    state = items_state.State(
        officer_channel_id=1,
        bindings={"111": "Jjew", "222": "Kobe"},
        not_players=["333", "444"],
    )

    restored = items_state.decode_shards(items_state.encode_state(state))

    assert restored.bindings == {"111": "Jjew", "222": "Kobe"}
    assert restored.not_players == ["333", "444"]


def test_a_state_with_no_bindings_spends_no_bytes_on_them():
    """An always-present empty key would shrink every guild's shard budget."""
    state = items_state.State(officer_channel_id=1, igns={"111": "Jjew"})

    assert "bindings" not in items_state.encode_state(state)[0]


def test_a_state_with_bindings_still_encodes_them():
    state = items_state.State(officer_channel_id=1, bindings={"111": "Jjew"})

    assert items_state.decode_shards(items_state.encode_state(state)).bindings == {
        "111": "Jjew"
    }


def test_a_pin_written_before_bindings_existed_loads_with_them_empty():
    """Production pins predate this field; they must not fail to load."""
    old = items_state.State(officer_channel_id=1, igns={"111": "Jjew"})

    restored = items_state.decode_shards(items_state.encode_state(old))

    assert restored.bindings == {}
    assert restored.not_players == []
    assert restored.igns == {"111": "Jjew"}


def test_bindings_spill_across_shards_and_all_survive():
    """One shard cannot hold hundreds of bindings; none may be dropped."""
    state = items_state.State(
        officer_channel_id=1,
        bindings={str(10**17 + i): f"PlayerName{i:03d}" for i in range(300)},
    )

    contents = items_state.encode_state(state)
    restored = items_state.decode_shards(contents)

    assert len(contents) > 1, "300 bindings must not fit one shard"
    assert restored.bindings == state.bindings


def test_three_hundred_bindings_still_fit_the_pinned_message():
    """Measured at design time: ~38 bytes each, 18 of 20 shards at 300."""
    state = items_state.State(
        officer_channel_id=1,
        bindings={str(10**17 + i): f"PlayerName{i:03d}" for i in range(300)},
        raffles=[
            _raffle(
                item=f"Special Log Number {n}",
                created=f"2026-08-09 {n:02d}:00:00",
                listed=True,
                eligible=tuple(f"PlayerName{i:02d}" for i in range(40)),
            )
            for n in range(20)
        ],
    )

    assert items_state.fits(state)


def test_to_dict_still_writes_the_legacy_winner_key_for_an_older_bot():
    """A rollback must not read a drawn raffle as undrawn and supersede it."""
    raw = _raffle(winners=("Jjew", "Kobe"), drawn=True).to_dict()

    assert raw["winner"] == "Jjew"
    assert raw["winners"] == ["Jjew", "Kobe"]
    assert items_state.Raffle.from_dict(raw).winners == ("Jjew", "Kobe")


def test_an_undrawn_raffle_writes_an_empty_legacy_winner():
    assert _raffle().to_dict()["winner"] == ""


def test_a_raffle_saved_under_the_old_single_winner_key_still_loads():
    """State written before multi-winner is sitting in the pinned message."""
    legacy = {
        "item": "Asta's Heart",
        "channel_id": 42,
        "message_id": 999,
        "created_at": "2026-08-09 01:00:00",
        "ends_at": "2026-08-09 10:00:00",
        "eligible": ["Jjew", "Kobe"],
        "listed": True,
        "winner": "Jjew",
    }

    raffle = items_state.Raffle.from_dict(legacy)

    assert raffle.winners == ("Jjew",)
    assert raffle.drawn is True


def test_a_legacy_raffle_with_no_winner_loads_as_undrawn():
    legacy = {
        "item": "Asta's Heart",
        "channel_id": 42,
        "message_id": 999,
        "created_at": "2026-08-09 01:00:00",
        "ends_at": "2026-08-09 10:00:00",
        "winner": "",
    }

    raffle = items_state.Raffle.from_dict(legacy)

    assert raffle.winners == ()
    assert raffle.drawn is False


def test_several_winners_survive_a_round_trip():
    raffle = _raffle(winners=("Jjew", "Kobe"), drawn=True)

    restored = items_state.Raffle.from_dict(raffle.to_dict())

    assert restored.winners == ("Jjew", "Kobe")
    assert restored.drawn is True


def test_eviction_will_not_drop_a_partly_drawn_raffle():
    """Its ticked checkboxes and unfinished draw are only recorded here.

    The oldest raffle carrying winners is the partly drawn one, so a
    filter on `winners` rather than `drawn` would pick it.
    """
    # raffle_to_evict returns early unless every slot is taken, so the
    # list is padded to capacity with undrawn fillers.
    raffles = [
        _raffle(item="Partly drawn", created="2026-08-01 10:00:00",
                winners=("Kobe",), drawn=False),
        _raffle(item="Fully drawn", created="2026-08-02 10:00:00",
                winners=("Jjew",), drawn=True),
    ]
    raffles += [
        _raffle(item=f"Log {n}", created=f"2026-08-03 {n:02d}:00:00")
        for n in range(items_state.MAX_RAFFLES - 2)
    ]
    state = items_state.State(raffles=raffles)

    allowed, victim = items_state.raffle_to_evict(state)

    assert allowed is True
    assert victim.item == "Fully drawn"


def test_a_raffle_survives_an_encode_decode_round_trip():
    state = items_state.State(
        officer_channel_id=1,
        raffle_channel_id=2,
        raffle_role_ids=[10, 11],
        # Partly drawn on purpose: a drawn raffle no longer stores its
        # entry list, so this round trip checks a raffle that still does.
        # test_a_drawn_raffle_survives_the_round_trip_without_its_pool
        # covers the other case.
        raffles=[_raffle(eligible=("Jjew", "Kobe"), listed=True, winners=("Jjew",), drawn=False)],
    )

    restored = items_state.decode_shards(items_state.encode_state(state))

    assert restored.raffle_channel_id == 2
    assert restored.raffle_role_ids == [10, 11]
    assert len(restored.raffles) == 1
    assert restored.raffles[0].item == "Asta's Heart"
    assert restored.raffles[0].eligible == ("Jjew", "Kobe")
    assert restored.raffles[0].listed is True
    assert restored.raffles[0].winners == ("Jjew",)


def test_a_pin_written_before_raffles_existed_still_loads():
    """Production pins have none of the three new keys."""
    old = items_state.State(officer_channel_id=1)
    contents = items_state.encode_state(old)

    restored = items_state.decode_shards(contents)

    assert restored.raffles == []
    assert restored.raffle_role_ids == []
    assert restored.raffle_channel_id is None


def test_raffles_spill_into_further_shards_rather_than_being_dropped():
    state = items_state.State(
        officer_channel_id=1,
        raffles=[
            _raffle(item=f"Special Log {n}", eligible=tuple(f"Player {i:03d}" for i in range(40)))
            for n in range(items_state.MAX_RAFFLES)
        ],
    )

    contents = items_state.encode_state(state)
    restored = items_state.decode_shards(contents)

    assert len(contents) > 1
    assert [r.item for r in restored.raffles] == [r.item for r in state.raffles]


def test_find_raffle_matches_case_and_spacing_insensitively():
    state = items_state.State(raffles=[_raffle()])

    assert items_state.find_raffle(state, "  asta's   heart ").item == "Asta's Heart"
    assert items_state.find_raffle(state, "Benji's Heart") is None


def test_find_raffle_returns_the_most_recent_when_a_name_repeats():
    state = items_state.State(
        raffles=[
            _raffle(created="2026-08-01 10:00:00", winners=("Kobe",), drawn=True),
            _raffle(created="2026-08-09 10:00:00"),
        ]
    )

    assert items_state.find_raffle(state, "Asta's Heart").created_at == "2026-08-09 10:00:00"


def test_replace_raffle_swaps_the_record_in_place():
    original = _raffle()
    state = items_state.State(raffles=[original])

    updated = items_state.replace_raffle(state, original, winners=("Jjew",), drawn=True)

    assert state.raffles == [updated]
    assert updated.winners == ("Jjew",)
    assert original.winners == ()


def _full_of(**kwargs):
    """MAX_RAFFLES raffles, oldest first, all sharing the given fields."""
    return items_state.State(
        raffles=[
            _raffle(
                item=f"Log {n}",
                created=f"2026-08-09 {n:02d}:00:00",
                **kwargs,
            )
            for n in range(items_state.MAX_RAFFLES)
        ]
    )


def test_evicting_drops_the_oldest_DRAWN_raffle_when_full():
    state = _full_of(ends="2026-08-09 12:00:00")
    # Log 0 and Log 1 have been drawn; the rest are ended but undrawn.
    for index in (0, 1):
        items_state.replace_raffle(state, state.raffles[index], winners=("Kobe",), drawn=True)

    assert items_state.evict_for_new_raffle(state, "2026-08-09 13:00:00")
    assert "Log 0" not in [r.item for r in state.raffles]
    assert "Log 1" in [r.item for r in state.raffles]


def test_evicting_never_discards_an_ended_raffle_nobody_has_drawn():
    """The whole point of a raffle is the draw.

    An ended-but-undrawn raffle still holds the frozen pool !winner
    checks against. Dropping it to make room would silently destroy the
    only record of who was eligible, so a full state refuses instead.
    """
    state = _full_of(ends="2026-08-09 12:00:00", listed=True, eligible=("Jjew",))

    assert not items_state.evict_for_new_raffle(state, "2026-08-09 13:00:00")
    assert len(state.raffles) == items_state.MAX_RAFFLES


def test_evicting_refuses_when_every_raffle_is_still_open():
    state = _full_of(ends="2026-12-31 23:59:59")

    assert not items_state.evict_for_new_raffle(state, "2026-08-09 13:00:00")
    assert len(state.raffles) == items_state.MAX_RAFFLES


def test_a_drawn_raffle_is_evictable_even_before_its_poll_closes():
    """A winner is recorded; the poll's clock no longer matters."""
    state = _full_of(ends="2026-12-31 23:59:59")
    items_state.replace_raffle(state, state.raffles[0], winners=("Kobe",), drawn=True)

    assert items_state.evict_for_new_raffle(state, "2026-08-09 13:00:00")
    assert "Log 0" not in [r.item for r in state.raffles]


def test_twenty_listed_raffles_and_a_full_queue_still_fit():
    """The reason MAX_SHARDS was raised: 20 items raffled in one day."""
    state = items_state.State(
        officer_channel_id=1,
        queue_channel_id=2,
        raffle_channel_id=3,
        raffle_role_ids=[10, 11],
        raffles=[
            _raffle(
                item=f"Special Log Number {n}",
                created=f"2026-08-09 {n:02d}:00:00",
                listed=True,
                eligible=tuple(f"PlayerName{i:02d}" for i in range(35)),
            )
            for n in range(20)
        ],
        queue=[
            items_state.PendingRequest(
                f"id{i:03d}", i, f"Player {i}", "Asta's Belt", "Gear",
                "2026-08-09 09:00:00",
            )
            for i in range(30)
        ],
    )

    assert items_state.fits(state)
    restored = items_state.decode_shards(items_state.encode_state(state))
    assert len(restored.raffles) == 20
    assert restored.raffles[0].eligible == state.raffles[0].eligible


def test_capacity_holds_when_every_raffle_has_several_winners():
    """Winner lists add bytes to the pinned state; capacity must survive."""
    state = items_state.State(
        raffles=[
            _raffle(
                item=f"Special Log Number {n}",
                created=f"2026-08-09 {n:02d}:00:00",
                listed=True,
                eligible=tuple(f"PlayerName{i:02d}" for i in range(35)),
                winners=("PlayerName01", "PlayerName02", "PlayerName03"),
                drawn=True,
            )
            for n in range(20)
        ],
        queue=[
            items_state.PendingRequest(
                f"id{i:03d}", i, f"Player {i}", "Asta's Belt", "Gear",
                "2026-08-09 09:00:00",
            )
            for i in range(30)
        ],
    )

    assert items_state.fits(state)


def test_evicting_does_nothing_below_the_ceiling():
    state = items_state.State(raffles=[_raffle()])

    assert items_state.evict_for_new_raffle(state, "2026-08-09 13:00:00")
    assert len(state.raffles) == 1


def test_raffle_item_names_lists_every_tracked_raffle():
    state = items_state.State(raffles=[_raffle(item="A"), _raffle(item="B")])

    assert items_state.raffle_item_names(state) == ["A", "B"]


def test_raffle_to_evict_names_the_victim_without_removing_it():
    """poll_cmd must know the cost BEFORE it posts a poll it cannot untake."""
    state = _full_of(ends="2026-08-09 12:00:00")
    items_state.replace_raffle(state, state.raffles[2], winners=("Kobe",), drawn=True)

    allowed, victim = items_state.raffle_to_evict(state)

    assert allowed
    assert victim.item == "Log 2"
    assert len(state.raffles) == items_state.MAX_RAFFLES


def test_raffle_to_evict_has_no_victim_below_the_ceiling():
    assert items_state.raffle_to_evict(items_state.State(raffles=[_raffle()])) == (True, None)


def test_raffle_to_evict_refuses_when_nothing_has_been_drawn():
    state = _full_of(ends="2026-08-09 12:00:00")

    assert items_state.raffle_to_evict(state) == (False, None)


def test_an_oversized_configuration_is_refused_rather_than_written():
    """Shard 0 carries fields no per-item loop measures.

    A huge raffle_role_ids list would otherwise render a shard past
    Discord's content limit, which save_state can only discover mid-write
    -- possibly after deleting the message it was replacing.
    """
    state = items_state.State(
        officer_channel_id=1,
        raffle_role_ids=[10**18 + n for n in range(200)],
    )

    assert not items_state.fits(state)
    try:
        items_state.encode_state(state)
    except ValueError as error:
        assert "too large" in str(error)
    else:
        raise AssertionError("an unstorable configuration must be reported")


def test_a_reasonable_number_of_raffle_roles_still_fits():
    state = items_state.State(
        officer_channel_id=1, raffle_role_ids=[10**18 + n for n in range(20)]
    )

    assert items_state.fits(state)
    assert items_state.decode_shards(items_state.encode_state(state)).raffle_role_ids == state.raffle_role_ids


def _session():
    return items_state.RaffleSession(
        items=("Asta's Heart", "Amentis Foot", "Benji's Heart"),
        position=1,
        results=(("Asta's Heart", ("Kobe",)),),
        skipped=(),
    )


def test_session_reports_the_current_item():
    assert _session().current_item == "Amentis Foot"


def test_a_finished_session_has_no_current_item():
    session = items_state.RaffleSession(items=("Asta's Heart",), position=1)

    assert session.finished is True
    assert session.current_item is None


def test_session_winners_are_flattened_from_the_results():
    session = items_state.RaffleSession(
        items=("A", "B"),
        position=2,
        results=(("A", ("Kobe", "Jjew")), ("B", ("wile-KAMOTE",))),
    )

    assert session.winners == ("Kobe", "Jjew", "wile-KAMOTE")


def test_a_session_survives_a_round_trip_through_the_pin():
    state = items_state.State(officer_channel_id=1, raffle_session=_session())

    restored = items_state.decode_shards(items_state.encode_state(state))

    assert restored.raffle_session == _session()


def test_a_pin_written_before_sessions_existed_loads_as_no_session():
    state = items_state.State(officer_channel_id=1)

    restored = items_state.decode_shards(items_state.encode_state(state))

    assert restored.raffle_session is None


def test_a_session_is_counted_by_fits():
    """A sitting that cannot be persisted must be refused at !startraffle."""
    huge = items_state.RaffleSession(items=tuple(f"Log {n}" * 200 for n in range(400)))
    state = items_state.State(officer_channel_id=1, raffle_session=huge)

    assert items_state.fits(state) is False


def test_a_pin_whose_session_is_not_an_object_decodes_as_no_session():
    """decode_state must return None or a Shard, never raise.

    A hand-edited pin is the realistic source of this, and a raise here
    would take restart recovery down with it.
    """
    content = (
        f"{items_state.STATE_MARKER}\n```json\n"
        '{"part":0,"total":1,"raffle_session":"oops"}\n```'
    )

    shard = items_state.decode_state(content)

    assert shard is not None
    assert shard.state.raffle_session is None


NOW = "2026-08-13 12:00:00"


def test_session_candidates_are_closed_and_undrawn_oldest_first():
    state = items_state.State(raffles=[
        _raffle("B", created="2026-08-09 11:00:00"),
        _raffle("A", created="2026-08-09 10:00:00"),
    ])

    assert [r.item for r in items_state.session_candidates(state, NOW)] == ["A", "B"]


def test_session_candidates_exclude_a_poll_still_open():
    state = items_state.State(raffles=[
        _raffle(
            "A",
            created="2026-08-09 10:00:00",
            ends="2099-01-01 00:00:00",
        ),
    ])

    assert items_state.session_candidates(state, NOW) == []


def test_session_candidates_exclude_a_drawn_raffle():
    state = items_state.State(raffles=[
        _raffle(
            "A",
            created="2026-08-09 10:00:00",
            winners=("Kobe",),
            drawn=True,
        ),
    ])

    assert items_state.session_candidates(state, NOW) == []


def test_session_candidates_include_a_partly_drawn_raffle():
    """A write that failed part way through still has names to record."""
    state = items_state.State(raffles=[
        _raffle(
            "A",
            created="2026-08-09 10:00:00",
            winners=("Kobe",),
            drawn=False,
        ),
    ])

    assert [r.item for r in items_state.session_candidates(state, NOW)] == ["A"]


def test_session_candidates_include_a_frozen_but_undrawn_raffle():
    """A poll skipped in an earlier sitting is picked up by the next one."""
    state = items_state.State(raffles=[
        _raffle(
            "A",
            created="2026-08-09 10:00:00",
            eligible=("Jjew",),
            listed=True,
        ),
    ])

    assert [r.item for r in items_state.session_candidates(state, NOW)] == ["A"]


# -- A drawn raffle stores no entry list --------------------------------------
#
# The entry list exists to draw a winner from. Once the draw is finished
# nothing reads it again -- the session excludes previous winners from
# RaffleSession.results, not from any earlier raffle's pool -- but it was
# still being written to the pin, 246 names across 15 finished raffles on
# the live guild, which is a whole shard of a five-shard state. Every save
# rewrites shards, so the dead weight cost Discord requests on every write.

def _pooled_raffle(**changes):
    base = items_state.Raffle(
        item="Asta's Heart", channel_id=1, message_id=2,
        created_at="2026-08-18 09:00:00", ends_at="2026-08-18 10:00:00",
        eligible=("Jjew", "Kobe", "Dajz"),
    )
    return dataclasses.replace(base, **changes)


def test_a_drawn_raffle_does_not_store_its_entry_list():
    stored = _pooled_raffle(winners=("Jjew",), drawn=True).to_dict()

    assert stored.get("eligible", []) == []


def test_an_open_raffle_keeps_its_entry_list():
    stored = _pooled_raffle(listed=True).to_dict()

    assert stored["eligible"] == ["Jjew", "Kobe", "Dajz"]


def test_a_partly_drawn_raffle_keeps_its_entry_list():
    """A failed sheet write leaves drawn False -- it may be drawn again.

    Dropping the pool there would leave the remaining names undrawable.
    """
    stored = _pooled_raffle(winners=("Jjew",), drawn=False, listed=True).to_dict()

    assert stored["eligible"] == ["Jjew", "Kobe", "Dajz"]


def test_a_drawn_raffle_survives_the_round_trip_without_its_pool():
    state = items_state.State(officer_channel_id=1)
    state.raffles.append(_pooled_raffle(winners=("Jjew",), drawn=True))

    restored = items_state.decode_shards(items_state.encode_state(state))

    raffle = restored.raffles[0]
    assert raffle.winners == ("Jjew",)
    assert raffle.drawn is True
    assert raffle.eligible == ()


def test_dropping_the_pool_shrinks_the_stored_state():
    """The whole point: fewer shards means fewer writes per save."""
    fat = items_state.State(officer_channel_id=1)
    for n in range(15):
        fat.raffles.append(
            _pooled_raffle(
                item=f"Log {n}", created_at=f"2026-08-{n + 1:02d} 09:00:00",
                eligible=tuple(f"PlayerNumber{i:03d}" for i in range(16)),
                winners=("Jjew",), drawn=True,
            )
        )

    assert len(items_state.encode_state(fat)) < 4, (
        "15 drawn raffles carrying 16 names each should no longer need shards"
    )
