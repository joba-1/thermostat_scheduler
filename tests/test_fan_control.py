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


def test_circulation_legacy_signal_is_compressor_activity():
    mgr = make_mgr()
    assert mgr._circulation(hp('cooling')) == 'cooling'
    assert mgr._circulation(hp('heating')) == 'heating'
    assert mgr._circulation(hp('off')) is None
    assert mgr._circulation(hp('hot water')) is None
    assert mgr._circulation(None) is None


def pump(pc1flow=1400, heatingpump='on', activity='cooling', three_way='off',
         charging='off', state='cooling'):
    raw = {'heatingpump': heatingpump, 'hpactivity': activity,
           'dhw': {'3wayvalve': three_way, 'charging': charging}}
    if pc1flow is not None:
        raw['pc1flow'] = pc1flow
    return {'state': state, 'raw': raw}


def test_circulation_follows_pc1_through_compressor_pauses():
    """PC1 (buffer -> radiators) keeps pumping while the compressor and PC0
    (`heatingpump`) pause, so the buffer's cold still reaches the radiators."""
    mgr = make_mgr()
    mgr.last_mode = 'cooling'
    assert mgr._circulation(pump()) == 'cooling'
    assert mgr._circulation(pump(heatingpump='off', activity='off')) == 'cooling'
    # PC1 stopped (-1 = no flow) -> nothing to fan
    assert mgr._circulation(pump(pc1flow=-1, activity='off')) is None
    assert mgr._circulation(pump(pc1flow=0)) is None
    # hot-water charge: never radiator circulation
    assert mgr._circulation(pump(three_way='on', activity='hot water')) is None
    mgr.last_mode = 'heating'
    assert mgr._circulation(pump(activity='heating')) == 'heating'
    mgr.last_mode = 'standby'
    assert mgr._circulation(pump()) is None


def test_circulation_without_pc1flow_falls_back_to_pc0():
    mgr = make_mgr()
    mgr.last_mode = 'cooling'
    assert mgr._circulation(pump(pc1flow=None)) == 'cooling'
    assert mgr._circulation(pump(pc1flow=None, heatingpump='off')) is None
    assert mgr._circulation(pump(pc1flow=None, charging='on')) is None


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


def make_room_mgr(**fan_cfg):
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['fan_control'] = dict(cfg['fan_control'], off_delay=0,
                              fans=[{'type': 'zigbee', 'name': 'SZ Ventilator',
                                     'room': 'Schlafzimmer'},
                                    {'type': 'tasmota', 'topic': 'vent_wz',
                                     'room': 'Wohnzimmer'}], **fan_cfg)
    cfg['season'] = {'cool_target': 21}
    # all-day "day" schedule so the heating setpoint doesn't depend on the clock
    cfg['thermostats'] = {'Schlafzimmer': {'day_hour': '00:00', 'day_temperature': 20,
                                           'night_hour': '00:00', 'night_temperature': 20},
                          'Wohnzimmer': {'day_hour': '00:00', 'day_temperature': 22,
                                         'night_hour': '00:00', 'night_temperature': 22}}
    cfg['device_state_file'] = tempfile.mkdtemp() + '/devices.json'
    return tm.Manager(cfg)


def temps(mgr, sz, wz=None):
    mgr.last_state['Schlafzimmer'] = {'local_temperature': sz}
    if wz is not None:
        mgr.last_state['Wohnzimmer'] = {'local_temperature': wz}


SZ_ON = ('zigbee2mqtt/SZ Ventilator/set', '{"state": "ON"}')
SZ_OFF = ('zigbee2mqtt/SZ Ventilator/set', '{"state": "OFF"}')


def test_cooling_fan_stops_for_the_last_degree_above_target():
    mgr = make_room_mgr()
    c = FakeClient()
    temps(mgr, 23.0, 21.8)                        # WZ within 1 K of 21 -> not needed
    mgr._apply_fan_control(c, hp('cooling'), now=1000)
    mgr._apply_fan_control(c, hp('cooling'), now=1031)
    assert SZ_ON in c.pub
    assert not any(t == 'cmnd/vent_wz/POWER' for t, _ in c.pub)

    c.pub.clear()
    temps(mgr, 22.0)                              # = target + 1 -> off, alone
    mgr._apply_fan_control(c, hp('cooling'), now=1100)
    assert c.pub == [SZ_OFF]
    assert mgr._fans_on is True

    c.pub.clear()
    temps(mgr, 22.4)                              # hysteresis: not yet
    mgr._apply_fan_control(c, hp('cooling'), now=1200)
    assert c.pub == []
    temps(mgr, 22.6)
    mgr._apply_fan_control(c, hp('cooling'), now=1300)
    assert c.pub == [SZ_ON]


def test_heating_fan_runs_only_until_one_degree_below_setpoint():
    mgr = make_room_mgr()
    c = FakeClient()
    temps(mgr, 18.5, 21.5)                        # SZ 1.5 K below 20; WZ 0.5 K below 22
    mgr._apply_fan_control(c, hp('heating'), now=1000)
    mgr._apply_fan_control(c, hp('heating'), now=1031)
    assert SZ_ON in c.pub
    assert not any(t == 'cmnd/vent_wz/POWER' for t, _ in c.pub)
    c.pub.clear()
    temps(mgr, 19.0)                              # = setpoint - 1 -> off
    mgr._apply_fan_control(c, hp('heating'), now=1100)
    assert c.pub == [SZ_OFF]


def test_heating_can_be_disabled_globally_or_per_fan():
    mgr = make_room_mgr(heating=False)
    c = FakeClient()
    temps(mgr, 15.0, 15.0)
    mgr._apply_fan_control(c, hp('heating'), now=1000)
    mgr._apply_fan_control(c, hp('heating'), now=1031)
    assert c.pub == []
    mgr = make_room_mgr()
    mgr.fans[0]['heating'] = False
    c = FakeClient()
    temps(mgr, 15.0, 15.0)
    mgr._apply_fan_control(c, hp('heating'), now=1000)
    mgr._apply_fan_control(c, hp('heating'), now=1031)
    assert c.pub == [('cmnd/vent_wz/POWER', 'ON')]


def test_fans_stop_as_soon_as_circulation_stops():
    mgr = make_room_mgr()
    c = FakeClient()
    temps(mgr, 25.0, 25.0)
    mgr._apply_fan_control(c, hp('cooling'), now=1000)
    mgr._apply_fan_control(c, hp('cooling'), now=1031)
    c.pub.clear()
    mgr._apply_fan_control(c, hp('off'), now=1040)
    assert SZ_OFF in c.pub and ('cmnd/vent_wz/POWER', 'OFF') in c.pub


def test_open_window_stops_the_rooms_fan():
    mgr = make_room_mgr()
    mgr.room_windows['Schlafzimmer'] = ['SZ Fenster']
    mgr.sensor_state['SZ Fenster'] = {'contact': False}
    c = FakeClient()
    temps(mgr, 25.0, 25.0)
    mgr._apply_fan_control(c, hp('cooling'), now=1000)
    mgr._apply_fan_control(c, hp('cooling'), now=1031)
    assert SZ_ON not in c.pub
    assert ('cmnd/vent_wz/POWER', 'ON') in c.pub


def test_fan_with_unknown_room_temperature_follows_circulation():
    mgr = make_room_mgr()
    c = FakeClient()
    mgr._apply_fan_control(c, hp('cooling'), now=1000)
    mgr._apply_fan_control(c, hp('cooling'), now=1031)
    assert ('cmnd/vent_wz/POWER', 'ON') in c.pub
    assert SZ_ON in c.pub
