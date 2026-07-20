"""Standby-season TRV control via _apply_cooling (off on standby, restore on
exit — including undoing the off marker). No MQTT connection."""
import json
import tempfile

import thermostat_monitor as tm

TYPES = {
    'VNTH-T2_v2': {'schedule_mode': {'system_mode': 'heat', 'preset': 'schedule'},
                   'cooling_open': {'preset': 'comfort', 'comfort_temperature': 34},
                   'cooling_restore': {'preset': 'schedule'},
                   'manual_marker': {'field': 'preset', 'equals': 'manual'},
                   'off_signature': {'system_mode': 'off', 'frost_protection': 'ON'},
                   'off_clear': {'frost_protection': 'OFF'}},
}

CFG = {
    'mqtt': {'base_topic': 'zigbee2mqtt'},
    'alerts': {'enabled': False},
    'season': {'mode': 'standby', 'control': True},
    'window_control': {'enabled': False},
    'thermostat_types': TYPES,
    'thermostats': {
        'Esszimmer': {'day_hour': '05:00', 'day_temperature': 21.0, 'night_hour': '23:00',
                      'night_temperature': 19.5, 'type': 'VNTH-T2_v2', 'sensors': {}},
    },
}

ESS_SET = 'zigbee2mqtt/Esszimmer Thermostat/set'


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, qos=0, **kw):
        self.published.append((topic, payload))

    def subscribe(self, *a, **k):
        pass


def make_mgr():
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['device_state_file'] = tempfile.mkdtemp() + '/devices.json'
    return tm.Manager(cfg)


def sent(client, topic):
    return [json.loads(p) for (t, p) in client.published if t == topic]


def test_standby_switches_valve_off():
    mgr, c = make_mgr(), FakeClient()
    mgr.last_state['Esszimmer'] = {'preset': 'schedule'}   # currently scheduled
    mgr._apply_cooling(c, 'standby', [])
    msgs = sent(c, ESS_SET)
    assert msgs and msgs[-1]['system_mode'] == 'off'
    assert msgs[-1]['frost_protection'] == 'ON'
    assert mgr.applied_mode['Esszimmer'] == 'standby'


def test_standby_idempotent_when_already_off():
    mgr, c = make_mgr(), FakeClient()
    mgr.last_state['Esszimmer'] = {'preset': 'schedule'}
    mgr._apply_cooling(c, 'standby', [])
    c.published.clear()
    # a second pass with the mode already applied publishes nothing
    mgr._apply_cooling(c, 'standby', [])
    assert sent(c, ESS_SET) == []


def test_exit_standby_to_heating_clears_off_marker():
    mgr, c = make_mgr(), FakeClient()
    mgr.applied_mode['Esszimmer'] = 'standby'
    mgr.last_state['Esszimmer'] = {'system_mode': 'off', 'frost_protection': 'ON'}
    mgr._apply_cooling(c, 'heating', [])
    msgs = sent(c, ESS_SET)
    assert msgs, "leaving standby should push a restore payload"
    p = msgs[-1]
    assert p['preset'] == 'schedule'          # cooling_restore applied
    assert p['frost_protection'] == 'OFF'     # off marker undone (off_clear)
    assert p['system_mode'] == 'heat'         # valve turned back on
    assert mgr.applied_mode['Esszimmer'] == 'heating'


def test_exit_standby_to_cooling_clears_off_and_opens():
    mgr, c = make_mgr(), FakeClient()
    mgr.applied_mode['Esszimmer'] = 'standby'
    mgr.last_state['Esszimmer'] = {'system_mode': 'off', 'frost_protection': 'ON'}
    mgr._apply_cooling(c, 'cooling', [])
    p = sent(c, ESS_SET)[-1]
    assert p['preset'] == 'comfort' and p['comfort_temperature'] == 34   # open
    assert p['frost_protection'] == 'OFF'     # off marker undone
    assert mgr.applied_mode['Esszimmer'] == 'cooling'


def test_standby_leaves_manual_alone():
    mgr, c = make_mgr(), FakeClient()
    mgr.last_state['Esszimmer'] = {'preset': 'manual'}   # user took control
    mgr._apply_cooling(c, 'standby', [])
    assert sent(c, ESS_SET) == []
    assert mgr.applied_mode.get('Esszimmer') != 'standby'
