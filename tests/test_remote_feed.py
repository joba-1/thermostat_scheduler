"""Heat-pump remote sensor feed: republish Zigbee temp/humidity + stale alert."""
import tempfile
import time

import thermostat_monitor as tm


def make_mgr(**rf):
    cfg = {
        'mqtt': {'base_topic': 'zigbee2mqtt'},
        'alerts': {'enabled': False},
        'season': {'mode': 'cooling', 'cool_target': 21},
        'heatpump': {
            'enabled': True,
            'remote_feed': {
                'enabled': True,
                'sensor': 'Wohnzimmer Luft',
                'control_topic': 'ems-esp/thermostat/hc1/control',
                'control_value': 'RC100H',
                'temp_topic': 'ems-esp/thermostat/hc1/remotetemp',
                'hum_topic': 'ems-esp/thermostat/hc1/remotehum',
                'interval': 60,
                'stale_after': 1800,
                **rf,
            },
        },
        'thermostats': {},
        'thermostat_types': {},
        'device_state_file': tempfile.mkdtemp() + '/devices.json',
    }
    return tm.Manager(cfg)


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0):
        self.published.append((topic, payload))


def test_feed_sensor_is_registered_and_subscribed():
    mgr = make_mgr()
    assert 'Wohnzimmer Luft' in mgr.sensor_kind
    assert 'zigbee2mqtt/Wohnzimmer Luft' in mgr.sensor_topic


def test_publish_remote_feed_sends_temp_and_humidity():
    mgr = make_mgr()
    mgr.sensor_state['Wohnzimmer Luft'] = {'temperature': 26.5, 'humidity': 53}
    c = FakeClient()
    mgr.publish_remote_feed(c)
    topics = dict(c.published)
    assert topics['ems-esp/thermostat/hc1/remotetemp'] == '26.5'
    assert topics['ems-esp/thermostat/hc1/remotehum'] == '53'


def test_temp_offset_lowers_fed_temperature_only():
    mgr = make_mgr(temp_offset=-5)
    mgr.sensor_state['Wohnzimmer Luft'] = {'temperature': 28.3, 'humidity': 50}
    c = FakeClient()
    mgr.publish_remote_feed(c)
    topics = dict(c.published)
    assert topics['ems-esp/thermostat/hc1/remotetemp'] == '23.3'   # 28.3 - 5
    assert topics['ems-esp/thermostat/hc1/remotehum'] == '50'      # humidity unchanged


def test_temp_only_sensor_is_not_eligible():
    # dew point needs temp AND humidity from the same sensor -> temp-only is skipped
    mgr = make_mgr()
    mgr.sensor_state['Wohnzimmer Luft'] = {'temperature': 24.0}  # no humidity
    c = FakeClient()
    mgr.publish_remote_feed(c)
    assert c.published == []


def test_no_publish_when_disabled():
    mgr = make_mgr(enabled=False)
    mgr.sensor_state['Wohnzimmer Luft'] = {'temperature': 24.0, 'humidity': 50}
    c = FakeClient()
    mgr.publish_remote_feed(c)
    assert c.published == []


def test_fresh_sensor_with_humidity_has_no_issue():
    mgr = make_mgr()
    now = time.time()
    mgr.sensor_seen['Wohnzimmer Luft'] = tm.iso_now()
    mgr.start_ts = now - 10_000          # past the restart grace window
    mgr.sensor_state['Wohnzimmer Luft'] = {'temperature': 26.5, 'humidity': 53}
    assert mgr._remote_feed_issue(now) is None


def test_stale_sensor_raises_alert():
    mgr = make_mgr()
    now = time.time()
    mgr.start_ts = now - 10_000          # so effective_seen reflects the real age
    mgr.sensor_seen['Wohnzimmer Luft'] = time.strftime(
        '%Y-%m-%dT%H:%M:%S', time.localtime(now - 3600))   # 1h old > 1800s
    mgr.sensor_state['Wohnzimmer Luft'] = {'temperature': 26.5, 'humidity': 53}
    iss = mgr._remote_feed_issue(now)
    assert iss is not None and iss.severity == 'alert'
    assert iss.kind == 'remote_feed_stale'


def test_missing_humidity_raises_alert():
    mgr = make_mgr()
    now = time.time()
    mgr.start_ts = now - 10_000
    mgr.sensor_seen['Wohnzimmer Luft'] = tm.iso_now()
    mgr.sensor_state['Wohnzimmer Luft'] = {'temperature': 26.5}   # no humidity
    iss = mgr._remote_feed_issue(now)
    assert iss is not None and iss.kind == 'remote_feed_stale'


def _multi_mgr():
    cfg = {
        'mqtt': {'base_topic': 'zigbee2mqtt'},
        'alerts': {'enabled': False},
        'season': {'mode': 'cooling'},
        'heatpump': {'enabled': True, 'remote_feed': {
            'enabled': True,
            'sensors': [
                {'sensor': 'Wohnzimmer Luft', 'room': 'Wohnzimmer'},
                {'sensor': 'Arbeitszimmer Bewegungsmelder', 'room': 'Arbeitszimmer'},
            ],
            'temp_topic': 'ems-esp/thermostat/hc1/remotetemp',
            'hum_topic': 'ems-esp/thermostat/hc1/remotehum',
            'stale_after': 1800,
        }},
        'thermostats': {
            'Wohnzimmer': {'day_hour': '05:00', 'day_temperature': 22, 'night_hour': '23:00',
                           'night_temperature': 20, 'type': 'X',
                           'sensors': {'windows': ['Wohnzimmer Fenster']}},
            'Arbeitszimmer': {'day_hour': '05:00', 'day_temperature': 22, 'night_hour': '23:00',
                              'night_temperature': 20, 'type': 'X',
                              'sensors': {'windows': ['Arbeitszimmer Fenster']}},
        },
        'thermostat_types': {'X': {}},
        'device_state_file': tempfile.mkdtemp() + '/devices.json',
    }
    return tm.Manager(cfg)


def test_highest_temp_candidate_is_selected():
    mgr = _multi_mgr()
    mgr.sensor_state['Wohnzimmer Luft'] = {'temperature': 25.1, 'humidity': 52}
    mgr.sensor_state['Arbeitszimmer Bewegungsmelder'] = {'temperature': 28.5, 'humidity': 46}
    c = FakeClient()
    mgr.publish_remote_feed(c)
    topics = dict(c.published)
    assert topics['ems-esp/thermostat/hc1/remotetemp'] == '28.5'   # the warmer room
    assert topics['ems-esp/thermostat/hc1/remotehum'] == '46'      # its own humidity


def test_window_open_candidate_is_excluded():
    mgr = _multi_mgr()
    mgr.sensor_state['Wohnzimmer Luft'] = {'temperature': 25.1, 'humidity': 52}
    mgr.sensor_state['Arbeitszimmer Bewegungsmelder'] = {'temperature': 28.5, 'humidity': 46}
    mgr.sensor_state['Arbeitszimmer Fenster'] = {'contact': False}   # window OPEN
    c = FakeClient()
    mgr.publish_remote_feed(c)
    topics = dict(c.published)
    assert topics['ems-esp/thermostat/hc1/remotetemp'] == '25.1'   # warmer one excluded
    assert topics['ems-esp/thermostat/hc1/remotehum'] == '52'


def _iso_ago(seconds):
    import datetime
    return (datetime.datetime.now() - datetime.timedelta(seconds=seconds)).isoformat()


def test_fresh_window_open_beats_stale_closed_room():
    # a current reading from a window-open room is better dew data than a stale
    # window-closed value -> feed the fresh open room, no blind-stale alert.
    mgr = _multi_mgr()
    now = time.time()
    mgr.start_ts = now - 10_000
    mgr.sensor_seen['Wohnzimmer Luft'] = _iso_ago(7200)              # stale (closed)
    mgr.sensor_state['Wohnzimmer Luft'] = {'temperature': 25.0, 'humidity': 50}
    mgr.sensor_seen['Arbeitszimmer Bewegungsmelder'] = tm.iso_now()  # fresh
    mgr.sensor_state['Arbeitszimmer Bewegungsmelder'] = {'temperature': 28.5, 'humidity': 46}
    mgr.sensor_state['Arbeitszimmer Fenster'] = {'contact': False}   # window OPEN
    c = FakeClient()
    mgr.publish_remote_feed(c)
    topics = dict(c.published)
    assert topics['ems-esp/thermostat/hc1/remotetemp'] == '28.5'     # fresh open, not stale closed
    assert mgr._remote_feed_issue(now) is None                       # not a blind-stale alert


def test_no_fresh_candidate_keeps_last_value_and_alerts():
    mgr = _multi_mgr()
    now = time.time()
    mgr.start_ts = now - 10_000
    # one good reading establishes a last-good value
    mgr.sensor_seen['Wohnzimmer Luft'] = tm.iso_now()
    mgr.sensor_state['Wohnzimmer Luft'] = {'temperature': 25.0, 'humidity': 50}
    c = FakeClient()
    mgr.publish_remote_feed(c)
    # now every candidate is completely stale (not just window-open)
    mgr.sensor_seen['Wohnzimmer Luft'] = _iso_ago(7200)
    c2 = FakeClient()
    mgr.publish_remote_feed(c2)
    assert dict(c2.published)['ems-esp/thermostat/hc1/remotetemp'] == '25.0'  # last good held
    assert mgr._remote_feed_issue(now) is not None     # and an alert is raised
