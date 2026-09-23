"""Timestamp conversion that cannot crash a workflow.

WhatsApp timestamps are not as uniform as the protocol documentation suggests.
``MessageInfo.Timestamp`` is normally epoch **seconds**, but some payloads -
system messages, certain clients, a few notification types - carry
**milliseconds**, and a malformed or absent value arrives as ``0`` or as
something nonsensical.

That matters more on Windows than anywhere else, because
``datetime.fromtimestamp()`` there raises ``OSError: [Errno 22] Invalid
argument`` for anything out of range rather than ``ValueError``. A single
message with a millisecond timestamp is therefore enough to kill an Alteryx
tool with an error that says nothing about time at all::

    The WhatsApp Input tool failed unexpectedly: [Errno 22] Invalid argument

One unusual message must never cost a workflow its whole batch, so every
conversion in this connector goes through here, and nothing here raises.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

#: Fallback for a value that cannot be interpreted at all.
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

#: datetime's own limits, as epoch seconds. Beyond these, every conversion
#: fails - on Windows with OSError, elsewhere with ValueError.
MIN_SECONDS = -62135596800        # 0001-01-01
MAX_SECONDS = 253402300799        # 9999-12-31 23:59:59

#: Above this, a value is certainly not seconds: it is year 5138 already, and
#: WhatsApp did not exist then. Used to detect ms / us / ns scales.
_IMPLAUSIBLE_SECONDS = 10**11


def to_datetime(value: Any, default: datetime | None = None) -> datetime:
    """Convert a WhatsApp timestamp to an aware UTC datetime. Never raises.

    Handles, in order:

    * ``None``, empty strings and values that are not numbers -> ``default``;
    * millisecond, microsecond and nanosecond scales -> divided down to seconds
      (a value is rescaled while it is too large to be a plausible second
      count, which collapses all three cases);
    * anything still outside ``datetime``'s range -> clamped, rather than
      discarded, so the row survives with a visibly wrong but harmless date.

    :param default: returned when the value carries no information at all.
        Defaults to the epoch, which sorts first and is obviously not real.
    """
    fallback = EPOCH if default is None else default

    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return fallback

    if seconds == 0:
        return fallback

    # Collapse ms (1e12), us (1e15) and ns (1e18) onto seconds. Each pass
    # divides by a thousand, so one loop covers every scale.
    guard = 0
    while abs(seconds) > _IMPLAUSIBLE_SECONDS and guard < 4:
        seconds //= 1000
        guard += 1

    seconds = max(MIN_SECONDS, min(MAX_SECONDS, seconds))

    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        # Should be unreachable after clamping, but this function exists
        # precisely so that "should be unreachable" is not load-bearing.
        return fallback


def to_epoch_seconds(value: datetime | None) -> int | None:
    """Epoch seconds for a datetime, or ``None``. Never raises."""
    if value is None:
        return None
    try:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    except (OSError, OverflowError, ValueError):
        return 0


