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

OPEN = {'preset': 'comfort', 'comfort_temperature': 34}


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
    # reset_manual verifies its writes afterwards; don't spend real seconds on it
    monkeypatch.setattr(ts.time, 'sleep', lambda s: None)
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


def check_output(monkeypatch, capsys, season, checked, outdoor=31.7):
    patch_state(monkeypatch, outdoor, checked)
    ts.check_thermostats(cfg(season), FakeClient(),
                         {'responses': {'ems-esp/boiler_data': {}}}, timeout=0)
    return capsys.readouterr().out


def test_check_in_cooling_compares_against_open_not_schedule(monkeypatch, capsys):
    """An open valve in cooling season is correct — not a schedule violation."""
    out = check_output(monkeypatch, capsys, {'mode': 'cooling'},
                       {'Caros': {'state': OPEN}, 'Julians': {'state': OPEN}})
    assert 'Mode: cooling' in out
    assert 'OK (cooling/open)' in out
    assert 'MISMATCH' not in out


def test_check_derives_cooling_from_outdoor_temperature(monkeypatch, capsys):
    """The season-blind path: auto + outdoor_temp must not fall back to heating."""
    out = check_output(monkeypatch, capsys, None,
                       {'Caros': {'state': OPEN}, 'Julians': {'state': OPEN}})
    assert 'Mode: cooling' in out
    assert 'MISMATCH' not in out


def test_check_in_standby_reports_off_valves_as_correct(monkeypatch, capsys):
    """Off is the target in standby — don't blame a window for it."""
    off = {'system_mode': 'off', 'frost_protection': 'ON'}
    out = check_output(monkeypatch, capsys, {'mode': 'standby'},
                       {'Caros': {'state': off}, 'Julians': {'state': off}}, outdoor=20)
    assert 'OK (standby/off)' in out
    assert 'window' not in out


def test_check_in_standby_flags_a_valve_that_is_not_off(monkeypatch, capsys):
    """Real standby drift: a valve sitting in its schedule must not read OK."""
    scheduled = {'system_mode': 'heat', 'preset': 'schedule'}
    out = check_output(monkeypatch, capsys, {'mode': 'standby'},
                       {'Caros': {'state': scheduled}}, outdoor=20)
    assert 'MISMATCHES (standby/off)' in out
    assert 'OK (schedule)' not in out


def test_check_in_heating_still_compares_against_the_schedule(monkeypatch, capsys):
    out = check_output(monkeypatch, capsys, {'mode': 'heating'},
                       {'Caros': {'state': OPEN}}, outdoor=5)
    assert 'Mode: heating' in out
    assert 'MISMATCHES (schedule)' in out


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
