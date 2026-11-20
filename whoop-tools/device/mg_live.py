"""Read-only live session for a WHOOP 5.0/MG (service fd4b0001).

Uses only the standard GATT characteristics Diego's strap advertised:
device-information strings, Battery Level, and Heart Rate Measurement.
Nothing here writes. fd4b0002 is the vendor command channel and is not
touched. The 4.0 handshake, historical drain, alarm, and buzz are not
part of this path.

2A37 RR intervals are Bluetooth SIG units of 1/1024 s. Whether that
characteristic notifies before any vendor command is not known; this
session does not send one to find out.
"""

import asyncio

from device.protocol import BATTERY_LEVEL, HR_MEASUREMENT, parse_heart_rate_measurement

LIVE_SECONDS = 15

# SIG short UUIDs Diego's read-only GATT listing showed as readable.
DEVICE_INFO = (
    ("manufacturer", "00002a29-0000-1000-8000-00805f9b34fb"),
    ("model", "00002a24-0000-1000-8000-00805f9b34fb"),
    ("serial", "00002a25-0000-1000-8000-00805f9b34fb"),
    ("firmware", "00002a26-0000-1000-8000-00805f9b34fb"),
    ("hardware", "00002a27-0000-1000-8000-00805f9b34fb"),
)

# Vendor command characteristic. Reads and notifies of the custom service
# are also out of scope: this path does not touch any fd4b characteristic.
FD4B_COMMAND = "fd4b0002-cce1-4033-93ce-002d5875f58a"

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
            emit("2A37 may stay quiet off the wrist, or until a vendor command this path does not send.")
        else:
            word = "notification" if len(samples) == 1 else "notifications"
            emit(f"{len(samples)} heart-rate {word} in {seconds:.0f}s.")
    return {"info": info, "battery": battery, "samples": samples}
