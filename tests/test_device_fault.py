"""Self-reported device faults must reach the report mails while they last.

A faulted TRV is invisible to every other check: it keeps answering on the mesh,
its battery reads fine, and it may still accept some writes while silently
refusing mode changes. Bad OG sat at `fault_alarm: 2` with a fresh 100% battery,
ignoring even a setpoint write, while the room climbed 27 -> 28.4 °C.
"""
import health

LIMITS = {'battery_limit': 20, 'unseen_interval': 1800, 'stale_temp_secs': 0}

TYPES = {
    'VNTH-T2_v2': {'schedule_mode': {'system_mode': 'heat', 'preset': 'schedule'},
                   'cooling_open': {'preset': 'comfort', 'comfort_temperature': 34},
                   'off_signature': {'system_mode': 'off', 'frost_protection': 'ON'},
                   'manual_marker': {'field': 'preset', 'equals': 'manual'}},
}
ITEM = {'day_hour': '05:00', 'day_temperature': 21.5, 'night_hour': '23:00',
        'night_temperature': 19.5, 'type': 'VNTH-T2_v2'}

BAD_OG = {'system_mode': 'off', 'frost_protection': 'ON', 'preset': 'comfort',
          'comfort_temperature': 34, 'battery': 100, 'fault_alarm': 2,
          'valve_alarm': None, 'local_temperature': 28.4}


def classify(reported, mode='cooling'):
    return health.classify_device('Bad OG', ITEM, TYPES, {'base_topic': 'zigbee2mqtt'},
                                  reported, 1000.0, 1000.0, LIMITS, mode=mode)


def kinds(issues):
    return [i.kind for i in issues]


def test_fault_alarm_is_reported():
    issues = classify(BAD_OG)
    assert 'device_fault' in kinds(issues)
    iss = [i for i in issues if i.kind == 'device_fault'][0]
    assert 'fault_alarm=2' in iss.detail
    assert iss.severity == 'alert', "must appear in the report mails, not as a note"


def test_fault_is_reported_in_every_season():
    """The heating-only early return must not hide a fault while cooling."""
    for mode in ('heating', 'cooling', 'standby'):
        assert 'device_fault' in kinds(classify(BAD_OG, mode=mode)), mode


def test_healthy_device_raises_no_fault():
    ok = {'system_mode': 'heat', 'preset': 'comfort', 'comfort_temperature': 34,
          'battery': 100, 'fault_alarm': 0, 'valve_alarm': None, 'error': None}
    assert 'device_fault' not in kinds(classify(ok))


def test_no_fault_values_are_not_flagged():
    for value in (None, False, 0, '', 'OFF', 'none', 'false', 'OK', 'normal', '0'):
        assert not health._fault_active(value), value


def test_active_fault_values_are_flagged():
    for value in (2, 1, True, 'E1', 'valve stuck', -1):
        assert health._fault_active(value), value


def test_several_fault_fields_are_combined():
    st = dict(BAD_OG, valve_alarm=True, error='E5')
    iss = [i for i in classify(st) if i.kind == 'device_fault'][0]
    for part in ('error=E5', 'fault_alarm=2', 'valve_alarm=True'):
        assert part in iss.detail


def test_nested_fault_payload_is_flattened():
    """TR-M3Z reports `fault_alarm: {'error': 2}` — read it, don't dump a dict."""
    st = dict(BAD_OG, fault_alarm={'error': 2})
    iss = [i for i in classify(st) if i.kind == 'device_fault'][0]
    assert 'fault_alarm.error=2' in iss.detail
    assert '{' not in iss.detail


def test_nested_payload_with_no_active_member_is_not_flagged():
    """A cleared nested fault must not read as active just for being a dict."""
    st = dict(BAD_OG, fault_alarm={'error': 0})
    assert 'device_fault' not in kinds(classify(st))
    assert not health._fault_active({'error': 0, 'other': None})
    assert health._fault_active({'error': 0, 'other': 3})


def test_fault_key_is_stable_so_it_clears_when_fixed():
    """One key per room: the issue stays open across passes, then resolves."""
    iss = [i for i in classify(BAD_OG) if i.kind == 'device_fault'][0]
    assert iss.key == 'Bad OG:fault'
    healthy = dict(BAD_OG, fault_alarm=0)
    assert 'device_fault' not in kinds(classify(healthy))
