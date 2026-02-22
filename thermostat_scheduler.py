#!/usr/bin/env python3
"""
Thermostat Schedule Controller
Publishes daily temperature schedules to different thermostat types via MQTT
"""

import json
import time
import argparse
import os
import sys
import yaml
import re
from decimal import Decimal
import threading


def load_config(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, 'r') as f:
        cfg = yaml.safe_load(f) or {}

    # Basic top-level validation
    for key in ('mqtt', 'thermostats', 'thermostat_types'):
        if key not in cfg:
            raise ValueError(f"Missing required top-level section in config: {key}")

    return cfg


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


def battery_status_note(reported, limit):
    """Return a parenthesized battery note for display, or empty string.
    """
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


def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print("Connected to MQTT broker successfully")
        if isinstance(userdata, threading.Event):
            userdata.set()
        elif isinstance(userdata, dict) and 'connect_event' in userdata and isinstance(userdata['connect_event'], threading.Event):
            userdata['connect_event'].set()
    else:
        print(f"Failed to connect to MQTT broker. Return code: {rc}")


def on_publish(client, userdata, mid, rc, properties=None):
    """Callback for successful message publish """
    print(f"Message published successfully (Message ID: {mid})")


def on_message(client, userdata, msg):
    """Collect incoming JSON state messages into userdata['responses'] by topic."""
    did_check = False
    try:
        payload = json.loads(msg.payload.decode('utf-8'))
    except Exception:
        # ignore non-json payloads
        return
    responses = userdata.setdefault('responses', {})
    responses[msg.topic] = payload


def build_expected_payload(name, thermostat_config, thermostat_types, mqtt_config):
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

    device_topic_name = f"{name} Thermostat"
    topic = f"{mqtt_config.get('base_topic')}/{device_topic_name}/set"

    return payload, topic


def pretty_payload(obj, indent=2):
    """Return a pretty-printed single-line-aligned JSON-like string for payloads."""
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


def check_thermostats(cfg, client, userdata, timeout=None):
    """Query `thermostat_monitor` for per-device status and collect responses.

    - Subscribes to `thermostat_monitor/+`
    - Publishes a single `get` message to `thermostat_monitor`
    - Waits `timeout` seconds (or `cfg['mqtt']['check_timeout']` or 5)
    - Collects responses from `userdata['responses']` into a `checked` dict
    - Prints the checked dict pretty-printed with indentation
    """
    if timeout is None:
        timeout = cfg.get('mqtt', {}).get('check_timeout', 5)

    monitor_base = 'thermostat_monitor'
    try:
        client.subscribe(f"{monitor_base}/+")
    except Exception:
        pass

    # Ask monitor to publish current device statuses
    try:
        client.publish(monitor_base, 'get')
    except Exception:
        pass

    time.sleep(timeout)
    print()

    responses = userdata.get('responses', {}) if isinstance(userdata, dict) else {}

    checked = {}
    for topic, payload in responses.items():
        if not topic.startswith(monitor_base + '/'):
            continue
        name = topic.split('/', 1)[1]
        checked[name] = payload

    # Pretty-print collected responses
    # try:
    #     print(json.dumps(checked, indent=2, ensure_ascii=False))
    # except Exception:
    #     print(f"Received responses: {checked}")

    # Compare expected payload keys to reported state keys (only intersecting keys)
    thermostats = cfg.get('thermostats', {})
    thermostat_types = cfg.get('thermostat_types', {})

    def normalize_str(s):
        return ' '.join(str(s).split())

    def compare_and_collect_mismatches(expected, reported_state):
        """Return a dict of mismatched keys -> (expected, reported).

        For every key in `expected`, report a mismatch if the key is
        missing from `reported_state` or the values differ. Always
        returns a dict (possibly empty) of mismatches.
        """
        mismatches = {}
        # If reported_state is not a dict, treat it as empty (all keys
        # missing) so we report all expected keys.
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

    def print_mismatch_table(mismatches, indent=2):
        pad = " " * indent
        if not mismatches:
            return
        # Prepare rows: header + items
        rows = [["Key", "Expected", "Reported"]]
        for k, (ev, rv) in mismatches.items():
            rows.append([k, str(ev), str(rv)])

        cols = list(zip(*rows))
        widths = [max(len(cell) for cell in col) for col in cols]
        lines = []
        header = pad + " | ".join(h.ljust(w) for h, w in zip(rows[0], widths))
        divider = pad + "-+-".join("-" * w for w in widths)
        lines.append(header)
        lines.append(divider)
        for row in rows[1:]:
            lines.append(pad + " | ".join(str(c).ljust(w) for c, w in zip(row, widths)))

        print("\n".join(lines))

    for name, cfg_item in thermostats.items():
        try:
            expected, _ = build_expected_payload(name, cfg_item, thermostat_types, cfg.get('mqtt', {}))
        except Exception as e:
            print(f"{name}: error building expected payload: {e}")
            continue

        try:
            reported = checked[name]['state']
        except KeyError:
            print(f"{name}: no monitored state found: {e}")
            continue

        battery_note = battery_status_note(reported, 20)

        mismatches = compare_and_collect_mismatches(expected, reported)
        if not mismatches:
            print(f"{name}: OK{battery_note}")
        else:
            print(f"{name}: MISMATCHES{battery_note}:")
            print_mismatch_table(mismatches, indent=2)

    print()
    return checked


def configure_thermostat(client, name, thermostat_config, index, thermostat_types, mqtt_config, dry_run=False):
    print(f"\nConfiguring thermostat {index}: {name}")

    thermostat_type = thermostat_config["type"]
    type_config = thermostat_types.get(thermostat_type)
    if not type_config:
        print(f"Unknown thermostat type: {thermostat_type}")
        return

    payload, topic = build_expected_payload(name, thermostat_config, thermostat_types, mqtt_config)
    payload_str = pretty_payload(payload, indent=2)

    print(f"Topic: {topic}")
    print("Payload:")
    print(payload_str)

    payload_json = json.dumps(payload)

    if dry_run or client is None:
        print("Dry run: not publishing to MQTT.")
        return payload, topic

    try:
        info = client.publish(topic, payload_json, qos=1, retain=False)
        try:
            info.wait_for_publish(timeout=10)
        except Exception:
            pass

        if getattr(info, "is_published", lambda: False)():
            print(f"✓ Successfully sent configuration to {name}")
        else:
            print(f"✗ Failed to send configuration to {name} (info: {info})")
    except Exception as e:
        print(f"Publish error for {name}: {e}")

    return payload, topic


def print_thermostat_table(thermostats):
    headers = ["Name", "Day Hour", "Day Temp", "Night Hour", "Night Temp", "Type"]
    rows = []
    for name, cfg in thermostats.items():
        rows.append([
            name,
            cfg.get("day_hour", ""),
            str(cfg.get("day_temperature", "")),
            cfg.get("night_hour", ""),
            str(cfg.get("night_temperature", "")),
            cfg.get("type", ""),
        ])

    cols = list(zip(*([headers] + rows))) if rows else [headers]
    widths = [max(len(str(cell)) for cell in col) for col in cols]

    sep = " | "
    header_line = sep.join(h.ljust(w) for h, w in zip(headers, widths))
    divider = "-+-".join("-" * w for w in widths)

    print("\nThermostat settings:")
    print(header_line)
    print(divider)
    for row in rows:
        print(sep.join(str(cell).ljust(w) for cell, w in zip(row, widths)))
    print()


def main():
    parser = argparse.ArgumentParser(description='Thermostat scheduler using YAML config')
    parser.add_argument('--config', '-c', default='config.yaml', help='Path to YAML config file')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--dry-run', action='store_true', help='Do not connect to MQTT; only print topics/payloads')
    group.add_argument('--check', action='store_true', help='Run configuration checks using thermostat_monitor')
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except Exception as e:
        print(f"Failed to load config '{args.config}': {e}")
        sys.exit(1)

    mqtt_cfg = cfg.get('mqtt', {})
    thermostats = cfg.get('thermostats', {})
    thermostat_types = cfg.get('thermostat_types', {})

    print("=== Thermostat Schedule Controller ===")
    print(f"MQTT Broker: {mqtt_cfg.get('broker')}:{mqtt_cfg.get('port')}")
    print(f"Base Topic: {mqtt_cfg.get('base_topic')}")
    print(f"Configuring {len(thermostats)} thermostats...")

    client = None
    if not args.dry_run:
        try:
            import paho.mqtt.client as mqtt
        except Exception as e:
            print(f"Failed to import paho.mqtt.client: {e}")
            sys.exit(1)

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.on_connect = on_connect
        client.on_publish = on_publish
        client.on_message = on_message
        if mqtt_cfg.get('username'):
            client.username_pw_set(mqtt_cfg.get('username'), mqtt_cfg.get('password'))

    try:
        if not args.dry_run:
            print(f"Connecting to MQTT broker at {mqtt_cfg.get('broker')}:{mqtt_cfg.get('port')}...")
            evt = threading.Event()
            userdata = {'connect_event': evt, 'responses': {}}
            client.user_data_set(userdata)
            client.connect(mqtt_cfg.get('broker'), mqtt_cfg.get('port'), 60)
            client.loop_start()
            if not userdata['connect_event'].wait(10):
                print("Warning: MQTT connection timeout")

        if args.check:
            check_thermostats(cfg, client, userdata, timeout=mqtt_cfg.get('check_timeout', 5))
        else:
            for i, (thermostat_name, config) in enumerate(thermostats.items()):
                result = configure_thermostat(client, thermostat_name, config, i+1, thermostat_types, mqtt_cfg, dry_run=args.dry_run)
                if not args.dry_run:
                    time.sleep(mqtt_cfg.get('delay_between_messages', 1))

            if args.dry_run:
                print("Dry run complete")
            else:
                print("All thermostats configured")

    except Exception as e:
        print(f"Error: {e}")

    finally:
        if not args.dry_run and client is not None:
            client.loop_stop()
            client.disconnect()
            print("Disconnected from MQTT broker.")
        if not args.check:
            print_thermostat_table(thermostats)


if __name__ == "__main__":
    main()
