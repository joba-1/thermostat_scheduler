"""Free-cooling reminder: detect outside<inside opportunity, mail on the rising edge."""
import tempfile

import thermostat_monitor as tm

CFG = {
    'mqtt': {'base_topic': 'zigbee2mqtt'},
    'alerts': {'enabled': True},
    'season': {'mode': 'cooling', 'cool_target': 21},
    'free_cooling': {'enabled': True, 'margin': 2, 'remind_interval_hours': 8},
    'thermostat_types': {'VNTH-T2_v2': {}},
    'thermostats': {
        'Bad OG': {'type': 'VNTH-T2_v2', 'sensors': {'temperature': 'Bad OG Luft'}},
        'WC OG': {'type': 'VNTH-T2_v2', 'sensors': {'temperature': 'WC OG Luft'}},
    },
}


def make_mgr():
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['device_state_file'] = tempfile.mkdtemp() + '/devices.json'
    mgr = tm.Manager(cfg)
    mgr.sensor_state['Bad OG Luft'] = {'temperature': 26.0}
    mgr.sensor_state['WC OG Luft'] = {'temperature': 24.0}
    sent = []
    mgr.alerter.enabled = True
    mgr.alerter._sender = lambda s, b, html=None: sent.append((s, b)) or True
    return mgr, sent


def hp(outside):
    return {'mode': 'cooling', 'telemetry': {'outdoor': outside}, 'raw': {}}


def test_opportunity_detected_when_outside_cooler():
    mgr, _ = make_mgr()                       # warmest room = Bad OG 26°C
    assert mgr._free_cooling_state('cooling', hp(20.0))['room'] == 'Bad OG'   # 20 <= 26-2
    assert mgr._free_cooling_state('cooling', hp(25.0)) is None               # 25 > 26-2
    assert mgr._free_cooling_state('heating', hp(10.0)) is None               # not cooling
    # rooms already at/below target -> no opportunity
    mgr.sensor_state['Bad OG Luft'] = {'temperature': 20.0}
    mgr.sensor_state['WC OG Luft'] = {'temperature': 19.0}
    assert mgr._free_cooling_state('cooling', hp(15.0)) is None


def test_reminder_mails_once_on_rising_edge_then_throttled():
    mgr, sent = make_mgr()
    mgr._apply_free_cooling('cooling', hp(20.0), now_ts=1000)
    assert len(sent) == 1 and 'open windows' in sent[0][0].lower()
    assert mgr._free_cooling_info['room'] == 'Bad OG'
    # still available next pass -> no second mail (no new rising edge)
    mgr._apply_free_cooling('cooling', hp(20.0), now_ts=1100)
    assert len(sent) == 1
    # clears (outside warms) then re-available within throttle window -> still no mail
    mgr._apply_free_cooling('cooling', hp(30.0), now_ts=2000)
    assert mgr._free_cooling_info is None
    mgr._apply_free_cooling('cooling', hp(20.0), now_ts=3000)        # <8h later
    assert len(sent) == 1
    # past the throttle window -> reminds again
    mgr._apply_free_cooling('cooling', hp(30.0), now_ts=40000)
    mgr._apply_free_cooling('cooling', hp(20.0), now_ts=40001)       # >8h after first
    assert len(sent) == 2
