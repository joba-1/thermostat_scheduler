"""Manager status report + manual-override helpers (no MQTT connection)."""
import tempfile

import thermostat_monitor as tm

CFG = {
    'mqtt': {'base_topic': 'zigbee2mqtt'},
    'alerts': {'enabled': False},
    'season': {'mode': 'heating'},
    'thermostat_types': {
        'VNTH-T2_v2': {'schedule_mode': {'system_mode': 'heat', 'preset': 'schedule'},
                       'cooling_open': {'preset': 'comfort', 'comfort_temperature': 34},
                       'manual_marker': {'field': 'preset', 'equals': 'manual'}},
    },
    'thermostats': {
        'Bad OG': {'day_hour': '05:00', 'day_temperature': 21.5, 'night_hour': '23:00',
                   'night_temperature': 19.5, 'type': 'VNTH-T2_v2',
                   'sensors': {'temperature': 'Bad OG Luft', 'windows': ['Bad OG']}},
    },
}


def make_mgr(state_file=None):
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    # Isolate persistence to a throwaway path so tests never touch real state.
    cfg['device_state_file'] = state_file or (
        tempfile.mkdtemp() + '/devices.json')
    return tm.Manager(cfg)


def test_device_state_persists_across_restart(tmp_path):
    sf = str(tmp_path / 'devices.json')
    mgr = make_mgr(sf)
    mgr.last_state['Bad OG'] = {'preset': 'schedule', 'battery': 55}
    mgr.last_seen['Bad OG'] = '2026-06-19T10:00:00'
    mgr.sensor_state['Bad OG Luft'] = {'temperature': 22.3, 'battery': 90}
    mgr.sensor_seen['Bad OG Luft'] = '2026-06-19T10:01:00'
    mgr._save_device_state()

    # a fresh manager (simulating restart) loads the saved readings
    mgr2 = make_mgr(sf)
    assert mgr2.last_state['Bad OG'] == {'preset': 'schedule', 'battery': 55}
    assert mgr2.last_seen['Bad OG'] == '2026-06-19T10:00:00'
    assert mgr2.sensor_state['Bad OG Luft']['temperature'] == 22.3


def test_restored_old_timestamp_gets_restart_grace():
    """A restored, long-stale last_seen must not immediately alert no_life_sign."""
    mgr = make_mgr()
    # last seen far in the past, but the device hasn't reported this session
    eff = mgr._effective_seen('2000-01-01T00:00:00')
    assert eff >= mgr.start_ts        # clamped up to startup -> full grace window


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
    mgr.alerter._sender = lambda subj, body, html=None: sent.append((subj, body)) or True

    mgr._notify_mode_change('heating', 'cooling')
    assert len(sent) == 1
    assert 'cooling' in sent[0][0]
    assert 'Keller' in sent[0][1] and 'OPEN' in sent[0][1]


def test_mode_change_silent_without_manual_valves():
    mgr = make_mgr()
    sent = []
    mgr.alerter.enabled = True
    mgr.alerter._sender = lambda subj, body, html=None: sent.append((subj, body)) or True
    mgr._notify_mode_change('cooling', 'heating')
    assert sent == []


def test_status_report_contains_overview():
    mgr = make_mgr()
    mgr.last_state['Bad OG'] = {'preset': 'manual', 'battery': 8}
    mgr.sensor_state['Bad OG Luft'] = {'temperature': 20.0}
    report = mgr.status_report()
    assert 'Thermostat status' in report
    assert 'Bad OG' in report and 'MANUAL' in report
    assert 'Mode' in report and 'heating' in report
    # battery filled, missing fields render as a dash rather than '?'
    assert '8%' in report and '—' in report


def test_state_uses_our_vocabulary_and_uniform_setpoint_in_header():
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['season'] = {'mode': 'cooling', 'cool_target': 21}
    cfg['device_state_file'] = tempfile.mkdtemp() + '/devices.json'
    mgr = tm.Manager(cfg)
    # cooling-open signature -> classified as our 'open', not the raw 'comfort'
    mgr.last_state['Bad OG'] = {'preset': 'comfort', 'comfort_temperature': 34}
    d = mgr._report_data()
    assert d['set_line'] == '21°C'                 # lifted to a header line
    assert 'set' not in d['thermo']['headers']      # ...and dropped as a column
    assert d['thermo']['rows'][0][1] == 'open'      # our state, not 'comfort'
    report = mgr.status_report()
    assert 'Set point: 21°C (all rooms)' in report


def test_setpoint_stays_a_column_when_rooms_differ():
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['thermostats'] = dict(cfg['thermostats'])
    cfg['thermostats']['WC OG'] = {
        'day_hour': '05:00', 'day_temperature': 18.0, 'night_hour': '23:00',
        'night_temperature': 16.0, 'type': 'VNTH-T2_v2',
        'sensors': {'temperature': 'WC OG Luft'}}
    cfg['device_state_file'] = tempfile.mkdtemp() + '/devices.json'
    mgr = tm.Manager(cfg)   # heating: per-room schedule points differ
    d = mgr._report_data()
    assert d['set_line'] is None
    assert 'set' in d['thermo']['headers']


def test_sensor_value_shows_humidity():
    mgr = make_mgr()
    mgr.sensor_state['Bad OG Luft'] = {'temperature': 22.3, 'humidity': 55}
    report = mgr.status_report()
    assert '22.3°C' in report and '55%RH' in report


def _seed_history(mgr, name, temps, step_min=5):
    """Push (ts, running_state, temp) samples spaced step_min apart, oldest first."""
    import time
    now = time.time()
    n = len(temps)
    for i, t in enumerate(temps):
        ts = now - (n - 1 - i) * step_min * 60
        mgr.history[name].append((ts, 'cool', t))


def test_improving_within_grace_is_quiet():
    mgr = make_mgr()
    # only 15 min of history (< 30 min grace) -> too soon to judge
    _seed_history(mgr, 'Bad OG', [26.0, 25.8, 25.6, 25.4])  # 4 * 5 min = 15 min
    assert mgr._temp_improving('Bad OG', 21.0, 'cooling') is True


def test_fast_enough_cooling_stays_quiet():
    mgr = make_mgr()
    # 60 min span, fell 1.2 °C -> 1.2 °C/h >= required 1 °C/h
    _seed_history(mgr, 'Bad OG', [26.2, 25.9, 25.6, 25.3, 25.2, 25.1, 25.0],
                  step_min=10)
    assert mgr._temp_improving('Bad OG', 21.0, 'cooling') is True


def test_too_slow_cooling_escalates():
    mgr = make_mgr()
    # 60 min span, fell only 0.3 °C -> 0.3 °C/h < required 1 °C/h -> loud
    _seed_history(mgr, 'Bad OG', [26.0, 25.95, 25.9, 25.85, 25.8, 25.75, 25.7],
                  step_min=10)
    assert mgr._temp_improving('Bad OG', 21.0, 'cooling') is False


def test_cooling_not_open_is_reported():
    import time
    mgr = make_mgr()
    # Bad OG (VNTH-T2_v2) drifted back to its schedule preset in cooling mode
    mgr.last_state['Bad OG'] = {'preset': 'schedule', 'comfort_temperature': 34}
    issues = mgr.collect_issues('cooling', time.time(), time.localtime(), None)
    keys = [i.key for i in issues]
    assert 'Bad OG:notopen' in keys
    iss = next(i for i in issues if i.key == 'Bad OG:notopen')
    assert iss.severity == 'info'      # report-only, not a loud alert


def test_cooling_open_room_not_flagged():
    import time
    mgr = make_mgr()
    mgr.last_state['Bad OG'] = {'preset': 'comfort', 'comfort_temperature': 34}
    issues = mgr.collect_issues('cooling', time.time(), time.localtime(), None)
    assert 'Bad OG:notopen' not in [i.key for i in issues]


def test_cooling_off_room_not_flagged_as_drift():
    import time
    mgr = make_mgr()
    # Switched off (e.g. window open) is intended, not a drift -> no notopen flag
    mgr.last_state['Bad OG'] = {'system_mode': 'off', 'preset': 'schedule',
                                'comfort_temperature': 34}
    issues = mgr.collect_issues('cooling', time.time(), time.localtime(), None)
    assert 'Bad OG:notopen' not in [i.key for i in issues]


def test_status_report_html_has_scrollable_tables():
    mgr = make_mgr()
    mgr.last_state['Bad OG'] = {'preset': 'manual', 'battery': 8}
    html = mgr.status_report_html()
    assert '<table' in html and 'overflow-x:auto' in html
    assert 'Bad OG' in html
    # HTML is escaped (no raw angle brackets injected from data)
    assert '<script' not in html


def test_html_color_cues():
    # cooling mode so "above target" is the problematic direction
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['season'] = {'mode': 'cooling', 'cool_target': 21}
    cfg['device_state_file'] = tempfile.mkdtemp() + '/devices.json'
    mgr = tm.Manager(cfg)
    mgr.last_state['Bad OG'] = {'preset': 'comfort', 'battery': 8}     # low battery
    mgr.sensor_state['Bad OG Luft'] = {'temperature': 26.0}           # >21+1.5 -> hot
    mgr.sensor_state['Bad OG'] = {'contact': False}                   # window open
    html = mgr.status_report_html()
    assert tm.Manager._CSS_BAD in html      # low battery coloured
    assert tm.Manager._CSS_HOT in html      # too-warm temp coloured
    assert tm.Manager._CSS_WARN in html     # open window coloured
