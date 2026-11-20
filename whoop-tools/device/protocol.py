"""WHOOP 4.0 frame codec and the connect commands this engine sends.

Nothing in this module was checked against a physical strap in this fork.
Bytes are taken from public write-ups. Where those write-ups disagree, the
value is left raw or is not sent.

Sources (read as text, not re-captured here):

- NOOP, ``docs/PROTOCOL.md`` and ``whoop_protocol.json``
  (github.com/muftiarfan/noop). GATT UUIDs for 4.0, CRC8 over the length
  bytes, CRC32 over the inner record, packet types, the safe command
  numbers, the 8-byte SET_CLOCK, the connect order, and the list of
  opcodes NOOP refuses to send.
- OpenStrap, ``lib/src/commands.dart`` (github.com/OpenStrap/protocol).
  The same 8-byte SET_CLOCK (unix seconds plus subseconds in 1/32768 s)
  and an empty body on GET_CLOCK. OpenStrap also says a wrong-length
  SET_CLOCK is acknowledged and not latched.
- Bluetooth SIG Heart Rate Service: characteristic 0x2A37. Flag byte,
  8- or 16-bit HR, optional energy field, RR intervals in units of
  1/1024 s. NOOP documents that same layout and treats 0x2A37 as the
  reliable HR/RR source.

Not driven here: WHOOP 5.0 / MG (service ``fd4b0001-…``, CRC16 header).
NOOP and OpenVitals both say that envelope is different.

Left unresolved without a strap:

- Custom-stream RR units. OpenVitals concluded the 0x28 / type-47 RR
  counts are whole milliseconds. NOOP does not publish that unit. The
  standard 0x2A37 stream is 1/1024 s. ``parse_agreed_*`` returns the
  custom counts raw.
- Bytes after the accelerometer in a version-24 history record.
  OpenVitals and the NOOP schema name different quantities at different
  offsets (skin temperature, optical channels, steps, skin contact).
  ``parse_agreed_history`` does not pick a winner. The processing
  pipeline still reads ``whoop.parse_historical``, which is the imported
  OpenVitals layout, and that layout is a hypothesis.
- Alarm payload length. NOOP's builder is 7 bytes; OpenStrap's revision-1
  form (the one OpenVitals sends) is 9 bytes. This module does not build
  alarm frames. ``whoop.py`` keeps the imported 9-byte form.
- Characteristic ``…0007``. NOOP lists it on 5.0 only. OpenVitals
  subscribed to it on a 4.0. The session subscribes only when the GATT
  table actually contains it.
- Bonding write. NOOP's first write is a confirmed GetBatteryLevel, to
  make CoreBluetooth bond. The imported client writes without a response.
  This engine keeps write-without-response. If a strap never answers, that
  confirmed write is the first thing to try, and it has not been tried here.
"""

import binascii
import struct

# --- GATT (NOOP §1). 5.0 is named so a scan can tell it apart, and not driven. ---
WHOOP4_SERVICE = "61080001-8d6d-82b8-614a-1c8cb0f8dcc6"
WHOOP5_SERVICE = "fd4b0001-cce1-4033-93ce-002d5875f58a"
_BASE = "6108{:04x}-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_CHAR = _BASE.format(2)            # write, host → strap
NOTIFY_CHARS = (_BASE.format(3), _BASE.format(4), _BASE.format(5))  # response, event, data
OPTIONAL_NOTIFY = _BASE.format(7)     # 5.0 in NOOP; present-or-not on 4.0 is unverified
HR_MEASUREMENT = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"

# Packet types. Decimal values are NOOP's PacketType enum (whoop_protocol.json).
COMMAND = 35
COMMAND_RESPONSE = 36
REALTIME_DATA = 40
HISTORICAL_DATA = 47
EVENT = 48
METADATA = 49
CONSOLE_LOGS = 50

# Safe command numbers (NOOP CommandNumber / PROTOCOL.md §6). Decimal.
GET_HELLO_HARVARD = 35
GET_ADVERTISING_NAME_HARVARD = 76
SET_CLOCK = 10
GET_CLOCK = 11
SEND_R10_R11_REALTIME = 63
GET_DATA_RANGE = 34
EXIT_HIGH_FREQ_SYNC = 97
TOGGLE_GENERIC_HR_PROFILE = 14
TOGGLE_REALTIME_HR = 3
SEND_HISTORICAL_DATA = 22
HISTORICAL_DATA_RESULT = 23
GET_BATTERY_LEVEL = 26
REPORT_VERSION_INFO = 7
ABORT_HISTORICAL_TRANSMITS = 20

# NOOP §6 "Destructive commands — do not send". Recognized so the session can refuse them.
DESTRUCTIVE = frozenset({
    25,   # FORCE_TRIM
    29,   # REBOOT_STRAP
    32,   # POWER_CYCLE_STRAP
    36,   # START_FIRMWARE_LOAD
    37,   # LOAD_FIRMWARE_DATA
    38,   # PROCESS_FIRMWARE_IMAGE
    45,   # ENTER_BLE_DFU
    99,   # RESET_FUEL_GAUGE
})

# Each step is (name, public source, opcode, payload).
# The first seven are NOOP's connect handshake (§5), with EXIT_HIGH_FREQ_SYNC
# in place of enter: NOOP says current builds send the exit defensively and
# that a plain historical read does not need high-freq sync.
# The last step is OpenVitals' note that 0x2A37 stays quiet until
# ToggleGenericHrProfile. The opcode and the on-payload are in both
# OpenVitals and the NOOP command table. Whether this strap needs it was
# not re-checked.
HANDSHAKE = (
    ("get_hello", "noop-protocol-s5", GET_HELLO_HARVARD, b"\x00"),
    ("get_name", "noop-protocol-s5", GET_ADVERTISING_NAME_HARVARD, b"\x00"),
    ("set_clock", "noop-protocol-s5+openstrap", SET_CLOCK, None),  # filled with the 8-byte clock
    ("get_clock", "noop-protocol-s5+openstrap", GET_CLOCK, b""),
    ("stop_raw_flood", "noop-protocol-s5", SEND_R10_R11_REALTIME, b"\x00"),
    ("get_data_range", "noop-protocol-s5", GET_DATA_RANGE, b"\x00"),
    ("exit_high_freq", "noop-protocol-s6", EXIT_HIGH_FREQ_SYNC, b"\x00"),
    ("enable_standard_hr", "openvitals-protocol+noop-command-14", TOGGLE_GENERIC_HR_PROFILE, b"\x01"),
)


def crc8(data: bytes) -> int:
    """CRC-8/SMBUS (poly 0x07, init 0) over the two length bytes. NOOP §2.4."""
    c = 0
    for b in data:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x07) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def build(ptype: int, seq: int, cmd: int, data: bytes = b"") -> bytes:
    """WHOOP 4.0 frame: AA | len u16 LE = len(inner)+4 | crc8(len) | inner | crc32(inner)."""
    inner = bytes([ptype, seq, cmd]) + data
    ln = struct.pack("<H", len(inner) + 4)
    return b"\xAA" + ln + bytes([crc8(ln)]) + inner + struct.pack("<I", binascii.crc32(inner) & 0xFFFFFFFF)


def command(opcode: int, data: bytes = b"", seq: int = 0) -> bytes:
    if opcode in DESTRUCTIVE:
        raise ValueError(f"opcode {opcode} is on NOOP's do-not-send list")
    return build(COMMAND, seq, opcode, data)


def unframe(buf: bytes):
    """Split a notify buffer into inner payloads. Returns (inners, leftover).

    Resyncs on the next 0xAA whose header CRC8 checks out, and drops a frame
    whose CRC32 does not match. Same rules as NOOP's reassembly (§2.5) plus
    the CRC32 reject in §2.4.
    """
    out = []
    while True:
        i = buf.find(b"\xAA")
        if i < 0:
            return out, b""
        buf = buf[i:]
        if len(buf) < 4:
            break
        ln = struct.unpack_from("<H", buf, 1)[0]
        if ln < 5 or crc8(buf[1:3]) != buf[3]:
            buf = buf[1:]
            continue
        flen = 4 + ln
        if len(buf) < flen:
            break
        inner = buf[4:flen - 4]
        if struct.unpack_from("<I", buf, flen - 4)[0] != binascii.crc32(inner) & 0xFFFFFFFF:
            print(f"  (dropped frame with bad CRC32, {flen} bytes)")
            buf = buf[1:]
            continue
        out.append(inner)
        buf = buf[flen:]
    return out, buf


def set_clock_payload(epoch: float) -> bytes:
    """8-byte SET_CLOCK body: unix seconds u32 LE, then subseconds u32 LE.

    Subseconds are 1/32768 s in the low half of the second word (OpenStrap
    ``cmdSetClock``; NOOP's ``setClockPayload`` uses the same width and says
    zero subseconds is fine). A 9-byte body, which is what OpenVitals sent,
    is the length OpenStrap warns does not latch.
    """
    ms = int(epoch * 1000)
    sec = ms // 1000
    sub = ((ms % 1000) * 32768) // 1000
    return struct.pack("<II", sec & 0xFFFFFFFF, sub)


def connect_handshake(epoch: float):
    """Frames for one connect, in order. See HANDSHAKE."""
    frames = []
    for _name, _source, opcode, payload in HANDSHAKE:
        body = set_clock_payload(epoch) if payload is None else payload
        frames.append(command(opcode, body))
    return frames


def parse_heart_rate_measurement(data: bytes):
    """Bluetooth Heart Rate Measurement (0x2A37).

    Returns HR in bpm, RR in milliseconds (the SIG unit is 1/1024 s), and
    skin-contact when the flags say the feature is supported. ``contact``
    is None when the strap does not report it.
    """
    if not data:
        return None
    flags = data[0]
    i = 1
    if flags & 0x01:
        if i + 2 > len(data):
            return None
        hr = int.from_bytes(data[i:i + 2], "little")
        i += 2
    else:
        if i >= len(data):
            return None
        hr = data[i]
        i += 1
    contact = bool(flags & 0x04) if flags & 0x02 else None
    if flags & 0x08:                 # energy expended, not used
        i += 2
    rr_ms = []
    if flags & 0x10:
        while i + 1 < len(data):
            raw = int.from_bytes(data[i:i + 2], "little")
            rr_ms.append(raw * 1000 / 1024)
            i += 2
    return {"hr": hr, "rr_ms": rr_ms, "contact": contact, "unit": "1/1024s"}


def parse_agreed_realtime(inner: bytes):
    """Live custom frame (type 40 / 0x28). Offsets match NOOP's schema and OpenVitals.

    ``rr_raw`` is the u16 counts as they are on the wire. Their unit is not
    settled (see the module note). Heart rate is a u8 bpm in both sources.
    """
    if len(inner) < 10 or inner[0] != REALTIME_DATA:
        return None
    n = min(inner[9], 4, (len(inner) - 10) // 2)
    return {
        "ts": struct.unpack_from("<I", inner, 2)[0],
        "subsec": struct.unpack_from("<H", inner, 6)[0],
        "hr": inner[8],
        "rr_count": inner[9],
        "rr_raw": [struct.unpack_from("<H", inner, 10 + 2 * i)[0] for i in range(n)],
    }


def parse_agreed_history(inner: bytes):
    """Stored-history record, only the fields both public layouts share.

    Agreed for the version byte both call 24 (NOOP schema "24", OpenVitals
    ``parse_historical``): unix seconds at data offset 4, HR at 14, RR count
    at 15, RR counts at 16, and a 3-float accelerometer / gravity triplet at
    33 (frame offset 40). A second triplet at data offset 49 is present in
    both layouts; they do not agree on whether it repeats the first.

    Skin temperature, optical channels, steps and skin contact are omitted.
    The two layouts put different names on those bytes.
    """
    if len(inner) < 3 or inner[0] != HISTORICAL_DATA:
        return None
    out = {"version": inner[1], "cmd": inner[2]}
    data = inner[3:]
    if len(data) < 24:
        return out
    out["unix"] = struct.unpack_from("<I", data, 4)[0]
    out["hr"] = data[14]
    out["rr_count"] = data[15]
    n = min(data[15], 4, (len(data) - 16) // 2)
    out["rr_raw"] = [struct.unpack_from("<H", data, 16 + 2 * i)[0] for i in range(n)]
    if inner[1] == 24 and len(data) >= 45:
        out["accel_g"] = list(struct.unpack_from("<3f", data, 33))
    if inner[1] == 24 and len(data) >= 61:
        out["accel2_g"] = list(struct.unpack_from("<3f", data, 49))
    return out
