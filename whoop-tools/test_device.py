"""Device-boundary checks. No Bluetooth and no strap.

The known command hexes are the frames OpenVitals checked against captures.
They also fall out of the public CRC8/CRC32 envelope, so a match here means
the codec follows that envelope. It is not a new capture.
"""
import struct
import unittest

import device.discover as discover
import device.mg_live as mg
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


class Discovery(unittest.TestCase):
    """Diego's scan: a near MG named WHOOP DS, a far 4.0 named WHOOP GOAT.

    Addresses are not part of the decision. macOS rotates them.
    """

    DS = {
        "name": "WHOOP DS",
        "rssi": -57,
        "services": ["fd4b0001-cce1-4033-93ce-002d5875f58a"],
        "address": "near-rotating-id",
    }
    GOAT = {
        "name": "WHOOP GOAT",
        "rssi": -95,
        "services": [
            "0000180d-0000-1000-8000-00805f9b34fb",
            "61080001-8d6d-82b8-614a-1c8cb0f8dcc6",
        ],
        "address": "far-rotating-id",
    }

    def test_near_mg_is_chosen_over_the_distant_4(self):
        # GOAT is listed first. A first-match scan would pick it.
        chosen = discover.choose([self.GOAT, self.DS])
        self.assertEqual(chosen["name"], "WHOOP DS")
        self.assertEqual(chosen["family"], "mg")
        self.assertIsNone(discover.command_address(chosen))
        names = [rec["name"] for rec in discover.rank([self.GOAT, self.DS])]
        self.assertEqual(names, ["WHOOP DS", "WHOOP GOAT"])

    def test_stronger_rssi_is_the_rule(self):
        nearer_goat = dict(self.GOAT, rssi=-40)
        far_ds = dict(self.DS, rssi=-90)
        chosen = discover.choose([far_ds, nearer_goat])
        self.assertEqual(chosen["name"], "WHOOP GOAT")
        self.assertEqual(chosen["family"], "4.0")
        self.assertEqual(discover.command_address(chosen), "far-rotating-id")

    def test_name_whoop_matches_without_a_service(self):
        chosen = discover.choose([{"name": "Whoop", "rssi": -60, "services": []}])
        self.assertEqual(chosen["name"], "Whoop")
        self.assertIsNone(chosen["family"])
        self.assertIsNone(discover.command_address(chosen))

    def test_unrelated_device_is_ignored(self):
        self.assertIsNone(discover.choose([{
            "name": "Headphones",
            "rssi": -30,
            "services": ["0000180d-0000-1000-8000-00805f9b34fb"],
        }]))

    def test_missing_rssi_loses_to_a_reading(self):
        mute = dict(self.DS, rssi=None)
        chosen = discover.choose([mute, self.GOAT])
        self.assertEqual(chosen["name"], "WHOOP GOAT")


class MgLive(unittest.TestCase):
    """Read-only MG live path. No radio, and no write to fd4b0002."""

    def test_reads_info_and_prints_hr_in_sig_units(self):
        link = _Link()
        lines = []

        async def pause(_seconds):
            self.assertEqual(_seconds, 15)
            return None

        result = __import__("asyncio").run(
            mg.read_only_live(link, sleep=pause, emit=lines.append))
        self.assertEqual(link.writes, [])
        touched = link.reads + link.notifies
        self.assertNotIn(mg.FD4B_COMMAND, touched)
        self.assertTrue(all(not u.lower().startswith("fd4b") for u in touched))
        self.assertEqual(result["info"]["manufacturer"], "WHOOP")
        self.assertEqual(result["info"]["firmware"], "5.0.1")
        self.assertEqual(result["battery"], 80)
        self.assertEqual(result["samples"][0]["hr"], 61)
        self.assertEqual(result["samples"][0]["rr_raw"], [1024])
        self.assertEqual(result["samples"][0]["unit"], "1/1024s")
        self.assertIn("Manufacturer: WHOOP", lines)
        self.assertIn("Battery: 80%", lines)
        self.assertIn("HR  61 bpm  RR 1024 x 1/1024 s", lines)
        self.assertLess(lines.index("Battery: 80%"), lines.index("HR  61 bpm  RR 1024 x 1/1024 s"))

    def test_hr_without_rr_omits_the_interval(self):
        self.assertEqual(mg.format_hr({"hr": 55, "rr_raw": []}), "HR  55 bpm")

    def test_vendor_channel_is_refused(self):
        with self.assertRaises(RuntimeError):
            mg._forbid(mg.FD4B_COMMAND)

    def test_quiet_window_does_not_invent_a_sample(self):
        link = _Link(notes=[])
        lines = []
        result = __import__("asyncio").run(
            mg.read_only_live(link, sleep=_instant, emit=lines.append))
        self.assertEqual(result["samples"], [])
        self.assertTrue(any("No heart-rate" in line for line in lines))
        self.assertEqual(link.writes, [])


async def _instant(_seconds):
    return None


class _Link:
    def __init__(self, notes=None):
        self.reads = []
        self.notifies = []
        self.writes = []
        self.values = {
            "00002a29-0000-1000-8000-00805f9b34fb": b"WHOOP",
            "00002a24-0000-1000-8000-00805f9b34fb": b"MG",
            "00002a25-0000-1000-8000-00805f9b34fb": b"SN",
            "00002a26-0000-1000-8000-00805f9b34fb": b"5.0.1",
            "00002a27-0000-1000-8000-00805f9b34fb": b"rev",
            "00002a19-0000-1000-8000-00805f9b34fb": bytes([80]),
        }
        # flags: RR present, 8-bit HR 61, one interval of 1024 (1 second)
        self.notes = notes if notes is not None else [bytes([0x10, 61]) + struct.pack("<H", 1024)]

    async def read(self, uuid):
        self.reads.append(uuid)
        return self.values[uuid]

    async def start_notify(self, uuid, callback):
        self.notifies.append(uuid)
        for note in self.notes:
            callback(note)

    async def stop_notify(self, uuid):
        return None

    async def write(self, uuid, data):
        self.writes.append((uuid, data))
        raise AssertionError("MG live path must not write")


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
