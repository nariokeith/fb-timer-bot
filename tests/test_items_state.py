"""Tests for the pinned-message state format and queue operations."""

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


def test_a_raffle_survives_an_encode_decode_round_trip():
    state = items_state.State(
        officer_channel_id=1,
        raffle_channel_id=2,
        raffle_role_ids=[10, 11],
        raffles=[_raffle(eligible=("Jjew", "Kobe"), listed=True, winner="Jjew")],
    )

    restored = items_state.decode_shards(items_state.encode_state(state))

    assert restored.raffle_channel_id == 2
    assert restored.raffle_role_ids == [10, 11]
    assert len(restored.raffles) == 1
    assert restored.raffles[0].item == "Asta's Heart"
    assert restored.raffles[0].eligible == ("Jjew", "Kobe")
    assert restored.raffles[0].listed is True
    assert restored.raffles[0].winner == "Jjew"


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
            _raffle(created="2026-08-01 10:00:00", winner="Kobe"),
            _raffle(created="2026-08-09 10:00:00"),
        ]
    )

    assert items_state.find_raffle(state, "Asta's Heart").created_at == "2026-08-09 10:00:00"


def test_replace_raffle_swaps_the_record_in_place():
    original = _raffle()
    state = items_state.State(raffles=[original])

    updated = items_state.replace_raffle(state, original, winner="Jjew")

    assert state.raffles == [updated]
    assert updated.winner == "Jjew"
    assert original.winner == ""


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
        items_state.replace_raffle(state, state.raffles[index], winner="Kobe")

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
    items_state.replace_raffle(state, state.raffles[0], winner="Kobe")

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


def test_evicting_does_nothing_below_the_ceiling():
    state = items_state.State(raffles=[_raffle()])

    assert items_state.evict_for_new_raffle(state, "2026-08-09 13:00:00")
    assert len(state.raffles) == 1


def test_raffle_item_names_lists_every_tracked_raffle():
    state = items_state.State(raffles=[_raffle(item="A"), _raffle(item="B")])

    assert items_state.raffle_item_names(state) == ["A", "B"]
