#!/usr/bin/env python3
"""
EMS-ESP heat pump integration.

The heat pump is read directly from MQTT (not via Home Assistant). EMS-ESP
publishes two retained-ish JSON payloads we care about:

  ems-esp/boiler_data      -> hp4way, hpactivity, curflowtemp (Vorlauf),
                              rettemp (Ruecklauf), outdoortemp, hpcurrpower,
                              syspress, ...
  ems-esp/thermostat_data  -> hc1: {coolingon, hpmode, hpcooling, cooltemp, ...}

`parse()` flattens both into one lookup (hc1 keys overlaid on boiler keys) so
config can reference any field by its plain name, and derives a coarse
mode (heating / cooling) plus the operator-relevant telemetry.
"""


def _flatten(boiler, thermostat):
    """Merge boiler_data and thermostat_data.hc1 into a single flat dict.

    hc1 (the heat circuit) wins on key collisions because its values describe
    the controller's intent for the living space.
    """
    merged = {}
    if isinstance(boiler, dict):
        merged.update(boiler)
    if isinstance(thermostat, dict):
        hc1 = thermostat.get('hc1')
        if isinstance(hc1, dict):
            merged.update(hc1)
    return merged


def is_cooling(merged, cooling_when):
    """Return True if any of the OR'd `cooling_when` conditions match.

    Each condition is a dict with a `field` plus one of `equals` / `contains`.
    """
    for cond in cooling_when or []:
        field = cond.get('field')
        if field is None or field not in merged:
            continue
        val = merged.get(field)
        if 'equals' in cond:
            if str(val).strip().lower() == str(cond['equals']).strip().lower():
                return True
        if 'contains' in cond:
            if str(cond['contains']).strip().lower() in str(val).strip().lower():
                return True
    return False


def telemetry(merged, fields):
    """Map the configured field names to canonical telemetry keys."""
    out = {}
    for canonical, src in (fields or {}).items():
        if src in merged:
            out[canonical] = merged[src]
    return out


def is_active(merged):
    """True if the compressor is running.

    Bounds (e.g. expected flow temperature) only make sense while the pump is
    working; an idle pump in summer sits at ambient temperatures and would
    otherwise trip every range check. Defaults to active when no `hpcompon`
    field is present, so the check still runs on installs without it.
    """
    if 'hpcompon' in merged:
        return str(merged['hpcompon']).strip().lower() == 'on'
    return True


def parse(boiler, thermostat, hp_cfg):
    """Return {'mode', 'cooling', 'active', 'telemetry', 'raw'} from EMS-ESP payloads."""
    merged = _flatten(boiler, thermostat)
    cooling = is_cooling(merged, hp_cfg.get('cooling_when'))
    return {
        'mode': 'cooling' if cooling else 'heating',
        'cooling': cooling,
        'active': is_active(merged),
        'telemetry': telemetry(merged, hp_cfg.get('fields')),
        'raw': merged,
    }


def check_bounds(tele, mode, bounds_cfg):
    """Return a list of (canonical_field, value, low, high) out-of-range tuples.

    `bounds_cfg` is keyed by mode (heating/cooling), each a dict of
    canonical_field -> [low, high]. Missing telemetry is skipped silently.
    """
    out = []
    ranges = (bounds_cfg or {}).get(mode, {})
    for field, (low, high) in ranges.items():
        if field not in tele:
            continue
        try:
            val = float(tele[field])
        except (TypeError, ValueError):
            continue
        if val < low or val > high:
            out.append((field, val, low, high))
    return out
