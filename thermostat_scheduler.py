#!/usr/bin/env python3
"""
Thermostat Schedule Controller
Publishes daily temperature schedules to different thermostat types via MQTT
"""

import json
import time
import paho.mqtt.client as mqtt

# Configuration
MQTT_BROKER = "192.168.1.4"  # Change to your MQTT broker IP/hostname
MQTT_PORT = 1883
MQTT_BASE_TOPIC = "zigbee2mqtt"
MQTT_DELAY_BETWEEN_MESSAGES = 5  # seconds

# Dictionary of all thermostats
THERMOSTATS = {
    "Arbeitszimmer": {
        "day_hour": "09:00",
        "day_temperature": 21.5,
        "night_hour": "23:00", 
        "night_temperature": 19.5,
        "type": "ME168_1"
    },
    "Bad OG": {
        "day_hour": "05:00",
        "day_temperature": 21.5,
        "night_hour": "23:00",
        "night_temperature": 19.5,
        "type": "VNTH-T2_v2"
    },
    "Caros": {
        "day_hour": "05:00",
        "day_temperature": 21.5,
        "night_hour": "00:00",
        "night_temperature": 19.5,
        "type": "TRVZB"
    },
    "Dusche": {
        "day_hour": "05:00",
        "day_temperature": 19.5,
        "night_hour": "23:00",
        "night_temperature": 18.5,
        "type": "ME168_1"
    },
    "Esszimmer": {
        "day_hour": "05:00",
        "day_temperature": 21.5,
        "night_hour": "23:00",
        "night_temperature": 20.5,
        "type": "VNTH-T2_v2"
    },
    "Julians": {
        "day_hour": "06:00",
        "day_temperature": 24,
        "night_hour": "23:00",
        "night_temperature": 20.5,
        "type": "VNTH-T2_v2"
    },
    "Schlafzimmer": {
        "day_hour": "05:00",
        "day_temperature": 20.5,
        "night_hour": "00:00",
        "night_temperature": 20,
        "type": "TRVZB"
    },
    "Waschküche": {
        "day_hour": "05:00",
        "day_temperature": 20.5,
        "night_hour": "23:00",
        "night_temperature": 19.5,
        "type": "TR-M3Z"
    },
    "WC OG": {
        "day_hour": "05:00",
        "day_temperature": 20.5,
        "night_hour": "23:00",
        "night_temperature": 19.5,
        "type": "VNTH-T2_v2"
    },
    "Wohnzimmer": {
        "day_hour": "05:00",
        "day_temperature": 22.5,
        "night_hour": "23:00",
        "night_temperature": 20.5,
        "type": "VNTH-T2_v2"
    }
}

# Dictionary of thermostat types with their specific keys and values for schedule mode
THERMOSTAT_TYPES = {
    "VNTH-T2_v2": {
        "temperature_sensitivity": 0.5,
        "system_mode": "heat",
        "preset": "schedule"
    },
    "TR-M3Z": {
        "system_mode": "heat",
        "preset": "schedule"
    },
    "ME168_1": {
        "system_mode": "auto"
    },
    "ME167": {
        "system_mode": "auto"
    },
    "TRVZB": {
        "system_mode": "auto",
        "temperature_accuracy": -0.6
    }
}

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
    - 2 pairs from night to day
    - 4 pairs from day to night
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

    # 2 pairs from night to day (use night_temp)
    for i in range(2):
        t = int(round((night_minutes + i * step_night_to_day) % DAY_MINUTES))
        schedule_pairs.append(f"{minutes_to_time(t)}/{night_temp}")

    # 4 pairs from day to night (use day_temp)
    for i in range(4):
        t = int(round((day_minutes + i * step_day_to_night) % DAY_MINUTES))
        schedule_pairs.append(f"{minutes_to_time(t)}/{day_temp}")

    # Sort pairs by time (HH:MM) ascending
    schedule_pairs.sort(key=lambda pair: time_to_minutes(pair.split('/')[0]))

    return " ".join(schedule_pairs)

def on_connect(client, userdata, flags, rc, properties=None):
    """Callback for MQTT connection """
    if rc == 0:
        print("Connected to MQTT broker successfully")
        global connected
        connected = True
    else:
        print(f"Failed to connect to MQTT broker. Return code: {rc}")

def on_publish(client, userdata, mid, rc, properties=None):
    """Callback for successful message publish """
    print(f"Message published successfully (Message ID: {mid})")

def configure_thermostat(client, thermostat_name, thermostat_config, index):
    """Configure a single thermostat with schedule"""

    print(f"\nConfiguring thermostat {index}: {thermostat_name}")

    # Get thermostat type configuration
    thermostat_type = thermostat_config["type"]
    type_config = THERMOSTAT_TYPES.get(thermostat_type)
    
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
    
    # Construct payload
    payload = type_config.copy()  # Start with thermostat type configuration
    
    # Add schedule for each weekday
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    prefix = "schedule"
    if thermostat_type == "TRVZB":
        prefix = "weekly_schedule"
    for weekday in weekdays:
        payload[f"{prefix}_{weekday}"] = schedule_string
    
    # Construct topic
    topic = f"{MQTT_BASE_TOPIC}/{thermostat_name}/set"
    
    # Convert payload to JSON
    payload_json = json.dumps(payload, indent=2)
    
    print(f"Topic: {topic}")
    print(f"Payload: {payload_json}")
    
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

def print_thermostat_table():
    """Print a table of all thermostat settings (name, type, day/night hours and temps)."""
    headers = ["Name", "Day Hour", "Day Temp", "Night Hour", "Night Temp", "Type"]
    rows = []
    for name, cfg in THERMOSTATS.items():
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
    
    print("=== Thermostat Schedule Controller ===")
    print(f"MQTT Broker: {MQTT_BROKER}:{MQTT_PORT}")
    print(f"Base Topic: {MQTT_BASE_TOPIC}")
    print(f"Configuring {len(THERMOSTATS)} thermostats...\n")
    
    # Create MQTT client
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_publish = on_publish
    
    # Optional: Set username and password if required
    # client.username_pw_set("username", "password")
    
    try:
        # Connect to MQTT broker
        print(f"Connecting to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}...")
        global connected
        connected = False
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        
        # Start the network loop
        client.loop_start()
        
        # Wait for connection
        while not connected:
            time.sleep(0.1)
        
        # Configure each thermostat
        for i, (thermostat_name, config) in enumerate(THERMOSTATS.items()):
            configure_thermostat(client, f"{thermostat_name} Thermostat", config, i+1)
            time.sleep(MQTT_DELAY_BETWEEN_MESSAGES)  # Delay to ensure message processing

        print("\n=== Configuration Complete ===")
        print("All thermostats have been configured with their schedules.")
        
    except Exception as e:
        print(f"Error: {e}")
    
    finally:
        # Cleanup
        client.loop_stop()
        client.disconnect()
        print("Disconnected from MQTT broker.")
        # Print table of settings at the end
        print_thermostat_table()

if __name__ == "__main__":
    main()

