"""Manager status report + manual-override helpers (no MQTT connection)."""
import thermostat_monitor as tm

CFG = {
    'mqtt': {'base_topic': 'zigbee2mqtt'},
    'alerts': {'enabled': False},
    'season': {'mode': 'heating'},
    'thermostat_types': {
        'VNTH-T2_v2': {'schedule_mode': {'system_mode': 'heat', 'preset': 'schedule'},
                       'manual_marker': {'field': 'preset', 'equals': 'manual'}},
    },
    'thermostats': {
        'Bad OG': {'day_hour': '05:00', 'day_temperature': 21.5, 'night_hour': '23:00',
                   'night_temperature': 19.5, 'type': 'VNTH-T2_v2',
                   'sensors': {'temperature': 'Bad OG Luft', 'windows': ['Bad OG']}},
    },
}


def make_mgr():
    return tm.Manager({k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()})


def test_manual_override_listed():
    mgr = make_mgr()
    mgr.last_state['Bad OG'] = {'preset': 'manual'}
    assert mgr.manual_overrides() == ['Bad OG']
    mgr.last_state['Bad OG'] = {'preset': 'schedule'}
    assert mgr.manual_overrides() == []


def test_sensor_and_thermostat_namespaces_dont_collide():
    """A contact sensor named 'Bad OG' must not clobber the 'Bad OG' thermostat."""
    mgr = make_mgr()
    mgr.last_state['Bad OG'] = {'preset': 'schedule', 'battery': 50}
    mgr.sensor_state['Bad OG'] = {'contact': False, 'battery': 80}
    assert mgr.last_state['Bad OG']['battery'] == 50
    assert mgr.sensor_state['Bad OG']['battery'] == 80


def test_mode_change_notifies_manual_valves():
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['manual_thermostats'] = ['Keller', 'Gäste WC']
    mgr = tm.Manager(cfg)
    sent = []
    mgr.alerter.enabled = True
    mgr.alerter._sender = lambda subj, body: sent.append((subj, body)) or True

    mgr._notify_mode_change('heating', 'cooling')
    assert len(sent) == 1
    assert 'cooling' in sent[0][0]
    assert 'Keller' in sent[0][1] and 'OPEN' in sent[0][1]


def test_mode_change_silent_without_manual_valves():
    mgr = make_mgr()
    sent = []
    mgr.alerter.enabled = True
    mgr.alerter._sender = lambda subj, body: sent.append((subj, body)) or True
    mgr._notify_mode_change('cooling', 'heating')
    assert sent == []


def test_status_report_contains_overview():
    mgr = make_mgr()
    mgr.last_state['Bad OG'] = {'preset': 'manual', 'battery': 8}
    mgr.sensor_state['Bad OG Luft'] = {'temperature': 20.0}
    report = mgr.status_report()
    assert 'Thermostat status' in report
    assert 'Bad OG' in report and 'MANUAL' in report
    assert 'Desired mode: heating' in report
