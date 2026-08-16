"""A re-onboard must verify, not just publish.

z2m expands one `/set` into a burst of Zigbee writes, and a freshly re-joined or
weak-link TRV drops some. Waschküche came back from a re-join with all seven
schedule days written but `preset` still 'manual' — fully configured to look at,
valve shut at setpoint 5. Re-sending that one key alone worked immediately.
"""
import json

import thermostat_scheduler as ts

TOPIC = 'zigbee2mqtt/Waschküche Thermostat/set'
PAYLOAD = {'schedule_monday': '00:00/19.5', 'preset': 'comfort',
           'comfort_temperature': 34}


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, qos=0, **kw):
        self.published.append((topic, json.loads(payload)))

        class _Info:
            def wait_for_publish(self, timeout=None): pass
            def is_published(self): return True
        return _Info()

    def subscribe(self, *a, **k):
        pass


def drive(monkeypatch, states):
    """Run verify_and_retry against a scripted sequence of device states."""
    seq = list(states)
    monkeypatch.setattr(ts.time, 'sleep', lambda s: None)
    monkeypatch.setattr(ts, 'query_monitor',
                        lambda *a, **k: {'Waschküche': {'state': seq.pop(0)}}
                        if seq else {'Waschküche': {'state': states[-1]}})
    c = FakeClient()
    still = ts.verify_and_retry(c, {}, 'Waschküche', TOPIC, PAYLOAD, timeout=0)
    return c, still


def test_dropped_key_is_resent_alone(monkeypatch):
    """The real Waschküche case: schedules took, preset did not."""
    dropped = {'schedule_monday': '00:00/19.5', 'preset': 'manual',
               'comfort_temperature': 34}
    applied = dict(dropped, preset='comfort')
    c, still = drive(monkeypatch, [dropped, applied])
    assert still == []
    # exactly one retry, carrying only the key that failed
    assert c.published == [(TOPIC, {'preset': 'comfort'})]


def test_nothing_resent_when_everything_applied(monkeypatch):
    applied = {'schedule_monday': '00:00/19.5', 'preset': 'comfort',
               'comfort_temperature': 34}
    c, still = drive(monkeypatch, [applied])
    assert still == []
    assert c.published == []


def test_unreported_keys_are_never_retried(monkeypatch):
    """A key the device does not echo cannot be verified — retrying it would
    loop forever on a device that is actually fine."""
    partial = {'preset': 'comfort'}          # schedule/comfort_temperature absent
    c, still = drive(monkeypatch, [partial, partial])
    assert still == []
    assert c.published == []


def test_a_device_refusing_writes_is_reported(monkeypatch):
    """Bad OG's failure mode: it echoes the old value no matter how often we
    write. Give up after the bounded rounds and say so, rather than spinning."""
    refusing = {'schedule_monday': '00:00/19.5', 'preset': 'manual',
                'comfort_temperature': 34}
    c, still = drive(monkeypatch, [refusing, refusing, refusing])
    assert still == ['preset']
    assert [p for _t, p in c.published] == [{'preset': 'comfort'}] * 2


def test_no_daemon_state_skips_verification(monkeypatch):
    monkeypatch.setattr(ts.time, 'sleep', lambda s: None)
    monkeypatch.setattr(ts, 'query_monitor', lambda *a, **k: {})
    c = FakeClient()
    assert ts.verify_and_retry(c, {}, 'Waschküche', TOPIC, PAYLOAD, timeout=0) == []
    assert c.published == []


def test_no_client_is_a_noop(monkeypatch):
    assert ts.verify_and_retry(None, {}, 'Waschküche', TOPIC, PAYLOAD, timeout=0) == []
