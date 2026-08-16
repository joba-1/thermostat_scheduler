import time

import heatpump
import cooling
import thermostat_monitor as tm

HP_CFG = {
    'fields': {'vorlauf': 'curflowtemp', 'ruecklauf': 'rettemp',
               'outdoor': 'outdoortemp', 'pressure': 'syspress'},
    'cooling_when': [
        {'field': 'coolingon', 'equals': 'on'},
        {'field': 'hp4way', 'contains': 'cooling'},
    ],
}

BOILER = {'curflowtemp': 18.3, 'rettemp': 18.5, 'outdoortemp': 30.3,
          'syspress': 1.8, 'hp4way': 'cooling & defrost'}
THERMO = {'hc1': {'coolingon': 'off', 'hpmode': 'heating & cooling'}}


def test_parse_detects_cooling_via_hp4way():
    state = heatpump.parse(BOILER, THERMO, HP_CFG)
    assert state['cooling'] is True
    assert state['mode'] == 'cooling'
    assert state['telemetry']['vorlauf'] == 18.3


def test_no_cooling_when_signals_off():
    boiler = dict(BOILER, hp4way='heating')
    thermo = {'hc1': {'coolingon': 'off'}}
    state = heatpump.parse(boiler, thermo, HP_CFG)
    assert state['cooling'] is False
    assert state['mode'] == 'heating'


def test_check_bounds_flags_low_cooling_flow():
    tele = {'vorlauf': 5.0, 'pressure': 1.8}
    bounds = {'cooling': {'vorlauf': [8, 25], 'pressure': [1.0, 2.5]}}
    out = heatpump.check_bounds(tele, 'cooling', bounds)
    assert any(f == 'vorlauf' for f, *_ in out)
    assert not any(f == 'pressure' for f, *_ in out)


def test_alarm_state_parses_open_and_cleared_lastcode():
    # ongoing fault ("- now") -> active
    a = heatpump.alarm_state({'lastcode': ' --(5140) 22.06.2026 08:01 - now'})
    assert a == {'code': '5140', 'since': '22.06.2026 08:01',
                 'until': 'now', 'active': True}
    # cleared fault (start - end timestamps) -> not active, end captured
    c = heatpump.alarm_state({'lastcode': ' --(5140) 22.06.2026 08:01 - 22.06.2026 08:07'})
    assert c['active'] is False and c['until'] == '22.06.2026 08:07'
    # missing / unparsable -> None
    assert heatpump.alarm_state({}) is None
    assert heatpump.alarm_state({'lastcode': ''}) is None
    assert heatpump.alarm_state({'lastcode': '0H'}) is None


def test_desired_mode_forced_and_auto():
    assert cooling.desired_mode({'mode': 'cooling'}, None) == 'cooling'
    assert cooling.desired_mode({'mode': 'standby'}, None) == 'standby'
    assert cooling.desired_mode({'mode': 'auto', 'source': 'heatpump'},
                                {'cooling': True}) == 'cooling'
    assert cooling.desired_mode({'mode': 'auto', 'source': 'heatpump'},
                                {'cooling': False}) == 'heating'
    assert cooling.desired_mode({'mode': 'auto', 'source': 'config'}, None) == 'heating'


SEASON_OUTDOOR = {'mode': 'auto', 'source': 'outdoor_temp',
                  'standby_below': 15, 'standby_above': 24, 'standby_hysteresis': 1}


def test_desired_mode_outdoor_three_seasons():
    # below standby_below -> heating; above standby_above -> cooling; between -> standby
    assert cooling.desired_mode(SEASON_OUTDOOR, None, outdoor_temp=8) == 'heating'
    assert cooling.desired_mode(SEASON_OUTDOOR, None, outdoor_temp=30) == 'cooling'
    assert cooling.desired_mode(SEASON_OUTDOOR, None, outdoor_temp=19) == 'standby'
    # thresholds are exclusive: exactly at a bound is not (yet) heating/cooling
    assert cooling.desired_mode(SEASON_OUTDOOR, None, outdoor_temp=15) == 'standby'
    assert cooling.desired_mode(SEASON_OUTDOOR, None, outdoor_temp=24) == 'standby'
    # no telemetry -> safe winter default
    assert cooling.desired_mode(SEASON_OUTDOOR, None, outdoor_temp=None) == 'heating'


def test_desired_mode_outdoor_hysteresis_holds_standby():
    # 14.5°C is below standby_below(15), so from any non-standby mode it heats...
    assert cooling.desired_mode(SEASON_OUTDOOR, None, outdoor_temp=14.5,
                                last_mode='heating') == 'heating'
    # ...but coming *from* standby, the 1°C band (15 -> 14) keeps it in standby
    # until it drops below 14, so a reading hovering at 14.5 doesn't flap.
    assert cooling.desired_mode(SEASON_OUTDOOR, None, outdoor_temp=14.5,
                                last_mode='standby') == 'standby'
    assert cooling.desired_mode(SEASON_OUTDOOR, None, outdoor_temp=13.5,
                                last_mode='standby') == 'heating'
    # symmetric at the cooling boundary (24 -> 25 while already in standby)
    assert cooling.desired_mode(SEASON_OUTDOOR, None, outdoor_temp=24.5,
                                last_mode='standby') == 'standby'
    assert cooling.desired_mode(SEASON_OUTDOOR, None, outdoor_temp=25.5,
                                last_mode='standby') == 'cooling'


def test_build_off_payload_uses_signature_or_plain_off():
    with_sig = {'off_signature': {'system_mode': 'off', 'frost_protection': 'ON'}}
    assert cooling.build_off_payload(with_sig) == {'system_mode': 'off',
                                                   'frost_protection': 'ON'}
    # no signature (e.g. TRVZB) -> plain off
    assert cooling.build_off_payload({}) == {'system_mode': 'off'}


def test_current_setpoint_standby_has_no_target():
    # standby: the room is intentionally off, so there is no comfort target and
    # (via the setpoint-is-None short-circuit) no deviation is ever reported.
    item = {'day_hour': '05:00', 'day_temperature': 21.5,
            'night_hour': '23:00', 'night_temperature': 19.5}
    now = time.localtime()
    assert tm.current_setpoint(item, now, 'standby', {'cool_target': 21}) is None
    # heating still returns a scheduled temp; cooling returns the cool target
    assert tm.current_setpoint(item, now, 'cooling', {'cool_target': 21}) == 21
    assert tm.current_setpoint(item, now, 'heating', {}) in (21.5, 19.5)


def test_manual_override_detection():
    vnth = {'schedule_mode': {'preset': 'schedule'},
            'manual_marker': {'field': 'preset', 'equals': 'manual'}}
    assert cooling.is_manual_override(vnth, {'preset': 'manual'})
    assert not cooling.is_manual_override(vnth, {'preset': 'schedule'})


def test_classify_state_signatures():
    t = {'schedule_mode': {'system_mode': 'auto'},
         'cooling_open': {'system_mode': 'heat', 'current_heating_setpoint': 34},
         'off_signature': {'system_mode': 'off', 'frost_protection': 'ON'},
         'manual_marker': {'field': 'system_mode', 'equals': 'heat'}}
    assert cooling.classify_state(t, {'system_mode': 'heat', 'current_heating_setpoint': 34}) == 'open'
    assert cooling.classify_state(t, {'system_mode': 'off', 'frost_protection': 'ON'}) == 'off'
    assert cooling.classify_state(t, {'system_mode': 'auto'}) == 'schedule'
    assert cooling.classify_state(t, {'system_mode': 'heat', 'current_heating_setpoint': 21.5}) == 'manual'
    assert cooling.classify_state(t, {'system_mode': 'off'}) == 'manual'   # user plain off
    assert cooling.classify_state(t, None) == 'unknown'
    assert cooling.is_our_off(t, {'system_mode': 'off', 'frost_protection': 'ON'})
    assert not cooling.is_our_off(t, {'system_mode': 'off'})


def test_off_wins_over_cooling_open_when_both_match():
    """TECH/Tuya cooling_open carries no system_mode, so an off valve that still
    holds `preset: comfort` matches both signatures. Off must win — otherwise the
    status page calls a shut valve "open" and cooling_not_open never fires."""
    t = {'schedule_mode': {'system_mode': 'heat', 'preset': 'schedule'},
         'cooling_open': {'preset': 'comfort', 'comfort_temperature': 34},
         'off_signature': {'system_mode': 'off', 'frost_protection': 'ON'},
         'manual_marker': {'field': 'preset', 'equals': 'manual'}}
    both = {'system_mode': 'off', 'frost_protection': 'ON',
            'preset': 'comfort', 'comfort_temperature': 34}
    assert cooling.classify_state(t, both) == 'off'
    assert not cooling.is_open(t, both)
    assert cooling.is_our_off(t, both)
    # a genuinely open valve is still 'open'
    assert cooling.classify_state(
        t, {'preset': 'comfort', 'comfort_temperature': 34}) == 'open'


def test_cooling_open_state_not_seen_as_manual():
    # AVATTO/SONOFF type: cooling_open uses system_mode=heat, same as manual_marker.
    tcfg = {'cooling_open': {'system_mode': 'heat', 'current_heating_setpoint': 30},
            'manual_marker': {'field': 'system_mode', 'equals': 'heat'}}
    # our cooling state -> NOT manual
    assert not cooling.is_manual_override(tcfg, {'system_mode': 'heat', 'current_heating_setpoint': 30})
    # user manual heat at a normal setpoint -> manual
    assert cooling.is_manual_override(tcfg, {'system_mode': 'heat', 'current_heating_setpoint': 22})


def test_is_open_matches_cooling_open_payload():
    tcfg = {'cooling_open': {'preset': 'comfort', 'comfort_temperature': 34}}
    assert cooling.is_open(tcfg, {'preset': 'comfort', 'comfort_temperature': 34})
    # drifted back to schedule (comfort_temperature lingers) -> not open
    assert not cooling.is_open(tcfg, {'preset': 'schedule', 'comfort_temperature': 34})
    assert not cooling.is_open(tcfg, None)
    assert not cooling.is_open({}, {'preset': 'comfort'})


INTENDED_TYPES = {
    'VNTH-T2_v2': {
        'schedule_mode': {'system_mode': 'heat', 'preset': 'schedule',
                          'temperature_sensitivity': 0.5},
        'cooling_open': {'preset': 'comfort', 'comfort_temperature': 34},
        'off_signature': {'system_mode': 'off', 'frost_protection': 'ON'},
        'manual_marker': {'field': 'preset', 'equals': 'manual'},
    },
}
INTENDED_ITEM = {'type': 'VNTH-T2_v2', 'day_hour': '05:00', 'day_temperature': 21.5,
                 'night_hour': '23:00', 'night_temperature': 19.5}
MQTT = {'base_topic': 'zigbee2mqtt'}


def test_intended_heating_pushes_schedule():
    payload, topic, note = cooling.build_intended_payload(
        'Bad OG', INTENDED_ITEM, INTENDED_TYPES, MQTT, 'heating',
        {'preset': 'schedule'})
    assert payload['preset'] == 'schedule'
    assert payload['system_mode'] == 'heat'
    assert any(k.startswith('schedule_') for k in payload)
    assert 'heating' in note


def test_intended_cooling_pushes_open():
    payload, topic, note = cooling.build_intended_payload(
        'Bad OG', INTENDED_ITEM, INTENDED_TYPES, MQTT, 'cooling',
        {'preset': 'schedule'})
    assert payload['preset'] == 'comfort' and payload['comfort_temperature'] == 34
    assert 'system_mode' not in payload          # heating mode field stripped
    assert payload['temperature_sensitivity'] == 0.5   # calibration kept
    assert any(k.startswith('schedule_') for k in payload)  # stored schedule kept


def test_intended_standby_pushes_off():
    # standby season on a scheduled (not-yet-off) TRV -> push the off signature,
    # dropping active control fields but keeping the stored schedule/calibration.
    payload, topic, note = cooling.build_intended_payload(
        'Bad OG', INTENDED_ITEM, INTENDED_TYPES, MQTT, 'standby',
        {'preset': 'schedule'})
    assert payload['system_mode'] == 'off' and payload['frost_protection'] == 'ON'
    assert 'preset' not in payload               # active control fields stripped
    assert payload['temperature_sensitivity'] == 0.5   # calibration kept
    assert any(k.startswith('schedule_') for k in payload)  # stored schedule kept
    assert 'standby' in note and 'warm water' in note


def test_intended_manual_keeps_only_config():
    payload, topic, note = cooling.build_intended_payload(
        'Bad OG', INTENDED_ITEM, INTENDED_TYPES, MQTT, 'cooling',
        {'preset': 'manual'})
    # no mode/preset/setpoint fields -> manual state preserved
    assert not (cooling.CONTROL_FIELDS & set(payload))
    assert payload['temperature_sensitivity'] == 0.5
    assert any(k.startswith('schedule_') for k in payload)
    assert 'manual' in note


def test_intended_off_keeps_only_config():
    payload, topic, note = cooling.build_intended_payload(
        'Bad OG', INTENDED_ITEM, INTENDED_TYPES, MQTT, 'cooling',
        {'system_mode': 'off'})
    assert not (cooling.CONTROL_FIELDS & set(payload))
    assert 'off' in note


def test_reclaim_manual_pushes_active_season_state():
    """reclaim_manual overrides the manual exception: a manual VNTH TRV in
    cooling is forced to cooling_open (the --reset-manual one-step fix)."""
    payload, topic, note = cooling.build_intended_payload(
        'Bad OG', INTENDED_ITEM, INTENDED_TYPES, MQTT, 'cooling',
        {'preset': 'manual'}, reclaim_manual=True)
    assert payload['preset'] == 'comfort' and payload['comfort_temperature'] == 34
    assert 'manual' not in note                  # no longer the leave-alone branch
    # and in heating it reclaims to the schedule instead
    payload_h, _, _ = cooling.build_intended_payload(
        'Bad OG', INTENDED_ITEM, INTENDED_TYPES, MQTT, 'heating',
        {'preset': 'manual'}, reclaim_manual=True)
    assert payload_h['preset'] == 'schedule' and payload_h['system_mode'] == 'heat'


def test_reclaim_manual_still_leaves_off_off():
    """The off exception holds even with reclaim_manual — never force an off
    (e.g. window-open) valve on."""
    payload, topic, note = cooling.build_intended_payload(
        'Bad OG', INTENDED_ITEM, INTENDED_TYPES, MQTT, 'cooling',
        {'system_mode': 'off'}, reclaim_manual=True)
    assert not (cooling.CONTROL_FIELDS & set(payload))
    assert 'off' in note


def test_intended_unknown_state_falls_back_to_season():
    payload, topic, note = cooling.build_intended_payload(
        'Bad OG', INTENDED_ITEM, INTENDED_TYPES, MQTT, 'cooling', None)
    assert payload['preset'] == 'comfort'        # season-intended, best effort
    assert 'state unknown' in note


def test_open_and_restore_payloads():
    tcfg = {'cooling_open': {'preset': 'comfort', 'comfort_temperature': 30},
            'cooling_restore': {'preset': 'schedule'}}
    assert cooling.build_open_payload(tcfg) == {'preset': 'comfort', 'comfort_temperature': 30}
    assert cooling.build_restore_payload(tcfg) == {'preset': 'schedule'}
    assert cooling.build_open_payload({}) is None
