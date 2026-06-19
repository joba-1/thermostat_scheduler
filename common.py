#!/usr/bin/env python3
"""
Shared helpers for the thermostat scheduler, monitor and manager.

This module holds the pure, side-effect-free building blocks (config loading,
schedule generation, payload building, schedule/state comparison, battery
notes) so the scheduler (one-shot) and the manager daemon can reuse them
without duplication. Keeping these here also makes them straightforward to
unit-test.
"""

import os
import logging
from decimal import Decimal

import yaml

log = logging.getLogger("thermostat")


def setup_logging(level=logging.INFO):
    """Configure root logging once, in a journal/syslog-friendly format.

    The systemd unit captures stdout/stderr into the journal with a
    SyslogIdentifier, so a plain stream handler is all we need. Timestamps are
    left out by default because the journal adds its own; set the env var
    ``THERMOSTAT_LOG_TS=1`` to include them when running interactively.
    """
    if logging.getLogger().handlers:
        return log
    fmt = "%(levelname)s %(name)s: %(message)s"
    if os.environ.get("THERMOSTAT_LOG_TS"):
        fmt = "%(asctime)s " + fmt
    logging.basicConfig(level=level, format=fmt)
    return log


def load_config(path):
    """Load and minimally validate the YAML config file."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, 'r') as f:
        cfg = yaml.safe_load(f) or {}

    # Basic top-level validation
    for key in ('mqtt', 'thermostats', 'thermostat_types'):
        if key not in cfg:
            raise ValueError(f"Missing required top-level section in config: {key}")

    return cfg


def mqtt_credentials(mqtt_cfg):
    """Return (username, password), preferring env vars over the config file.

    Per the security standard, credentials should not live in the committed
    config. THERMOSTAT_MQTT_USER / THERMOSTAT_MQTT_PASS override any values in
    config.yaml; returns (None, None) if neither is set.
    """
    user = os.environ.get('THERMOSTAT_MQTT_USER') or mqtt_cfg.get('username')
    pw = os.environ.get('THERMOSTAT_MQTT_PASS') or mqtt_cfg.get('password')
    return user, pw


def time_to_minutes(time_str):
    """Convert HH:MM format to minutes since midnight"""
    hours, minutes = map(int, time_str.split(':'))
    return hours * 60 + minutes


def minutes_to_time(minutes):
    """Convert minutes since midnight to HH:MM format"""
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours:02d}:{mins:02d}"


def generate_schedule_string(day_hour, day_temp, night_hour, night_temp):
    """Generate a schedule string using the new algorithm:

    - Produce 3 "day" entries between `day_hour` (inclusive) and
      `night_hour` (exclusive): those are spaced in 2 equal intervals
      (3 points total: start + 2 interior points).
    - Produce 3 "night" entries between `night_hour` (inclusive) and
      `day_hour` (exclusive): similarly spaced (3 points total).
    - `day_hour` and `night_hour` are kept exactly (they appear in the
      generated points if they are the respective segment starts).
    - If no entry lands exactly at midnight (00:00), move the closest
      generated time to midnight.
    - Finally sort by time and return a space-separated list of
      "HH:MM/temperature" pairs. No additional deduplication step is
      required by this construction.
    """
    DAY_MINUTES = 24 * 60
    day_minutes = time_to_minutes(day_hour)
    night_minutes = time_to_minutes(night_hour)

    def span(a, b):
        # minutes going forward from a to b (wraps past midnight)
        return (b - a) % DAY_MINUTES

    def round_to_half_hour(mins):
        # Round to nearest 30-minute increment, wrap around 24h
        rounded = int(round(mins / 30.0) * 30) % DAY_MINUTES
        return rounded

    schedule = []

    # Day segment: from day_minutes (inclusive) toward night_minutes (exclusive)
    day_span = span(day_minutes, night_minutes)
    # produce 3 points: i in [0,1,2] -> positions at day + i*(day_span/3)
    for i in range(3):
        if i == 0:
            # keep day_start exactly
            t = day_minutes
        else:
            pos = (day_minutes + (i * (day_span / 3.0))) % DAY_MINUTES
            t = round_to_half_hour(pos)
        schedule.append((t, day_temp))

    # Night segment: from night_minutes (inclusive) toward day_minutes (exclusive)
    night_span = span(night_minutes, day_minutes)
    for i in range(3):
        if i == 0:
            # keep night_start exactly
            t = night_minutes
        else:
            pos = (night_minutes + (i * (night_span / 3.0))) % DAY_MINUTES
            t = round_to_half_hour(pos)
        schedule.append((t, night_temp))

    # Ensure there's an entry at midnight (00:00). If none exists, move
    # the closest time to 0.
    times = [t for t, _ in schedule]
    if 0 not in times:
        # find index of closest time to midnight (consider wrap)
        # but do not move the segment start points (day_start at index 0,
        # night_start at index 3). Prefer moving one of the interior points
        # (indices 1,2,4,5).
        def dist_to_mid(t):
            return min((t - 0) % DAY_MINUTES, (0 - t) % DAY_MINUTES)

        candidate_indices = [i for i in range(len(times)) if i not in (0, 3)]
        if not candidate_indices:
            # Fallback: allow any index (shouldn't normally happen)
            candidate_indices = list(range(len(times)))

        closest_idx = min(candidate_indices, key=lambda i: dist_to_mid(times[i]))
        schedule[closest_idx] = (0, schedule[closest_idx][1])

    # Sort by time and format
    schedule.sort(key=lambda it: it[0])
    pairs = [f"{minutes_to_time(t)}/{v}" for t, v in schedule]
    return " ".join(pairs)


def _normalize_temp_token_for_compare(token):
    """Return a canonical string for a numeric temperature token suitable for comparison.

    Attempts to parse with Decimal and returns a plain string without
    leading/trailing zeros or an unnecessary decimal point (e.g. '24.0' -> '24').
    If parsing fails, returns the original token stripped.
    """
    s = str(token).strip()
    try:
        d = Decimal(s)
        # Use 'f' format to avoid exponent notation and remove trailing zeros
        normalized = format(d.normalize(), 'f')
        return normalized
    except Exception:
        return s


def compare_schedule_strings(a, b):
    """Compare two schedule strings token-by-token, ignoring insignificant zeros.

    Returns True if they match (times identical and numeric temps equal
    after normalization). Falls back to simple whitespace-normalized string
    comparison when tokens don't look like schedule tokens.
    """
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    parts_a = a.split()
    parts_b = b.split()
    if len(parts_a) != len(parts_b):
        return False

    for pa, pb in zip(parts_a, parts_b):
        if '/' not in pa or '/' not in pb:
            # Not a schedule-like token; fall back to normalized string compare
            return ' '.join(a.split()) == ' '.join(b.split())
        ta, va = pa.split('/', 1)
        tb, vb = pb.split('/', 1)
        if ta != tb:
            return False
        na = _normalize_temp_token_for_compare(va)
        nb = _normalize_temp_token_for_compare(vb)
        if na != nb:
            return False

    return True


def normalize_str(s):
    return ' '.join(str(s).split())


def compare_and_collect_mismatches(expected, reported_state):
    """Return a dict of mismatched keys -> (expected, reported).

    For every key in `expected`, report a mismatch if the key is missing
    from `reported_state` or the values differ. Always returns a dict
    (possibly empty) of mismatches.
    """
    mismatches = {}
    # If reported_state is not a dict, treat it as empty (all keys missing)
    # so we report all expected keys.
    if not isinstance(reported_state, dict):
        for k in sorted(expected.keys()):
            mismatches[k] = (expected[k], None)
        return mismatches

    for k in sorted(expected.keys()):
        ev = expected.get(k)
        if k not in reported_state:
            mismatches[k] = (ev, None)
            continue
        rv = reported_state.get(k)
        # numeric comparison
        try:
            evf = float(ev)
            rvf = float(rv)
            if abs(evf - rvf) > 1e-6:
                mismatches[k] = (ev, rv)
            continue
        except Exception:
            pass

        # string comparison (normalize whitespace)
        if isinstance(ev, str) and isinstance(rv, str):
            # Prefer schedule-aware comparison that ignores insignificant
            # zeros/decimal formatting. Falls back to whitespace-normalized
            # string comparison if not schedule-like.
            try:
                if compare_schedule_strings(ev, rv):
                    continue
            except Exception:
                pass

            if normalize_str(ev) != normalize_str(rv):
                mismatches[k] = (ev, rv)
        else:
            if ev != rv:
                mismatches[k] = (ev, rv)

    return mismatches


def battery_status_note(reported, limit):
    """Return a parenthesized battery note for display, or empty string."""
    try:
        low = reported.get('battery_low')
        if low is True:
            return " (battery low)"
        level = reported.get('battery')
        if level is not None and level < limit:
            return f" (battery {level}%)"
        if low is False or level is not None:
            return ""
    except Exception:
        pass
    return " (battery unknown)"


def device_topic_name(name):
    """zigbee2mqtt friendly name for a configured thermostat room."""
    return f"{name} Thermostat"


def build_expected_payload(name, thermostat_config, thermostat_types, mqtt_config):
    """Build the schedule payload + set-topic expected for a thermostat."""
    thermostat_type = thermostat_config["type"]
    type_config = thermostat_types.get(thermostat_type)
    if not type_config:
        raise ValueError(f"Unknown thermostat type: {thermostat_type}")

    schedule_string = generate_schedule_string(
        thermostat_config["day_hour"],
        thermostat_config["day_temperature"],
        thermostat_config["night_hour"],
        thermostat_config["night_temperature"],
    )

    payload = type_config['schedule_mode'].copy()
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    prefix = type_config.get('schedule_prefix', 'schedule')
    for weekday in weekdays:
        payload[f"{prefix}_{weekday}"] = schedule_string

    topic = f"{mqtt_config.get('base_topic')}/{device_topic_name(name)}/set"

    return payload, topic


def pretty_payload(obj, indent=2):
    """Return a pretty-printed single-line-aligned JSON-like string for payloads."""
    import json
    items = [(json.dumps(k), json.dumps(v)) for k, v in obj.items()]
    max_key = max((len(k) for k, _ in items), default=0)
    lines = ["{"]
    pad = " " * indent
    for i, (k, v) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        space = " " * (max_key - len(k))
        lines.append(f"{pad}{k}{space}: {v}{comma}")
    lines.append("}")
    return "\n".join(lines)
