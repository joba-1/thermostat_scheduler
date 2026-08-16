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


def test_refusing_device_is_not_rewritten_every_pass():
    """A valve that will not take the payload (latched fault) keeps reporting the
    old state. Without backoff we would re-publish once a minute forever — into a
    battery device. Bad OG did exactly this for ~20 minutes."""
    mgr, c = make_mgr(), FakeClient()
    cached_as(mgr, 'cooling', SCHEDULE_STATE, seen='2026-08-16T13:20:00')
    mgr._apply_cooling(c, 'cooling', [])
    assert sent(c, ESS_SET), "first attempt should be published"
    c.published.clear()
    # Device still refuses: same contradicting report, still newer than our write.
    for _ in range(5):
        mgr.last_seen['Esszimmer'] = '2026-08-16T13:30:00'
        mgr.applied_at['Esszimmer'] = '2026-08-16T13:25:00'
        mgr.applied_mode['Esszimmer'] = 'cooling'
        mgr._apply_cooling(c, 'cooling', [])
    assert sent(c, ESS_SET) == [], "backoff should suppress the immediate retries"


def test_backoff_clears_once_the_device_confirms():
    """After the valve finally takes it, the next drift retries immediately."""
    mgr, c = make_mgr(), FakeClient()
    cached_as(mgr, 'cooling', SCHEDULE_STATE, seen='2026-08-16T13:20:00')
    mgr._apply_cooling(c, 'cooling', [])
    mgr.last_state['Esszimmer'] = OPEN_STATE          # device confirms
    mgr._apply_cooling(c, 'cooling', [])
    assert ('season', 'Esszimmer') not in mgr.retry_state
    c.published.clear()
    cached_as(mgr, 'cooling', SCHEDULE_STATE, seen='2026-08-16T13:40:00')
    mgr._apply_cooling(c, 'cooling', [])
    assert sent(c, ESS_SET), "a fresh drift after success must be acted on at once"


def test_season_writes_are_spaced_out(monkeypatch):
    """A season change touches every room; bursting them is how weak-link TRVs
    miss commands. Writes must honour mqtt.delay_between_messages."""
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CFG.items()}
    cfg['mqtt'] = {'base_topic': 'zigbee2mqtt', 'delay_between_messages': 7}
    cfg['thermostats'] = dict(CFG['thermostats'])
    cfg['thermostats']['Julians'] = dict(CFG['thermostats']['Esszimmer'])
    cfg['device_state_file'] = tempfile.mkdtemp() + '/devices.json'
    mgr, c = tm.Manager(cfg), FakeClient()
    slept = []
    monkeypatch.setattr(tm.time, 'sleep', lambda s: slept.append(s))
    for r in ('Esszimmer', 'Julians'):
        mgr.last_state[r] = SCHEDULE_STATE
    mgr._apply_cooling(c, 'cooling', [])
    assert len(c.published) == 2, "both rooms should be written"
    assert slept == [7], "exactly one gap between the two writes"


def test_publish_records_apply_time():
    """Each write stamps applied_at, so the next pass can date-compare reports."""
    mgr, c = make_mgr(), FakeClient()
    mgr.last_state['Esszimmer'] = SCHEDULE_STATE
    mgr._apply_cooling(c, 'cooling', [])
    assert mgr.applied_at.get('Esszimmer'), "publish should stamp applied_at"
