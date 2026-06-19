#!/usr/bin/env python3
"""
Zigbee sensor integration (temperature + door/window contact + battery).

Sensors are read directly from zigbee2mqtt MQTT (not via Home Assistant). Each
thermostat room may declare:

  sensors:
    temperature: "Bad OG Luft"      # friendly name of a temp/humidity sensor
    windows: ["Bad OG"]             # friendly names of contact sensors

`evaluate_room` warns when the room temperature does not follow the active
setpoint — but suppresses that warning when a window/door in the room is open,
since that explains the deviation (reported instead as info). Sensor battery
and life-sign checks reuse the same machinery as thermostats.
"""

from alerts import make_issue
from health import battery_issue


def window_open(contact_state):
    """zigbee2mqtt contact sensors report contact=true when CLOSED."""
    if not isinstance(contact_state, dict):
        return False
    if 'contact' in contact_state:
        return contact_state.get('contact') is False
    return False


def evaluate_room(room, temp_state, windows_states, setpoint, mode, tolerance):
    """Return issues for one room's comfort situation.

    temp_state: parsed temp sensor payload (or None)
    windows_states: dict of {friendly_name: parsed_payload}
    """
    issues = []
    any_open = any(window_open(s) for s in (windows_states or {}).values())

    temp = temp_state.get('temperature') if isinstance(temp_state, dict) else None
    if temp is None or setpoint is None:
        return issues

    too_cold = mode == 'heating' and temp < setpoint - tolerance
    too_hot = mode == 'cooling' and temp > setpoint + tolerance
    if too_cold or too_hot:
        direction = "below" if too_cold else "above"
        if any_open:
            issues.append(make_issue(
                f"{room}:window", 'window_open', f"{room} room",
                f"temp {temp}°C {direction} target {setpoint}°C, but a window is open",
                severity='info'))
        else:
            issues.append(make_issue(
                f"{room}:temp", 'temp_unexpected', f"{room} room",
                f"temp {temp}°C {direction} target {setpoint}°C (±{tolerance})"))
    return issues


def classify_sensor(name, kind, reported, last_seen_ts, now_ts, limits):
    """Battery + life-sign check for a standalone sensor (temp / contact / leak)."""
    issues = []
    subject = f"{name} sensor"
    unseen = limits.get('unseen_interval', 7200)

    if last_seen_ts is None:
        issues.append(make_issue(f"{name}:life", 'no_life_sign', subject,
                                 "no MQTT message since the manager started"))
        return issues
    if now_ts - last_seen_ts > unseen:
        mins = int((now_ts - last_seen_ts) / 60)
        issues.append(make_issue(f"{name}:life", 'no_life_sign', subject,
                                 f"not seen for {mins} min"))

    bat = battery_issue(reported, limits.get('battery_limit', 20))
    if bat:
        issues.append(make_issue(f"{name}:battery", 'battery_low', subject, bat))

    if isinstance(reported, dict) and reported.get('water_leak') is True:
        issues.append(make_issue(f"{name}:leak", 'water_leak', subject,
                                 "WATER LEAK detected"))
    return issues
