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
    """
    Generate schedule string with 6 time/temperature pairs:
    - 2 pairs from night to day (one starting at midnight if night_hour > day_hour)
    - 4 pairs from day to night (one starting at midnight if day_hour > night_hour)
    """
    day_minutes = time_to_minutes(day_hour)
    night_minutes = time_to_minutes(night_hour)
    DAY_MINUTES = 24 * 60

    def mod_diff(a, b):
        # Minutes from a to b going forward in time, wrapping past midnight
        return (b - a) % DAY_MINUTES

    night_to_day_dur = mod_diff(night_minutes, day_minutes)
    day_to_night_dur = mod_diff(day_minutes, night_minutes)

    # Compute step sizes (may be fractional); we'll round to nearest minute
    step_night_to_day = (night_to_day_dur / 2.0) if night_to_day_dur != 0 else 0.0
    step_day_to_night = (day_to_night_dur / 4.0) if day_to_night_dur != 0 else 0.0

    schedule_pairs = []

    # 2 pairs from night to day (use night_temp). If the interval crosses
    # midnight (night_minutes > day_minutes) ensure one of the pairs starts
    # at 00:00 as per the docstring.
    night_points = []
    for i in range(2):
        t = int(round((night_minutes + i * step_night_to_day) % DAY_MINUTES))
        night_points.append(t)
    if night_minutes > day_minutes:
        # ensure one pair at midnight
        night_points[0] = 0
    for t in night_points:
        schedule_pairs.append(f"{minutes_to_time(t)}/{night_temp}")

    # 4 pairs from day to night (use day_temp). If the interval crosses
    # midnight (day_minutes > night_minutes) ensure one of the pairs starts
    # at 00:00.
    day_points = []
    for i in range(4):
        t = int(round((day_minutes + i * step_day_to_night) % DAY_MINUTES))
        day_points.append(t)
    if day_minutes > night_minutes:
        day_points[0] = 0
    for t in day_points:
        schedule_pairs.append(f"{minutes_to_time(t)}/{day_temp}")

    # Sort pairs by time (HH:MM) ascending and remove duplicates (keeping
    # first occurrence for a given time).
    schedule_pairs.sort(key=lambda pair: time_to_minutes(pair.split('/')[0]))
    seen = set()
    unique = []
    for pair in schedule_pairs:
        t = pair.split('/')[0]
        if t in seen:
            continue
        seen.add(t)
        unique.append(pair)

    return " ".join(unique)


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

    # pretty format
    items = [(json.dumps(k), json.dumps(v)) for k, v in payload.items()]
    max_key = max((len(k) for k, _ in items), default=0)
    lines = ["{"]
    pad = " " * 2
    for i, (k, v) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        space = " " * (max_key - len(k))
        lines.append(f"{pad}{k}{space}: {v}{comma}")
    lines.append("}")
    payload_str = "\n".join(lines)

    return payload, topic, payload_str


def configure_thermostat(client, name, thermostat_config, index, thermostat_types, mqtt_config, dry_run=False):
    print(f"\nConfiguring thermostat {index}: {name}")

    thermostat_type = thermostat_config["type"]
    type_config = thermostat_types.get(thermostat_type)
    if not type_config:
        print(f"Unknown thermostat type: {thermostat_type}")
        return

    payload, topic, payload_str = build_expected_payload(name, thermostat_config, thermostat_types, mqtt_config)

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
    parser.add_argument('--dry-run', action='store_true', help='Do not connect to MQTT; only print topics/payloads')
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
    print(f"Configuring {len(thermostats)} thermostats...\n")

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

        for i, (thermostat_name, config) in enumerate(thermostats.items()):
            result = configure_thermostat(client, thermostat_name, config, i+1, thermostat_types, mqtt_cfg, dry_run=args.dry_run)
            if result and not args.dry_run:
                pass
            if not args.dry_run:
                time.sleep(mqtt_cfg.get('delay_between_messages', 1))

        print("\n=== Configuration Complete ===")
        if args.dry_run:
            print("Dry run complete — no MQTT connections were made.")
        else:
            print("All thermostats have been configured with their schedules.")

    except Exception as e:
        print(f"Error: {e}")

    finally:
        if not args.dry_run and client is not None:
            client.loop_stop()
            client.disconnect()
            print("Disconnected from MQTT broker.")
        print_thermostat_table(thermostats)


if __name__ == "__main__":
    main()
