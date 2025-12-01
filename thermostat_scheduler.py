#!/usr/bin/env python3
"""
Thermostat Schedule Controller
Publishes daily temperature schedules to different thermostat types via MQTT
"""

import json
import time
from datetime import datetime, timedelta
import paho.mqtt.client as mqtt

# Configuration
MQTT_BROKER = "192.168.1.4"  # Change to your MQTT broker IP/hostname
MQTT_PORT = 1883
MQTT_BASE_TOPIC = "zigbee2mqtt"

# Dictionary of all thermostats
THERMOSTATS = {
    "Arbeitszimmer": {
        "day_hour": "05:00",
        "day_temperature": 21,
        "night_hour": "23:00", 
        "night_temperature": 19,
        "type": "ME168_1"
    },
    "Bad OG": {
        "day_hour": "05:00",
        "day_temperature": 21,
        "night_hour": "23:00",
        "night_temperature": 19,
        "type": "VNTH-T2_v2"
    },
    "Caros": {
        "day_hour": "05:00",
        "day_temperature": 21,
        "night_hour": "23:00",
        "night_temperature": 19,
        "type": "VNTH-T2_v2"
    },
    "Dusche": {
        "day_hour": "05:00",
        "day_temperature": 21,
        "night_hour": "23:00",
        "night_temperature": 19,
        "type": "ME168_1"
    },
    "Esszimmer": {
        "day_hour": "05:00",
        "day_temperature": 21,
        "night_hour": "23:00",
        "night_temperature": 19,
        "type": "VNTH-T2_v2"
    },
    "Julians": {
        "day_hour": "05:00",
        "day_temperature": 21,
        "night_hour": "23:00",
        "night_temperature": 19,
        "type": "VNTH-T2_v2"
    },
    "Schlafzimmer": {
        "day_hour": "05:00",
        "day_temperature": 21,
        "night_hour": "23:00",
        "night_temperature": 19,
        "type": "VNTH-T2_v2"
    },
    "Waschküche": {
        "day_hour": "05:00",
        "day_temperature": 21,
        "night_hour": "23:00",
        "night_temperature": 19,
        "type": "TR-M3Z"
    },
    "WC OG": {
        "day_hour": "05:00",
        "day_temperature": 21,
        "night_hour": "23:00",
        "night_temperature": 19,
        "type": "VNTH-T2_v2"
    },
    "Wohnzimmer": {
        "day_hour": "05:00",
        "day_temperature": 21,
        "night_hour": "23:00",
        "night_temperature": 19,
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
        schedule_pairs.append(f"{minutes_to_time(t)}/{int(night_temp)}")

    # 4 pairs from day to night (use day_temp)
    for i in range(4):
        t = int(round((day_minutes + i * step_day_to_night) % DAY_MINUTES))
        schedule_pairs.append(f"{minutes_to_time(t)}/{int(day_temp)}")

    return " ".join(schedule_pairs)

def on_connect(client, userdata, flags, rc):
    """Callback for MQTT connection"""
    if rc == 0:
        print("Connected to MQTT broker successfully")
    else:
        print(f"Failed to connect to MQTT broker. Return code: {rc}")

def on_publish(client, userdata, mid):
    """Callback for successful message publish"""
    print(f"Message published successfully (Message ID: {mid})")

def configure_thermostat(client, thermostat_name, thermostat_config):
    """Configure a single thermostat with schedule"""
    
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
    for weekday in weekdays:
        payload[f"schedule_{weekday}"] = schedule_string
    
    # Construct topic
    topic = f"{MQTT_BASE_TOPIC}/{thermostat_name}/set"
    
    # Convert payload to JSON
    payload_json = json.dumps(payload, indent=2)
    
    print(f"\nConfiguring thermostat: {thermostat_name}")
    print(f"Topic: {topic}")
    print(f"Payload: {payload_json}")
    
    # Publish to MQTT
    # result = client.publish(topic, payload_json, qos=1, retain=False)
    
    # if result.rc == mqtt.MQTT_ERR_SUCCESS:
    #     print(f"✓ Successfully sent configuration to {thermostat_name}")
    # else:
    #     print(f"✗ Failed to send configuration to {thermostat_name}")
    
    return

def main():
    """Main function to configure all thermostats"""
    
    print("=== Thermostat Schedule Controller ===")
    print(f"MQTT Broker: {MQTT_BROKER}:{MQTT_PORT}")
    print(f"Base Topic: {MQTT_BASE_TOPIC}")
    print(f"Configuring {len(THERMOSTATS)} thermostats...\n")
    
    # Create MQTT client
    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_publish = on_publish
    
    # Optional: Set username and password if required
    # client.username_pw_set("username", "password")
    
    try:
        # Connect to MQTT broker
        print(f"Connecting to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}...")
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        
        # Start the network loop
        client.loop_start()
        
        # Wait for connection
        time.sleep(1)
        
        # Configure each thermostat
        for thermostat_name, config in THERMOSTATS.items():
            configure_thermostat(client, f"{thermostat_name} Thermostat", config)
            time.sleep(0.5)  # Small delay between configurations
        
        # Wait for all messages to be sent
        time.sleep(2)
        
        print("\n=== Configuration Complete ===")
        print("All thermostats have been configured with their schedules.")
        
    except Exception as e:
        print(f"Error: {e}")
    
    finally:
        # Cleanup
        client.loop_stop()
        client.disconnect()
        print("Disconnected from MQTT broker.")

if __name__ == "__main__":
    main()

