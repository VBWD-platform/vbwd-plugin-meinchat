"""S86.1 Slice 1b — SSE room fan-out.

A room message is delivered to every member's stream and NEVER to a
non-member's. Asserted at the bus/subscription layer the way the existing
event-bus contract tests do (no real HTTP stream needed): the route builds a
`subscribe_many` over the caller's own channel plus each room they belong to,
so a member's stream sees `room:<id>` and a stranger's does not.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from plugins.meinchat.meinchat.services.event_bus import MeinchatEventBus


def _collect(sub, expected, deadline):
    out = []
    for event in sub.iter_events(timeout=deadline):
        if event.get("type") == "heartbeat":
            continue
        out.append(event)
        if len(out) >= expected:
            break
    return out


@pytest.mark.integration
def test_room_message_reaches_members_not_strangers():
    bus = MeinchatEventBus()
    member_a, member_b, stranger = uuid4(), uuid4(), uuid4()
    room_id = uuid4()

    # Each member's stream subscribes to their own channel + the room channel
    # (what the SSE route assembles from meinchat_room_member). The stranger
    # subscribes only to their own channel — they belong to no room.
    sub_a = bus.subscribe_many([f"user:{member_a}", f"room:{room_id}"])
    sub_b = bus.subscribe_many([f"user:{member_b}", f"room:{room_id}"])
    sub_stranger = bus.subscribe_many([f"user:{stranger}"])

    bus.publish(f"room:{room_id}", {"type": "message", "body": "hi room"})

    assert _collect(sub_a, 1, 1.0) == [{"type": "message", "body": "hi room"}]
    assert _collect(sub_b, 1, 1.0) == [{"type": "message", "body": "hi room"}]
    assert _collect(sub_stranger, 1, 0.3) == []
