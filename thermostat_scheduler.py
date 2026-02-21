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

# No module-level config — pass config dicts around


def load_config(path):
        """Load configuration from a YAML file and apply to module globals.

        Expected YAML structure:
            mqtt:
                broker: host
                port: 1883
                base_topic: zigbee2mqtt
                delay_between_messages: 5
                username: user  # optional
                password: pass  # optional
            thermostats: { ... }
            thermostat_types: { ... }
        """

        if not os.path.exists(path):
            raise FileNotFoundError(f"Config file not found: {path}")

        with open(path, 'r') as f:
            cfg = yaml.safe_load(f) or {}

        # required top-level sections: try once for all three to report missing sections
        try:
            mqtt_cfg = cfg['mqtt']
            thermostats_cfg = cfg['thermostats']
            thermostat_types_cfg = cfg['thermostat_types']
        except KeyError as e:
            raise ValueError(f"Missing required top-level section in config: {e}")

        
        # Validate sections via helper
        validate_section('mqtt', mqtt_cfg, expected_type=dict, required_keys={
            'broker': str,
            'port': int,
            'base_topic': str,
            'delay_between_messages': int,
        })
        validate_section('thermostats', thermostats_cfg, expected_type=dict, per_item_required_keys={
            'type': str,
            'day_hour': 'time',
            'day_temperature': 'number',
            'night_hour': 'time',
            'night_temperature': 'number',
        })

        validate_section('thermostat_types', thermostat_types_cfg, expected_type=dict)

        # Thermostat_types may optionally supply a `schedule_prefix` string that
        # is used as the prefix for weekday schedule keys (default is "schedule_"). 
        # Type-specific payload keys needed to enter scheduling mode are placed
        # under the `schedule_mode` subsection. Validate those shapes.
        for tname, tcfg in thermostat_types_cfg.items():
            if not isinstance(tcfg, dict):
                raise ValueError(f"Thermostat type '{tname}' must be a mapping")
            # `schedule_prefix` is optional; when provided it must be a
            # string. `schedule_mode` is required and must be a mapping.
            if 'schedule_prefix' in tcfg and not isinstance(tcfg['schedule_prefix'], str):
                raise ValueError(f"Thermostat type '{tname}' key 'schedule_prefix' must be a string")
            if 'schedule_mode' not in tcfg:
                raise ValueError(f"Thermostat type '{tname}' must include a 'schedule_mode' mapping")
            if not isinstance(tcfg['schedule_mode'], dict):
                raise ValueError(f"Thermostat type '{tname}' key 'schedule_mode' must be a mapping")

        # Ensure every thermostat references an existing thermostat type
        missing_types = sorted({tcfg['type'] for tcfg in thermostats_cfg.values()} - set(thermostat_types_cfg.keys()))
        if missing_types:
            raise ValueError(f"Undefined thermostat types referenced: {', '.join(missing_types)}")

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


def validate_section(name, data, expected_type=dict, required_keys=None, per_item_required_keys=None):
    """Generic validator for configuration sections.

    - name: section name (for error messages)
    - data: the config object to validate
    - expected_type: expected top-level type (e.g., dict)
    - required_keys: dict of key->type expected at top-level of data
    - per_item_required_keys: dict of key->type for each item when data is a mapping of items

    Supported special types for required keys: 'time' (HH:MM) and 'number' (int/float or numeric string).
    """
    if not isinstance(data, expected_type):
        raise ValueError(f"'{name}' must be a {expected_type.__name__}")

    if required_keys:
        for k, expected in required_keys.items():
            if k not in data:
                raise ValueError(f"Missing key '{k}' in section '{name}'")
            val = data[k]
            if not isinstance(val, expected):
                raise ValueError(f"Key '{k}' in section '{name}' must be of type {expected.__name__}")

    if per_item_required_keys:
        if not isinstance(data, dict):
            raise ValueError(f"Section '{name}' must be a mapping for per-item validation")
        for item_name, item in data.items():
            if not isinstance(item, dict):
                raise ValueError(f"Item '{item_name}' in section '{name}' must be a mapping")
            for k, expected in per_item_required_keys.items():
                if k not in item:
                    raise ValueError(f"Item '{item_name}' in '{name}' missing key '{k}'")
                val = item[k]
                if expected == 'time':
                    # validate HH:MM
                    if not isinstance(val, str):
                        raise ValueError(f"Item '{item_name}' key '{k}' must be time string HH:MM")
                    m = re.match(r'^(\d{1,2}):(\d{2})$', val)
                    if not m:
                        raise ValueError(f"Item '{item_name}' key '{k}' must be time string HH:MM")
                    hh = int(m.group(1)); mm = int(m.group(2))
                    if not (0 <= hh < 24 and 0 <= mm < 60):
                        raise ValueError(f"Item '{item_name}' key '{k}' has invalid time value")
                elif expected == 'number':
                    try:
                        float(val)
                    except Exception:
                        raise ValueError(f"Item '{item_name}' key '{k}' must be numeric")
                else:
                    if not isinstance(val, expected):
                        raise ValueError(f"Item '{item_name}' key '{k}' must be of type {expected.__name__}")

def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        print("Connected to MQTT broker successfully")
        # userdata may be an Event (old behavior) or a dict with a
        # 'connect_event' key; support both.
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

def configure_thermostat(client, name, thermostat_config, index, thermostat_types, mqtt_config, dry_run=False):
    """Configure a single thermostat with schedule

    `name` is the YAML key (used both for topics and display output)
    """

    print(f"\nConfiguring thermostat {index}: {name}")

    # Get thermostat type configuration
    thermostat_type = thermostat_config["type"]
    type_config = thermostat_types.get(thermostat_type)
    
    if not type_config:
        print(f"Unknown thermostat type: {thermostat_type}")
        return
    
    # Generate schedule string
    schedule_string = generate_schedule_string(
        thermostat_config["day_hour"],
        thermostat_config["day_temperature"],
        thermostat_config["night_hour"],
        thermostat_config["night_temperature"]
    )
    
    # Construct payload from the required `schedule_mode` subsection.
    payload = type_config['schedule_mode'].copy()

    # Add schedule for each weekday using the configured prefix for this
    # thermostat type (defaults to 'schedule'). The prefix may be, e.g.,
    # 'schedule' or 'weekly_schedule' depending on the device.
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    prefix = type_config.get('schedule_prefix', 'schedule')
    for weekday in weekdays:
        payload[f"{prefix}_{weekday}"] = schedule_string
    
    # Construct topic: allow per-thermostat override via `topic` key in config
    # Topics are deterministic: '<base>/<name> Thermostat/set'
    device_topic_name = f"{name} Thermostat"
    topic = f"{mqtt_config.get('base_topic')}/{device_topic_name}/set"
    
    # Convert payload to a nicely formatted string with aligned colons for
    # readability when printed to the terminal.
    def format_payload_aligned(obj, indent=2):
        # Represent simple JSON values using json.dumps
        items = []
        for k, v in obj.items():
            items.append((json.dumps(k), json.dumps(v)))
        # compute max key width
        max_key = max((len(k) for k, _ in items), default=0)
        lines = ["{"]
        pad = " " * indent
        for i, (k, v) in enumerate(items):
            comma = "," if i < len(items) - 1 else ""
            # align the colon by padding the key to max_key
            space = " " * (max_key - len(k))
            lines.append(f"{pad}{k}{space}: {v}{comma}")
        lines.append("}")
        return "\n".join(lines)

    payload_str = format_payload_aligned(payload, indent=2)

    print(f"Topic: {topic}")
    print("Payload:")
    print(payload_str)

    if dry_run:
        print("Dry run: not publishing to MQTT.")
        return

    return payload, topic

    # Publish to MQTT using the newer synchronous API (returns MQTTMessageInfo)
    info = client.publish(topic, payload_json, qos=1, retain=False)
    try:
        info.wait_for_publish(timeout=10)
    except Exception:
        pass

    if getattr(info, "is_published", lambda: False)():
        print(f"✓ Successfully sent configuration to {thermostat_name}")
    else:
        print(f"✗ Failed to send configuration to {thermostat_name} (info: {info})")
    
    return

def print_thermostat_table(thermostats):
    """Print a table of all thermostat settings (name, type, day/night hours and temps)."""
    headers = ["Name", "Day Hour", "Day Temp", "Night Hour", "Night Temp", "Type"]
    rows = []
    for name, cfg in thermostats.items():
        rows.append([
            name,
            cfg.get("day_hour", ""),
            str(cfg.get("day_temperature", "")),
            cfg.get("night_hour", ""),
            str(cfg.get("night_temperature", "")),
            cfg.get("type", "")
        ])

    # compute column widths
    cols = list(zip(*([headers] + rows)))
    widths = [max(len(str(cell)) for cell in col) for col in cols]

    # format strings
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
    """Main function to configure all thermostats"""
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
        # Import paho only when actually needed
        try:
            import paho.mqtt.client as mqtt
        except Exception as e:
            print(f"Failed to import paho.mqtt.client: {e}")
            sys.exit(1)

        # Create MQTT client
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.on_connect = on_connect
        client.on_publish = on_publish
        client.on_message = on_message
        # Optional: Set username and password if provided
        if mqtt_cfg.get('username'):
            client.username_pw_set(mqtt_cfg.get('username'), mqtt_cfg.get('password'))

    try:
        if not args.dry_run:
            # Connect to MQTT broker
            print(f"Connecting to MQTT broker at {mqtt_cfg.get('broker')}:{mqtt_cfg.get('port')}...")
            evt = threading.Event()
            userdata = {'connect_event': evt, 'responses': {}}
            client.user_data_set(userdata)
            client.connect(mqtt_cfg.get('broker'), mqtt_cfg.get('port'), 60)

            # Start the network loop
            client.loop_start()

            # Wait for connection (10s timeout)
            if not userdata['connect_event'].wait(10):
                print("Warning: MQTT connection timeout")

        # Configure each thermostat (publish)
        for i, (thermostat_name, config) in enumerate(thermostats.items()):
            # thermostat_name is the key from YAML and used as the display name
            result = configure_thermostat(client, thermostat_name, config, i+1, thermostat_types, mqtt_cfg, dry_run=args.dry_run)
            if result and not args.dry_run:
                # result returned payload,topic only when not dry-run path
                pass
            if not args.dry_run:
                time.sleep(mqtt_cfg.get('delay_between_messages'))  # Delay to ensure message processing

        print("\n=== Configuration Complete ===")
        if args.dry_run:
            print("Dry run complete — no MQTT connections were made.")
        else:
            print("All thermostats have been configured with their schedules.")

    except Exception as e:
        print(f"Error: {e}")

    finally:
        # Cleanup
        if not args.dry_run and client is not None:
            client.loop_stop()
            client.disconnect()
            print("Disconnected from MQTT broker.")
        print_thermostat_table(thermostats)

if __name__ == "__main__":
    main()

