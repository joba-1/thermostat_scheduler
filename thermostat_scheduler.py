#!/usr/bin/env python3
"""
Thermostat Schedule Controller
Publishes daily temperature schedules to different thermostat types via MQTT
"""

import json
import time
import argparse
import sys
import threading

from common import (
    setup_logging,
    log,
    load_config,
    generate_schedule_string,  # noqa: F401  (kept importable for tests/back-compat)
    compare_schedule_strings,  # noqa: F401
    compare_and_collect_mismatches,
    normalize_str,  # noqa: F401
    battery_status_note,
    build_expected_payload,
    pretty_payload,
    mqtt_credentials,
)

# Re-exported for back-compatibility / unit tests that import from this module.
from common import time_to_minutes, minutes_to_time  # noqa: F401
import cooling
import heatpump


def on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        log.info("Connected to MQTT broker successfully")
        if isinstance(userdata, threading.Event):
            userdata.set()
        elif isinstance(userdata, dict) and 'connect_event' in userdata and isinstance(userdata['connect_event'], threading.Event):
            userdata['connect_event'].set()
    else:
        log.error("Failed to connect to MQTT broker. Return code: %s", rc)


def on_publish(client, userdata, mid, rc, properties=None):
    """Callback for successful message publish """
    log.debug("Message published successfully (Message ID: %s)", mid)


def on_message(client, userdata, msg):
    """Collect incoming JSON state messages into userdata['responses'] by topic."""
    try:
        payload = json.loads(msg.payload.decode('utf-8'))
    except Exception:
        # ignore non-json payloads
        return
    responses = userdata.setdefault('responses', {})
    responses[msg.topic] = payload


def query_monitor(client, userdata, timeout):
    """Ask thermostat_monitor for current per-device state; return {name: payload}."""
    monitor_base = 'thermostat_monitor'
    try:
        client.subscribe(f"{monitor_base}/+")
        client.publish(monitor_base, 'get')
    except Exception:
        pass
    time.sleep(timeout)
    responses = userdata.get('responses', {}) if isinstance(userdata, dict) else {}
    checked = {}
    for topic, payload in responses.items():
        if not topic.startswith(monitor_base + '/'):
            continue
        checked[topic.split('/', 1)[1]] = payload
    return checked


def list_manual(cfg, client, userdata, timeout=None):
    """Print thermostats currently reporting an end-user manual override."""
    if timeout is None:
        timeout = cfg.get('mqtt', {}).get('check_timeout', 5)
    checked = query_monitor(client, userdata, timeout)
    print()
    thermostat_types = cfg.get('thermostat_types', {})
    manual = []
    for name, item in cfg.get('thermostats', {}).items():
        type_cfg = thermostat_types.get(item.get('type'), {})
        state = (checked.get(name) or {}).get('state')
        if cooling.is_manual_override(type_cfg, state):
            manual.append(name)
    if manual:
        print("Thermostats in MANUAL override:")
        for name in manual:
            print(f"  - {name}")
        print("\nReset with: thermostat_scheduler.py --reset-manual "
              + " ".join(f'"{n}"' for n in manual))
    else:
        print("No thermostats are in manual override.")
    return manual


def request_status(cfg, client, userdata, mail=False, timeout=None):
    """Ask the running manager daemon for a full status report.

    The daemon builds the report from its full accumulated state (so it has
    real data, not the '?' of a cold one-shot) and publishes it back; with
    `mail=True` it also emails it. Returns the report text or None.
    """
    if timeout is None:
        timeout = max(cfg.get('mqtt', {}).get('check_timeout', 5), 4)
    monitor_base = 'thermostat_monitor'
    try:
        client.subscribe(f"{monitor_base}/_report")
        client.publish(monitor_base, 'status-mail' if mail else 'report')
    except Exception:
        pass
    time.sleep(timeout)
    responses = userdata.get('responses', {}) if isinstance(userdata, dict) else {}
    payload = responses.get(f"{monitor_base}/_report")
    report = payload.get('report') if isinstance(payload, dict) else None
    print()
    if report is None:
        print("No response from the manager daemon — is thermostat_monitor "
              "running and connected to the broker?")
        return None
    print(report)
    if mail:
        print("\nStatus report emailed by the manager.")
    return report


def detect_mode(cfg, client, userdata, timeout):
    """Determine the active season + collect per-device state from the daemon.

    Subscribes to the (retained) heat-pump topics, asks `thermostat_monitor` for
    current device state, and derives the desired mode. Returns
    (mode, hp_state, checked) where checked is {name: {'state', 'last_seen'}}.
    """
    hp_cfg = cfg.get('heatpump', {})
    season_cfg = cfg.get('season', {})
    if hp_cfg.get('enabled') and hp_cfg.get('boiler_topic'):
        try:
            client.subscribe(hp_cfg['boiler_topic'])
            if hp_cfg.get('thermostat_topic'):
                client.subscribe(hp_cfg['thermostat_topic'])
        except Exception:
            pass
    checked = query_monitor(client, userdata, timeout)
    responses = userdata.get('responses', {}) if isinstance(userdata, dict) else {}
    hp_state = None
    if hp_cfg.get('enabled'):
        boiler = responses.get(hp_cfg.get('boiler_topic'))
        thermo = responses.get(hp_cfg.get('thermostat_topic'))
        if boiler is not None or thermo is not None:
            hp_state = heatpump.parse(boiler, thermo, hp_cfg)
    # Thread the outdoor temperature through, same as the daemon does: with
    # season.source == 'outdoor_temp' a missing reading silently falls back to
    # 'heating', which would push winter schedules onto open valves mid-summer.
    outdoor = (hp_state.get('telemetry') or {}).get('outdoor') if hp_state else None
    mode = cooling.desired_mode(season_cfg, hp_state, outdoor)
    if mode == 'heating' and season_cfg.get('mode', 'auto') == 'auto' \
            and season_cfg.get('source') == 'outdoor_temp' and outdoor is None:
        raise RuntimeError(
            "no outdoor temperature available (heat pump telemetry missing), so the "
            "season cannot be determined and would default to heating. Refusing to "
            "act season-blind — check the heat pump/MQTT, or set season.mode "
            "explicitly in config.yaml.")
    return mode, hp_state, checked


def configure_intended(cfg, client, userdata, dry_run=False, timeout=None):
    """Reinstate every thermostat's currently intended state (season-aware).

    Heating -> weekly schedule; cooling -> valves open. Devices in manual
    override or off keep that state (only their stored schedule/calibration is
    refreshed). Uses the running manager daemon for live state + heat-pump mode.
    """
    if timeout is None:
        timeout = max(cfg.get('mqtt', {}).get('check_timeout', 5), 4)
    mqtt_cfg = cfg.get('mqtt', {})
    thermostats = cfg.get('thermostats', {})
    thermostat_types = cfg.get('thermostat_types', {})

    mode, hp_state, checked = detect_mode(cfg, client, userdata, timeout)
    print(f"\nIntended mode: {mode}"
          + (f" (heat pump: {hp_state['mode']})" if hp_state else "")
          + ("  [no daemon — can't detect manual/off per device]" if not checked else ""))

    for i, (name, cfg_item) in enumerate(thermostats.items()):
        reported = (checked.get(name) or {}).get('state')
        try:
            payload, topic, note = cooling.build_intended_payload(
                name, cfg_item, thermostat_types, mqtt_cfg, mode, reported)
        except Exception as e:
            print(f"{name}: error building payload: {e}")
            continue
        if not payload:
            print(f"{name}: {note}")
            continue
        print(f"\n{name} [{note}]\n  {topic}\n  {pretty_payload(payload, indent=2)}")
        if dry_run or client is None:
            continue
        try:
            info = client.publish(topic, json.dumps(payload), qos=1, retain=False)
            try:
                info.wait_for_publish(timeout=10)
            except Exception:
                pass
            print(f"  {'✓ sent' if getattr(info, 'is_published', lambda: False)() else '✗ not confirmed'}")
        except Exception as e:
            print(f"  publish error: {e}")
        time.sleep(mqtt_cfg.get('delay_between_messages', 1))


def reset_manual(cfg, client, userdata, targets, timeout=None):
    """Re-onboard named thermostats from manual override into active-season control.

    `targets` is a list of room names, or None for "every room currently in
    manual override" (resolved from live daemon state below).

    Like `configure_intended` but with `reclaim_manual=True`: a device currently
    in manual override is pushed to the season-intended state (cooling -> open,
    heating -> schedule) instead of being left alone. A device that is genuinely
    OFF (e.g. window open) is still left off — we never force an open valve on a
    window. This is the one-step fix for "a TRV was touched; put it back under
    control" (see also the status page's re-onboard hint).
    """
    if timeout is None:
        timeout = max(cfg.get('mqtt', {}).get('check_timeout', 5), 4)
    mqtt_cfg = cfg.get('mqtt', {})
    thermostats = cfg.get('thermostats', {})
    thermostat_types = cfg.get('thermostat_types', {})

    mode, hp_state, checked = detect_mode(cfg, client, userdata, timeout)
    print(f"\nRe-onboarding (mode: {mode}"
          + (f", heat pump: {hp_state['mode']}" if hp_state else "")
          + (")  [no daemon — can't detect off per device]" if not checked else ")"))

    if targets is None:
        # Whole-house default: exactly the manual-override rooms, nothing else.
        # Without live state we cannot tell manual from controlled, so rather
        # than guess (and rewrite the house) we do nothing.
        if not checked:
            print("  no daemon running — cannot tell which rooms are in manual "
                  "override; name the rooms explicitly to re-onboard them")
            return
        targets = [n for n, item in thermostats.items()
                   if cooling.is_manual_override(
                       thermostat_types.get(item.get('type'), {}),
                       (checked.get(n) or {}).get('state'))]
        if not targets:
            print("  no thermostats in manual override — nothing to do")
            return
        print(f"  manual-override rooms: {', '.join(targets)}")

    for name in targets:
        if name not in thermostats:
            print(f"  unknown thermostat: {name}")
            continue
        reported = (checked.get(name) or {}).get('state')
        try:
            payload, topic, note = cooling.build_intended_payload(
                name, thermostats[name], thermostat_types, mqtt_cfg, mode,
                reported, reclaim_manual=True)
        except Exception as e:
            print(f"{name}: error building payload: {e}")
            continue
        if not payload:
            print(f"{name}: {note}")
            continue
        print(f"\n{name} [{note}]\n  {topic}\n  {pretty_payload(payload, indent=2)}")
        if client is None:
            continue
        try:
            info = client.publish(topic, json.dumps(payload), qos=1, retain=False)
            try:
                info.wait_for_publish(timeout=10)
            except Exception:
                pass
            print(f"  {'✓ sent' if getattr(info, 'is_published', lambda: False)() else '✗ not confirmed'}")
        except Exception as e:
            print(f"  publish error: {e}")
        time.sleep(mqtt_cfg.get('delay_between_messages', 1))


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

    # Determine the active season first: in cooling mode the valves are
    # deliberately forced open, so comparing against the heating schedule would
    # report false violations for every device.
    mode, hp_state, checked = detect_mode(cfg, client, userdata, timeout)
    print()
    print(f"Mode: {mode}"
          + (f" (heat pump: {hp_state['mode']})" if hp_state else ""))
    print()

    thermostats = cfg.get('thermostats', {})
    thermostat_types = cfg.get('thermostat_types', {})

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
            reported = checked[name]['state']
        except KeyError:
            print(f"{name}: no monitored state found")
            continue

        battery_note = battery_status_note(reported, 20)
        type_cfg = thermostat_types.get(cfg_item.get('type'), {})

        # A manually overridden device is intentionally off-schedule (the user's
        # choice) — report it as such, not as a violation.
        if cooling.is_manual_override(type_cfg, reported):
            print(f"{name}: MANUAL override — comfort control suspended{battery_note}")
            continue

        # A switched-off device (e.g. window open) is intended, not a mismatch.
        if cooling.is_off(reported):
            print(f"{name}: OFF (e.g. window open) — left as-is{battery_note}")
            continue

        # Compare against what *should* be applied for the active season.
        if mode == 'cooling':
            expected = cooling.build_open_payload(type_cfg)
            label = "cooling/open"
        else:
            try:
                expected, _ = build_expected_payload(name, cfg_item, thermostat_types, cfg.get('mqtt', {}))
            except Exception as e:
                print(f"{name}: error building expected payload: {e}")
                continue
            label = "schedule"

        if not expected:
            print(f"{name}: no {label} payload to compare{battery_note}")
            continue

        mismatches = compare_and_collect_mismatches(expected, reported)
        if not mismatches:
            print(f"{name}: OK ({label}){battery_note}")
        else:
            print(f"{name}: MISMATCHES ({label}){battery_note}:")
            print_mismatch_table(mismatches, indent=2)

    print()
    print("Note: a TRV applies a /set and reports back only after several seconds")
    print("(battery Zigbee devices longest), so mismatches seen right after a")
    print("reconcile may be a stale report — re-check in ~30 s to confirm.")
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
    group.add_argument('--list-manual', action='store_true',
                       help='List thermostats currently in end-user manual override')
    group.add_argument('--reset-manual', nargs='*', metavar='NAME',
                       help='Re-onboard NAMEs from manual override into active-season '
                            'control (cooling->open, heating->schedule; off/window '
                            'rooms left off). All manual-override thermostats if no '
                            'NAME given.')
    group.add_argument('--status', action='store_true',
                       help='Print a full status report from the running manager')
    group.add_argument('--status-mail', action='store_true',
                       help='Have the running manager email a full status report')
    args = parser.parse_args()

    setup_logging()

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
        mqtt_user, mqtt_pass = mqtt_credentials(mqtt_cfg)
        if mqtt_user:
            client.username_pw_set(mqtt_user, mqtt_pass)

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

        if args.status or args.status_mail:
            request_status(cfg, client, userdata, mail=args.status_mail)
        elif args.check:
            check_thermostats(cfg, client, userdata, timeout=mqtt_cfg.get('check_timeout', 5))
        elif args.list_manual:
            list_manual(cfg, client, userdata, timeout=mqtt_cfg.get('check_timeout', 5))
        elif args.reset_manual is not None:
            # No NAMEs -> only the rooms actually in manual override (resolved in
            # reset_manual, which has the live state). Never the whole house: a
            # blanket reclaim rewrites rooms that were already under control.
            targets = args.reset_manual or None
            reset_manual(cfg, client, userdata, targets,
                         timeout=mqtt_cfg.get('check_timeout', 5))
        else:
            # Reinstate each thermostat's currently intended state (season-aware,
            # honouring manual/off). In --dry-run with no broker we can't read
            # live state, so it falls back to the heating schedule per device.
            configure_intended(cfg, client, userdata if not args.dry_run else {},
                               dry_run=args.dry_run)
            print("Dry run complete" if args.dry_run else "All thermostats reconciled")

    except Exception as e:
        print(f"Error: {e}")

    finally:
        if not args.dry_run and client is not None:
            client.loop_stop()
            client.disconnect()
            print("Disconnected from MQTT broker.")
        if not (args.check or args.list_manual or args.reset_manual is not None
                or args.status or args.status_mail):
            print_thermostat_table(thermostats)


if __name__ == "__main__":
    main()
