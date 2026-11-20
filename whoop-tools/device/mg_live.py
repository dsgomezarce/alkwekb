"""Read-only sessions for a WHOOP 5.0/MG (service fd4b0001).

``read_only_live`` reads the standard device-info strings, battery, and
Heart Rate Measurement. ``read_only_listen`` subscribes to the four vendor
notify characteristics plus Heart Rate Measurement and prints raw bytes.
``bond_then_listen`` asks the operating system to pair, then does that
same subscribe. ``start_published`` is the one path that writes, and the
only bytes it writes are the published 16-byte CLIENT_HELLO. There is no
4.0 handshake, historical drain, history ack, alarm, buzz, reboot, DFU,
or trim.

2A37 RR intervals are Bluetooth SIG units of 1/1024 s. On WHOOP DS that
characteristic stayed silent for 15 seconds while the strap was worn.
This module still does not write fd4b0002 to find out why. listen
subscribes to the four vendor notify characteristics and to 2A37, prints
the raw bytes, and does not decode them. It does not drain, ack history,
buzz, or alarm.
"""

import asyncio

from device.protocol import BATTERY_LEVEL, HR_MEASUREMENT, parse_heart_rate_measurement

LIVE_SECONDS = 15
LISTEN_SECONDS = 20
# How long to keep the connection up after macOS returns "encryption
# insufficient", so the system pairing dialog can be answered.
BOND_WAIT = 45
PAIR_HINT = "If macOS shows a pairing dialog, tap Pair."

# SIG short UUIDs Diego's read-only GATT listing showed as readable.
DEVICE_INFO = (
    ("manufacturer", "00002a29-0000-1000-8000-00805f9b34fb"),
    ("model", "00002a24-0000-1000-8000-00805f9b34fb"),
    ("serial", "00002a25-0000-1000-8000-00805f9b34fb"),
    ("firmware", "00002a26-0000-1000-8000-00805f9b34fb"),
    ("hardware", "00002a27-0000-1000-8000-00805f9b34fb"),
)

# Vendor command characteristic. Never written, never subscribed.
FD4B_COMMAND = "fd4b0002-cce1-4033-93ce-002d5875f58a"
_FD4B = "fd4b000{}-cce1-4033-93ce-002d5875f58a"
FD4B_NOTIFY = (_FD4B.format(3), _FD4B.format(4), _FD4B.format(5), _FD4B.format(7))
# listen subscribes to these and nothing else. fd4b0002 is not in the tuple.
LISTEN_UUIDS = FD4B_NOTIFY + (HR_MEASUREMENT,)

# WHOOP 5.0 / MG session start. Not built here: copied from NOOP
# DeviceFamily.whoop5ClientHello, which quotes GooseHello.clientHelloFrameHex
# "aa0108000001e67123019101363e5c8d". Inner bytes are type 0x23, seq 0x01,
# cmd 0x91 GET_HELLO, b3 0x01, and no payload. It is not SEND_HISTORICAL.
CLIENT_HELLO = bytes((
    0xAA, 0x01, 0x08, 0x00, 0x00, 0x01, 0xE6, 0x71,
    0x23, 0x01, 0x91, 0x01, 0x36, 0x3E, 0x5C, 0x8D,
))

READS = tuple(uuid for _name, uuid in DEVICE_INFO) + (BATTERY_LEVEL,)
NOTIFIES = (HR_MEASUREMENT,)


def text(data):
    """UTF-8 device-info string. Empty when the characteristic has no bytes."""
    if not data:
        return ""
    return bytes(data).decode("utf-8", "replace").strip("\x00").strip()


def format_hr(sample):
    """One line: bpm, and RR counts in 1/1024 s when the notification has them."""
    line = f"HR {sample['hr']:3d} bpm"
    raws = sample.get("rr_raw") or []
    if raws:
        line += "  RR " + " ".join(str(v) for v in raws) + " x 1/1024 s"
    return line


def _forbid(uuid):
    if str(uuid).lower().startswith("fd4b"):
        raise RuntimeError(f"refusing {uuid}: the MG vendor channel is not used")


async def read_only_live(link, seconds=LIVE_SECONDS, sleep=asyncio.sleep, emit=None):
    """Read device info and battery, then listen on 2A37.

    ``link.read(uuid) -> bytes`` and ``link.start_notify(uuid, callback)`` /
    ``link.stop_notify(uuid)``. ``callback`` receives the notification bytes.
    ``emit(line)`` prints a sample as it arrives. Returns the collected record.
    """
    info = {}
    for name, uuid in DEVICE_INFO:
        _forbid(uuid)
        try:
            info[name] = text(await link.read(uuid))
        except Exception as ex:
            info[name] = None
            info[name + "_error"] = type(ex).__name__
    battery = None
    _forbid(BATTERY_LEVEL)
    try:
        raw = await link.read(BATTERY_LEVEL)
        if raw and raw[0] <= 100:
            battery = raw[0]
    except Exception as ex:
        info["battery_error"] = type(ex).__name__

    if emit:
        labels = (
            ("manufacturer", "Manufacturer"),
            ("model", "Model"),
            ("serial", "Serial"),
            ("firmware", "Firmware"),
            ("hardware", "Hardware"),
        )
        for key, label in labels:
            value = info.get(key)
            if value:
                emit(f"{label}: {value}")
            elif info.get(key + "_error"):
                emit(f"{label}: unreadable ({info[key + '_error']})")
            else:
                emit(f"{label}: (empty)")
        if battery is None:
            emit("Battery: unreadable")
        else:
            emit(f"Battery: {battery}%")
        emit(f"Listening on 2A37 for {seconds:.0f}s. RR, when present, is in 1/1024 s.")

    samples = []

    def on_data(data):
        parsed = parse_heart_rate_measurement(bytes(data))
        if not parsed:
            return
        samples.append(parsed)
        if emit:
            emit(format_hr(parsed))

    _forbid(HR_MEASUREMENT)
    await link.start_notify(HR_MEASUREMENT, on_data)
    try:
        await sleep(seconds)
    finally:
        try:
            await link.stop_notify(HR_MEASUREMENT)
        except Exception:
            pass
    if emit:
        if not samples:
            emit("No heart-rate notifications in this window.")
            emit("This path does not write fd4b0002.")
        else:
            word = "notification" if len(samples) == 1 else "notifications"
            emit(f"{len(samples)} heart-rate {word} in {seconds:.0f}s.")
    return {"info": info, "battery": battery, "samples": samples}


def format_note(uuid, data):
    """One notification: its UUID and the raw bytes as hex. No decode."""
    return f"{uuid} {bytes(data).hex()}"


def _refuse_command(uuid):
    if str(uuid).lower() == FD4B_COMMAND or str(uuid).lower().startswith("fd4b0002"):
        raise RuntimeError("refusing fd4b0002: listen does not write or subscribe the command channel")


async def read_only_listen(link, seconds=LISTEN_SECONDS, sleep=asyncio.sleep, emit=None):
    """Subscribe and print. No reads, no writes, no history ack.

    ``link.start_notify(uuid, callback)`` / ``link.stop_notify(uuid)``.
    ``callback`` receives the notification bytes. A subscribe that the
    stack refuses is reported and the other characteristics are still opened.
    """
    for uuid in LISTEN_UUIDS:
        _refuse_command(uuid)
    notes = []
    opened = []

    def on_uuid(uuid):
        def on_data(data):
            raw = bytes(data)
            notes.append((uuid, raw))
            if emit:
                emit(format_note(uuid, raw))
        return on_data

    if emit:
        emit(f"Subscribing for {seconds:.0f}s. Notifications print as UUID then hex.")
        emit("No write to fd4b0002. No handshake, drain, buzz, alarm, or history ack.")

    for uuid in LISTEN_UUIDS:
        try:
            await link.start_notify(uuid, on_uuid(uuid))
        except Exception as ex:
            if emit:
                emit(f"{uuid} subscribe failed ({type(ex).__name__}: {ex})")
            continue
        opened.append(uuid)

    try:
        await sleep(seconds)
    finally:
        for uuid in opened:
            try:
                await link.stop_notify(uuid)
            except Exception:
                pass
    if emit:
        if notes:
            word = "notification" if len(notes) == 1 else "notifications"
            emit(f"{len(notes)} {word} in {seconds:.0f}s.")
        else:
            emit("No notifications in this window.")
    return {"notes": notes, "subscribed": opened}


async def bond_then_listen(link, seconds=LISTEN_SECONDS, bond_wait=BOND_WAIT,
                           sleep=asyncio.sleep, emit=None):
    """Pair through the OS, then subscribe and print. No vendor write.

    ``link.pair()`` is the platform pair call. CoreBluetooth has none, so
    there it raises ``NotImplementedError`` and the dialog is the one
    macOS shows when an encrypted characteristic is opened. The open used
    here is a subscribe on ``fd4b0003``, the same characteristic that
    returned "Encryption is insufficient." It is not a write to ``fd4b0002``.
    """
    for uuid in LISTEN_UUIDS:
        _refuse_command(uuid)
    if emit:
        emit(PAIR_HINT)
        emit("No write to fd4b0002. No handshake, drain, buzz, alarm, or history ack.")

    try:
        await link.pair()
    except NotImplementedError:
        if emit:
            emit("This OS has no explicit pair call. Opening fd4b0003 so macOS can show its dialog.")
        trigger = FD4B_NOTIFY[0]
        try:
            await link.start_notify(trigger, lambda _data: None)
        except Exception as ex:
            if emit:
                emit(f"{trigger} bond trigger failed ({type(ex).__name__}: {ex})")
                emit(f"Waiting {bond_wait:.0f}s. {PAIR_HINT}")
            await sleep(bond_wait)
        else:
            try:
                await link.stop_notify(trigger)
            except Exception:
                pass
    except Exception as ex:
        if emit:
            emit(f"OS pair failed ({type(ex).__name__}: {ex})")
            emit(PAIR_HINT)
    else:
        if emit:
            emit("OS pair finished.")

    heard = await read_only_listen(link, seconds=seconds, sleep=sleep, emit=emit)
    return heard


async def start_published(link, seconds=LISTEN_SECONDS, sleep=asyncio.sleep, emit=None):
    """Subscribe, write the published CLIENT_HELLO once, then print for ~20s.

    ``link.write(uuid, data, response=True)`` is called once. ``data`` is
    ``CLIENT_HELLO`` and ``uuid`` is ``fd4b0002``. A failed subscribe does
    not add another write. A failed write does not add one either.
    """
    if CLIENT_HELLO != bytes.fromhex("aa0108000001e67123019101363e5c8d"):
        raise RuntimeError("CLIENT_HELLO no longer matches the published frame")
    notes = []
    opened = []

    def on_uuid(uuid):
        def on_data(data):
            raw = bytes(data)
            notes.append((uuid, raw))
            if emit:
                emit(format_note(uuid, raw))
        return on_data

    if emit:
        emit(f"Subscribing, then one write-with-response of CLIENT_HELLO ({CLIENT_HELLO.hex()}).")
        emit("No drain, history ack, buzz, alarm, reboot, DFU, or trim.")

    for uuid in LISTEN_UUIDS:
        _refuse_command(uuid)
        try:
            await link.start_notify(uuid, on_uuid(uuid))
        except Exception as ex:
            if emit:
                emit(f"{uuid} subscribe failed ({type(ex).__name__}: {ex})")
            continue
        opened.append(uuid)

    wrote = False
    try:
        await link.write(FD4B_COMMAND, CLIENT_HELLO, response=True)
        wrote = True
        if emit:
            emit(f"{FD4B_COMMAND} {CLIENT_HELLO.hex()}")
    except Exception as ex:
        if emit:
            emit(f"{FD4B_COMMAND} write failed ({type(ex).__name__}: {ex})")

    try:
        await sleep(seconds)
    finally:
        for uuid in opened:
            try:
                await link.stop_notify(uuid)
            except Exception:
                pass
    if emit and not notes:
        emit("No notifications in this window.")
    return {"notes": notes, "subscribed": opened, "wrote": wrote}
