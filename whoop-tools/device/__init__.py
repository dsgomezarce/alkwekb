"""Hardware boundary for the Vitals DS engine.

The session loop (daemon, collector) still drives a strap through ``whoop.Whoop``.
This package is the part that knows the peer is a WHOOP 4.0: GATT layout, frame
codec, and the connect commands. See ``protocol.py`` for which bytes come from
public sources and which claims this fork does not repeat without a strap.
"""

from device.discover import choose, family_of, matches, rank
from device.protocol import (
    BATTERY_LEVEL,
    CMD_CHAR,
    DESTRUCTIVE,
    HR_MEASUREMENT,
    NOTIFY_CHARS,
    OPTIONAL_NOTIFY,
    WHOOP4_SERVICE,
    WHOOP5_SERVICE,
    connect_handshake,
    parse_agreed_history,
    parse_agreed_realtime,
    parse_heart_rate_measurement,
    set_clock_payload,
)

__all__ = [
    "choose",
    "family_of",
    "matches",
    "rank",
    "BATTERY_LEVEL",
    "CMD_CHAR",
    "DESTRUCTIVE",
    "HR_MEASUREMENT",
    "NOTIFY_CHARS",
    "OPTIONAL_NOTIFY",
    "WHOOP4_SERVICE",
    "WHOOP5_SERVICE",
    "connect_handshake",
    "parse_agreed_history",
    "parse_agreed_realtime",
    "parse_heart_rate_measurement",
    "set_clock_payload",
]
