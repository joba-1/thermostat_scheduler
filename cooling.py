#!/usr/bin/env python3
"""
Cooling-mode control.

There is no universal "eco" preset across the five thermostat types, and eco
means a *lower* setpoint anyway, so it is the wrong lever for cooling. Instead,
each type is forced fully open by temporarily switching it out of its weekly
schedule and giving it a high "open" setpoint (a heating TRV opens its valve
when the room is below setpoint; with cold water in the loop this cools the
room). The weekly schedule stays stored on the device, so returning to heating
just restores the schedule preset / auto mode — nothing is reprogrammed.

The exact per-type payloads live in `config.yaml` under each
`thermostat_types.<type>` as `cooling_open` / `cooling_restore`, mirroring the
existing `schedule_mode`. This module decides the desired season and assembles
those payloads; it hard-codes nothing device-specific.
"""


# Fields that set a thermostat's *mode / target* (as opposed to stored config
# like the weekly schedule strings or calibration). When a device is in manual
# override or off, these are left untouched so we don't drag it out of that
# state or overwrite the user's manual setpoint.
CONTROL_FIELDS = frozenset({
    'system_mode', 'preset',
    'occupied_heating_setpoint', 'current_heating_setpoint', 'comfort_temperature',
})


def is_off(reported_state):
    """True if the thermostat reports it is switched off (e.g. window open)."""
    if not isinstance(reported_state, dict):
        return False
    return str(reported_state.get('system_mode')).strip().lower() == 'off'


def build_intended_payload(name, cfg_item, thermostat_types, mqtt_cfg, mode,
                           reported, control_fields=CONTROL_FIELDS):
    """Build the payload that reinstates a thermostat's *currently intended* state.

    Season-aware: in cooling the intended active state is `cooling_open`; in
    heating it is the weekly `schedule_mode`. Either way the stored weekly
    schedule strings + calibration come along.

    Exceptions — a device in **manual override** or **off** keeps that mode: we
    strip every control field (mode/preset/setpoint) and push only the stored
    schedule + calibration, so manual/off and the user's manual setpoint stay.

    Returns (payload, topic, note). When the device's state is unknown (no
    daemon running / dry-run) manual/off can't be detected, so it falls back to
    the season-intended payload and says so in the note.
    """
    from common import build_expected_payload
    base, topic = build_expected_payload(name, cfg_item, thermostat_types, mqtt_cfg)
    type_cfg = thermostat_types.get(cfg_item.get('type'), {})
    config_only = {k: v for k, v in base.items() if k not in control_fields}

    if isinstance(reported, dict) and is_manual_override(type_cfg, reported):
        return config_only, topic, "manual override — schedule/calibration only"
    if isinstance(reported, dict) and is_off(reported):
        return config_only, topic, "off (e.g. window open) — schedule/calibration only"
    suffix = "" if isinstance(reported, dict) else " [state unknown]"
    if mode == 'cooling':
        payload = dict(config_only)
        payload.update(build_open_payload(type_cfg) or {})
        return payload, topic, "cooling: open" + suffix
    return base, topic, "heating: schedule" + suffix


def desired_mode(season_cfg, heatpump_state):
    """Return 'heating' or 'cooling'.

    season.mode = heating|cooling forces that mode. season.mode = auto derives
    it from season.source: 'heatpump' uses the live EMS-ESP cooling signal,
    anything else defaults to heating (the safe winter default).
    """
    mode = (season_cfg or {}).get('mode', 'auto')
    if mode in ('heating', 'cooling'):
        return mode
    source = (season_cfg or {}).get('source', 'heatpump')
    if source == 'heatpump' and heatpump_state is not None:
        return 'cooling' if heatpump_state.get('cooling') else 'heating'
    return 'heating'


def build_open_payload(type_cfg):
    """Payload that forces a thermostat of this type fully open, or None."""
    payload = type_cfg.get('cooling_open')
    return dict(payload) if isinstance(payload, dict) else None


def build_restore_payload(type_cfg):
    """Payload that returns a thermostat to its stored weekly schedule, or None."""
    payload = type_cfg.get('cooling_restore')
    return dict(payload) if isinstance(payload, dict) else None


def is_open(type_cfg, reported_state):
    """True if the device's reported state already matches its cooling_open payload.

    Used to detect whether a thermostat is actually fully open in cooling mode
    (vs. having drifted back to its schedule because some other controller — HA,
    zigbee2mqtt, a stray cron — reset it).
    """
    if not isinstance(reported_state, dict):
        return False
    op = type_cfg.get('cooling_open')
    if not isinstance(op, dict):
        return False
    return all(str(reported_state.get(k)) == str(v) for k, v in op.items())


def is_manual_override(type_cfg, reported_state):
    """True if the end user has taken manual control of this thermostat.

    The per-type `manual_marker` describes the reported field+value that
    indicates manual operation (e.g. preset == 'manual', or system_mode ==
    'heat' on a device we normally drive in 'auto'). Such devices must NOT be
    touched by cooling control, but should be surfaced as a low-priority
    warning so the operator knows comfort control is partly suspended.
    """
    if not isinstance(reported_state, dict):
        return False
    # If the reported state matches our own cooling-open payload, it is the
    # manager's cooling action, not a user manual override. This disambiguates
    # the system_mode=heat that both cooling and a manual override produce on
    # AVATTO/SONOFF types — important across daemon restarts mid-cooling.
    op = type_cfg.get('cooling_open')
    if isinstance(op, dict) and all(
            str(reported_state.get(k)) == str(v) for k, v in op.items()):
        return False
    marker = type_cfg.get('manual_marker')
    if not isinstance(marker, dict):
        return False
    field = marker.get('field')
    if field is None or field not in reported_state:
        return False
    val = reported_state.get(field)
    if 'equals' in marker:
        return str(val).strip().lower() == str(marker['equals']).strip().lower()
    if 'in' in marker:
        return str(val).strip().lower() in [str(x).strip().lower() for x in marker['in']]
    return False
