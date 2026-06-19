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


def is_manual_override(type_cfg, reported_state):
    """True if the end user has taken manual control of this thermostat.

    The per-type `manual_marker` describes the reported field+value that
    indicates manual operation (e.g. preset == 'manual', or system_mode ==
    'heat' on a device we normally drive in 'auto'). Such devices must NOT be
    touched by cooling control, but should be surfaced as a low-priority
    warning so the operator knows comfort control is partly suspended.
    """
    marker = type_cfg.get('manual_marker')
    if not isinstance(marker, dict) or not isinstance(reported_state, dict):
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
