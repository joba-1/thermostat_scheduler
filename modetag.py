#!/usr/bin/env python3
"""Mode tag carried in the weekly schedule.

We need to know which of our modes (heating/cooling/idle) a valve was last put
into *by us*, distinguishable from a state a user produced at the device. The
control fields cannot carry that: they are what the user changes, and on several
types our "cooling" signature is indistinguishable from a plain manual setting.

So the tag rides in a place the user cannot reach. The TRVs expose no free
setting on every type (TRVZB has neither `frost_protection` nor
`scale_protection`), but all four accept **minute-precision schedule times** and
echo them back byte-exact — verified on ME168_1, TR-M3Z, TRVZB and VNTH-T2_v2.
The schedule is also not visible or editable on the devices themselves, so the
tag cannot be disturbed by someone pressing buttons on a valve.

Carrier: the **2nd entry** of the Saturday and Sunday schedules. Not a literal
"03:00" — `generate_schedule_string` derives the interior points from each
room's day/night hours (Waschküche lands on 03:00, Julians on 03:30), so the
carrier is addressed by position. Entry 2 always repeats entry 1's temperature
(both are night-segment points), so moving it within its hour changes nothing
thermally, and staying inside the hour keeps the schedule sorted.

Payload: 4 bits, written as minutes 32..47.

    bits 0-1   mode        0 heating, 1 cooling, 2 idle, 3 reserved
    bits 2-3   generation  0..3, incremented on every write we make

The offset is what makes an untagged schedule distinguishable, and it costs the
5th bit for a good reason: `generate_schedule_string` rounds interior points to
:00 or :30, so a plain 0..31 encoding reads a *natural* schedule as a valid tag
— live, every untagged room decoded as `heating/gen0` and the two rooms with a
:30 point decoded as `idle/gen7`. Minutes >= 32 are unreachable by the generator,
so anything below the base is simply "not written by us".

The generation is what separates "our write never landed" from "the user changed
it": a tag one generation behind means our newest write was lost, while a
current tag whose actuation disagrees means someone else moved the valve.
Saturday and Sunday carry the same value; a mismatch between them means the
schedule was corrupted or rewritten by something that is not us.
"""

import re

MODES = ('heating', 'cooling', 'idle', 'reserved')
CARRIER_DAYS = ('saturday', 'sunday')
CARRIER_INDEX = 1          # 2nd entry
GENERATIONS = 4
TAG_BASE = 32              # keeps the tag clear of the generator's :00 / :30
TAG_MAX = TAG_BASE + GENERATIONS * len(MODES) - 1      # 47, well under 59
_ENTRY = re.compile(r'^(\d{1,2}):(\d{2})/(-?\d+(?:\.\d+)?)$')


def encode(mode, generation=0):
    """Pack (mode, generation) into the carrier minute value 32..47."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    return TAG_BASE + (((generation % GENERATIONS) << 2) | MODES.index(mode))


def decode(minutes):
    """Unpack a carrier minute value. Returns (mode, generation), or None when
    the value is outside the tag range — i.e. a schedule we did not write."""
    if minutes is None or not (TAG_BASE <= minutes <= TAG_MAX):
        return None
    v = minutes - TAG_BASE
    return MODES[v & 0b11], v >> 2


def _split(schedule_string):
    """Parse 'HH:MM/temp ...' into [(hh, mm, temp_str), ...] or None."""
    if not isinstance(schedule_string, str) or not schedule_string.strip():
        return None
    entries = []
    for part in schedule_string.split():
        mo = _ENTRY.match(part)
        if not mo:
            return None
        entries.append((int(mo.group(1)), int(mo.group(2)), mo.group(3)))
    return entries or None


def _join(entries):
    return " ".join(f"{h:02d}:{m:02d}/{t}" for h, m, t in entries)


def is_schedule(value):
    """True if `value` looks like one of our schedule strings."""
    return _split(value) is not None


def apply(schedule_string, minutes):
    """Return `schedule_string` with the carrier entry's minutes set."""
    entries = _split(schedule_string)
    if not entries or len(entries) <= CARRIER_INDEX:
        return schedule_string
    h, _m, t = entries[CARRIER_INDEX]
    entries[CARRIER_INDEX] = (h, minutes % 60, t)
    return _join(entries)


def read(schedule_string):
    """Return the carrier minute value, or None if unreadable."""
    entries = _split(schedule_string)
    if not entries or len(entries) <= CARRIER_INDEX:
        return None
    return entries[CARRIER_INDEX][1]


def normalize(schedule_string):
    """Zero the carrier minutes so two schedules compare equal regardless of the
    tag. Used everywhere schedules are diffed, or the tag would show up forever
    as a `settings_mismatch`."""
    entries = _split(schedule_string)
    if not entries or len(entries) <= CARRIER_INDEX:
        return schedule_string
    h, _m, t = entries[CARRIER_INDEX]
    entries[CARRIER_INDEX] = (h, 0, t)
    return _join(entries)


def tag_payload(payload, prefix, mode, generation=0):
    """Stamp the carrier days of a schedule payload in place. Returns `payload`."""
    minutes = encode(mode, generation)
    for day in CARRIER_DAYS:
        key = f"{prefix}_{day}"
        if key in payload:
            payload[key] = apply(payload[key], minutes)
    return payload


def read_state(state, prefix):
    """Decode the tag from a device's reported state.

    Returns a dict: {'mode', 'generation', 'minutes', 'agree', 'days'}, or None
    when neither carrier day is readable. `agree` is False when Saturday and
    Sunday disagree — the schedule was rewritten by something that is not us.
    """
    if not isinstance(state, dict):
        return None
    found = {}
    for day in CARRIER_DAYS:
        got = read(state.get(f"{prefix}_{day}"))
        if got is not None:
            found[day] = got
    if not found:
        return None
    values = set(found.values())
    minutes = next(iter(found.values()))
    dec = decode(minutes)
    return {
        'mode': dec[0] if dec else None,
        'generation': dec[1] if dec else None,
        'minutes': minutes,
        'agree': len(values) == 1 and len(found) == len(CARRIER_DAYS),
        'days': found,
    }
