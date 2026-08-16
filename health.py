#!/usr/bin/env python3
"""
Health classification for thermostats and sensors.

Pure functions: given the expected payload, the last reported state and the
last-seen timestamp, produce a list of `Issue`s. No I/O, no MQTT — easy to
unit-test. The daemon feeds these into the `Alerter`.

Issue kinds:
  no_life_sign      device hasn't reported within its expected interval
  battery_low       battery_low flag set, or battery level below limit
  settings_mismatch reported state doesn't match the pushed schedule (persisted)
  manual_override   end user took manual control (info/warn, not a fault)
  no_reaction       valve demanding heat/cool but room temp not moving toward target
  device_fault      the device reports a fault about itself (fault_alarm, ...)
"""

from common import compare_and_collect_mismatches, build_expected_payload
from alerts import make_issue
import cooling


def stale_temp_issue(key, subject, what, temp_seen_ts, now_ts, stale_secs):
    """Flag a temperature reading that has not *changed* for too long.

    `temp_seen_ts` is the (restart-grace-floored) epoch of the last change in the
    monitored reading. A device can still be on the mesh and publishing — a frozen
    value is its own warning sign of an unreliable sensor to restart or replace, so
    this is an alert independent of the no-life-sign check. Returns an Issue or None.
    """
    if temp_seen_ts is None or stale_secs <= 0:
        return None
    age = now_ts - temp_seen_ts
    if age <= stale_secs:
        return None
    hours = age / 3600.0
    return make_issue(
        key, 'stale_temperature', subject,
        f"{what} unchanged for {hours:.1f}h (frozen — restart or replace the device)")


# z2m fields through which a TRV reports a fault about itself. Values meaning
# "no fault" are filtered by `_fault_active`; anything else is surfaced verbatim,
# so an unrecognised code still reaches the report instead of being swallowed.
FAULT_FIELDS = ('fault_alarm', 'valve_alarm', 'error')

_FAULT_CLEAR = {'', 'none', 'null', 'off', 'false', '0', 'no', 'ok', 'normal', 'clear'}


def _fault_active(value):
    """True if a fault field carries something other than a 'no fault' value.

    Some types report a nested payload rather than a scalar (TR-M3Z sends
    `fault_alarm: {'error': 2}`), so a dict is active if any member is.
    """
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, dict):
        return any(_fault_active(v) for v in value.values())
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() not in _FAULT_CLEAR


def _fault_parts(field, value):
    """Render one fault field as readable `name=value` parts, flattening the
    nested form so a report reads `fault_alarm.error=2`, not a raw dict."""
    if isinstance(value, dict):
        parts = []
        for k, v in sorted(value.items()):
            if _fault_active(v):
                parts += _fault_parts(f"{field}.{k}", v)
        return parts
    return [f"{field}={value}"]


def device_fault_issue(key, subject, reported):
    """Flag a hardware fault the device reports about itself.

    Nothing else here catches this: a faulted TRV keeps talking on the mesh and
    its battery/life-sign look fine, yet the valve silently refuses commands and
    the room drifts. Bad OG sat at `fault_alarm: 2` with a fresh 100% battery,
    ignoring every write including setpoints, while the status page showed it as
    a healthy room. Stays open — and so stays in the report mails — for as long
    as the device keeps reporting the fault. Returns an Issue or None.
    """
    if not isinstance(reported, dict):
        return None
    parts = []
    for f in FAULT_FIELDS:
        if _fault_active(reported.get(f)):
            parts += _fault_parts(f, reported.get(f))
    if not parts:
        return None
    detail = ", ".join(sorted(parts))
    return make_issue(
        key, 'device_fault', subject,
        f"device reports {detail} — the valve may be refusing commands "
        f"(check the valve pin/battery, then remount the head to re-run adaptation)")


def battery_issue(reported, limit):
    """Return a human battery string if the battery is low/flagged, else None."""
    if not isinstance(reported, dict):
        return None
    if reported.get('battery_low') is True:
        return "battery low"
    level = reported.get('battery')
    if isinstance(level, (int, float)) and level < limit:
        return f"battery {level}%"
    return None


def classify_device(name, cfg_item, thermostat_types, mqtt_cfg,
                    reported, last_seen_ts, now_ts, limits, mode='heating'):
    """Classify one thermostat. `limits` carries battery_limit + unseen_interval.

    Manual-override and settings-mismatch checks only run in heating mode: in
    cooling mode the device is intentionally driven off its weekly schedule, so
    those comparisons are meaningless. Battery and life-sign always run.
    """
    issues = []
    subject = f"{name} thermostat"
    type_cfg = thermostat_types.get(cfg_item.get('type'), {})
    unseen = cfg_item.get('unseen_interval', limits.get('unseen_interval', 1800))

    # life sign
    if last_seen_ts is None:
        issues.append(make_issue(f"{name}:life", 'no_life_sign', subject,
                                 "no MQTT message since the manager started"))
        return issues  # nothing else is trustworthy without a recent report
    if now_ts - last_seen_ts > unseen:
        mins = int((now_ts - last_seen_ts) / 60)
        issues.append(make_issue(f"{name}:life", 'no_life_sign', subject,
                                 f"not seen for {mins} min"))

    if not isinstance(reported, dict):
        return issues

    # battery
    bat = battery_issue(reported, limits.get('battery_limit', 20))
    if bat:
        issues.append(make_issue(f"{name}:battery", 'battery_low', subject, bat))

    # self-reported hardware fault — checked in every season (a valve faults just
    # as easily while cooling), so it must precede the heating-only early return.
    fault = device_fault_issue(f"{name}:fault", subject, reported)
    if fault:
        issues.append(fault)

    # manual override vs settings mismatch (heating mode only)
    if mode != 'heating':
        return issues
    if cooling.is_manual_override(type_cfg, reported):
        issues.append(make_issue(f"{name}:manual", 'manual_override', subject,
                                 "in manual mode — comfort control suspended for this room",
                                 severity='info'))
    else:
        try:
            expected, _ = build_expected_payload(name, cfg_item, thermostat_types, mqtt_cfg)
            mism = compare_and_collect_mismatches(expected, reported)
        except Exception:
            mism = {}
        if mism:
            keys = ", ".join(sorted(mism.keys()))
            issues.append(make_issue(f"{name}:settings", 'settings_mismatch', subject,
                                     f"not applying configured settings ({keys})"))
    return issues


def no_reaction_issue(name, history, mode, setpoint, min_minutes, deadband=0.3):
    """Detect a valve demanding but the room temperature not following.

    `history` is a list of (ts, running_state, temp) samples, oldest first.
    If the device has been demanding (running_state == 'heat') for the whole
    window of at least `min_minutes`, and the room temperature has not moved
    toward the setpoint by more than `deadband`, flag it. Returns an Issue or
    None. Best-effort: needs both a running_state and a temperature reading.
    """
    if setpoint is None or not history:
        return None
    span = history[-1][0] - history[0][0]
    if span < min_minutes * 60:
        return None
    states = [h[1] for h in history if h[1] is not None]
    if not states or any(s != 'heat' for s in states):
        return None  # not continuously demanding
    temps = [h[2] for h in history if isinstance(h[2], (int, float))]
    if len(temps) < 2:
        return None
    # progress toward setpoint over the window
    if mode == 'cooling':
        progress = temps[0] - temps[-1]   # we want temp to fall
        off_target = temps[-1] > setpoint
    else:
        progress = temps[-1] - temps[0]   # we want temp to rise
        off_target = temps[-1] < setpoint
    if off_target and progress < deadband:
        return make_issue(f"{name}:reaction", 'no_reaction', f"{name} thermostat",
                          f"valve demanding for {int(span/60)} min but room "
                          f"temp {temps[-1]} not moving toward {setpoint}")
    return None
