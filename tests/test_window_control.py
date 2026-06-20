"""Window/door -> TRV control (off on open, restore on close). No MQTT."""
import json
import tempfile

import thermostat_monitor as tm

TYPES = {
    'VNTH-T2_v2': {'schedule_mode': {'system_mode': 'heat', 'preset': 'schedule'},
                   'cooling_open': {'preset': 'comfort', 'comfort_temperature': 34},
                   'manual_marker': {'field': 'preset', 'equals': 'manual'},
                   'builtin_window_off': {'window_detection': 'OFF'}},
}

CFG = {
    'mqtt': {'base_topic': 'zigbee2mqtt'},
    'alerts': {'enabled': False},
    'season': {'mode': 'heating'},
    'window_control': {'enabled': True, 'act': True,
                       'open_debounce': 5, 'close_debounce': 5, 'disable_builtin': True},
    'thermostat_types': TYPES,
    'thermostats': {
        'Esszimmer': {'day_hour': '05:00', 'day_temperature': 21.0, 'night_hour': '23:00',
                      'night_temperature': 19.5, 'type': 'VNTH-T2_v2',
                      'sensors': {'windows': ['Esszimmer Terrasse']}},
        'Waschküche': {'day_hour': '05:00', 'day_temperature': 20.5, 'night_hour': '23:00',
                       'night_temperature': 19.5, 'type': 'VNTH-T2_v2',
                       'sensors': {'windows': ['Waschküche Fenster']},
                       'window': {'humidity_guard': {'sensor': 'Waschküche Luft', 'below': 50}}},
    },
}

ESS_SET = 'zigbee2mqtt/Esszimmer Thermostat/set'
WAS_SET = 'zigbee2mqtt/Waschküche Thermostat/set'


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, qos=0, **kw):
        self.published.append((topic, payload))

    def subscribe(self, *a, **k):
        pass


def make_mgr(state_file=None):
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['device_state_file'] = state_file or (tempfile.mkdtemp() + '/devices.json')
    return tm.Manager(cfg)


def sent(client, topic):
    return [json.loads(p) for (t, p) in client.published if t == topic]


def _open(mgr, contact):
    mgr.sensor_state[contact] = {'contact': False}   # z2m: contact=false => OPEN


def _closed(mgr, contact):
    mgr.sensor_state[contact] = {'contact': True}


def test_window_open_switches_off_and_latches():
    mgr, c = make_mgr(), FakeClient()
    mgr.last_state['Esszimmer'] = {'preset': 'schedule'}
    _open(mgr, 'Esszimmer Terrasse')
    mgr._apply_window_control('Esszimmer', c)
    assert {'system_mode': 'off'} in sent(c, ESS_SET)
    assert 'Esszimmer' in mgr.window_off


def test_window_close_restores_heating_schedule():
    mgr, c = make_mgr(), FakeClient()
    mgr.window_off['Esszimmer'] = '2026-06-20T10:00:00'
    mgr.last_state['Esszimmer'] = {'system_mode': 'off'}
    _closed(mgr, 'Esszimmer Terrasse')
    mgr._apply_window_control('Esszimmer', c)
    payloads = sent(c, ESS_SET)
    assert payloads and payloads[0].get('preset') == 'schedule'
    assert payloads[0].get('system_mode') == 'heat'    # turned back on
    assert 'Esszimmer' not in mgr.window_off


def test_window_close_restores_cooling_open_with_system_mode():
    mgr, c = make_mgr(), FakeClient()
    mgr.season_cfg = {'mode': 'cooling'}               # force cooling
    mgr.window_off['Esszimmer'] = '2026-06-20T10:00:00'
    mgr.last_state['Esszimmer'] = {'system_mode': 'off'}
    _closed(mgr, 'Esszimmer Terrasse')
    mgr._apply_window_control('Esszimmer', c)
    p = sent(c, ESS_SET)[0]
    assert p.get('preset') == 'comfort' and p.get('comfort_temperature') == 34
    assert p.get('system_mode') == 'heat'              # off valve actually turned on


def test_closed_room_we_did_not_close_is_untouched():
    mgr, c = make_mgr(), FakeClient()
    mgr.last_state['Esszimmer'] = {'system_mode': 'off'}   # user turned it off
    _closed(mgr, 'Esszimmer Terrasse')
    mgr._apply_window_control('Esszimmer', c)              # not in window_off
    assert c.published == []


def test_manual_override_is_skipped():
    mgr, c = make_mgr(), FakeClient()
    mgr.last_state['Esszimmer'] = {'preset': 'manual'}
    _open(mgr, 'Esszimmer Terrasse')
    mgr._apply_window_control('Esszimmer', c)
    assert c.published == []
    assert 'Esszimmer' not in mgr.window_off


def test_humidity_guard_blocks_off_when_humid():
    mgr, c = make_mgr(), FakeClient()
    mgr.last_state['Waschküche'] = {'preset': 'schedule'}
    _open(mgr, 'Waschküche Fenster')
    mgr.sensor_state['Waschküche Luft'] = {'humidity': 70}   # >= 50 -> keep heating
    mgr._apply_window_control('Waschküche', c)
    assert c.published == []
    assert 'Waschküche' not in mgr.window_off


def test_humidity_guard_allows_off_when_dry():
    mgr, c = make_mgr(), FakeClient()
    mgr.last_state['Waschküche'] = {'preset': 'schedule'}
    _open(mgr, 'Waschküche Fenster')
    mgr.sensor_state['Waschküche Luft'] = {'humidity': 40}   # < 50 -> ventilate
    mgr._apply_window_control('Waschküche', c)
    assert {'system_mode': 'off'} in sent(c, WAS_SET)


def test_humidity_guard_unknown_blocks_off():
    mgr, c = make_mgr(), FakeClient()
    mgr.last_state['Waschküche'] = {'preset': 'schedule'}
    _open(mgr, 'Waschküche Fenster')
    mgr.sensor_state['Waschküche Luft'] = {}                 # no humidity reading
    mgr._apply_window_control('Waschküche', c)
    assert c.published == []


def test_act_false_logs_but_does_not_publish():
    mgr, c = make_mgr(), FakeClient()
    mgr.window_cfg['act'] = False
    mgr.last_state['Esszimmer'] = {'preset': 'schedule'}
    _open(mgr, 'Esszimmer Terrasse')
    mgr._apply_window_control('Esszimmer', c)
    assert c.published == []
    assert 'Esszimmer' in mgr.window_off       # latch still tracked for status


def test_disable_builtin_window_on_connect():
    mgr, c = make_mgr(), FakeClient()
    mgr.on_connect(c, None, None, 0)
    assert {'window_detection': 'OFF'} in sent(c, ESS_SET)
    assert {'window_detection': 'OFF'} in sent(c, WAS_SET)


def test_window_off_latch_persists_across_restart(tmp_path):
    sf = str(tmp_path / 'devices.json')
    mgr = make_mgr(sf)
    mgr.window_off['Esszimmer'] = '2026-06-20T10:00:00'
    mgr._save_device_state()
    mgr2 = make_mgr(sf)
    assert mgr2.window_off == {'Esszimmer': '2026-06-20T10:00:00'}


def test_debounce_uses_per_room_override():
    mgr = make_mgr()
    # global open_debounce is 5; a per-room override wins
    mgr.thermostats['Esszimmer']['window'] = {'debounce': 2}
    assert mgr._window_debounce('Esszimmer', opening=True) == 2
    assert mgr._window_debounce('Waschküche', opening=True) == 5
