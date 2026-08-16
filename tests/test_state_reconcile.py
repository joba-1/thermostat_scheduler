"""The cooling cache must yield to fresh device reports.

`applied_mode` records what we last *sent*; the TRV is the authority on what it
actually is. These cover the drift that let a whole house sit in the winter
schedule through a 31 °C afternoon: the daemon had cached 'cooling' for every
room, so it skipped them even though the valves reported 'schedule'.
"""
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
    'season': {'mode': 'cooling', 'control': True},
    'window_control': {'enabled': False},
    'thermostat_types': TYPES,
    'thermostats': {
        'Esszimmer': {'day_hour': '05:00', 'day_temperature': 21.0, 'night_hour': '23:00',
                      'night_temperature': 19.5, 'type': 'VNTH-T2_v2', 'sensors': {}},
    },
}

ESS_SET = 'zigbee2mqtt/Esszimmer Thermostat/set'
SCHEDULE_STATE = {'system_mode': 'heat', 'preset': 'schedule'}
OPEN_STATE = {'preset': 'comfort', 'comfort_temperature': 34}


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


def cached_as(mgr, mode, reported, applied='2026-08-16T13:00:00', seen=None):
    """Put the manager in 'we pushed `mode`, device now reports `reported`'."""
    mgr.applied_mode['Esszimmer'] = mode
    mgr.applied_at['Esszimmer'] = applied
    mgr.last_state['Esszimmer'] = reported
    mgr.last_seen['Esszimmer'] = seen


def test_fresh_report_contradicting_cache_is_trusted():
    """A valve that reports 'schedule' after we set cooling gets re-opened."""
    mgr, c = make_mgr(), FakeClient()
    cached_as(mgr, 'cooling', SCHEDULE_STATE, seen='2026-08-16T13:20:00')
    mgr._apply_cooling(c, 'cooling', [])
    msgs = sent(c, ESS_SET)
    assert msgs, "drifted valve should be driven back open"
    assert msgs[-1]['comfort_temperature'] == 34
    assert mgr.applied_mode['Esszimmer'] == 'cooling'


def test_stale_report_does_not_trigger_republish():
    """A report from *before* our write is not new data — don't fight ourselves."""
    mgr, c = make_mgr(), FakeClient()
    cached_as(mgr, 'cooling', SCHEDULE_STATE, seen='2026-08-16T12:00:00')
    mgr._apply_cooling(c, 'cooling', [])
    assert sent(c, ESS_SET) == []


def test_agreeing_report_stays_idempotent():
    mgr, c = make_mgr(), FakeClient()
    cached_as(mgr, 'cooling', OPEN_STATE, seen='2026-08-16T13:20:00')
    mgr._apply_cooling(c, 'cooling', [])
    assert sent(c, ESS_SET) == []


def test_manual_override_during_cooling_is_still_left_alone():
    """Reconciling must not stomp a user's manual valve — 'manual' is not a
    signature we ever produce, so it is never treated as our drift."""
    mgr, c = make_mgr(), FakeClient()
    cached_as(mgr, 'cooling', {'preset': 'manual'}, seen='2026-08-16T13:20:00')
    mgr._apply_cooling(c, 'cooling', [])
    assert sent(c, ESS_SET) == []


def test_off_report_is_not_reconciled():
    """An off valve is ambiguous (window-open off / user off / standby), so a
    report of 'off' must never re-drive it open onto an open window."""
    mgr, c = make_mgr(), FakeClient()
    cached_as(mgr, 'cooling', {'system_mode': 'off', 'frost_protection': 'ON'},
              seen='2026-08-16T13:20:00')
    mgr._apply_cooling(c, 'cooling', [])
    assert sent(c, ESS_SET) == []


def test_publish_records_apply_time():
    """Each write stamps applied_at, so the next pass can date-compare reports."""
    mgr, c = make_mgr(), FakeClient()
    mgr.last_state['Esszimmer'] = SCHEDULE_STATE
    mgr._apply_cooling(c, 'cooling', [])
    assert mgr.applied_at.get('Esszimmer'), "publish should stamp applied_at"
