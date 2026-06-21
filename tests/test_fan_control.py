"""Radiator-fan control: cooling-driven plug switching (zigbee + tasmota)."""
import tempfile

import thermostat_monitor as tm

CFG = {
    'mqtt': {'base_topic': 'zigbee2mqtt'},
    'alerts': {'enabled': False},
    'heatpump': {'enabled': True, 'boiler_topic': 'ems-esp/boiler_data'},
    'fan_control': {
        'enabled': True, 'act': True, 'on_debounce': 30, 'off_delay': 600,
        'fans': [
            {'type': 'zigbee', 'name': 'SZ Ventilator'},
            {'type': 'tasmota', 'topic': 'vent_wz', 'power': 'POWER'},
        ],
    },
    'thermostats': {},
}


class FakeClient:
    def __init__(self):
        self.pub = []

    def publish(self, topic, payload, qos=0):
        self.pub.append((topic, payload))


def make_mgr():
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['device_state_file'] = tempfile.mkdtemp() + '/devices.json'
    return tm.Manager(cfg)


def hp(activity):
    return {'raw': {'hpactivity': activity}, 'active': activity == 'cooling'}


def test_cooling_active_signal():
    assert tm.Manager._cooling_active(hp('cooling')) is True
    assert tm.Manager._cooling_active(hp('off')) is False
    assert tm.Manager._cooling_active(hp('hot water')) is False
    assert tm.Manager._cooling_active(None) is False


def test_fans_on_after_debounce_then_off_after_delay():
    mgr = make_mgr()
    c = FakeClient()
    t0 = 1000.0
    # cooling starts -> within debounce, nothing yet
    mgr._apply_fan_control(c, hp('cooling'), now=t0)
    assert c.pub == [] and mgr._fans_on in (None, False)
    # past on_debounce -> both plugs switched ON, correct topics/payloads
    mgr._apply_fan_control(c, hp('cooling'), now=t0 + 31)
    assert mgr._fans_on is True
    assert ('zigbee2mqtt/SZ Ventilator/set', '{"state": "ON"}') in c.pub
    assert ('cmnd/vent_wz/POWER', 'ON') in c.pub

    # cooling stops -> held ON during off_delay (no new publish)
    c.pub.clear()
    mgr._apply_fan_control(c, hp('off'), now=t0 + 100)
    assert mgr._fans_on is True and c.pub == []
    # cooling resumes within off_delay -> still on, no flap
    mgr._apply_fan_control(c, hp('cooling'), now=t0 + 200)
    assert mgr._fans_on is True and c.pub == []
    # stops again, off_delay elapses -> both plugs OFF
    mgr._apply_fan_control(c, hp('off'), now=t0 + 300)
    mgr._apply_fan_control(c, hp('off'), now=t0 + 300 + 601)
    assert mgr._fans_on is False
    assert ('zigbee2mqtt/SZ Ventilator/set', '{"state": "OFF"}') in c.pub
    assert ('cmnd/vent_wz/POWER', 'OFF') in c.pub


def test_act_false_logs_but_does_not_publish():
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['fan_control'] = dict(cfg['fan_control'], act=False)
    cfg['device_state_file'] = tempfile.mkdtemp() + '/devices.json'
    mgr = tm.Manager(cfg)
    c = FakeClient()
    mgr._apply_fan_control(c, hp('cooling'), now=1000)       # establish cooling edge
    mgr._apply_fan_control(c, hp('cooling'), now=1031)       # past on_debounce
    assert mgr._fans_on is True        # state advances
    assert c.pub == []                 # but nothing published


def test_disabled_does_nothing():
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['fan_control'] = dict(cfg['fan_control'], enabled=False)
    cfg['device_state_file'] = tempfile.mkdtemp() + '/devices.json'
    mgr = tm.Manager(cfg)
    c = FakeClient()
    mgr._apply_fan_control(c, hp('cooling'), now=2000)
    assert c.pub == [] and mgr._fans_on is None
