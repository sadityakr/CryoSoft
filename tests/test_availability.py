# ---
# description: |
#   Unit tests for the Availability standard's pure policy core
#   (cryosoft.core.availability): the tag/state vocabularies, TAG_POLICY's
#   coverage of the vocabulary, state_for()/decide_availability()'s
#   precedence resolution, and Availability construction/validation. No
#   sims, no Qt, no Station — pure value objects and pure functions only.
# entry_point: pytest tests/test_availability.py -v
# last_updated: 2026-07-27
# ---

"""Tests for the Availability standard's pure policy core."""

from __future__ import annotations

import pytest
from cryosoft.core.availability import (
    AVAILABILITY_STATES,
    AVAILABILITY_TAGS,
    TAG_POLICY,
    TAG_PRECEDENCE,
    Availability,
    decide_availability,
    state_for,
)


# ----------------------------------------------------------------------
# Vocabulary
# ----------------------------------------------------------------------


def test_availability_tags_is_the_closed_vocabulary():
    assert AVAILABILITY_TAGS == ("connect_failed", "operator", "not_responding", "detached")


def test_availability_states_are_the_four_mutually_exclusive_situations():
    assert AVAILABILITY_STATES == ("live", "detached", "faulted", "absent")


def test_tag_policy_covers_exactly_availability_tags():
    assert set(TAG_POLICY.keys()) == set(AVAILABILITY_TAGS)
    for tag, policy in TAG_POLICY.items():
        assert policy.tag == tag


def test_tag_policy_states_are_all_in_vocabulary():
    for policy in TAG_POLICY.values():
        assert policy.state in AVAILABILITY_STATES


def test_tag_precedence_is_a_permutation_of_availability_tags():
    assert sorted(TAG_PRECEDENCE) == sorted(AVAILABILITY_TAGS)
    assert len(TAG_PRECEDENCE) == len(set(TAG_PRECEDENCE))


def test_tag_policy_table_matches_the_declared_rows():
    assert TAG_POLICY["connect_failed"].state == "absent"
    assert TAG_POLICY["connect_failed"].enumerable is False
    assert TAG_POLICY["connect_failed"].controllable is False
    assert TAG_POLICY["connect_failed"].fails_claimed_run is False
    assert TAG_POLICY["connect_failed"].raises_error_event is False
    assert TAG_POLICY["connect_failed"].recovery == "connect"

    assert TAG_POLICY["operator"].state == "absent"
    assert TAG_POLICY["operator"].enumerable is False
    assert TAG_POLICY["operator"].controllable is False
    assert TAG_POLICY["operator"].fails_claimed_run is False
    assert TAG_POLICY["operator"].raises_error_event is False
    assert TAG_POLICY["operator"].recovery == "connect"

    assert TAG_POLICY["not_responding"].state == "faulted"
    assert TAG_POLICY["not_responding"].enumerable is True
    assert TAG_POLICY["not_responding"].controllable is False
    assert TAG_POLICY["not_responding"].fails_claimed_run is True
    assert TAG_POLICY["not_responding"].raises_error_event is True
    assert TAG_POLICY["not_responding"].recovery == "retry"

    assert TAG_POLICY["detached"].state == "detached"
    assert TAG_POLICY["detached"].enumerable is True
    assert TAG_POLICY["detached"].controllable is True
    assert TAG_POLICY["detached"].fails_claimed_run is False
    assert TAG_POLICY["detached"].raises_error_event is False
    assert TAG_POLICY["detached"].recovery == ""


# ----------------------------------------------------------------------
# state_for()
# ----------------------------------------------------------------------


def test_state_for_empty_tags_is_live():
    assert state_for(frozenset()) == "live"


@pytest.mark.parametrize(
    "tag,expected_state",
    [
        ("connect_failed", "absent"),
        ("operator", "absent"),
        ("not_responding", "faulted"),
        ("detached", "detached"),
    ],
)
def test_state_for_single_tag(tag, expected_state):
    assert state_for(frozenset({tag})) == expected_state


def test_state_for_operator_and_connect_failed_resolves_to_absent():
    # The bug-fix scenario: an operator-released VI whose reconnect then
    # fails on hardware. Both tags imply "absent" so the state agrees
    # regardless of which wins precedence.
    assert state_for(frozenset({"operator", "connect_failed"})) == "absent"


def test_state_for_not_responding_and_detached_prefers_the_more_restrictive_tag():
    # Per TAG_PRECEDENCE, "not_responding" is more restrictive than
    # "detached" and wins.
    assert state_for(frozenset({"not_responding", "detached"})) == "faulted"


# ----------------------------------------------------------------------
# decide_availability()
# ----------------------------------------------------------------------


def test_decide_availability_empty_tags_is_none():
    assert decide_availability(frozenset()) is None


def test_decide_availability_single_tag_returns_its_policy():
    policy = decide_availability(frozenset({"not_responding"}))
    assert policy is TAG_POLICY["not_responding"]


def test_decide_availability_operator_and_connect_failed_prefers_connect_failed():
    policy = decide_availability(frozenset({"operator", "connect_failed"}))
    assert policy is TAG_POLICY["connect_failed"]


def test_decide_availability_not_responding_and_detached_prefers_not_responding():
    policy = decide_availability(frozenset({"not_responding", "detached"}))
    assert policy is TAG_POLICY["not_responding"]


# ----------------------------------------------------------------------
# Availability construction/validation
# ----------------------------------------------------------------------


def test_availability_live_vi_constructs():
    a = Availability(
        vi_name="magnet_z",
        vi_type="system",
        state="live",
        tags=frozenset(),
        reason="",
        since=0.0,
    )
    assert a.state == "live"
    assert a.tags == frozenset()


def test_availability_absent_vi_constructs():
    a = Availability(
        vi_name="magnet_z",
        vi_type="system",
        state="absent",
        tags=frozenset({"operator"}),
        reason="Disconnected by the operator",
        since=123.0,
    )
    assert a.state == "absent"


def test_availability_rejects_unknown_tag():
    with pytest.raises(ValueError):
        Availability(
            vi_name="magnet_z",
            vi_type="system",
            state="live",
            tags=frozenset({"bogus_tag"}),
            reason="",
            since=0.0,
        )


def test_availability_rejects_unknown_state():
    with pytest.raises(ValueError):
        Availability(
            vi_name="magnet_z",
            vi_type="system",
            state="bogus_state",
            tags=frozenset(),
            reason="",
            since=0.0,
        )


def test_availability_rejects_state_disagreeing_with_tags():
    with pytest.raises(ValueError):
        Availability(
            vi_name="magnet_z",
            vi_type="system",
            state="live",
            tags=frozenset({"operator"}),
            reason="",
            since=0.0,
        )


def test_availability_rejects_absent_state_with_no_tags():
    with pytest.raises(ValueError):
        Availability(
            vi_name="magnet_z",
            vi_type="system",
            state="absent",
            tags=frozenset(),
            reason="",
            since=0.0,
        )
