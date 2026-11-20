"""Device-boundary checks. No Bluetooth and no strap.

The known command hexes are the frames OpenVitals checked against captures.
They also fall out of the public CRC8/CRC32 envelope, so a match here means
the codec follows that envelope. It is not a new capture.
"""
import struct
import unittest

import device.protocol as proto
import whoop


# OpenVitals selftest vectors (command frames).
VECTORS = {
    "HR on": (whoop.cmd(whoop.C_TOGGLE_HR, b"\x01"), "aa0800a823000e01d4c2477a"),
    "HR off": (whoop.cmd(whoop.C_TOGGLE_HR, b"\x00"), "aa0800a823000e0042f2400d"),
    "realtime on": (whoop.cmd(whoop.C_REALTIME_HR, b"\x01"), "aa0800a82300030199bce9cf"),
    "battery": (whoop.cmd(whoop.C_BATTERY, b"\x00"), "aa0800a823001a001725ee23"),
    "version": (whoop.cmd(whoop.C_VERSION, b"\x00"), "aa0800a8230007000b4982dc"),
    "hello": (whoop.cmd(whoop.C_HELLO_HARVARD, b"\x00"), "aa0800a823002300ada86a2d"),
    "getname": (whoop.cmd(whoop.C_GET_NAME, b"\x00"), "aa0800a823004c00c5df0fcf"),
    "drain": (whoop.cmd(whoop.C_SEND_HIST, b"\x00"), "aa0800a8230016001b6a5b8f"),
    "highfreq enter": (whoop.cmd(whoop.C_HIGHFREQ_ON), "aa07006b230060f340f888"),
    "abort": (whoop.cmd(whoop.C_ABORT_HIST), "aa07006b230014d6f590df"),
}


class Codec(unittest.TestCase):
    def test_public_envelope_matches_known_frames(self):
        for name, (frame, want) in VECTORS.items():
            self.assertEqual(frame.hex(), want, name)
            inners, left = proto.unframe(frame)
            self.assertEqual(left, b"")
            self.assertEqual(len(inners), 1)

    def test_opcodes_match_the_session_constants(self):
        pairs = (
            (proto.GET_HELLO_HARVARD, whoop.C_HELLO_HARVARD),
            (proto.SET_CLOCK, whoop.C_SET_CLOCK),
            (proto.GET_CLOCK, whoop.C_GET_CLOCK),
            (proto.TOGGLE_REALTIME_HR, whoop.C_REALTIME_HR),
            (proto.TOGGLE_GENERIC_HR_PROFILE, whoop.C_TOGGLE_HR),
            (proto.SEND_HISTORICAL_DATA, whoop.C_SEND_HIST),
            (proto.HISTORICAL_DATA_RESULT, whoop.C_HIST_ACK),
            (proto.GET_BATTERY_LEVEL, whoop.C_BATTERY),
            (proto.REPORT_VERSION_INFO, whoop.C_VERSION),
            (proto.GET_ADVERTISING_NAME_HARVARD, whoop.C_GET_NAME),
            (proto.ABORT_HISTORICAL_TRANSMITS, whoop.C_ABORT_HIST),
            (proto.COMMAND, whoop.T_COMMAND),
            (proto.REALTIME_DATA, whoop.T_REALTIME),
            (proto.HISTORICAL_DATA, whoop.T_HISTORICAL),
        )
        for public, local in pairs:
            self.assertEqual(public, local)

    def test_gatt_ids(self):
        self.assertEqual(whoop.WHOOP_SVC, proto.WHOOP4_SERVICE)
        self.assertEqual(whoop.CMD_CHAR, proto.CMD_CHAR)
        self.assertEqual((whoop.CH_RESP, whoop.CH_EVENT, whoop.CH_DATA), proto.NOTIFY_CHARS)
        self.assertTrue(proto.WHOOP5_SERVICE.startswith("fd4b0001-"))

    def test_bad_crc_is_dropped(self):
        frame = bytearray(whoop.cmd(whoop.C_BATTERY, b"\x00"))
        frame[-1] ^= 0xFF
        inners, left = proto.unframe(bytes(frame))
        self.assertEqual(inners, [])
        self.assertEqual(left, b"")


class Handshake(unittest.TestCase):
    def test_clock_is_eight_bytes(self):
        body = proto.set_clock_payload(1_700_000_000.5)
        self.assertEqual(len(body), 8)
        sec, sub = struct.unpack("<II", body)
        self.assertEqual(sec, 1_700_000_000)
        self.assertEqual(sub, (500 * 32768) // 1000)

    def test_handshake_follows_the_public_order_and_skips_destructive(self):
        frames = proto.connect_handshake(1_700_000_000)
        opcodes = []
        for frame in frames:
            (inner,), _ = proto.unframe(frame)
            self.assertEqual(inner[0], proto.COMMAND)
            opcodes.append(inner[2])
            self.assertNotIn(inner[2], proto.DESTRUCTIVE)
        self.assertEqual(opcodes, [step[2] for step in proto.HANDSHAKE])
        self.assertIn(proto.EXIT_HIGH_FREQ_SYNC, opcodes)
        self.assertNotIn(0x60, opcodes)          # ENTER_HIGH_FREQ_SYNC is not sent
        self.assertEqual(frames[0].hex(), "aa0800a823002300ada86a2d")   # hello
        self.assertEqual(frames[1].hex(), "aa0800a823004c00c5df0fcf")   # get name
        self.assertEqual(frames[-1].hex(), "aa0800a823000e01d4c2477a")  # standard HR on
        clock = proto.unframe(frames[2])[0][0]
        self.assertEqual(clock[2], proto.SET_CLOCK)
        self.assertEqual(len(clock) - 3, 8)      # inner minus type, seq, opcode

    def test_empty_get_clock_body(self):
        (inner,), _ = proto.unframe(proto.connect_handshake(0)[3])
        self.assertEqual(inner, bytes([proto.COMMAND, 0, proto.GET_CLOCK]))

    def test_destructive_opcode_is_refused(self):
        for opcode in proto.DESTRUCTIVE:
            with self.assertRaises(ValueError):
                proto.command(opcode, b"\x00")
            with self.assertRaises(ValueError):
                whoop.cmd(opcode, b"\x00")


class Parse(unittest.TestCase):
    def test_standard_hr_is_sig_units(self):
        # flags: 8-bit HR, contact supported+detected, RR present. HR 60. RR = 1024 → 1000 ms.
        data = bytes([0x16, 60]) + struct.pack("<H", 1024)
        sample = proto.parse_heart_rate_measurement(data)
        self.assertEqual(sample["hr"], 60)
        self.assertEqual(sample["rr_ms"], [1000])
        self.assertTrue(sample["contact"])
        self.assertEqual(sample["unit"], "1/1024s")

    def test_standard_hr_without_contact_feature(self):
        sample = proto.parse_heart_rate_measurement(bytes([0x00, 55]))
        self.assertEqual(sample["hr"], 55)
        self.assertIsNone(sample["contact"])
        self.assertEqual(sample["rr_ms"], [])

    def test_agreed_history_keeps_shared_fields_only(self):
        inner = whoop.history_record(77, 1_795_000_020, hr=52, rr=(1100,),
                                      accel=(0.5, -0.25, 0.75), optical=(601, 912),
                                      temp=343, steps=4321)
        agreed = proto.parse_agreed_history(inner)
        self.assertEqual(agreed["unix"], 1_795_000_020)
        self.assertEqual(agreed["hr"], 52)
        self.assertEqual(agreed["rr_raw"], [1100])
        self.assertEqual(agreed["accel_g"], [0.5, -0.25, 0.75])
        for key in ("optical", "skin_temp_raw", "steps_total", "skin_contact"):
            self.assertNotIn(key, agreed)
        # the pipeline parser still exposes the imported hypothesis
        legacy = whoop.parse_historical(inner)
        self.assertEqual(legacy["skin_temp_raw"], 343)
        self.assertEqual(legacy["steps_total"], 4321)

    def test_agreed_realtime_matches_offsets(self):
        inner = bytes([0x28, 0]) + struct.pack("<IH", 99, 7) + bytes([55, 1]) + struct.pack("<H", 1100)
        parsed = proto.parse_agreed_realtime(inner)
        self.assertEqual((parsed["ts"], parsed["subsec"], parsed["hr"], parsed["rr_raw"]), (99, 7, 55, [1100]))


class SessionGuard(unittest.TestCase):
    def test_send_refuses_a_reboot_frame(self):
        # Build the frame with the codec primitive, bypassing command()'s own check,
        # the way a bad caller might. send() must still refuse it before any write.
        frame = proto.build(proto.COMMAND, 0, 29, b"\x00")  # REBOOT_STRAP
        w = whoop.Whoop.__new__(whoop.Whoop)

        class Client:
            async def write_gatt_char(self, *a, **k):
                raise AssertionError("destructive frame was written")

        w.client = Client()
        with self.assertRaises(RuntimeError):
            import asyncio
            asyncio.run(w.send(frame))


if __name__ == "__main__":
    unittest.main()
