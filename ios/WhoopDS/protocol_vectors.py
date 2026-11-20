"""Lock the bytes this iPhone app is allowed to send and the fields it may name.

Sources, and only these, for anything that goes on the wire:

- Alec Jude Wilson, WHOOP 5.0 protocol playground, https://judes.club/experiments/whoop5/
  FLAGS array, command(), encodeFrame(), buildR22() offsets, ack note for type 0x31 subtype 0x02.
- Wilson, "Cracking the WHOOP 5.0 over Bluetooth",
  https://judes.club/writing/cracking-the-whoop-5-bluetooth-protocol/
  inner cursor [13:21], write-with-response, do not send SET_ALARM_TIME.
- NOOP docs/PROTOCOL_WHOOP5.md fixed client hello
  AA 01 08 00 00 01 E6 71 23 01 91 01 36 3E 5C 8D
- NOOP Whoop5ConfigTests golden enable_r22_packets frame (seq 1).
- NOOP docs/PROTOCOL_TRANSPORT.md format-1 framing, history body 01 + the original 8-byte
  END token, persist before that ack, no FORCE_TRIM / SET_READ_POINTER.
- NOOP docs/PROTOCOL_SENSORS.md packet 40, R18, R21, R26.
- Bluetooth SIG Heart Rate Measurement (0x2A37) and Battery Level (0x2A19), as Wilson
  also describes them.

Run: python3 ios/WhoopDS/protocol_vectors.py
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "WhoopDS" / "selfcheck.json"

PUBLISHED_HELLO = "AA0108000001E67123019101363E5C8D"
# Packages/WhoopProtocol/Tests/WhoopProtocolTests/Whoop5ConfigTests.swift
# testEnableR22PacketsGoldenFrame, seq = 1.
PUBLISHED_ENABLE_R22 = (
    "AA0130000001EB1123017801656E61626C655F7232325F7061636B657473"
    "00000000000000000000000000003200000000000000D2EEB0B7"
)

# judes.club/experiments/whoop5/ FLAGS array, in that order. enable_sig12 is not in it.
FLAGS = [
    ("enable_r22_packets", 0x32),
    ("enable_r22_v2_packets", 0x32),
    ("enable_r22_v3_packets", 0x32),
    ("enable_r22_v4_packets", 0x31),
    ("enable_r22_v5_packets", 0x32),
    ("enable_r22_v6_packets", 0x32),
    ("enable_r22_v8_packets", 0x32),
    ("make_hrfm_visible", 0x32),
    ("disable_pip_r26_packets", 0x32),
    ("wear_detect_bias", 0x32),
    ("hr_ch_switching", 0x32),
    ("ir_hw_switching", 0x32),
    ("enable_passive_strap_fit_gen5", 0x31),
    ("enable_sig11_during_sleep", 0x32),
    ("dorset_inhibit_wpt", 0x32),
]

# Decimal command numbers from NOOP whoop_protocol.json CommandNumber.
# Buzz, alarm, reboot, DFU, trim. History ack 0x17 is not in this set.
FORBIDDEN = {
    0x13,  # 19 RUN_HAPTIC_PATTERN_MAVERICK
    0x19,  # 25 FORCE_TRIM
    0x1D,  # 29 REBOOT_STRAP
    0x20,  # 32 POWER_CYCLE_STRAP
    0x21,  # 33 SET_READ_POINTER
    0x24,  # 36 START_FIRMWARE_LOAD
    0x25,  # 37 LOAD_FIRMWARE_DATA
    0x26,  # 38 PROCESS_FIRMWARE_IMAGE
    0x2D,  # 45 ENTER_BLE_DFU
    0x42,  # 66 SET_ALARM_TIME — Wilson: do not send, it buzzes
    0x44,  # 68 RUN_ALARM
    0x45,  # 69 DISABLE_ALARM
    0x4F,  # 79 RUN_HAPTICS_PATTERN
    0x53,  # 83 VERIFY_FIRMWARE_IMAGE
    0x8E,  # 142 START_FIRMWARE_LOAD_NEW
    0x8F,  # 143 LOAD_FIRMWARE_DATA_NEW
    0x90,  # 144 PROCESS_FIRMWARE_IMAGE_NEW
}

ALLOWED_CMDS = {0x91, 0x8D, 0x78, 0x22, 0x16, 0x17}


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def crc32(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xEDB88320 if crc & 1 else crc >> 1
    return (crc ^ 0xFFFFFFFF) & 0xFFFFFFFF


def encode_frame(inner: bytes) -> bytes:
    # Wilson encodeFrame: field defaults to 0x0100, stored little-endian as 00 01.
    # len counts field + crc16 + inner.
    declared = 2 + 2 + len(inner)
    header = bytes([0xAA, 0x01, declared & 0xFF, (declared >> 8) & 0xFF, 0x00, 0x01])
    c16 = crc16_modbus(header)
    body = header + bytes([c16 & 0xFF, (c16 >> 8) & 0xFF]) + inner
    c32 = crc32(inner)
    return body + struct.pack("<I", c32)


def command(cmd: int, seq: int, b3: int, payload: bytes = b"") -> bytes:
    if cmd in FORBIDDEN:
        raise ValueError(f"refusing forbidden command 0x{cmd:02X}")
    if cmd not in ALLOWED_CMDS:
        raise ValueError(f"command 0x{cmd:02X} is not in the published send set")
    inner = bytes([0x23, seq & 0xFF, cmd & 0xFF, b3 & 0xFF]) + payload
    pad = (4 - (len(inner) % 4)) % 4
    if pad:
        inner += bytes(pad)
    return encode_frame(inner)


def set_config_payload(name: str, value: int) -> bytes:
    raw = name.encode("ascii")
    if len(raw) > 32:
        raise ValueError(name)
    return raw + bytes(32 - len(raw)) + bytes([value & 0xFF]) + bytes(7)


def hx(data: bytes) -> str:
    return data.hex().upper()


def u16(frame: bytes, off: int) -> int:
    return frame[off] | (frame[off + 1] << 8)


def u32(frame: bytes, off: int) -> int:
    return struct.unpack_from("<I", frame, off)[0]


def f32(frame: bytes, off: int) -> float:
    return struct.unpack_from("<f", frame, off)[0]


def i16(frame: bytes, off: int) -> int:
    return struct.unpack_from("<h", frame, off)[0]


def rr_ms(ticks: int) -> int:
    # NOOP PROTOCOL_SENSORS.md: (ticks * 1000 + 512) // 1024. Zero is omitted by the caller.
    return (ticks * 1000 + 512) // 1024


def decode_hr_measurement(data: bytes) -> dict:
    # Bluetooth Heart Rate Measurement. Flag bit 0 = 16-bit HR, bit 3 = energy, bit 4 = RR.
    flags = data[0]
    i = 1
    if flags & 0x01:
        hr = data[i] | (data[i + 1] << 8)
        i += 2
    else:
        hr = data[i]
        i += 1
    if flags & 0x08:
        i += 2
    rr = []
    if flags & 0x10:
        while i + 1 < len(data):
            ticks = data[i] | (data[i + 1] << 8)
            i += 2
            if ticks:
                rr.append(rr_ms(ticks))
    out = {"hr": hr, "rrMs": rr}
    if flags & 0x04:
        out["contact"] = bool(flags & 0x02)
    return out


def verify(frame: bytes) -> bool:
    if len(frame) < 16 or frame[0] != 0xAA or frame[1] != 0x01:
        return False
    declared = frame[2] | (frame[3] << 8)
    if declared + 8 != len(frame):
        return False
    if crc16_modbus(frame[:6]) != u16(frame, 6):
        return False
    inner = frame[8:-4]
    return crc32(inner) == u32(frame, len(frame) - 4)


def decode_private(frame: bytes) -> dict:
    """Name a field only when a published WHOOP 5/MG layout maps it. Otherwise kind=raw."""
    if not verify(frame):
        return {"kind": "raw", "reason": "crc"}
    kind = "raw"
    fields: dict = {"packetType": frame[8]}
    cursor = None
    ptype = frame[8]
    layout = frame[9] if len(frame) > 9 else None
    end = len(frame) - 4

    if ptype == 36 and end >= 13:
        fields.update({
            "cmd": frame[10],
            "origin": frame[11],
            "result": frame[12],
        })
        return {"kind": "command-response", "fields": fields}

    if ptype == 0x31 and len(frame) >= 33 and frame[10] == 0x02 and 29 <= end:
        # Wilson: status type 0x31 subtype 0x02, cursor at inner[13:21] == frame[21:29].
        # NOOP metadata type 2 is HISTORY_END. Echo these eight bytes; do not rebuild them.
        cursor = frame[21:29]
        fields["cursor"] = hx(cursor)
        return {"kind": "history-end", "fields": fields, "cursor": hx(cursor)}

    if ptype == 0x31 and len(frame) > 10 and frame[10] == 0x01:
        return {"kind": "history-start", "fields": fields}
    if ptype == 0x31 and len(frame) > 10 and frame[10] == 0x03:
        return {"kind": "history-complete", "fields": fields}

    if ptype == 40 and end >= 18:
        # PROTOCOL_SENSORS.md packet 40, absolute format-1 offsets.
        n = frame[17]
        rr = []
        for i in range(n):
            off = 18 + 2 * i
            if off + 2 > end:
                break
            ticks = u16(frame, off)
            if ticks:
                rr.append(rr_ms(ticks))
        fields.update({"unix": u32(frame, 10), "hr": frame[16], "rrMs": rr})
        return {"kind": "packet40", "fields": fields}

    if ptype == 0x2F and len(frame) == 124 and layout == 18:
        # PROTOCOL_SENSORS.md R18, 124-byte summary. Offsets are absolute.
        fields.update({
            "unix": u32(frame, 15),
            "hr": frame[22],
            "rrCount": frame[23],
            "hr2": frame[37],
            "hrFlagsRaw": frame[36],
            "activityRaw": frame[63],
        })
        rr = []
        for i in range(min(frame[23], 4)):
            off = 24 + 2 * i
            if off + 2 > end:
                break
            ticks = u16(frame, off)
            if ticks:
                rr.append(rr_ms(ticks))
        fields["rrMs"] = rr
        dyn = f32(frame, 41)
        if dyn == dyn and 0 <= dyn <= 8:
            fields["dynamicAccelG"] = dyn
        grav = [f32(frame, 45), f32(frame, 49), f32(frame, 53)]
        if all(v == v for v in grav):
            fields["gravityG"] = grav
        skin_raw = u16(frame, 73)
        fields["skinTempRaw"] = skin_raw
        skin_c = skin_raw / 100
        if 5 <= skin_c <= 45:
            fields["skinTempC"] = skin_c
        for name, off in (("aux0", 69), ("aux1", 71)):
            raw = i16(frame, off)
            fields[name + "Raw"] = raw
            celsius = raw / 10
            if 0 <= celsius <= 60:
                fields[name + "C"] = celsius
        if frame[63] in (0, 1, 2):
            fields["activity"] = ("still", "walk", "run")[frame[63]]
        return {"kind": "r18", "fields": fields}

    inner_len = len(frame) - 12
    if (
        ptype == 0x2F
        and inner_len == 112
        and frame[10] in (0x80, 0x82)
        and layout != 18
    ):
        # Wilson buildR22: inner[7:11] timestamp, [14] HR, [29] HR2, floats at 37/41/45.
        # Frame offset = inner offset + 8.
        grav = [f32(frame, 45), f32(frame, 49), f32(frame, 53)]
        fields.update({
            "unix": u32(frame, 15),
            "hr": frame[22],
            "hr2": frame[37],
            "cmd": frame[10],
        })
        if all(v == v for v in grav):
            fields["accelG"] = grav
        return {"kind": "r22", "fields": fields}

    if ptype in (43, 47) and layout == 21 and len(frame) == 1244:
        # PROTOCOL_SENSORS.md R21. Scale is an earlier convention, so the fixture keeps raw counts.
        ac = u16(frame, 24)
        gc = u16(frame, 630)
        ac = min(ac, 100)
        gc = min(gc, 100)
        accel = []
        for i in range(ac):
            accel.append([i16(frame, 28 + 2 * i), i16(frame, 228 + 2 * i), i16(frame, 428 + 2 * i)])
        gyro = []
        for i in range(gc):
            gyro.append([i16(frame, 640 + 2 * i), i16(frame, 840 + 2 * i), i16(frame, 1040 + 2 * i)])
        fields.update({"unix": u32(frame, 15), "accelCount": ac, "gyroCount": gc, "accelRaw": accel, "gyroRaw": gyro})
        return {"kind": "r21", "fields": fields}

    if ptype == 47 and layout == 26 and len(frame) == 88:
        # R26 summary motion: physical conversion is not established. Keep the raw float only.
        motion = f32(frame, 75)
        fields.update({"unix": u32(frame, 15), "summaryMotionRaw": motion if motion == motion else None})
        return {"kind": "r26", "fields": fields}

    if ptype == 48 and end >= 16:
        # PROTOCOL_SENSORS.md: event number at byte 10, u32 timestamp at byte 12.
        fields.update({"event": frame[10], "unix": u32(frame, 12)})
        return {"kind": "event", "fields": fields}

    return {"kind": kind, "fields": fields, "cursor": cursor}


def build_r22(hr: int = 61, hr2: int = 62, accel=(0.0, 0.0, 1.0), unix: int = 1717200000, cmd: int = 0x80) -> bytes:
    inner = bytearray(112)
    inner[0] = 0x2F
    inner[2] = cmd
    struct.pack_into("<I", inner, 7, unix)
    inner[14] = hr
    inner[29] = hr2
    # A non-zero byte at the R18 skin-temp slot must NOT become a temperature on this packet.
    inner[65] = 0xFF
    inner[66] = 0xFF
    for off, value in zip((37, 41, 45), accel):
        struct.pack_into("<f", inner, off, value)
    return encode_frame(bytes(inner))


def build_r18() -> bytes:
    inner = bytearray(112)
    inner[0] = 0x2F
    inner[1] = 18
    struct.pack_into("<I", inner, 7, 1_700_000_000)  # frame 15
    inner[14] = 55  # frame 22 HR
    inner[15] = 1  # RR count
    struct.pack_into("<H", inner, 16, 1024)  # one R-R word, 1000 ms
    inner[28] = 0x80  # frame 36 flags, stored raw
    inner[29] = 56  # frame 37 alternate HR
    struct.pack_into("<f", inner, 33, 0.25)  # frame 41 dynamic
    struct.pack_into("<f", inner, 37, 0.0)
    struct.pack_into("<f", inner, 41, 0.0)
    struct.pack_into("<f", inner, 45, 1.0)
    inner[55] = 1  # frame 63 walk
    struct.pack_into("<h", inner, 61, 250)  # frame 69 aux 25.0 C
    struct.pack_into("<h", inner, 63, 100)  # frame 71 aux 10.0 C
    struct.pack_into("<H", inner, 65, 3325)  # frame 73 skin 33.25 C
    return encode_frame(bytes(inner))


def build_packet40() -> bytes:
    # type, seq, then the published fields. Pad the inner record to 4 bytes.
    inner = bytearray(24)
    inner[0] = 40
    inner[1] = 1
    struct.pack_into("<I", inner, 2, 1_700_000_100)  # frame 10
    inner[8] = 48  # frame 16 HR
    inner[9] = 2  # frame 17 count
    struct.pack_into("<H", inner, 10, 1024)
    struct.pack_into("<H", inner, 12, 512)  # 500 ms
    return encode_frame(bytes(inner))


def build_history_end(cursor: bytes) -> bytes:
    assert len(cursor) == 8
    inner = bytearray(24)
    inner[0] = 0x31
    inner[2] = 0x02
    inner[13:21] = cursor
    return encode_frame(bytes(inner))


def build_r21() -> bytes:
    inner = bytearray(1232)
    inner[0] = 47
    inner[1] = 21
    struct.pack_into("<I", inner, 7, 1_700_000_300)  # frame 15
    struct.pack_into("<H", inner, 16, 1)  # frame 24 accel count
    struct.pack_into("<h", inner, 20, 100)  # frame 28
    struct.pack_into("<h", inner, 220, 200)  # frame 228
    struct.pack_into("<h", inner, 420, 300)  # frame 428
    struct.pack_into("<H", inner, 622, 1)  # frame 630 gyro count
    struct.pack_into("<h", inner, 632, 1)  # frame 640
    struct.pack_into("<h", inner, 832, 2)  # frame 840
    struct.pack_into("<h", inner, 1032, 3)  # frame 1040
    return encode_frame(bytes(inner))


def build_r26() -> bytes:
    inner = bytearray(76)
    inner[0] = 47
    inner[1] = 26
    struct.pack_into("<I", inner, 7, 1_700_000_400)  # frame 15
    struct.pack_into("<f", inner, 67, 0.5)  # frame 75, unit not established
    return encode_frame(bytes(inner))


def build_event() -> bytes:
    inner = bytearray(8)
    inner[0] = 48
    inner[2] = 9  # WRIST_ON in the published EventNumber enum
    struct.pack_into("<I", inner, 4, 1_700_000_200)  # frame 12
    return encode_frame(bytes(inner))


def reassemble(chunks: list[bytes]) -> list[bytes]:
    buf = bytearray()
    out = []
    for chunk in chunks:
        buf += chunk
        while True:
            sof = buf.find(b"\xAA")
            if sof < 0:
                buf.clear()
                break
            if sof:
                del buf[:sof]
            if len(buf) < 8:
                break
            declared = buf[2] | (buf[3] << 8)
            total = declared + 8
            if total < 16 or total > 8192 or buf[1] != 0x01 or crc16_modbus(buf[:6]) != u16(buf, 6):
                del buf[:1]
                continue
            if len(buf) < total:
                break
            frame = bytes(buf[:total])
            del buf[:total]
            if verify(frame):
                out.append(frame)
    return out


def main() -> None:
    assert crc32(b"123456789") == zlib.crc32(b"123456789") & 0xFFFFFFFF

    hello = command(0x91, 1, 0x01)
    assert hx(hello) == PUBLISHED_HELLO, hx(hello)
    assert verify(hello)

    flag = command(0x78, 1, 0x01, set_config_payload("enable_r22_packets", 0x32))
    assert hx(flag) == PUBLISHED_ENABLE_R22, hx(flag)

    sent = []
    seq = 1
    sent.append(command(0x91, seq, 0x01))
    seq = (seq + 1) & 0xFF
    sent.append(command(0x8D, seq, 0x01))
    seq = (seq + 1) & 0xFF
    for name, value in FLAGS:
        sent.append(command(0x78, seq, 0x01, set_config_payload(name, value)))
        seq = (seq + 1) & 0xFF
    sent.append(command(0x22, seq, 0x00))
    range_seq = seq
    seq = (seq + 1) & 0xFF
    sent.append(command(0x16, seq, 0x00))
    for frame in sent:
        assert frame[8] == 0x23
        assert frame[10] not in FORBIDDEN
        assert frame[10] in ALLOWED_CMDS
    assert sent[0][10] == 0x91 and sent[0][11] == 0x01
    assert sent[-2][10] == 0x22 and sent[-2][11] == 0x00
    assert sent[-1][10] == 0x16 and sent[-1][11] == 0x00
    assert range_seq == 18  # seq 1 hello, 2 name, 3–17 flags, 18 range

    try:
        command(0x42, 1, 0x04)
    except ValueError:
        pass
    else:
        raise AssertionError("alarm command was built")
    for cmd in (0x19, 0x1D, 0x20, 0x21, 0x2D):
        try:
            command(cmd, 1, 0x00)
        except ValueError:
            pass
        else:
            raise AssertionError(f"forbidden 0x{cmd:02X} was built")

    r22 = build_r22()
    got = decode_private(r22)
    assert got["kind"] == "r22", got
    assert got["fields"]["hr"] == 61
    assert got["fields"]["hr2"] == 62
    assert got["fields"]["unix"] == 1717200000
    assert [round(v, 5) for v in got["fields"]["accelG"]] == [0.0, 0.0, 1.0]
    assert "skinTempC" not in got["fields"]
    assert "skinTempRaw" not in got["fields"]

    r18 = build_r18()
    got = decode_private(r18)
    assert got["kind"] == "r18", got
    assert got["fields"]["hr"] == 55
    assert got["fields"]["hr2"] == 56
    assert got["fields"]["rrMs"] == [1000]
    assert got["fields"]["skinTempRaw"] == 3325
    assert got["fields"]["skinTempC"] == 33.25
    assert got["fields"]["gravityG"] == [0.0, 0.0, 1.0]
    assert got["fields"]["dynamicAccelG"] == 0.25
    assert got["fields"]["activity"] == "walk"
    assert got["fields"]["aux0C"] == 25.0

    p40 = build_packet40()
    got = decode_private(p40)
    assert got["kind"] == "packet40"
    assert got["fields"]["hr"] == 48
    assert got["fields"]["rrMs"] == [1000, 500]

    hr = decode_hr_measurement(bytes([0x10, 60, 0x00, 0x04]))
    assert hr == {"hr": 60, "rrMs": [1000]}
    hr = decode_hr_measurement(bytes([0x14, 60, 0x00, 0x04]))
    assert hr["contact"] is False
    hr = decode_hr_measurement(bytes([0x16, 60, 0x00, 0x04]))
    assert hr["contact"] is True

    cursor = bytes([1, 2, 3, 4, 5, 6, 7, 8])
    end = build_history_end(cursor)
    got = decode_private(end)
    assert got["kind"] == "history-end"
    assert got["cursor"] == hx(cursor)
    ack = command(0x17, 9, 0x01, cursor)
    assert ack[8:12] == bytes([0x23, 9, 0x17, 0x01])
    assert cursor in ack
    assert 0x19 not in ack[8:12]

    # A start marker is not an ack, even if the cursor slot is filled.
    start = bytearray(build_history_end(cursor))
    # inner[2] is frame[10]. Flip subtype to HISTORY_START and fix the CRC.
    start[10] = 0x01
    inner = bytes(start[8:-4])
    start = bytearray(encode_frame(inner))
    assert decode_private(bytes(start))["kind"] == "history-start"

    r21 = decode_private(build_r21())
    assert r21["kind"] == "r21", r21["kind"]
    assert r21["fields"]["accelRaw"] == [[100, 200, 300]]
    assert r21["fields"]["gyroRaw"] == [[1, 2, 3]]
    assert "accelG" not in r21["fields"]

    r26 = decode_private(build_r26())
    assert r26["kind"] == "r26"
    assert r26["fields"]["summaryMotionRaw"] == 0.5
    assert "hr" not in r26["fields"]

    event = decode_private(build_event())
    assert event["kind"] == "event"
    assert event["fields"]["event"] == 9
    assert event["fields"]["unix"] == 1_700_000_200

    parts = reassemble([hello[:5], hello[5:]])
    assert parts == [hello]
    assert reassemble([b"\x00\x01", hello]) == [hello]

    # Skin temp outside the published 5–45 °C window stays raw.
    cold = bytearray(build_r18())
    inner = bytearray(cold[8:-4])
    struct.pack_into("<H", inner, 65, 100)  # 1.00 °C
    cold = encode_frame(bytes(inner))
    got = decode_private(cold)
    assert got["fields"]["skinTempRaw"] == 100
    assert "skinTempC" not in got["fields"]

    fixture = {
        "hello": hx(hello),
        "enableR22Seq1": hx(flag),
        "flags": [{"name": n, "value": v} for n, v in FLAGS],
        "forbidden": sorted(FORBIDDEN),
        "cases": [
            {"name": "r22", "frame": hx(r22), "kind": "r22", "hr": 61, "hr2": 62, "unix": 1717200000, "accelG": [0, 0, 1], "forbid": ["skinTempC", "skinTempRaw"]},
            {"name": "r18", "frame": hx(r18), "kind": "r18", "hr": 55, "hr2": 56, "rrMs": [1000], "skinTempC": 33.25, "skinTempRaw": 3325, "gravityG": [0, 0, 1], "dynamicAccelG": 0.25, "activity": "walk"},
            {"name": "packet40", "frame": hx(p40), "kind": "packet40", "hr": 48, "rrMs": [1000, 500]},
            {"name": "history-end", "frame": hx(end), "kind": "history-end", "cursor": hx(cursor)},
            {"name": "history-start", "frame": hx(bytes(start)), "kind": "history-start"},
            {"name": "event", "frame": hx(build_event()), "kind": "event", "event": 9, "unix": 1700000200},
            {"name": "r21", "frame": hx(build_r21()), "kind": "r21", "accelRaw": [[100, 200, 300]], "gyroRaw": [[1, 2, 3]], "forbid": ["accelG"]},
            {"name": "r26", "frame": hx(build_r26()), "kind": "r26", "summaryMotionRaw": 0.5, "forbid": ["hr", "skinTempC"]},
            {"name": "hr", "hrBytes": "10003C0004", "hr": 60, "rrMs": [1000]},
            {"name": "battery", "batteryByte": "40", "battery": 64},
        ],
        "ack": {"seq": 9, "cursor": hx(cursor), "frame": hx(ack)},
        "helloSplit": [hx(hello[:5]), hx(hello[5:])],
    }
    FIXTURE.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
    print(f"ok {FIXTURE} hello={hx(hello)} flags={len(FLAGS)} frames={len(sent)}")


if __name__ == "__main__":
    main()
