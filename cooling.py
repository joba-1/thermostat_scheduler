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
    """True if the thermostat reports it is switched off (any off — ours or the
    user's). For 'off that *we* set' use `is_our_off`."""
    if not isinstance(reported_state, dict):
        return False
    return str(reported_state.get('system_mode')).strip().lower() == 'off'


def _matches(reported_state, signature):
    """True if every field in `signature` equals the reported value (string-compared)."""
    if not isinstance(reported_state, dict) or not isinstance(signature, dict) or not signature:
        return False
    return all(str(reported_state.get(k)) == str(v) for k, v in signature.items())


def _is_schedule(type_cfg, reported_state):
    """True if the device is in its normal weekly-schedule mode. Derived from the
    type's schedule_mode using the same field the manual_marker keys on (so VNTH
    -> preset==schedule, AVATTO/SONOFF -> system_mode==auto), no extra config."""
    sm = type_cfg.get('schedule_mode') or {}
    field = (type_cfg.get('manual_marker') or {}).get('field')
    if field and field in sm:
        return str(reported_state.get(field)) == str(sm[field])
    return False


def classify_state(type_cfg, reported_state):
    """Classify a reported TRV state as one of OUR signatures, else manual.

    We cannot read who set a state (it can change from the device buttons, the
    z2m web UI, a raw MQTT /set, this software, ...). So instead of detecting
    'manual' positively, we define the exact state-combos *we* produce and treat
    anything else as manual:

      - 'off'      -> matches `off_signature` (our window-off, e.g. off + frost_protection ON)
      - 'open'     -> matches `cooling_open` (e.g. heat / preset comfort + setpoint 34)
      - 'schedule' -> in the weekly-schedule mode
      - 'manual'   -> none of the above (a user/other controller did it) -> leave alone
      - 'unknown'  -> no reported state yet

    **Off is tested first.** On types whose `cooling_open` carries no
    `system_mode` (TECH/Tuya: `preset: comfort` + `comfort_temperature: 34`), a
    valve that is switched off but still holds that preset matches *both*
    signatures. A closed valve is closed whatever preset it remembers, so the
    off signature has to win — otherwise the state page reports the room as
    "open" and `cooling_not_open` never fires while the room bakes. (Bad OG did
    exactly this: off with a latched fault, displayed as open, 27 -> 28.4 °C.)
    `build_intended_payload` and `--check` already gave off precedence; this
    aligns the classifier with them.
    """
    if not isinstance(reported_state, dict):
        return 'unknown'
    if _matches(reported_state, type_cfg.get('off_signature')):
        return 'off'
    if _matches(reported_state, type_cfg.get('cooling_open')):
        return 'open'
    if _is_schedule(type_cfg, reported_state):
        return 'schedule'
    return 'manual'


def is_our_off(type_cfg, reported_state):
    """True if the device is in the off state *we* set for an open window
    (matches the type's `off_signature`), not a user's plain off."""
    return _matches(reported_state, type_cfg.get('off_signature'))


def build_intended_payload(name, cfg_item, thermostat_types, mqtt_cfg, mode,
                           reported, control_fields=CONTROL_FIELDS,
                           reclaim_manual=False):
    """Build the payload that reinstates a thermostat's *currently intended* state.

    Season-aware: in cooling the intended active state is `cooling_open`; in
    heating it is the weekly `schedule_mode`; in standby (shoulder weather,
    neither heating nor cooling wanted) it is switched off, same as a window-
    open off. Either way the stored weekly schedule strings + calibration come
    along (except standby, which — like any off — carries no active setpoint).

    Exceptions — a device in **manual override** or **off** keeps that mode: we
    strip every control field (mode/preset/setpoint) and push only the stored
    schedule + calibration, so manual/off and the user's manual setpoint stay.

    `reclaim_manual=True` overrides the *manual* exception only (used by an
    explicit operator re-onboard, e.g. `--reset-manual`): a manual device is
    pushed to the active-season state instead of being left alone. The **off**
    exception always holds — we never force an off (e.g. window-open) valve on.

    Returns (payload, topic, note). When the device's state is unknown (no
    daemon running / dry-run) manual/off can't be detected, so it falls back to
    the season-intended payload and says so in the note.
    """
    from common import build_expected_payload
    base, topic = build_expected_payload(name, cfg_item, thermostat_types, mqtt_cfg)
    type_cfg = thermostat_types.get(cfg_item.get('type'), {})
    config_only = {k: v for k, v in base.items() if k not in control_fields}

    # Off (any off — ours or the user's) is left off; checked before manual so an
    # off device reads as "off" rather than the broader "manual" classification.
    if isinstance(reported, dict) and is_off(reported):
        return config_only, topic, "off (e.g. window open) — schedule/calibration only"
    if (not reclaim_manual and isinstance(reported, dict)
            and is_manual_override(type_cfg, reported)):
        return config_only, topic, "manual override — schedule/calibration only"
    suffix = "" if isinstance(reported, dict) else " [state unknown]"
    if mode == 'cooling':
        payload = dict(config_only)
        payload.update(build_open_payload(type_cfg) or {})
        return payload, topic, "cooling: open" + suffix
    if mode == 'standby':
        payload = dict(config_only)
        payload.update(build_off_payload(type_cfg))
        return payload, topic, "standby: off (warm water only)" + suffix
    return base, topic, "heating: schedule" + suffix


def desired_mode(season_cfg, heatpump_state, outdoor_temp=None, last_mode=None):
    """Return 'heating', 'standby', or 'cooling'.

    season.mode = heating|cooling|standby forces that mode. season.mode = auto
    derives it from season.source:

    - 'heatpump': the live EMS-ESP cooling signal (binary — no standby; the
      heat pump itself has no "neither" state, see `heatpump.hpmode`).
    - 'outdoor_temp': shoulder-season standby derived from `outdoor_temp`
      against `season.standby_below` / `season.standby_above`, so heating and
      cooling stay for genuinely cold/hot weather and the pump only otherwise
      runs its domestic hot water production. While already in standby
      (`last_mode == 'standby'`), `season.standby_hysteresis` widens the standby
      band by that many degrees on *both* sides, so a reading oscillating near a
      threshold doesn't flap the season every pass; `last_mode` is the mode
      returned by the previous call (None on the first evaluation, no hysteresis).

    Anything else (no telemetry yet, unknown source) defaults to heating (the
    safe winter default).
    """
    season_cfg = season_cfg or {}
    mode = season_cfg.get('mode', 'auto')
    if mode in ('heating', 'cooling', 'standby'):
        return mode
    source = season_cfg.get('source', 'heatpump')
    if source == 'heatpump' and heatpump_state is not None:
        return 'cooling' if heatpump_state.get('cooling') else 'heating'
    if source == 'outdoor_temp' and outdoor_temp is not None:
        below = season_cfg.get('standby_below')
        above = season_cfg.get('standby_above')
        # While already in standby, widen the band by hysteresis on *both* sides
        # (heat only below below-h, cool only above above+h) so a reading hovering
        # at a threshold doesn't flap the season every pass.
        h = season_cfg.get('standby_hysteresis', 0) if last_mode == 'standby' else 0
        if below is not None and outdoor_temp < below - h:
            return 'heating'
        if above is not None and outdoor_temp > above + h:
            return 'cooling'
        return 'standby'
    return 'heating'


def build_open_payload(type_cfg):
    """Payload that forces a thermostat of this type fully open, or None."""
    payload = type_cfg.get('cooling_open')
    return dict(payload) if isinstance(payload, dict) else None


def build_restore_payload(type_cfg):
    """Payload that returns a thermostat to its stored weekly schedule, or None."""
    payload = type_cfg.get('cooling_restore')
    return dict(payload) if isinstance(payload, dict) else None


def build_off_payload(type_cfg):
    """Payload that switches a thermostat off for standby season (shoulder
    weather — neither heating nor cooling wanted, DHW keeps running on its own).

    Reuses the type's `off_signature` (the same "our off" signature window
    control uses), falling back to a plain `system_mode: off` for a type with
    no signature configured (e.g. TRVZB, see docs/control-model.md)."""
    return dict(type_cfg.get('off_signature') or {'system_mode': 'off'})


def clear_off_marker(payload, type_cfg):
    """Merge the type's `off_clear` into a restore/open `payload`, undoing the
    off marker so a valve we had switched off (window-open or standby) actually
    wakes up — `cooling_restore`/`cooling_open` alone only set the season fields
    and would leave e.g. `frost_protection` on. Mutates and returns `payload`."""
    payload.update(type_cfg.get('off_clear') or {})
    return payload


def is_open(type_cfg, reported_state):
    """True if the device's reported state already matches its cooling_open payload.

    Used to detect whether a thermostat is actually fully open in cooling mode
    (vs. having drifted back to its schedule because some other controller — HA,
    zigbee2mqtt, a stray cron — reset it).
    """
    return classify_state(type_cfg, reported_state) == 'open'


def is_manual_override(type_cfg, reported_state):
    """True if the device is in a state we did NOT produce (= a user/other
    controller took manual control). Defined as: not one of our signatures
    (`cooling_open`, `off_signature`, the weekly schedule). Such devices are
    left untouched and surfaced as a low-priority warning. See `classify_state`
    for why we detect manual by exclusion rather than a positive marker.

    A device that has reported nothing yet (`unknown`) is not treated as manual.
    """
    return classify_state(type_cfg, reported_state) == 'manual'
