#!/usr/bin/env python3
"""
Thermostat Monitor
Subscribes to each device's state topic and remembers last seen time and payload.
Listens on topic `thermostat_monitor` for a payload of "get" and replies with a
JSON list of objects: {"name": ..., "value": {"last_seen": ..., "state": ...}}

Uses the same `config.yaml` format as `thermostat_scheduler.py`.
"""

import time
import json
import argparse
import threading

from thermostat_scheduler import load_config

try:
    import paho.mqtt.client as mqtt
except Exception as e:
    print(f"Failed to import paho.mqtt.client: {e}")
    raise


def iso_now():
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime())


def main():
    parser = argparse.ArgumentParser(description='Thermostat monitor')
    parser.add_argument('--config', '-c', default='config.yaml', help='Path to YAML config file')
    args = parser.parse_args()

    cfg = load_config(args.config)
    mqtt_cfg = cfg.get('mqtt', {})
    thermostats = cfg.get('thermostats', {})

    base = mqtt_cfg.get('base_topic')
    monitor_topic = 'thermostat_monitor'

    # state storage
    start_time = iso_now()
    last_seen = {}
    last_state = {}

    # map topic -> name for quick lookup
    topic_to_name = {}
    for name in thermostats.keys():
        device_topic_name = f"{name} Thermostat"
        state_topic = f"{base}/{device_topic_name}"
        topic_to_name[state_topic] = name
        # initialize with start time and unknown state
        last_seen[name] = start_time
        last_state[name] = None

    def on_connect(client, userdata, flags, rc, properties=None):
        if rc == 0:
            print("Connected to MQTT broker")
            # subscribe to device topics and monitor topic
            for topic in topic_to_name.keys():
                client.subscribe(topic)
            client.subscribe(monitor_topic)
            print(f"Subscribed to {len(topic_to_name)} device topics and '{monitor_topic}'")
        else:
            print(f"MQTT connect failed with rc={rc}")

    def on_message(client, userdata, msg):
        t = msg.topic
        payload = msg.payload.decode('utf-8', errors='ignore')
        now = iso_now()

        if t == monitor_topic:
            # request from external actor
            if payload.strip().lower() == 'get':
                # build response list
                out = []
                for name in thermostats.keys():
                    seen = last_seen.get(name, start_time)
                    state = last_state.get(name)
                    val = {
                        'last_seen': seen,
                        'state': state if state is not None else 'unknown'
                    }
                    out.append({'name': name, 'value': val})
                resp = json.dumps(out)
                client.publish(monitor_topic, resp, qos=1)
            return

        # device topic
        name = topic_to_name.get(t)
        if not name:
            return
        # attempt to parse JSON payload
        try:
            obj = json.loads(payload)
        except Exception:
            # keep raw string as state if not JSON
            obj = payload
        last_seen[name] = now
        last_state[name] = obj

    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message

    if mqtt_cfg.get('username'):
        client.username_pw_set(mqtt_cfg.get('username'), mqtt_cfg.get('password'))

    print(f"Connecting to MQTT broker {mqtt_cfg.get('broker')}:{mqtt_cfg.get('port')}")
    client.connect(mqtt_cfg.get('broker'), mqtt_cfg.get('port'), 60)
    client.loop_forever()


if __name__ == '__main__':
    main()
