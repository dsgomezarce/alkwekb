#!/usr/bin/env python3
"""
whoop.py — WHOOP 4.0 session client used by the daemon and the collector.

The frame codec, GATT ids, and connect handshake live in ``device/`` (the
hardware boundary). That module follows the public NOOP / OpenStrap notes:
8-byte SET_CLOCK, exit high-freq sync rather than enter, and it refuses the
destructive opcodes. This file still parses the imported OpenVitals history
layout for the processing pipeline. Fields those notes disagree on are called
out on ``parse_historical`` and are not re-checked without a strap.

CLI:  whoop.py info | live | drain
      whoop.py buzz [pattern] [loops]            vibrate now
      whoop.py alarm 07:00 [once] | off | show   silent alarm (stored on the strap)
"""
import asyncio, struct, sys, time, csv, json, os, datetime as dt
from bleak import BleakClient, BleakScanner

from device.discover import command_address, rank
from device.mg_live import (
    BOND_WAIT, LISTEN_SECONDS, LIVE_SECONDS, PAIR_HINT,
    bond_then_listen, read_only_listen, read_only_live,
)
from device.protocol import (
    BATTERY_LEVEL, CMD_CHAR as _CMD, DESTRUCTIVE, HR_MEASUREMENT, NOTIFY_CHARS,
    OPTIONAL_NOTIFY, WHOOP4_SERVICE, build, connect_handshake, crc8,
    parse_heart_rate_measurement, set_clock_payload, unframe,
)

WHOOP_SVC = WHOOP4_SERVICE
BASE = "6108{:04x}-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_CHAR = _CMD
CH_RESP, CH_EVENT, CH_DATA = NOTIFY_CHARS
CH_7 = OPTIONAL_NOTIFY
HR_CHAR = HR_MEASUREMENT
# Standard Battery Level. OpenVitals saw a different number here than GetBatteryLevel
# and guessed the slide-on pack. That guess is not proven. NOOP reads the same
# characteristic as a single battery percent and does not assign it to the pack.
PACK_CHAR = BATTERY_LEVEL
HERE = os.path.dirname(os.path.abspath(__file__))

# Hard limits on every Bluetooth call. Bleak waits on CoreBluetooth callbacks with no
# timeout of its own in several places (a write, a disconnect, cancelling a connect
# attempt). After the Mac sleeps, such a callback can simply never come, and the caller
# would wait forever. With these limits a stuck call raises TimeoutError instead.
CONNECT_TIMEOUT = 45      # connect + subscribe to the four notify channels
WRITE_TIMEOUT = 10
DISCONNECT_TIMEOUT = 10

# ---- packet types ----
T_COMMAND, T_CMD_RESP, T_REALTIME = 0x23, 0x24, 0x28
T_HISTORICAL, T_EVENT, T_METADATA, T_CONSOLE = 0x2F, 0x30, 0x31, 0x32
# ---- command opcodes ----
C_HELLO_HARVARD, C_VERSION, C_TOGGLE_HR = 0x23, 0x07, 0x0E
C_REALTIME_HR, C_SET_CLOCK, C_GET_CLOCK = 0x03, 0x0A, 0x0B
C_ABORT_HIST, C_SEND_HIST, C_HIST_ACK = 0x14, 0x16, 0x17
C_BATTERY, C_GET_NAME, C_HIGHFREQ_ON = 0x1A, 0x4C, 0x60
# Haptics and the strap's own alarm. Opcode numbers and payloads come from community
# reverse-engineering (sources and results in README "Silent alarm") and were then tried on
# a real WHOOP 4.0. STOP_HAPTICS is answered with status 3 and does not stop a buzz.
C_SET_ALARM, C_GET_ALARM, C_RUN_ALARM, C_DISABLE_ALARM = 0x42, 0x43, 0x44, 0x45   # 66..69
C_RUN_HAPTICS, C_STOP_HAPTICS = 0x4F, 0x7A                                         # 79, 122
# ---- event ids (inner[2] of a type 0x30 EVENT frame) ----
E_ALARM_SET, E_ALARM_FIRED, E_APP_ALARM, E_ALARM_DISABLED = 56, 57, 58, 59
E_HAPTICS_FIRED, E_HAPTICS_ENDED = 60, 100
EVENT_NAMES = {E_ALARM_SET: "alarm set", E_ALARM_FIRED: "alarm fired (strap)",
               E_APP_ALARM: "alarm fired (app)", E_ALARM_DISABLED: "alarm disabled",
               E_HAPTICS_FIRED: "haptics fired", E_HAPTICS_ENDED: "haptics ended"}


def cmd(opcode: int, data: bytes = b"", seq: int = 0) -> bytes:
    if opcode in DESTRUCTIVE:
        raise ValueError(f"opcode {opcode} is on NOOP's do-not-send list")
    return build(T_COMMAND, seq, opcode, data)


def parse_realtime(inner: bytes):
    # inner = [0x28][ver][ts u32 LE @2][subsec u16 @6][hr @8][rrN @9][4×u16 RR @10..18]
    # Offsets match NOOP's REALTIME_DATA schema and this file's older parser.
    # OpenVitals reads the custom RR counts as whole milliseconds (60000/mean
    # matched the strap's HR on their capture). NOOP does not publish that unit.
    # The standard 0x2A37 path (device.parse_heart_rate_measurement) is 1/1024 s.
    # This fork has not repeated the millisecond check. The key stays rr_ms so
    # the pipeline that already assumes milliseconds keeps working.
    if len(inner) < 10:
        return None
    ts = struct.unpack_from("<I", inner, 2)[0]
    hr = inner[8]
    n = min(inner[9], 4, (len(inner) - 10) // 2)      # never read past a short frame
    rr = [struct.unpack_from("<H", inner, 10 + 2 * i)[0] for i in range(n)]
    return {"ts": ts, "hr": hr, "rr_ms": rr}


HIST_V24_LEN = 93       # payload bytes after [type][version][cmd] in a version-24 record
OPTICAL_FLOOR = 547     # what the two optical levels (d61, d65) read with nothing on the sensor


def parse_historical(inner: bytes):
    """One stored-history record. inner=[0x2F][ver][cmd][data...]; data=inner[3:]; ts = Unix time.

    Version 24 (93 data bytes), checked against two stored nights (see decode.py):
      0 seq u32 (+1 per record, also across the time the strap was off the wrist)
      4 ts u32, 14 hr, 15 RR count, 16 4x RR u16 (ms)        - hr and RR match the live stream
      33 accel x,y,z f32 in g (49-60 repeat them)              - movement matches sleep/walks
      61 optical level A u16, 65 optical level B u16           - both fall to ~547 (no light)
         the second the strap comes off; which LED/wavelength each is, is NOT known
      69 skin temperature u16, raw                              - slow: climbs ~30 counts over
         20 minutes after putting the strap on, highest asleep; one paired night says ~0.1 C/count
      85 step counter u16, cumulative                          - +100-110 per minute walking,
         flat when still; it resets now and then (the WHOOP app reset it at sync)
    Not fields: d48 (the top byte of a float at d45, NOT skin contact), d73-76 (constant).
    Other versions only get seq/ts; their layout differs (25 = a 24 Hz optical burst).

    Vitals DS: HR, unix time, RR counts and the float triplet at data offset 33
    sit on the same offsets as NOOP's version-24 schema. Skin temperature,
    the two optical words, the step counter and "not skin contact" do not —
    NOOP names other quantities on those bytes (and puts skin temp at frame
    offset 72, which is this layout's second optical word). Those fields stay
    so sleep, steps and the nightly temperature hypothesis keep running.
    They are the imported layout, not a measurement this fork repeated.
    """
    d = inner[3:]
    if len(d) < 24:
        return {"ver": inner[1], "raw": d.hex()}
    rec = {
        "ver": inner[1],
        "seq": struct.unpack_from("<I", d, 0)[0],
        "ts": struct.unpack_from("<I", d, 4)[0],
        "hr": d[14],
        "rr_count": d[15],
        "rr_ms": [struct.unpack_from("<H", d, 16 + 2 * i)[0] for i in range(4)],
    }
    if inner[1] == 24 and len(d) >= HIST_V24_LEN:
        rec["accel_g"] = list(struct.unpack_from("<3f", d, 33))
        rec["optical"] = [struct.unpack_from("<H", d, 61)[0], struct.unpack_from("<H", d, 65)[0]]
        rec["skin_temp_raw"] = struct.unpack_from("<H", d, 69)[0]
        rec["steps_total"] = struct.unpack_from("<H", d, 85)[0]
    return rec


def parse_battery(inner: bytes):
    """Battery % from the strap's reply to GetBatteryLevel (0x1A), or None.

    Reply data (after type, seq, opcode): 2 bytes, then the fuel gauge in tenths of a
    percent as u16 LE (seen: 00 01 e8 03 = 100.0% on the charger). This is the strap's
    reading right now, unlike console "SOC report" lines, which a history drain replays
    from weeks or months back.
    """
    d = inner[3:]
    if len(d) < 4:
        return None
    tenths = struct.unpack_from("<H", d, 2)[0]
    return tenths / 10 if tenths <= 1000 else None


def history_record(seq, ts, hr=0, rr=(), accel=(0.0, 0.0, 1.0), optical=(600, 850), temp=330, steps=0, cmd=5):
    """Build a version-24 stored-history record (inner bytes) with the fields above. For tests."""
    d = bytearray(HIST_V24_LEN)
    struct.pack_into("<II", d, 0, seq, ts)
    d[14], d[15] = hr, len(rr)
    struct.pack_into("<4H", d, 16, *(list(rr) + [0, 0, 0, 0])[:4])
    struct.pack_into("<3f", d, 33, *accel)
    struct.pack_into("<3f", d, 49, *accel)
    struct.pack_into("<H", d, 61, optical[0])
    struct.pack_into("<H", d, 65, optical[1])
    struct.pack_into("<H", d, 69, temp)
    struct.pack_into("<H", d, 85, steps)
    return bytes([T_HISTORICAL, 24, cmd]) + bytes(d)


def haptics_payload(pattern: int = 2, loops: int = 1) -> bytes:
    """RUN_HAPTICS_PATTERN body: [pattern id][loops][0][0][0]. Pattern 2 is the app's alarm buzz."""
    if not (0 <= pattern <= 255 and 1 <= loops <= 5):
        raise ValueError("pattern must be 0-255 and loops 1-5")
    return bytes([pattern, loops, 0, 0, 0])


def alarm_payload(epoch: float) -> bytes:
    """SET_ALARM_TIME body, revision 1: [0x01][u32 unix s][u16 1/32768 s][u16 buzz mode 0]."""
    sec = int(epoch)
    sub = int((epoch - sec) * 32768) & 0xFFFF
    return struct.pack("<BIHH", 1, sec, sub, 0)


def parse_response(inner: bytes):
    """A COMMAND_RESPONSE: [0x24][seq][opcode][0][status][body...]. status 1 = OK."""
    if len(inner) < 5:
        return None
    return {"op": inner[2], "status": inner[4], "ok": inner[4] == 1, "body": bytes(inner[5:])}


def parse_alarm(body: bytes):
    """GET_ALARM_TIME reply body: [revision][armed flag][u32 unix seconds][u16 subsec].

    Seen on a real WHOOP 4.0 (2026-11-19): after arming 1795080778 the body was
    01 01 4a c2 fe 6a ..; after DISABLE_ALARM, and after the alarm rang, it read as not armed.
    Returns the armed time (unix seconds), or None when nothing is armed or the form is unknown.
    """
    if len(body) >= 6 and body[0] in (1, 4):
        t = struct.unpack_from("<I", body, 2)[0]
        return t if body[1] == 1 and t else None
    return None


def parse_metadata(inner: bytes):
    """History metadata: subtype 1 start, 2 end of a chunk (HISTORY_END), 3 complete.

    HISTORY_END data: 0 unix u32, 4 subsec u16, 6 unmapped u32, 10 the 8-byte ack block.
    The block's first u32 is the trim cursor; the second (constant 22 on this strap so far)
    is write-wrap state. The block goes back to the strap exactly as received.
    """
    sub = inner[2]
    d = inner[3:]
    trim = struct.unpack_from("<I", d, 10)[0] if len(d) >= 14 else None
    ack = bytes(d[10:18]) if len(d) >= 18 else None
    return {"subtype": sub, "trim": trim, "ack": ack}


class Whoop:
    def __init__(self, address):
        self.address = address
        self.client = BleakClient(address, timeout=20.0)
        self.buffers = {CH_RESP: b"", CH_EVENT: b"", CH_DATA: b"", CH_7: b""}
        self.on_realtime = None
        self.on_historical = None
        self.on_meta = None
        self.on_battery = None        # called with the battery % when the strap answers 0x1A
        self.raw_sink = None          # optional: called with (channel, inner) for every frame
        self.verbose = True           # print every response/event (the daemon turns this off)
        self._hist_done = asyncio.Event()
        self.on_event = None          # optional: called with (event id, inner) for every EVENT
        self.on_standard_hr = None    # optional: Bluetooth 0x2A37 sample (RR already in ms)
        self._waiting = {}            # opcode -> Future for that command's next response

    async def __aenter__(self):
        try:
            await asyncio.wait_for(self._connect(), CONNECT_TIMEOUT)
        except BaseException:
            await self.__aexit__()     # don't leave a half-open link behind
            raise
        return self

    async def _connect(self):
        await self.client.connect()
        for ch in (CH_RESP, CH_EVENT, CH_DATA):
            await self.client.start_notify(ch, self._mk(ch))
        present = set()
        services = getattr(self.client, "services", None)
        if services:
            for service in services:
                for char in service.characteristics:
                    present.add(str(char.uuid).lower())
        # …0007 is a 5.0 characteristic in NOOP. Subscribe only when this strap has it
        # (or when the table could not be read — then a missing char is ignored).
        if not present or CH_7.lower() in present:
            try:
                await self.client.start_notify(CH_7, self._mk(CH_7))
            except Exception:
                if self.verbose:
                    print("  (no …0007 notify)")
        try:
            await self.client.start_notify(HR_CHAR, self._on_std_hr)
        except Exception:
            if self.verbose:
                print("  (standard heart-rate notify unavailable)")

    def _on_std_hr(self, _, data):
        sample = parse_heart_rate_measurement(bytes(data))
        if sample and self.on_standard_hr:
            self.on_standard_hr(sample)

    async def __aexit__(self, *a):
        try:
            await asyncio.wait_for(self.client.disconnect(), DISCONNECT_TIMEOUT)
        except Exception:
            pass

    def _mk(self, ch):
        def handler(_, data):
            self.buffers[ch] += bytes(data)
            inners, self.buffers[ch] = unframe(self.buffers[ch])
            for inner in inners:
                self._dispatch(ch, inner)
        return handler

    def _dispatch(self, ch, inner):
        if len(inner) < 3:            # too short to carry a type, sequence and command
            return
        if self.raw_sink:
            self.raw_sink(ch, inner)
        t = inner[0]
        if t == T_REALTIME and self.on_realtime:
            r = parse_realtime(inner)
            if r:
                self.on_realtime(r)
        elif t == T_HISTORICAL and self.on_historical:
            self.on_historical(parse_historical(inner))
        elif t == T_METADATA:
            m = parse_metadata(inner)
            if self.on_meta:
                self.on_meta(m)
            if m["subtype"] == 2 and m["trim"] is not None:   # HISTORY_END -> ACK
                asyncio.create_task(self._ack(m["trim"], m["ack"]))
            if m["subtype"] == 3:                              # COMPLETE
                self._hist_done.set()
        elif t == T_CMD_RESP:
            if inner[2] == C_BATTERY and self.on_battery:
                pct = parse_battery(inner)
                if pct is not None:
                    self.on_battery(pct)
            fut = self._waiting.pop(inner[2], None)
            if fut and not fut.done():
                fut.set_result(parse_response(inner))
            if self.verbose:
                print(f"  [resp op=0x{inner[2]:02x}] {inner[3:].hex(' ')}")
        elif t == T_EVENT:
            if self.on_event:
                self.on_event(inner[2], inner)
            if self.verbose:
                name = EVENT_NAMES.get(inner[2], "")
                print(f"  [event #{inner[2]}{' ' + name if name else ''}] {inner[3:].hex(' ')}")

    async def send(self, frame):
        inners, _ = unframe(frame)
        for inner in inners:
            if len(inner) >= 3 and inner[0] == T_COMMAND and inner[2] in DESTRUCTIVE:
                raise RuntimeError(
                    f"refusing opcode 0x{inner[2]:02x}: NOOP lists it as destructive; this engine does not send it")
        await asyncio.wait_for(self.client.write_gatt_char(CMD_CHAR, frame, response=False),
                               WRITE_TIMEOUT)

    async def request(self, opcode, data=b"", timeout=3.0):
        """Send a command and wait for its COMMAND_RESPONSE. Returns parse_response(), or
        None if the strap did not answer within `timeout` seconds."""
        fut = asyncio.get_running_loop().create_future()
        self._waiting[opcode] = fut
        try:
            await self.send(cmd(opcode, data))
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            if self._waiting.get(opcode) is fut:
                del self._waiting[opcode]

    # ---- haptics and the strap's alarm ----
    async def buzz(self, pattern=2, loops=1):
        """Vibrate now. Pattern 2 is the buzz the WHOOP app uses for its alarm."""
        return await self.request(C_RUN_HAPTICS, haptics_payload(pattern, loops))

    async def stop_buzz(self):
        """Ask the strap to stop a running vibration. NOT WORKING on the tested firmware: the
        strap answers status 3 and keeps buzzing (2026-11-19). Kept for experiments."""
        return await self.request(C_STOP_HAPTICS, b"\x00")

    async def set_alarm(self, epoch):
        """Arm the strap's own alarm for `epoch` (unix seconds). The strap rings by itself at
        that time, connected or not, and then clears it (one-shot). The strap clock is set
        first so both sides agree on what that time is."""
        await self.set_time()
        return await self.request(C_SET_ALARM, alarm_payload(epoch))

    async def get_alarm(self):
        """The armed alarm time (unix seconds), or None. Raises TimeoutError if no answer."""
        r = await self.request(C_GET_ALARM, b"\x01")
        if r is None:
            raise TimeoutError("strap did not answer GET_ALARM_TIME")
        return parse_alarm(r["body"]) if r["ok"] else None

    async def disable_alarm(self):
        return await self.request(C_DISABLE_ALARM, b"\x01")

    async def run_alarm(self):
        """Ring the alarm now (the strap's full alarm buzz, until stopped or it times out)."""
        return await self.request(C_RUN_ALARM, b"\x01")

    async def _ack(self, trim, block=None):
        # Echo the strap's own 8-byte block (trim cursor + wrap state). Rebuilding it as
        # (trim, 0) also worked, but the wrap word is the strap's to keep, not ours to zero.
        body = block if block and len(block) == 8 else struct.pack("<II", trim, 0)
        await self.send(cmd(C_HIST_ACK, b"\x01" + body))

    async def init(self):
        """Public connect handshake (device.protocol.connect_handshake).

        Replaces the imported sequence (9-byte clock, then EnterHighFreqSync).
        NOOP and OpenStrap both use an 8-byte clock; NOOP exits high-freq sync
        instead of entering it. Not re-run on a strap in this fork.
        """
        for frame in connect_handshake(time.time()):
            await self.send(frame)

    async def set_time(self):
        await self.send(cmd(C_SET_CLOCK, set_clock_payload(time.time())))

    async def battery(self):
        await self.send(cmd(C_BATTERY, b"\x00"))

    async def pack_battery(self):
        """Battery Level characteristic (likely the battery pack), 0-100, or None."""
        v = await asyncio.wait_for(self.client.read_gatt_char(PACK_CHAR), WRITE_TIMEOUT)
        return v[0] if v and v[0] <= 100 else None

    async def version(self):
        await self.send(cmd(C_VERSION, b"\x00"))

    async def hr_broadcast(self, on=True):
        await self.send(cmd(C_TOGGLE_HR, bytes([1 if on else 0])))

    async def realtime(self, on=True):
        await self.send(cmd(C_REALTIME_HR, bytes([1 if on else 0])))

    async def drain(self, timeout=90):
        self._hist_done.clear()
        await self.send(cmd(C_SEND_HIST, b"\x00"))
        try:
            await asyncio.wait_for(self._hist_done.wait(), timeout)
        except asyncio.TimeoutError:
            if self.verbose:
                print("  (drain timed out — may just mean no/!more stored data)")
            return False
        return True


# ------------------------- CLI -------------------------
def _advert_record(address, dev, adv):
    services = list(adv.service_uuids or [])
    services.extend(getattr(adv, "service_data", {}).keys())
    return {
        "name": dev.name or adv.local_name or "",
        "address": dev.address or address,
        "rssi": adv.rssi,
        "services": services,
    }


async def scan(timeout=15.0):
    """Nearby strap candidates, nearest first. Does not connect."""
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    records = [_advert_record(addr, dev, adv) for addr, (dev, adv) in found.items()]
    return rank(records)


async def discover(timeout=15.0):
    """Nearest candidate (4.0, 5.0/MG, or a WHOOP name), or None."""
    ranked = await scan(timeout)
    return ranked[0] if ranked else None


async def find(timeout=15.0):
    """Address for the 4.0 command path only.

    When the nearest strap is 5.0/MG, returns None instead of a weaker 4.0
    that happens to be in range. The MG command set is not implemented, and
    the 4.0 handshake must not be written to fd4b.
    """
    return command_address(await discover(timeout))


def _show_choice(ranked):
    for rec in ranked:
        role = "selected" if rec is ranked[0] else "skipped "
        svcs = ",".join(rec.get("services") or []) or "-"
        print(f"  {role}  rssi {rec.get('rssi')}  {rec.get('name') or '?'}  {svcs}")
    chosen = ranked[0]
    family = chosen.get("family")
    if family == "mg":
        print("Nearest strap advertises fd4b0001 (WHOOP 5.0/MG).")
        print("The 4.0 handshake is not sent, and fd4b0002 is not written.")
    elif family == "4.0":
        print("Nearest strap advertises 61080001 (WHOOP 4.0).")
    else:
        print("Nearest strap matched the name WHOOP. GATT will decide before any command.")


async def read_only_gatt(address):
    """Connect, print service and characteristic UUIDs, write nothing.

    Returns ``"mg"``, ``"4.0"``, or ``"unknown"`` from the services actually
    discovered. No notify subscription and no command frame.
    """
    print("Connecting read-only. No command is written.")
    async with BleakClient(address, timeout=20.0) as client:
        services = list(client.services or [])
        if not services:
            print("GATT table was empty.")
            return "unknown"
        kind = "unknown"
        for service in services:
            uid = str(service.uuid).lower()
            print(f"SERVICE {service.uuid}")
            if uid.startswith("fd4b0001"):
                kind = "mg"
            elif uid.startswith("61080001") and kind != "mg":
                kind = "4.0"
            for char in service.characteristics:
                props = ",".join(char.properties)
                print(f"  CHAR {char.uuid} [{props}]")
        if kind == "mg":
            print("Service fd4b0001 is present. MG commands are not implemented.")
            print("Stopped. Nothing was drained, and no alarm or buzz was sent.")
        elif kind == "4.0":
            print("Service 61080001 is present.")
        else:
            print("No WHOOP command service in this GATT table. Stopped.")
        return kind


class _BleakReadOnly:
    """Reads and notifies. Has no write method, so the MG live path cannot send one."""

    def __init__(self, client):
        self.client = client

    async def read(self, uuid):
        return bytes(await self.client.read_gatt_char(uuid))

    async def start_notify(self, uuid, callback):
        def handler(_sender, data):
            callback(bytes(data))
        await self.client.start_notify(uuid, handler)

    async def stop_notify(self, uuid):
        await self.client.stop_notify(uuid)

    async def pair(self):
        await self.client.pair()


async def run_mg_live(address, seconds=LIVE_SECONDS):
    """Device-info, battery, and ~15s of standard HR. No vendor writes."""
    print("WHOOP 5.0/MG read-only live.")
    print("Reading device info and battery, then 2A37. fd4b0002 is not written.")
    print("No 4.0 handshake, drain, buzz, or alarm.")
    async with BleakClient(address, timeout=20.0) as client:
        return await read_only_live(_BleakReadOnly(client), seconds=seconds, emit=print)


async def run_mg_listen(address, seconds=LISTEN_SECONDS):
    """Subscribe to the fd4b notify characteristics and 2A37. No writes."""
    print("WHOOP 5.0/MG read-only listen.")
    async with BleakClient(address, timeout=20.0) as client:
        return await read_only_listen(_BleakReadOnly(client), seconds=seconds, emit=print)


async def run_mg_bond(address, seconds=LISTEN_SECONDS):
    """OS pair, then the same read-only 20s listen. No vendor writes."""
    print("WHOOP 5.0/MG bond, then read-only listen.")
    print(PAIR_HINT)
    print("fd4b0002 is not written. No handshake, drain, buzz, alarm, or history ack.")
    async with BleakClient(address, timeout=20.0) as client:
        return await bond_then_listen(_BleakReadOnly(client), seconds=seconds, emit=print)


async def _mg_command(addr, mode):
    """MG entry. live reads standard GATT. listen only subscribes. drain is refused."""
    if mode == "drain":
        print("Refusing drain on a WHOOP 5.0/MG.")
        print("Stored history stays on the strap. fd4b0002 is not written.")
        return
    if mode == "live":
        try:
            await asyncio.wait_for(run_mg_live(addr), CONNECT_TIMEOUT + LIVE_SECONDS)
        except Exception as ex:
            print(f"read-only live failed: {ex}")
            print("Stopped. The 4.0 handshake was not sent.")
        return
    if mode == "listen":
        try:
            await asyncio.wait_for(run_mg_listen(addr), CONNECT_TIMEOUT + LISTEN_SECONDS)
        except Exception as ex:
            print(f"read-only listen failed: {ex}")
            print("Stopped. fd4b0002 was not written.")
        return
    if mode == "bond":
        try:
            await asyncio.wait_for(
                run_mg_bond(addr), CONNECT_TIMEOUT + BOND_WAIT + LISTEN_SECONDS)
        except Exception as ex:
            print(f"bond-then-listen failed: {ex}")
            print("Stopped. fd4b0002 was not written.")
        return
    try:
        await asyncio.wait_for(read_only_gatt(addr), CONNECT_TIMEOUT)
    except Exception as ex:
        print(f"read-only connect failed: {ex}")
        print("Stopped. The 4.0 handshake was not sent.")


async def cli():
    mode = sys.argv[1] if len(sys.argv) > 1 else "info"
    if len(sys.argv) > 2:
        addr = sys.argv[2]
        family = None
        print("Address was passed in. GATT is read before any 4.0 command.")
    else:
        print("scanning...")
        ranked = await scan()
        if not ranked:
            print("WHOOP not found (close the phone app — one bond at a time).")
            return
        _show_choice(ranked)
        chosen = ranked[0]
        addr = chosen["address"]
        family = chosen.get("family")

    if family == "mg":
        await _mg_command(addr, mode)
        return

    if family is None:
        try:
            kind = await asyncio.wait_for(read_only_gatt(addr), CONNECT_TIMEOUT)
        except Exception as ex:
            print(f"read-only connect failed: {ex}")
            return
        if kind == "mg":
            if mode in ("live", "listen", "bond", "drain"):
                await _mg_command(addr, mode)
            return
        if kind != "4.0":
            return
        if mode in ("listen", "bond"):
            print(f"{mode} is only for an fd4b strap. No 4.0 handshake is sent.")
            return
        if mode != "info":
            print("GATT shows a WHOOP 4.0. Continuing with that command set.")

    if mode in ("listen", "bond"):
        print(f"{mode} is only for an fd4b strap. No 4.0 handshake is sent.")
        return

    if mode not in ("info", "live", "drain"):
        print(f"Unknown mode {mode!r}.")
        return

    async with Whoop(addr) as w:
        print("connected", addr)
        await w.init()
        await asyncio.sleep(1.5)

        if mode == "info":
            await w.battery(); await w.version()
            await asyncio.sleep(2)

        elif mode == "live":
            secs = float(sys.argv[3]) if len(sys.argv) > 3 else 60
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            f = open(os.path.join(HERE, f"live_{stamp}.csv"), "w", newline="")
            wr = csv.writer(f); wr.writerow(["iso", "hr", "rr_ms"])
            def rt(r):
                rr = [x for x in r["rr_ms"] if x]
                wr.writerow([dt.datetime.now().isoformat(), r["hr"], " ".join(map(str, rr))]); f.flush()
                print(f"HR {r['hr']:3d}  RR {rr}")
            w.on_realtime = rt
            await w.realtime(True)
            await asyncio.sleep(secs)
            await w.realtime(False)
            f.close(); print(f"saved live_{stamp}.csv")

        elif mode == "drain":
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            raw = open(os.path.join(HERE, f"history_{stamp}.ndjson"), "w")
            n = [0]
            def sink(ch, inner):
                raw.write(json.dumps({"ch": ch[4:8], "type": inner[0], "hex": inner.hex()}) + "\n")
                raw.flush(); n[0] += 1
            def meta(m): print(f"  META subtype={m['subtype']} trim={m['trim']}")
            w.raw_sink = sink; w.on_meta = meta
            await w.drain(timeout=120)
            raw.close()
            print(f"captured {n[0]} raw frames -> history_{stamp}.ndjson")


if __name__ == "__main__":
    # self-test of frame builder against CRC-verified captures
    if len(sys.argv) > 1 and sys.argv[1] in ("buzz", "alarm"):
        # buzz [pattern] [loops] | alarm 07:00 [once] | alarm off | alarm show
        # Goes through the daemon when it holds the strap, else connects directly (alarm.py).
        import alarm
        sys.exit(alarm.cli(sys.argv[1:]))
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        checks = {
            "HR on":   (cmd(C_TOGGLE_HR, b"\x01"), "aa0800a823000e01d4c2477a"),
            "HR off":  (cmd(C_TOGGLE_HR, b"\x00"), "aa0800a823000e0042f2400d"),
            "realtime on": (cmd(C_REALTIME_HR, b"\x01"), "aa0800a82300030199bce9cf"),
            "battery": (cmd(C_BATTERY, b"\x00"), "aa0800a823001a001725ee23"),
            "version": (cmd(C_VERSION, b"\x00"), "aa0800a8230007000b4982dc"),
            "hello":   (cmd(C_HELLO_HARVARD, b"\x00"), "aa0800a823002300ada86a2d"),
            "getname": (cmd(C_GET_NAME, b"\x00"), "aa0800a823004c00c5df0fcf"),
            "drain":   (cmd(C_SEND_HIST, b"\x00"), "aa0800a8230016001b6a5b8f"),
            "highfreq":(cmd(C_HIGHFREQ_ON),      "aa07006b230060f340f888"),
            "abort":   (cmd(C_ABORT_HIST),       "aa07006b230014d6f590df"),
            # sent to a real 4.0 on 2026-11-19; it buzzed (HAPTICS_FIRED, then HAPTICS_TERMINATED)
            "buzz":    (cmd(C_RUN_HAPTICS, haptics_payload(2, 1)), "aa0c00fc23004f02010000006036a92e"),
        }
        ok = True
        for name, (got, want) in checks.items():
            g = got.hex()
            mark = "OK " if g == want else "FAIL"
            if g != want: ok = False
            print(f"  [{mark}] {name:12s} {g}" + ("" if g == want else f"  != {want}"))
        print("ALL PASS" if ok else "MISMATCH")
    else:
        asyncio.run(cli())
