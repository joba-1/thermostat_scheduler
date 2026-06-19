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


def test_publish_skips_missing_fields():
    mgr = make_mgr()
    mgr.sensor_state['Wohnzimmer Luft'] = {'temperature': 24.0}  # no humidity yet
    c = FakeClient()
    mgr.publish_remote_feed(c)
    topics = dict(c.published)
    assert 'ems-esp/thermostat/hc1/remotetemp' in topics
    assert 'ems-esp/thermostat/hc1/remotehum' not in topics


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
    assert iss is not None and iss.kind == 'remote_feed_nohum'
