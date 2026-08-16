"""The CLI must never act season-blind, and must never reclaim the whole house.

Both bugs fired together once: `detect_mode` dropped the outdoor temperature, so
with `season.source: outdoor_temp` every CLI run resolved to 'heating', and a
no-argument `--reset-manual` targeted every room rather than only the
manual-override ones. One invocation pushed winter schedules onto ten open
valves on a 31 °C day.
"""
import pytest

import cooling
import thermostat_scheduler as ts

SEASON = {'mode': 'auto', 'source': 'outdoor_temp',
          'standby_below': 15, 'standby_above': 24, 'standby_hysteresis': 1}

TYPES = {
    'VNTH-T2_v2': {'schedule_mode': {'system_mode': 'heat', 'preset': 'schedule'},
                   'cooling_open': {'preset': 'comfort', 'comfort_temperature': 34},
                   'manual_marker': {'field': 'preset', 'equals': 'manual'},
                   'off_signature': {'system_mode': 'off', 'frost_protection': 'ON'}},
}


def cfg(season=None):
    return {
        'mqtt': {'base_topic': 'zigbee2mqtt', 'check_timeout': 0},
        'heatpump': {'enabled': True, 'boiler_topic': 'ems-esp/boiler_data'},
        'season': season if season is not None else dict(SEASON),
        'thermostat_types': TYPES,
        'thermostats': {
            'Julians': {'day_hour': '05:00', 'day_temperature': 21.0,
                        'night_hour': '23:00', 'night_temperature': 19.5,
                        'type': 'VNTH-T2_v2', 'sensors': {}},
            'Caros': {'day_hour': '05:00', 'day_temperature': 21.5,
                      'night_hour': '23:00', 'night_temperature': 19.5,
                      'type': 'VNTH-T2_v2', 'sensors': {}},
        },
    }


class FakeClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload=None, qos=0, **kw):
        self.published.append((topic, payload))

        class _Info:
            def wait_for_publish(self, timeout=None):
                pass

            def is_published(self):
                return True
        return _Info()

    def subscribe(self, *a, **k):
        pass


def patch_state(monkeypatch, outdoor, checked):
    """Stub the daemon/heat-pump round trip: give detect_mode telemetry + state."""
    monkeypatch.setattr(ts, 'query_monitor', lambda *a, **k: checked)
    hp = {'mode': 'cooling', 'cooling': True, 'telemetry': {'outdoor': outdoor}}
    monkeypatch.setattr(ts.heatpump, 'parse',
                        lambda *a, **k: (hp if outdoor is not None else
                                         {'mode': 'x', 'cooling': True, 'telemetry': {}}))


def test_detect_mode_uses_outdoor_temperature(monkeypatch):
    """31 °C outdoors is cooling season — not the 'heating' default."""
    patch_state(monkeypatch, 31.7, {})
    userdata = {'responses': {'ems-esp/boiler_data': {}}}
    mode, _hp, _checked = ts.detect_mode(cfg(), FakeClient(), userdata, 0)
    assert mode == 'cooling'


def test_detect_mode_refuses_when_outdoor_unavailable(monkeypatch):
    """No telemetry must fail loudly rather than silently mean 'heating'."""
    patch_state(monkeypatch, None, {})
    userdata = {'responses': {'ems-esp/boiler_data': {}}}
    with pytest.raises(RuntimeError, match='season'):
        ts.detect_mode(cfg(), FakeClient(), userdata, 0)


def test_detect_mode_allows_explicit_season_without_telemetry(monkeypatch):
    """An explicitly configured season needs no outdoor reading."""
    patch_state(monkeypatch, None, {})
    userdata = {'responses': {'ems-esp/boiler_data': {}}}
    mode, _hp, _checked = ts.detect_mode(
        cfg({'mode': 'cooling'}), FakeClient(), userdata, 0)
    assert mode == 'cooling'


def test_reset_manual_without_names_touches_only_manual_rooms(monkeypatch):
    """Caros is manual, Julians is already open: only Caros may be written."""
    checked = {
        'Caros': {'state': {'preset': 'manual'}},
        'Julians': {'state': {'preset': 'comfort', 'comfort_temperature': 34}},
    }
    patch_state(monkeypatch, 31.7, checked)
    c = FakeClient()
    ts.reset_manual(cfg(), c, {'responses': {'ems-esp/boiler_data': {}}}, None, timeout=0)
    topics = {t for t, _ in c.published}
    assert topics == {'zigbee2mqtt/Caros Thermostat/set'}


def test_reset_manual_without_names_is_a_noop_when_nothing_is_manual(monkeypatch):
    checked = {
        'Caros': {'state': {'preset': 'comfort', 'comfort_temperature': 34}},
        'Julians': {'state': {'preset': 'comfort', 'comfort_temperature': 34}},
    }
    patch_state(monkeypatch, 31.7, checked)
    c = FakeClient()
    ts.reset_manual(cfg(), c, {'responses': {'ems-esp/boiler_data': {}}}, None, timeout=0)
    assert c.published == []


def test_reset_manual_without_daemon_writes_nothing(monkeypatch):
    """No live state = no way to tell manual from controlled; don't guess."""
    patch_state(monkeypatch, 31.7, {})
    c = FakeClient()
    ts.reset_manual(cfg(), c, {'responses': {'ems-esp/boiler_data': {}}}, None, timeout=0)
    assert c.published == []


def test_named_rooms_are_still_reclaimed_explicitly(monkeypatch):
    """Naming a room keeps working even if it is not classified manual."""
    checked = {'Caros': {'state': {'preset': 'comfort', 'comfort_temperature': 34}}}
    patch_state(monkeypatch, 31.7, checked)
    c = FakeClient()
    ts.reset_manual(cfg(), c, {'responses': {'ems-esp/boiler_data': {}}},
                    ['Caros'], timeout=0)
    assert {t for t, _ in c.published} == {'zigbee2mqtt/Caros Thermostat/set'}


def test_cooling_season_never_pushes_a_heating_schedule(monkeypatch):
    """End-to-end guard on the actual damage: nothing sent in cooling season may
    carry the winter schedule preset."""
    checked = {'Caros': {'state': {'preset': 'manual'}}}
    patch_state(monkeypatch, 31.7, checked)
    c = FakeClient()
    ts.reset_manual(cfg(), c, {'responses': {'ems-esp/boiler_data': {}}}, None, timeout=0)
    import json
    for _t, payload in c.published:
        assert json.loads(payload).get('preset') != 'schedule'
