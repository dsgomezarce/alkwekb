"""Choose which advertised strap `whoop.py info` should open.

A record is a candidate when it advertises the WHOOP 5.0/MG service
(``fd4b0001-…``), the WHOOP 4.0 service (``61080001-…``), or its name
contains "WHOOP". The strongest RSSI wins (a larger number is nearer).
A missing RSSI sorts last. Nothing here stores a Bluetooth address: macOS
rotates the identifier, so the choice is name, service, and RSSI only.

The 4.0 command handshake is not valid on ``fd4b``. Callers must not write
commands when ``family`` is ``"mg"``.
"""

from device.protocol import WHOOP4_SERVICE, WHOOP5_SERVICE

_FAMILY_TIE = {"mg": 2, "4.0": 1}


def _uuids(services):
    return [str(u).lower() for u in (services or [])]


def family_of(services):
    """``"mg"`` for fd4b0001, ``"4.0"`` for 61080001, else None.

    MG wins if a single advert lists both. That combination is not expected.
    """
    uuids = _uuids(services)
    if any(u == WHOOP5_SERVICE or u.startswith("fd4b0001") for u in uuids):
        return "mg"
    if any(u == WHOOP4_SERVICE or u.startswith("61080001") for u in uuids):
        return "4.0"
    return None


def name_is_whoop(name):
    return "whoop" in (name or "").lower()


def matches(name, services):
    return family_of(services) is not None or name_is_whoop(name)


def rank(records):
    """Candidate records, nearest first. Each copy has a ``family`` key.

    ``family`` is taken from the advertised service. A name-only match has
    ``family`` None until a GATT read says otherwise.
    """
    found = []
    for rec in records:
        if not matches(rec.get("name"), rec.get("services")):
            continue
        item = dict(rec)
        item["family"] = family_of(rec.get("services"))
        found.append(item)

    def strength(rec):
        rssi = rec.get("rssi")
        if not isinstance(rssi, (int, float)):
            rssi = -10_000
        return (rssi, _FAMILY_TIE.get(rec.get("family"), 0))

    found.sort(key=strength, reverse=True)
    return found


def choose(records):
    """The nearest candidate, or None."""
    ranked = rank(records)
    return ranked[0] if ranked else None


def command_address(seen):
    """Bluetooth address only when the chosen strap is a WHOOP 4.0.

    A nearer 5.0/MG, or a name-only match, yields None so the 4.0 handshake
    is not written to that strap and a weaker 4.0 is not used instead.
    """
    if not seen or seen.get("family") != "4.0":
        return None
    return seen.get("address")
