"""Window/door -> TRV control (off on open, restore on close). No MQTT."""
import json
import tempfile

import thermostat_monitor as tm

TYPES = {
    'VNTH-T2_v2': {'schedule_mode': {'system_mode': 'heat', 'preset': 'schedule'},
                   'cooling_open': {'preset': 'comfort', 'comfort_temperature': 34},
                   'manual_marker': {'field': 'preset', 'equals': 'manual'},
                   'builtin_window_off': {'window_detection': 'OFF'},
                   'off_signature': {'system_mode': 'off', 'frost_protection': 'ON'},
                   'off_clear': {'frost_protection': 'OFF'}},
}

OUR_OFF = {'system_mode': 'off', 'frost_protection': 'ON'}   # the off_signature we set

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
    assert OUR_OFF in sent(c, ESS_SET)                 # off_signature, not a plain off
    assert 'Esszimmer' in mgr.window_off


def test_window_close_restores_heating_schedule():
    mgr, c = make_mgr(), FakeClient()
    mgr.window_off['Esszimmer'] = '2026-06-20T10:00:00'
    mgr.last_state['Esszimmer'] = dict(OUR_OFF)        # device is in our off signature
    _closed(mgr, 'Esszimmer Terrasse')
    mgr._apply_window_control('Esszimmer', c)
    payloads = sent(c, ESS_SET)
    assert payloads and payloads[0].get('preset') == 'schedule'
    assert payloads[0].get('system_mode') == 'heat'    # turned back on
    assert payloads[0].get('frost_protection') == 'OFF'   # off marker cleared
    assert 'Esszimmer' not in mgr.window_off


def test_window_close_restores_cooling_open_with_system_mode():
    mgr, c = make_mgr(), FakeClient()
    mgr.season_cfg = {'mode': 'cooling'}               # force cooling
    mgr.window_off['Esszimmer'] = '2026-06-20T10:00:00'
    mgr.last_state['Esszimmer'] = dict(OUR_OFF)
    _closed(mgr, 'Esszimmer Terrasse')
    mgr._apply_window_control('Esszimmer', c)
    p = sent(c, ESS_SET)[0]
    assert p.get('preset') == 'comfort' and p.get('comfort_temperature') == 34
    assert p.get('system_mode') == 'heat'              # off valve actually turned on
    assert p.get('frost_protection') == 'OFF'          # off marker cleared


def test_user_plain_off_not_restored():
    # A user's plain off (no frost_protection) is not our signature -> left alone,
    # even if we happen to have it latched (user took control mid-window).
    mgr, c = make_mgr(), FakeClient()
    mgr.window_off['Esszimmer'] = '2026-06-20T10:00:00'
    mgr.last_state['Esszimmer'] = {'system_mode': 'heat', 'current_heating_setpoint': 22}  # user manual
    _closed(mgr, 'Esszimmer Terrasse')
    mgr._apply_window_control('Esszimmer', c)
    assert c.published == []
    assert 'Esszimmer' not in mgr.window_off           # latch dropped, we stop tracking


def test_our_off_signature_restored_even_without_latch():
    # The off_signature is self-describing: if the device shows OUR off (off +
    # frost ON) we restore on close even if the in-memory latch was lost (e.g.
    # state-file wiped / restart) — the device itself tells us it's ours.
    mgr, c = make_mgr(), FakeClient()
    mgr.last_state['Esszimmer'] = dict(OUR_OFF)            # our off, but not latched
    _closed(mgr, 'Esszimmer Terrasse')
    mgr._apply_window_control('Esszimmer', c)             # not in window_off
    assert sent(c, ESS_SET)


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
    assert OUR_OFF in sent(c, WAS_SET)


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


def test_standby_restore_keeps_the_off_marker():
    """In standby the intended state IS off-with-marker. Clearing it here wiped
    the signature _apply_cooling had just written — house-wide, every pass, so
    no room was identifiable as 'our off' and window ownership fell back to the
    latch alone. Seen live: apply wrote frost_protection ON at 00:06:50, window
    restore cleared it 20s later."""
    mgr, c = make_mgr(), FakeClient()
    mgr.season_cfg = {'mode': 'standby'}
    mgr.window_off['Esszimmer'] = '2026-08-17T23:00:00'
    mgr.last_state['Esszimmer'] = dict(OUR_OFF)
    _closed(mgr, 'Esszimmer Terrasse')
    mgr._apply_window_control('Esszimmer', c)
    msgs = sent(c, ESS_SET)
    if msgs:
        assert msgs[0].get('frost_protection') != 'OFF', \
            "standby must not clear the off marker it just set"
